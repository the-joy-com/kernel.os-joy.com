"""LLM: a prompt in, an answer out, through a bigger cloud model with a fallback ladder home.

Two shapes pass through here, for two kinds of caller.
The ontology router wants judgments a vector distance can't make —
re-ranking the recalled candidates, breaking a tie in the grey zone — and each is a prompt in, JSON out:
`generate_json` holds those to an exact Pydantic shape.
The read path wants a spoken reply — free prose, held to no schema — and that is `generate`,
the same boundary with the schema dropped, returning the model's text as-is.

Beneath both sits one round trip (`_call`) and a **fallback ladder** it walks per request.
Generation runs on a bigger, faster model than the box can serve,
so the primary is a model on Scaleway (GPU-backed), reached through the OpenAI-compatible client Scaleway advertises.
A call that fails *outage-class* there — a transport error, a timeout, a 5xx, a 429 —
falls to Mistral's own web API,
and then, only if both clouds are down, to the local Ollama model that used to serve every call.
The ladder is deliberately **stateless**: each call tries the primary afresh, with no shared breaker counting failures.
The reply is composed inside a killable forked child (execution.run_with_deadline),
so breaker state set there would die with the child and never reach the next call;
and at one-symbiot volume the only cost of statelessness —
paying the primary's timeout once per call during an outage —
is one the intake deadline is sized to absorb (config.INTAKE_DEADLINE_SECONDS clears three tiers).
A 4xx (a bad request, a bad key) is *not* an outage: it is our own mistake,
so it surfaces at once rather than falling through to a provider that would fail identically and hide the bug.

Three call settings are fixed here so no caller has to remember them:
thinking is off —
every call is a fast judgment or a reply the symbiot is waiting on, not a problem that wants a visible reasoning trace —
so GLM's reasoning is disabled with Scaleway's documented `reasoning_effort="none"`,
Mistral Large has no trace to suppress, and the Ollama tier keeps `think=False`;
the output is held to the shape the caller demands —
`generate_json` hands its Pydantic model through each SDK's structured-output `parse` helper,
which binds the decoder to that model's schema, and validates the reply back through the same model,
so the answer that crosses this boundary is a typed object with its fields already checked,
and a reply that breaks the schema raises here rather than slipping through as a half-read decision;
and sampling is at temperature 0 for the router's scored judgments, so the same inputs score the same way twice,
while the spoken reply is left at the model's own default warmth.
There is no loose-JSON mode:
the model boundary gets the same typed discipline the HTTP boundary already gets from these DTOs (core/dtos.py),
from the first call rather than tightened later.

Before either call reaches a model, the prompt is held to that model's context budget (_fit, services.models):
if it would overrun the window the model reads well,
the summarisable context the caller marked is condensed to fit —
only that context, never the instructions around it —
so a prompt swollen with folded-in facts is trimmed rather than truncated blind.
The three generative tiers share one optimal window (131072),
so a prompt fitted for the primary fits every tier.

This crosses the kernel's old local-only stance on purpose:
generation now sends the symbiot's own words to an external provider,
a deliberate trade of the strictly-local posture for capability and speed.
Embedding does not make that trade — it stays on the box (embedding.py), tied to its model's vector dims.
"""

from typing import TypeVar

import httpx
import ollama
import openai
from mistralai.client import errors as mistral_errors, Mistral
from openai import OpenAI
from pydantic import BaseModel

from core import config
from services import models

M = TypeVar("M", bound=BaseModel)

# The smallest a summarised context is ever aimed at.
# A budget so tight it left the context almost no room would ask the summariser for nonsense,
# so the target is floored here — better a little over budget than a summary squeezed to nothing.
_MIN_CONTEXT_TOKENS = 128


class _Outage(Exception):
    """A generative tier failed in a way that warrants trying the next one down the ladder —
    a transport error, a timeout, a 5xx, or a 429.
    Distinct from a 4xx, which signals our own bad request
    and is left to propagate so it surfaces rather than being masked by a fall-through."""


def _scaleway(model_name: str, prompt: str, schema: type[BaseModel] | None, temperature: float | None) -> str:
    """One generative call to Scaleway through the OpenAI-compatible client Scaleway advertises.

    The client is built fresh per call (fork-safety for the reply's killable child) with retries off,
    so an outage fails fast to the next tier rather than the SDK burning its own retry budget first.
    Reasoning is disabled with `reasoning_effort="none"` —
    Scaleway's documented control for it,
    in place of the z.ai `chat_template_kwargs`/`thinking` fields their Generative APIs explicitly do not support.
    A schema, when given, goes through the SDK's `parse` helper:
    it hands the Pydantic model over as a strict structured-output request
    (the schema carrying `additionalProperties: false` and all-required,
    which Scaleway requires the decoder to bind to — a plain best-effort json_schema is only a hint here,
    and GLM answers past it).
    A free-text reply takes `create` with no response_format.
    Outage-class failures raise _Outage; a 4xx propagates.
    """
    client = OpenAI(
        api_key=config.SCALEWAY_API_KEY,
        base_url=config.SCALEWAY_API_BASE_URL,
        max_retries=0,
        timeout=config.LLM_TIMEOUT_SECONDS,
    )
    request = {
        "messages": [{"role": "user", "content": prompt}],
        "model": model_name,
        "reasoning_effort": "none",
    }
    if temperature is not None:
        request["temperature"] = temperature
    try:
        if schema is not None:
            completion = client.chat.completions.parse(response_format=schema, **request)
        else:
            completion = client.chat.completions.create(stream=False, **request)
    except (
        openai.APIConnectionError,
        openai.APITimeoutError,
        openai.InternalServerError,
        openai.RateLimitError,
    ) as exc:
        raise _Outage(f"Scaleway generative call failed outage-class: {exc}") from exc
    body = completion.choices[0].message.content
    if not body:
        raise RuntimeError(f"generative model {model_name!r} on Scaleway returned an empty response")
    return body


def _mistral(model_name: str, prompt: str, schema: type[BaseModel] | None, temperature: float | None) -> str:
    """One generative call to Mistral's own web API through the official mistralai client.

    The fallback tier when Scaleway is down —
    reached at Mistral directly, never Scaleway's Mistral,
    since the whole point is surviving Scaleway being unreachable.
    Built fresh per call, the same fork-safety reason.
    A schema, when given, goes through the SDK's `parse` helper,
    which converts the Pydantic model to Mistral's strict json_schema response_format and validates the reply —
    the mirror of the Scaleway path; a free-text reply takes `complete`.
    Outage-class failures — a 5xx, a 429, or no response at all — raise _Outage to fall through to the local tier;
    a 4xx propagates as itself.
    """
    client = Mistral(api_key=config.MISTRAL_API_KEY, timeout_ms=int(config.LLM_TIMEOUT_SECONDS * 1000))
    request: dict = {
        "messages": [{"role": "user", "content": prompt}],
        "model": model_name,
    }
    if temperature is not None:
        request["temperature"] = temperature
    try:
        if schema is not None:
            completion = client.chat.parse(response_format=schema, **request)
        else:
            completion = client.chat.complete(**request)
    except mistral_errors.SDKError as exc:
        status = getattr(getattr(exc, "raw_response", None), "status_code", None)
        if status is None or status >= 500 or status == 429:
            raise _Outage(f"Mistral generative call failed outage-class: {exc}") from exc
        raise
    except (httpx.TransportError, httpx.TimeoutException, mistral_errors.NoResponseError) as exc:
        raise _Outage(f"Mistral generative call unreachable: {exc}") from exc
    body = completion.choices[0].message.content
    # Mistral may answer with content chunks rather than a bare string; flatten to the text we asked for.
    if isinstance(body, list):
        body = "".join(getattr(chunk, "text", "") for chunk in body)
    if not body:
        raise RuntimeError(f"generative model {model_name!r} on Mistral returned an empty response")
    return body


def _ollama(model_name: str, prompt: str, schema: type[BaseModel] | None, temperature: float | None) -> str:
    """One generative call to the local Ollama model — the ladder's last resort, and the rollback target.

    Reached when both clouds are down, or directly when a model config points at a local name.
    Built fresh per call for fork-safety, as before.
    A schema becomes Ollama's `format` (its decode-time grammar); temperature 0 rides `options`.
    This is the last tier, so it raises its real errors rather than _Outage —
    there is nothing further to fall through to.
    """
    client = ollama.Client(host=config.OLLAMA_BASE_URL, timeout=config.LLM_TIMEOUT_SECONDS)
    request = {"model": model_name, "prompt": prompt, "stream": False, "think": False}
    if schema is not None:
        request["format"] = schema.model_json_schema()
    if temperature is not None:
        request["options"] = {"temperature": temperature}
    body = client.generate(**request).response
    if not body:
        raise RuntimeError(f"generative model {model_name!r} on Ollama returned an empty response")
    return body


def _call(*, model: str, prompt: str, schema: type[BaseModel] | None = None, temperature: float | None = None) -> str:
    """Run one generative call down the fallback ladder and return its reply text.

    The one place the round trip lives, shared by both public calls and the summariser beneath them.
    The requested model's provider (services.models) decides the entry point:
    a Scaleway model walks the full ladder — Scaleway, then Mistral, then local Ollama,
    each next tier tried only when the one above raised _Outage;
    a model named for another provider is called there directly, the one-line rollback path.
    A model not in the map is treated as a local Ollama name (its historical default).
    An empty reply raises inside each tier,
    so neither a transport failure nor a blank answer passes as a half-read decision
    or reaches the symbiot as silence.
    """
    spec = models.MODELS.get(model)
    provider = spec.provider if spec is not None else "ollama"
    if provider == "scaleway":
        try:
            return _scaleway(model, prompt, schema, temperature)
        except _Outage:
            pass
        try:
            return _mistral(config.GENERATIVE_FALLBACK_MODEL, prompt, schema, temperature)
        except _Outage:
            pass
        return _ollama(config.GENERATIVE_LOCAL_FALLBACK_MODEL, prompt, schema, temperature)
    if provider == "mistral":
        return _mistral(model, prompt, schema, temperature)
    return _ollama(model, prompt, schema, temperature)


def _fit(prompt: str, context: str | None, model_name: str) -> str:
    """Hold `prompt` to the model's optimal context budget, condensing `context` in place if it overruns.

    Consulted before every generative call.
    The budget is the model's optimal window (services.models),
    less a margin (config.CONTEXT_SAFETY_MARGIN) for the tokeniser's approximation and the reply's own output.
    Under it, the prompt is returned untouched.
    Over it, only `context` — the summarisable part the caller marked, the folded-in facts, never the instructions around them —
    is condensed to the room the instructions leave,
    and spliced back where it sat,
    so a compression can never delete the lines that tell the model what to do.

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
    names, dates, and numbers a diary answer turns on.
    It calls the boundary directly, bypassing _fit, so a large context can't recurse into fitting itself,
    and it is sent raw — the model accepts more than its optimal even where it reads that much less well.
    A summariser can overshoot the length it was asked for,
    so the result is truncated to `target_tokens`,
    making the budget a promise the guard keeps rather than a request the model may ignore.
    """
    prompt = (
        f"Condense the following notes to at most about {target_tokens} tokens. "
        "Keep every concrete fact, name, date, and number; drop only redundancy and elaboration. "
        "Return only the condensed notes, nothing else.\n\n"
        f"{context}"
    )
    summary = _call(model=model_name, prompt=prompt)
    return models.truncate_tokens(summary, target_tokens)


def generate(prompt: str, *, model: str | None = None, context: str | None = None) -> str:
    """Run one generative call and return its reply as free text.

    The counterpart to generate_json for the reply the read path composes:
    prose the caller cannot — and should not — hold to a schema,
    so no `response_format` is sent and the model is free to emit natural language rather than JSON.
    model defaults to the router's generative model (config.RERANK_MODEL);
    the reply path passes config.REPLY_MODEL, which may point at a different model than the router's.
    context, when given, is the summarisable slice of `prompt` the budget guard may condense
    if the prompt overruns the model's optimal window (see _fit) — for the reply, the folded-in facts.

    Thinking is off, as for every call through this boundary —
    the reply is the very thing the symbiot is waiting on, so a reasoning trace is latency this call can't spend.
    Temperature is deliberately *not* pinned to 0 here, unlike the router's calls:
    a scored judgment wants to land the same way twice,
    but a spoken reply reads better with the model's own default warmth than with the flattest wording —
    so this leaves it to the model.
    An empty response raises rather than returning a blank reply that would reach the symbiot as silence.
    """
    model_name = model or config.RERANK_MODEL
    return _call(model=model_name, prompt=_fit(prompt, context, model_name))


def generate_json(
    prompt: str, schema: type[M], *, model: str | None = None, context: str | None = None
) -> M:
    """Run one generative call and validate its reply into an instance of `schema`.

    schema is mandatory and is a Pydantic model class:
    its JSON Schema is handed to the provider as the output `response_format`,
    and the reply is parsed and validated back through the same model —
    so the answer that crosses this boundary is a typed object with its fields already checked,
    never a loose dict a caller has to second-guess.
    A reply that breaks the model's constraints raises here
    rather than slipping through as a half-read decision that would quietly mis-file a fact —
    the provider's schema is best-effort guidance, but this validation is the guarantee, whichever tier answered.
    model defaults to the router's generative model (config.RERANK_MODEL);
    a caller that wants a different model passes its own.
    context, when given, is the summarisable slice of `prompt` the budget guard may condense
    if the prompt overruns the model's optimal window (see _fit); the router's prompts are bounded, so they leave it unset.

    Thinking is off, and not offered:
    every call through here is a fast classification-style judgment,
    and sampling is pinned to temperature 0 so the same inputs score the same way twice.
    """
    model_name = model or config.RERANK_MODEL
    reply = _call(model=model_name, prompt=_fit(prompt, context, model_name), schema=schema, temperature=0)
    return schema.model_validate_json(reply)
