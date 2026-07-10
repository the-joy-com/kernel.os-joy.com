"""LLM: a prompt in, a structured answer out, through the local Ollama generative model.

The ontology router leans on a generative model for the judgments a vector distance can't make:
re-ranking the recalled candidates, and breaking a tie in the grey zone when their top score is ambiguous.
Both are the same shape — a prompt in, JSON out — so they share this one client.

Three call settings are fixed here so no caller has to remember them:
thinking is off (`think=false`) —
these are fast classification-style calls, not problems that want a visible reasoning trace,
and the trace would only cost tokens and latency;
the output is held to the exact shape the caller demands —
every call names a Pydantic model, whose schema becomes Ollama's `format`,
which Ollama compiles to a decode-time grammar so the model can only emit tokens that keep the reply conforming,
and that same model validates it on the way back;
and sampling is at temperature 0, so the same inputs score the same way twice.
There is no loose-JSON mode:
the model boundary gets the same typed discipline the HTTP boundary already gets from these DTOs (core/dtos.py),
from the first call rather than tightened later.
No external inference API —
Ollama serves the model on the box, the same stance as the embedder.
"""

from typing import TypeVar

import httpx
from pydantic import BaseModel

from core import config

M = TypeVar("M", bound=BaseModel)


def generate_json(prompt: str, schema: type[M], *, model: str | None = None) -> M:
    """Run one generative call and validate its reply into an instance of `schema`.

    schema is mandatory and is a Pydantic model class:
    its JSON Schema is handed to Ollama as the output grammar,
    and the reply is parsed and validated back through the same model —
    so the answer that crosses this boundary is a typed object with its fields already checked,
    never a loose dict a caller has to second-guess.
    A reply that breaks the model's constraints raises here
    rather than slipping through as a half-read decision that would quietly mis-file a fact.
    model defaults to the router's generative model (config.RERANK_MODEL);
    a caller that wants a different model passes its own.
    """
    resp = httpx.post(
        f"{config.OLLAMA_BASE_URL}/api/generate",
        json={
            "model": model or config.RERANK_MODEL,
            "prompt": prompt,
            "think": False,
            "stream": False,
            "format": schema.model_json_schema(),
            "options": {"temperature": 0},
        },
        timeout=config.LLM_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    body = resp.json().get("response")
    if not body:
        raise RuntimeError(f"generative model {model or config.RERANK_MODEL!r} returned an empty response")
    return schema.model_validate_json(body)
