"""LLM: a prompt in, an answer out, through a bigger cloud model with a fallback ladder home.

Two shapes pass through here, for two kinds of caller.
The ontology router wants judgments a vector distance can't make —
re-ranking the recalled candidates, breaking a tie in the grey zone — and each is a prompt in, JSON out:
`generate_json` holds those to an exact Pydantic shape.
The read path wants a spoken reply — free prose, held to no schema — and that is `generate`,
the same boundary with the schema dropped, returning the model's text as-is.

Beneath both sits one round trip (`_call`) and a **fallback ladder** it walks per request.
Generation runs on a bigger, faster model than the box can serve, on a four-rung ladder.
The top rung is Google (Gemini) on the Agent Platform — reached first for every automatic call, so in the steady state
it is what answers, and the reason the switch exists: the Agent Platform caches the large repeated prompt prefix
these calls front-load, billing it once where the rungs below re-bill it in full every turn.
A call that fails *outage-class* on Google — a transport error, a timeout, a 5xx, a 429, an unresolvable ADC credential —
falls to a model on Scaleway (GPU-backed), reached through the OpenAI-compatible client Scaleway advertises;
one that fails outage-class there falls to Mistral's own web API,
and then, only if every cloud is down, to the local Ollama model that used to serve every call.
The ladder is deliberately **stateless**: each call tries the primary afresh, with no shared breaker counting failures.
The reply is composed inside a killable forked child (execution.run_with_deadline),
so breaker state set there would die with the child and never reach the next call;
and at one-symbiot volume the only cost of statelessness —
paying the primary's timeout once per call during an outage —
is one the intake deadline is sized to absorb (config.INTAKE_DEADLINE_SECONDS clears three tiers).
A 4xx (a bad request, a bad key) is *not* an outage: it is our own mistake,
so it surfaces at once rather than falling through to a provider that would fail identically and hide the bug.

Four call settings are fixed here so no caller has to remember them:
thinking is off —
every call is a fast judgment or a reply the symbiot is waiting on, not a problem that wants a visible reasoning trace —
so on Google reasoning is turned down to its floor per model (`thinking_level` MINIMAL for the Flash pair, LOW for the Pro, since a 3.x model cannot turn it off at all; see _thinking_level),
on Scaleway reasoning is turned down per model (GLM and Qwen accept `reasoning_effort="none"`, gpt-oss its floor `"low"`, which still returns no trace for these bounded calls; see _reasoning_effort),
the Mistral tier has no trace to suppress, and the Ollama tier keeps `think=False`;
the output is held to the shape the caller demands —
`generate_json` hands its Pydantic model through each SDK's structured-output mechanism (Scaleway's and Mistral's `parse` helpers, Google's `response_schema`, Ollama's `format`),
which binds the decoder to that model's schema, and validates the reply back through the same model,
so the answer that crosses this boundary is a typed object with its fields already checked,
and a reply that breaks the schema raises here rather than slipping through as a half-read decision;
sampling is at temperature 0 for every call, judgment and reply alike —
the router wants the same inputs to score the same way twice,
and the reply may be composed by any tier of the ladder, so pinning 0 strips the sampling randomness rather than stacking it on top of the differences between the models (it doesn't make the tiers speak identically, but it stops adding avoidable variance to whichever one answers);
and the reply length is held to a per-model output ceiling (services.models), a guard that stops a runaway generation, generous above any real reply.
There is no loose-JSON mode:
the model boundary gets the same typed discipline the HTTP boundary already gets from these DTOs (core/dtos.py),
from the first call rather than tightened later.

Before either call reaches a model, the prompt is held to that model's context budget (_fit, services.models):
if it would overrun the window the model reads well,
the summarisable context the caller marked is condensed to fit —
only that context, never the instructions around it —
so a prompt swollen with folded-in facts is trimmed rather than truncated blind.
Within a tier every rung sits at or above the window of the rung above it that falls to it (services.models),
so a prompt fitted for the top rung it was resolved against can never overflow the humbler model that inherits it.

This crosses the kernel's old local-only stance on purpose:
generation now sends the symbiot's own words to an external provider,
a deliberate trade of the strictly-local posture for capability and speed.
Embedding does not make that trade — it stays on the box (embedding.py), tied to its model's vector dims.
"""

import os
from typing import TypeVar

import httpx
import ollama
import openai
from google import genai
from google.auth import exceptions as google_auth_errors
from google.genai import errors as genai_errors, types as genai_types
from mistralai.client import errors as mistral_errors, Mistral
from openai import OpenAI
from pydantic import BaseModel

from core import config
from services.adapters import models

M = TypeVar("M", bound=BaseModel)

# The smallest a summarised context is ever aimed at.
# A budget so tight it left the context almost no room would ask the summariser for nonsense,
# so the target is floored here — better a little over budget than a summary squeezed to nothing.
_MIN_CONTEXT_TOKENS = 128


class _Outage(Exception):
    """A generative tier failed in a way that warrants trying the next one down the ladder —
    a transport error, a timeout, a 5xx, or a 429.
    Distinct from a 4xx, which signals our own bad request
    and is left to propagate so it surfaces rather than being masked by a fall-through.
    """


def _reasoning_effort(model_name: str) -> str:
    """The reasoning_effort to send Scaleway for this model — always a value its schema accepts.

    Scaleway's Generative APIs take one of 'none' | 'low' | 'medium' | 'high',
    and every call here wants thinking off:
    a fast judgment, or a reply the symbiot is waiting on, not a visible reasoning trace.
    Most models accept 'none' and emit no trace;
    gpt-oss is the exception — it rejects 'none' with a 400 (its reasoning floor is 'low'),
    but at 'low' it still returns no reasoning trace for the bounded calls made here,
    so 'low' is its effective thinking-off.
    Keyed by name, so the value is always one the model accepts and a 400 on the effort field is impossible.
    """
    return "low" if model_name.startswith("gpt-oss") else "none"


def _thinking_level(model_name: str) -> str:
    """The thinking level to send a Gemini model — always its lowest, since these bounded calls never want a trace.

    Google made reasoning mandatory on the 3.x models: unlike Scaleway's "none", it cannot be turned off at all,
    only turned down. So each Gemini model runs at the floor it allows —
    the Flash models accept MINIMAL, and the Pro's own floor is LOW (it rejects MINIMAL) —
    the same shape as _reasoning_effort's gpt-oss exception, keyed by name so the value is always one the model accepts.
    """
    return "MINIMAL" if "flash" in model_name else "LOW"


def _google(
    model_name: str,
    prompt: str,
    schema: type[BaseModel] | None,
    temperature: float | None,
    max_output_tokens: int | None,
) -> str:
    """One generative call to Google (Gemini) on the Agent Platform through the google-genai client — the top rung.

    Reached first for every automatic call, so in the steady state this is what answers,
    and the whole reason the switch exists: the Agent Platform caches the large repeated prompt prefix
    these calls front-load, billing it once and then near-free where the rungs below re-bill it in full every turn.
    The client is built fresh per call (fork-safety for the reply's killable child) and authenticated by ADC —
    gcloud locally, a service account on the box — through `vertexai=True` with the project and region from config,
    never an API key (that path routes to the consumer Developer API, not the enterprise, zero-retention Agent Platform).
    Thinking cannot be turned off on a 3.x model, only down, so it runs at its floor (_thinking_level).
    A schema, when given, is handed over as the response_schema with a JSON mime type —
    the decoder is bound to it and the reply comes back as JSON text, validated our side like the other tiers'.
    A free-text reply names no schema and comes back as prose.

    An unwired box (no GOOGLE_CLOUD_PROJECT) raises _Outage at once, so the ladder falls straight through to Scaleway —
    the same "this rung can't answer" the empty-key case is for the rungs below.
    Outage-class failures — a 5xx, a 429, a transport error, or an ADC credential the box can't resolve —
    raise _Outage to fall through to Scaleway;
    a 4xx that is not a 429 is our own bad request and propagates, the same discipline the other tiers keep.
    """
    if not config.GOOGLE_CLOUD_PROJECT:
        raise _Outage("Google generative rung not wired (no GOOGLE_CLOUD_PROJECT); falling through to Scaleway")
    client = genai.Client(
        vertexai=True,
        project=config.GOOGLE_CLOUD_PROJECT,
        location=config.GOOGLE_CLOUD_LOCATION,
    )
    settings = genai_types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        thinking_config=genai_types.ThinkingConfig(thinking_level=_thinking_level(model_name)),
    )
    if schema is not None:
        settings.response_mime_type = "application/json"
        settings.response_schema = schema
    try:
        completion = client.models.generate_content(
            model=model_name, contents=prompt, config=settings
        )
    except genai_errors.ServerError as exc:
        raise _Outage(f"Google generative call failed outage-class: {exc}") from exc
    except genai_errors.ClientError as exc:
        if getattr(exc, "code", None) == 429:
            raise _Outage(f"Google generative call rate-limited: {exc}") from exc
        raise
    except (
        httpx.TransportError,
        httpx.TimeoutException,
        google_auth_errors.GoogleAuthError,
    ) as exc:
        raise _Outage(f"Google generative call unreachable: {exc}") from exc
    body = completion.text
    if not body:
        raise RuntimeError(
            f"generative model {model_name!r} on Google returned an empty response"
        )
    return body


def _scaleway(
    model_name: str,
    prompt: str,
    schema: type[BaseModel] | None,
    temperature: float | None,
    max_output_tokens: int | None,
) -> str:
    """One generative call to Scaleway through the OpenAI-compatible client Scaleway advertises.

    The client is built fresh per call (fork-safety for the reply's killable child) with retries off,
    so an outage fails fast to the next tier rather than the SDK burning its own retry budget first.
    Reasoning is turned down through Scaleway's `reasoning_effort` (see _reasoning_effort),
    the control their Generative APIs expose,
    in place of the z.ai `chat_template_kwargs`/`thinking` fields those APIs explicitly do not support.
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
        "reasoning_effort": _reasoning_effort(model_name),
    }
    if temperature is not None:
        request["temperature"] = temperature
    if max_output_tokens is not None:
        request["max_tokens"] = max_output_tokens
    try:
        if schema is not None:
            completion = client.chat.completions.parse(
                response_format=schema, **request
            )
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
        raise RuntimeError(
            f"generative model {model_name!r} on Scaleway returned an empty response"
        )
    return body


def _mistral(
    model_name: str,
    prompt: str,
    schema: type[BaseModel] | None,
    temperature: float | None,
    max_output_tokens: int | None,
) -> str:
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
    client = Mistral(
        api_key=config.MISTRAL_API_KEY,
        timeout_ms=int(config.LLM_TIMEOUT_SECONDS * 1000),
    )
    request: dict = {
        "messages": [{"role": "user", "content": prompt}],
        "model": model_name,
    }
    if temperature is not None:
        request["temperature"] = temperature
    if max_output_tokens is not None:
        request["max_tokens"] = max_output_tokens
    try:
        if schema is not None:
            completion = client.chat.parse(response_format=schema, **request)
        else:
            completion = client.chat.complete(**request)
    except mistral_errors.SDKError as exc:
        status = getattr(getattr(exc, "raw_response", None), "status_code", None)
        if status is None or status >= 500 or status == 429:
            raise _Outage(
                f"Mistral generative call failed outage-class: {exc}"
            ) from exc
        raise
    except (
        httpx.TransportError,
        httpx.TimeoutException,
        mistral_errors.NoResponseError,
    ) as exc:
        raise _Outage(f"Mistral generative call unreachable: {exc}") from exc
    body = completion.choices[0].message.content
    # Mistral may answer with content chunks rather than a bare string; flatten to the text we asked for.
    if isinstance(body, list):
        body = "".join(getattr(chunk, "text", "") for chunk in body)
    if not body:
        raise RuntimeError(
            f"generative model {model_name!r} on Mistral returned an empty response"
        )
    return body


def _ollama(
    model_name: str,
    prompt: str,
    schema: type[BaseModel] | None,
    temperature: float | None,
    max_output_tokens: int | None,
) -> str:
    """One generative call to the local Ollama model — the ladder's last resort, and the rollback target.

    Reached when both clouds are down, or directly when a model config points at a local name.
    Built fresh per call for fork-safety, as before.
    A schema becomes Ollama's `format` (its decode-time grammar); temperature and the output ceiling ride `options` —
    the ceiling as `num_predict`, which Ollama leaves unbounded (-1) by default, so setting it is what actually caps the reply here.
    The context window rides `options` too, as `num_ctx`, and it is not optional:
    Ollama opens 4096 tokens for every model whatever its weights allow,
    and a prompt past that is discarded from the *front* with no error —
    so the persona head, which leads every composed prompt, is the first thing lost.
    A prompt reaching this tier was fitted upstream to the *requested* model's window (see _fit),
    never re-fitted on the way down,
    which is why the catalog holds this floor at or above every rung that can fall to it (see services.models):
    the floor is the widest window in the ladder by design, and this is where that claim is made true rather than assumed.
    The window opened is the catalog's own figure for the model about to answer,
    so the number this call packs to and the number it opens are one number and cannot drift apart.
    A name the catalog doesn't carry opens no window and takes Ollama's default,
    the same unmapped-model case _fit and _output_cap already pass through unbudgeted.
    This is the last tier, so it raises its real errors rather than _Outage —
    there is nothing further to fall through to.
    """
    client = ollama.Client(
        host=config.OLLAMA_BASE_URL, timeout=config.LLM_TIMEOUT_SECONDS
    )
    request = {"model": model_name, "prompt": prompt, "stream": False, "think": False}
    if schema is not None:
        request["format"] = schema.model_json_schema()
    options = {}
    spec = models.spec(model_name)
    if spec is not None:
        options["num_ctx"] = spec.optimal_context_tokens
    if temperature is not None:
        options["temperature"] = temperature
    if max_output_tokens is not None:
        options["num_predict"] = max_output_tokens
    if options:
        request["options"] = options
    body = client.generate(**request).response
    if not body:
        raise RuntimeError(
            f"generative model {model_name!r} on Ollama returned an empty response"
        )
    return body


def _output_cap(override: int | None, model_name: str) -> int | None:
    """The output ceiling to hand one tier, resolved from the model about to answer so a fallback is capped
    at what it supports rather than the primary's figure.

    An ordinary call names no `override`, so it takes that tier's own model figure (services.models).
    The summariser names one — the room it needs up to its whole target — but that request is *clamped* to
    the tier's figure rather than winning outright: a target sized to the context budget can run far past
    what a provider accepts (Scaleway 400s over its cap, and a 400 does not fall through), so the clamp keeps
    even a huge-context summary a request the tier can honour. Clamping only shortens the summary, never the
    budget it must fit — the truncation after it (llm._summarise) still holds the promise.
    An unmapped model has no figure, so its `override` passes through and an ordinary call is left uncapped
    (None), the historical local-Ollama default."""
    spec = models.spec(model_name)
    cap = spec.max_output_tokens if spec is not None else None
    if override is not None:
        return min(override, cap) if cap is not None else override
    return cap


# The Scaleway model that catches each Gemini primary when it outages — keyed by the Gemini primary that failed.
# The Gemini primaries are genuinely distinct per tier (pro / flash / flash-lite), so this maps three keys,
# and the two cheaper tiers land on one Scaleway model (both gpt-oss-120b today) because the Scaleway rung shares it —
# the distinction lives in the Gemini rung above, not in the Scaleway one beneath.
# Keyed by the Gemini model *name* because that is all _call holds when the top rung outages.
_SCALEWAY_FALLBACK = {
    config.FLAGSHIP_MODEL: config.SCALEWAY_FLAGSHIP_MODEL,
    config.MID_MODEL: config.SCALEWAY_MID_MODEL,
    config.SMALL_MODEL: config.SCALEWAY_SMALL_MODEL,
}


def _scaleway_fallback(google_model: str) -> str:
    """The Scaleway model that catches this Gemini primary when it outages — its tier's second rung.

    A Gemini model in no tier — an operator's custom assignment through /models — falls to the flagship Scaleway:
    the widest window and the most capable of the rung, the safest catch when we can't place it in a tier.
    """
    return _SCALEWAY_FALLBACK.get(google_model, config.SCALEWAY_FLAGSHIP_MODEL)


# The Mistral model that catches each Scaleway rung when it outages — keyed by the Scaleway model that failed.
# One rung further down than _SCALEWAY_FALLBACK: it catches the Scaleway rung (itself the catch for Google),
# so the tier is carried by the Scaleway model name, all _call holds at that depth of the chain.
# The two cheaper tiers share one Scaleway model (gpt-oss-120b) and so share one Mistral catch (ministral-8b),
# exactly the iso shape the Scaleway rung has — the Mistral rung splits per tier only once the Scaleway one does.
_MISTRAL_FALLBACK = {
    config.SCALEWAY_FLAGSHIP_MODEL: config.MISTRAL_FLAGSHIP_MODEL,
    config.SCALEWAY_MID_MODEL: config.MISTRAL_MID_MODEL,
    config.SCALEWAY_SMALL_MODEL: config.MISTRAL_SMALL_MODEL,
}


def _mistral_fallback(scaleway_model: str) -> str:
    """The Mistral model that catches this Scaleway rung when it outages — its tier's cross-cloud fallback.

    A Scaleway model in no tier — an operator's custom assignment through /models — falls to the flagship Mistral:
    the widest window and the most capable of the rung, the safest catch when we can't place it in a tier.
    """
    return _MISTRAL_FALLBACK.get(scaleway_model, config.MISTRAL_FLAGSHIP_MODEL)


def _call(
    *,
    model: str,
    prompt: str,
    context: str | None = None,
    schema: type[BaseModel] | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
) -> str:
    """Run one generative call down the fallback ladder and return its reply text.

    The one place the round trip lives, shared by both public calls and the summariser beneath them.
    The requested model's provider (services.models) decides the entry point:
    a Google model walks the full ladder — Google, then its Scaleway rung, then that rung's Mistral catch, then local Ollama,
    each next rung tried only when the one above raised _Outage;
    a Scaleway model is the rollback path — Scaleway, then Mistral, then local, with the Google rung dropped —
    which is how an operator takes one role off Google through /models;
    a Mistral or local name is a bare call with nothing beneath it, the one-line rollback to a single provider.
    A model not in the map is treated as a local Ollama name (its historical default).
    The reply is held to an output ceiling (services.models), resolved per tier by _output_cap:
    the ceiling of the model about to answer, so a fallback is held to a cap it actually supports rather than
    the primary's, and every ordinary call is capped without the caller naming a number.
    A caller that passes `max_output_tokens` — the summariser — asks for more room, but that request is clamped
    to the answering tier's cap, never exceeding what the provider accepts.
    An empty reply raises inside each tier,
    so neither a transport failure nor a blank answer passes as a half-read decision
    or reaches the symbiot as silence.

    IS_LOCAL=1 short-circuits the ladder entirely: every call is served by Ollama on the box.
    Since the resolved role name is a cloud id Ollama can't serve, the call is substituted to the
    local SLM floor (config.GENERATIVE_LOCAL_FALLBACK_MODEL) unless the role already points at an Ollama model.
    That substitution is why `context` reaches this far.
    A prompt is fitted once, up in generate/generate_json, to the window of the model that was *requested* —
    which on a local-only box is a cloud model that will never see it.
    Down the fallback ladder that mismatch is harmless by construction, since every rung is at least as wide
    as the one above and the local floor is the widest of all.
    Here it is not: a measured box sizes its local window to the hardware (services.adapters.calibration),
    so the window can be far narrower than the cloud model the prompt was fitted for.
    So the local branch re-fits against the model that will actually answer, and only the local branch does —
    the ladder's own rungs keep the single fitting the widest-floor invariant already makes safe.
    """
    spec = models.spec(model)
    # Quick override to keep every call on the local box (IS_LOCAL=1): route to Ollama regardless of
    # the role's resolved provider. The resolved name is a cloud id (a role points at gpt-oss-120b, glm-5.2),
    # which Ollama can't serve — pulling it would 404 — so substitute the local SLM floor
    # (config.GENERATIVE_LOCAL_FALLBACK_MODEL) unless the role is already assigned an Ollama model.
    if os.getenv("IS_LOCAL") == "1":
        local = (
            model
            if spec is not None and spec.provider == "ollama"
            else config.GENERATIVE_LOCAL_FALLBACK_MODEL
        )
        # Re-fit for the model that answers, not the one that was asked for.
        # Skipped when the substitution was a no-op, since the prompt is already fitted to exactly this model
        # and re-fitting would spend a summarising call to reach the same place.
        if local != model:
            prompt = _fit(prompt, context, local)
        return _ollama(
            local, prompt, schema, temperature, _output_cap(max_output_tokens, local)
        )
    provider = spec.provider if spec is not None else "ollama"
    if provider == "google":
        # The full ladder: Google (the primary the roles resolve to), then its Scaleway rung,
        # then that rung's Mistral catch, then the local floor — each tried only when the one above raised _Outage.
        scaleway = _scaleway_fallback(model)
        try:
            return _google(
                model, prompt, schema, temperature, _output_cap(max_output_tokens, model)
            )
        except _Outage:
            pass
        try:
            return _scaleway(
                scaleway,
                prompt,
                schema,
                temperature,
                _output_cap(max_output_tokens, scaleway),
            )
        except _Outage:
            pass
        mistral = _mistral_fallback(scaleway)
        try:
            return _mistral(
                mistral,
                prompt,
                schema,
                temperature,
                _output_cap(max_output_tokens, mistral),
            )
        except _Outage:
            pass
        return _ollama(
            config.GENERATIVE_LOCAL_FALLBACK_MODEL,
            prompt,
            schema,
            temperature,
            _output_cap(max_output_tokens, config.GENERATIVE_LOCAL_FALLBACK_MODEL),
        )
    if provider == "scaleway":
        # The rollback path: a role pointed by hand at a Scaleway model, dropping the Google rung for it.
        # Still a real ladder from there down — Scaleway, then its Mistral catch, then the local floor.
        try:
            return _scaleway(
                model, prompt, schema, temperature, _output_cap(max_output_tokens, model)
            )
        except _Outage:
            pass
        fallback = _mistral_fallback(model)
        try:
            return _mistral(
                fallback,
                prompt,
                schema,
                temperature,
                _output_cap(max_output_tokens, fallback),
            )
        except _Outage:
            pass
        return _ollama(
            config.GENERATIVE_LOCAL_FALLBACK_MODEL,
            prompt,
            schema,
            temperature,
            _output_cap(max_output_tokens, config.GENERATIVE_LOCAL_FALLBACK_MODEL),
        )
    if provider == "mistral":
        # A role pointed by hand straight at a Mistral model — a bare call with nothing beneath it.
        return _mistral(
            model, prompt, schema, temperature, _output_cap(max_output_tokens, model)
        )
    return _ollama(
        model, prompt, schema, temperature, _output_cap(max_output_tokens, model)
    )


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
    spec = models.spec(model_name)
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
    Temperature is 0, as for every call through this boundary — a condensation wants to be faithful and reproducible, not warm.
    The output ceiling is raised toward `target_tokens`: this call legitimately needs room up to its whole target,
    more than an ordinary reply, so it names its own rather than take the default cap —
    but the ceiling is clamped to what the answering tier accepts (_output_cap), since a target sized to the
    context budget can outrun a provider's own cap, and asking Scaleway for more than it allows is a 400, not a longer reply.
    A summariser can still overshoot the length it was asked for,
    so the result is truncated to `target_tokens`,
    making the budget a promise the guard keeps rather than a request the model may ignore.
    """
    prompt = (
        f"Condense the following notes to at most about {target_tokens} tokens. "
        "Keep every concrete fact, name, date, and number; drop only redundancy and elaboration. "
        "Return only the condensed notes, nothing else.\n\n"
        f"{context}"
    )
    summary = _call(
        model=model_name, prompt=prompt, temperature=0, max_output_tokens=target_tokens
    )
    return models.truncate_tokens(summary, target_tokens)


def generate(
    prompt: str, *, model: str | None = None, context: str | None = None
) -> str:
    """Run one generative call and return its reply as free text.

    The counterpart to generate_json for the reply the read path composes:
    prose the caller cannot — and should not — hold to a schema,
    so no `response_format` is sent and the model is free to emit natural language rather than JSON.
    model defaults to the model assigned the router's rerank role (models.role_name("rerank"));
    the reply path passes the model assigned its own role, which may point at a different one than the router's.
    context, when given, is the summarisable slice of `prompt` the budget guard may condense
    if the prompt overruns the model's optimal window (see _fit) — for the reply, the folded-in facts.

    Thinking is off, as for every call through this boundary —
    the reply is the very thing the symbiot is waiting on, so a reasoning trace is latency this call can't spend.
    Temperature is pinned to 0, as it is for the router's judgments:
    the reply may be composed by any tier of the fallback ladder, and each provider warms its own default differently,
    so leaving it unset would stack that sampling randomness on top of the differences between the models themselves.
    Pinning 0 doesn't make the three tiers speak identically — they are different models with different voices —
    but it removes the avoidable variance, so each answers as consistently as it can and the same diary tends to reproduce the same reply.
    An empty response raises rather than returning a blank reply that would reach the symbiot as silence.
    """
    model_name = model or models.role_name("rerank")
    return _call(
        model=model_name,
        prompt=_fit(prompt, context, model_name),
        context=context,
        temperature=0,
    )


def generate_json(
    prompt: str,
    schema: type[M],
    *,
    model: str | None = None,
    context: str | None = None,
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
    model defaults to the model assigned the router's rerank role (models.role_name("rerank"));
    a caller that wants a different model passes its own.
    context, when given, is the summarisable slice of `prompt` the budget guard may condense
    if the prompt overruns the model's optimal window (see _fit); the router's prompts are bounded, so they leave it unset.

    Thinking is off, and not offered:
    every call through here is a fast classification-style judgment,
    and sampling is pinned to temperature 0 so the same inputs score the same way twice.
    """
    model_name = model or models.role_name("rerank")
    reply = _call(
        model=model_name,
        prompt=_fit(prompt, context, model_name),
        context=context,
        schema=schema,
        temperature=0,
    )
    return schema.model_validate_json(reply)
