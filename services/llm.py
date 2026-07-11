"""LLM: a prompt in, an answer out, through the local Ollama generative model.

Two shapes pass through here, for two kinds of caller.
The ontology router wants judgments a vector distance can't make —
re-ranking the recalled candidates, breaking a tie in the grey zone — and each is a prompt in, JSON out:
`generate_json` holds those to an exact Pydantic shape by a decode-time grammar.
The read path wants a spoken reply — free prose, held to no schema — and that is `generate`,
the same boundary with the grammar dropped, returning the model's text as-is.

Three call settings are fixed here so no caller has to remember them:
thinking is off — every call is a fast classification-style judgment on hardware that has to answer quickly,
not a problem that wants a visible reasoning trace,
and the trace would only cost tokens and latency we can't spare locally;
the output is held to the exact shape the caller demands —
every call names a Pydantic model, whose schema becomes Ollama's `format`,
which Ollama compiles to a decode-time grammar so the model can only emit tokens that keep the reply conforming,
and that same model validates it on the way back;
and sampling is at temperature 0, so the same inputs score the same way twice.
There is no loose-JSON mode:
the model boundary gets the same typed discipline the HTTP boundary already gets from these DTOs (core/dtos.py),
from the first call rather than tightened later.

Before either call reaches the model, the prompt is held to that model's context budget (_fit, services.models):
if it would overrun the window the model reads well, the summarisable context the caller marked is condensed to fit —
only that context, never the instructions around it — so a prompt swollen with folded-in facts is trimmed rather than truncated blind.

No external inference API —
Ollama serves the model on the box, the same stance as the embedder.
"""

from typing import TypeVar

import httpx
from pydantic import BaseModel

from core import config
from services import models

M = TypeVar("M", bound=BaseModel)

# The smallest a summarised context is ever aimed at.
# A budget so tight it left the context almost no room would ask the summariser for nonsense,
# so the target is floored here — better a little over budget than a summary squeezed to nothing.
_MIN_CONTEXT_TOKENS = 128


def _call(payload: dict) -> str:
    """POST one /api/generate payload to Ollama and return its reply text, raising on an empty response.

    The one place the HTTP round trip lives, shared by both public calls and the summariser beneath them.
    An empty reply raises rather than passing as a half-read decision or a blank answer to the symbiot.
    """
    resp = httpx.post(
        f"{config.OLLAMA_BASE_URL}/api/generate",
        json=payload,
        timeout=config.LLM_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    body = resp.json().get("response")
    if not body:
        raise RuntimeError(f"generative model {payload['model']!r} returned an empty response")
    return body


def _fit(prompt: str, context: str | None, model_name: str) -> str:
    """Hold `prompt` to the model's optimal context budget, condensing `context` in place if it overruns.

    Consulted before every generative call. The budget is the model's optimal window (services.models),
    less a margin (config.CONTEXT_SAFETY_MARGIN) for the tokeniser's approximation and the reply's own output.
    Under it, the prompt is returned untouched. Over it, only `context` — the summarisable part the caller
    marked, the folded-in facts, never the instructions around them — is condensed to the room the instructions
    leave, and spliced back where it sat, so a compression can never delete the lines that tell the model what to do.

    A model not in the map has no optimal to hold to, so its prompt passes through as given.
    An over-budget prompt with no `context` to condense raises rather than being sent:
    a prompt that grew that large with nothing marked summarisable is a bug to surface, not to paper over.
    """
    spec = models.MODELS.get(model_name)
    if spec is None:
        return prompt
    budget = int(spec.optimal_context_tokens * (1 - config.CONTEXT_SAFETY_MARGIN))
    if models.count_tokens(prompt) <= budget:
        return prompt
    if not context:
        raise RuntimeError(
            f"prompt for {model_name!r} exceeds its context budget ({budget} tokens) "
            "with no summarisable context to condense"
        )
    # The room left for the context once the surrounding instructions are counted against the budget.
    overhead = models.count_tokens(prompt) - models.count_tokens(context)
    target = max(budget - overhead, _MIN_CONTEXT_TOKENS)
    return prompt.replace(context, _summarise(context, target, model_name), 1)


def _summarise(context: str, target_tokens: int, model_name: str) -> str:
    """Condense `context` to about `target_tokens` tokens, keeping its facts, and guarantee the cap.

    One free-text call asks the model to drop redundancy and elaboration while keeping the concrete facts,
    names, dates, and numbers a diary answer turns on. It calls the boundary directly, bypassing _fit,
    so a large context can't recurse into fitting itself, and it is sent raw — the model accepts more than its
    optimal even where it reads that much less well. A summariser can overshoot the length it was asked for,
    so the result is truncated to `target_tokens`, making the budget a promise the guard keeps rather than a
    request the model may ignore.
    """
    prompt = (
        f"Condense the following notes to at most about {target_tokens} tokens. "
        "Keep every concrete fact, name, date, and number; drop only redundancy and elaboration. "
        "Return only the condensed notes, nothing else.\n\n"
        f"{context}"
    )
    summary = _call({"model": model_name, "prompt": prompt, "think": False, "stream": False})
    return models.truncate_tokens(summary, target_tokens)


def generate(prompt: str, *, model: str | None = None, context: str | None = None) -> str:
    """Run one generative call and return its reply as free text.

    The counterpart to generate_json for the reply the read path composes:
    prose the caller cannot — and should not — hold to a schema,
    so no `format` grammar is sent and the model is free to emit natural language rather than JSON.
    model defaults to the router's generative model (config.RERANK_MODEL);
    the reply path passes config.REPLY_MODEL, which may point at a larger model than the router's.
    context, when given, is the summarisable slice of `prompt` the budget guard may condense if the prompt
    overruns the model's optimal window (see _fit) — for the reply, the folded-in facts.

    Two settings are shared with generate_json and fixed for the same reasons:
    thinking is off — the hard local-speed requirement holds on the read path too,
    where the reply is the very thing the symbiot is waiting on, so a reasoning trace is latency this call can't spend;
    and streaming is off, so the whole reply comes back in one response rather than a token stream.
    Temperature is deliberately *not* pinned to 0 here, unlike the router's calls:
    a scored judgment wants to land the same way twice,
    but a spoken reply reads better with the model's own default warmth than with the flattest wording —
    so this leaves it to the model.
    An empty response raises rather than returning a blank reply that would reach the symbiot as silence.
    """
    model_name = model or config.RERANK_MODEL
    payload = {
        "model": model_name,
        "prompt": _fit(prompt, context, model_name),
        "think": False,
        "stream": False,
    }
    return _call(payload)


def generate_json(
    prompt: str, schema: type[M], *, model: str | None = None, context: str | None = None
) -> M:
    """Run one generative call and validate its reply into an instance of `schema`.

    schema is mandatory and is a Pydantic model class:
    its JSON Schema is handed to Ollama as the output grammar,
    which Ollama compiles to a decode-time grammar so the model can only emit tokens that keep the reply conforming,
    and the reply is parsed and validated back through the same model —
    so the answer that crosses this boundary is a typed object with its fields already checked,
    never a loose dict a caller has to second-guess.
    A reply that breaks the model's constraints raises here
    rather than slipping through as a half-read decision that would quietly mis-file a fact.
    model defaults to the router's generative model (config.RERANK_MODEL);
    a caller that wants a different model passes its own.
    context, when given, is the summarisable slice of `prompt` the budget guard may condense if the prompt
    overruns the model's optimal window (see _fit); the router's prompts are bounded, so they leave it unset.

    Thinking is off, and not offered:
    every call through here is a fast classification-style judgment on hardware that has to answer quickly,
    so we take the decode-time grammar — which the model can only have with thinking off —
    over a reasoning trace we can't afford.
    """
    model_name = model or config.RERANK_MODEL
    payload = {
        "model": model_name,
        "prompt": _fit(prompt, context, model_name),
        "think": False,
        "stream": False,
        "format": schema.model_json_schema(),
        "options": {"temperature": 0},
    }
    return schema.model_validate_json(_call(payload))
