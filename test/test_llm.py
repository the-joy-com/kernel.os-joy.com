"""The free-text generate path and the context-budget guard, with the generative client faked at the boundary.

generate_json's own contract is exercised in test_ontology.py alongside the router that leans on it;
here we cover the two things the read path added: a prose reply with no schema grammar, and the _fit guard
that holds a prompt to the model's optimal window by condensing only its context.

The default generative model is a Scaleway model, so the boundary faked here is the OpenAI client
(llm.OpenAI) Scaleway is reached through; the one test that pins an unmapped model exercises the ladder's
local Ollama tier instead, and fakes that.
"""

from types import SimpleNamespace

import httpx
import pytest

from services.adapters import llm


class _FakeChat:
    """Callable stand-in for llm.OpenAI (the Scaleway client): records each chat.completions.create
    payload in captured["json"] and the client's base_url in captured["base_url"], and answers from
    canned data. `generate` may be a value or a callable(kwargs); a raising callable is a landmine for
    a path that must never run. The recorded payload is the OpenAI request — the prompt sits at
    messages[-1]["content"], the schema grammar at response_format, thinking-off at extra_body.
    """

    def __init__(self, *, generate=None):
        self._generate = generate
        self.captured = {}

    def __call__(self, *, base_url=None, api_key=None, timeout=None, max_retries=None):
        self.captured["base_url"] = base_url
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create, parse=self._parse))
        return self

    def _create(self, **kwargs):
        # The free-text path (no schema).
        self.captured["json"] = kwargs
        text = self._generate(kwargs) if callable(self._generate) else self._generate
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    def _parse(self, **kwargs):
        # The structured path: response_format is the Pydantic model class the caller passed.
        self.captured["json"] = kwargs
        text = self._generate(kwargs) if callable(self._generate) else self._generate
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class _FakeOllama:
    """Callable stand-in for ollama.Client, for the ladder's local tier: records a generate() call's
    keyword payload in captured["json"] (the Ollama request, whose prompt sits under the "prompt" key)
    and answers from canned data.
    """

    def __init__(self, *, generate=None):
        self._generate = generate
        self.captured = {}

    def __call__(self, host, timeout=None):
        self.captured["host"] = host
        return self

    def generate(self, **kwargs):
        self.captured["json"] = kwargs
        text = self._generate(kwargs) if callable(self._generate) else self._generate
        return SimpleNamespace(response=text)


class _FakeMistral:
    """Callable stand-in for llm.Mistral (the fallback client): records each chat.complete payload in
    captured["json"] and answers from canned data. `complete` may be a value or a callable(kwargs).
    """

    def __init__(self, *, complete=None):
        self._complete = complete
        self.captured = {}

    def __call__(self, *, api_key=None, timeout_ms=None):
        self.chat = SimpleNamespace(complete=self._complete_call, parse=self._parse_call)
        return self

    def _complete_call(self, **kwargs):
        self.captured["json"] = kwargs
        text = self._complete(kwargs) if callable(self._complete) else self._complete
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    def _parse_call(self, *, response_format=None, **kwargs):
        kwargs["response_format"] = response_format
        self.captured["json"] = kwargs
        text = self._complete(kwargs) if callable(self._complete) else self._complete
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _never(msg):
    # A generate handler that fails the test if the boundary it guards is ever reached.
    def _raise(kwargs):  # pragma: no cover - must never be reached
        raise AssertionError(msg)

    return _raise


def _raises(exc):
    # A handler that raises `exc` when the boundary is reached — a provider outage, or our own 4xx.
    def _raise(kwargs):
        raise exc

    return _raise


def _scaleway_outage():
    # An outage-class Scaleway failure (unreachable), the kind the ladder falls through.
    return llm.openai.APIConnectionError(request=httpx.Request("POST", "https://api.scaleway.ai/v1"))


def _scaleway_bad_request():
    # A 4xx from Scaleway — our own bad request, which must surface rather than fall through.
    request = httpx.Request("POST", "https://api.scaleway.ai/v1")
    return llm.openai.BadRequestError("bad request", response=httpx.Response(400, request=request), body=None)


def _prompt(fake):
    # The single user message's content — where the prompt lands in an OpenAI chat request.
    return fake.captured["json"]["messages"][-1]["content"]


# --- the free-text generate path -----------------------------------------------------------


def test_generate_returns_free_text_with_no_schema_grammar(monkeypatch):
    # A spoken reply is prose, not JSON: no `response_format` is sent, thinking is off, streaming is off,
    # temperature is pinned to 0 (as for every call through this boundary), the reply is held to the model's
    # output ceiling, and the model's text comes back as-is.
    fake = _FakeChat(generate="here is a warm reply")
    monkeypatch.setattr(llm, "OpenAI", fake)

    out = llm.generate("say something kind")

    assert out == "here is a warm reply"
    assert "response_format" not in fake.captured["json"]  # free prose, unconstrained by a schema
    assert fake.captured["json"]["reasoning_effort"] == "none"  # thinking off, Scaleway's documented way
    assert fake.captured["json"]["stream"] is False
    assert fake.captured["json"]["temperature"] == 0  # sampling pinned, not left to the provider's default
    assert fake.captured["json"]["max_tokens"] == llm.models.MODELS["glm-5.2"].max_output_tokens  # the reply's runaway guard
    assert fake.captured["base_url"] == llm.config.SCALEWAY_API_BASE_URL


def test_generate_raises_on_empty_response(monkeypatch):
    # An empty generation must fail loud, never reach the symbiot as silence — and an empty reply is
    # our-side, not an outage, so it raises rather than falling down the ladder.
    monkeypatch.setattr(llm, "OpenAI", _FakeChat(generate=""))

    with pytest.raises(RuntimeError):
        llm.generate("say something")


# --- the context-budget guard (_fit) -------------------------------------------------------


def test_fit_passes_a_prompt_through_untouched_when_under_budget(monkeypatch):
    # Under the model's optimal, the prompt reaches the model exactly as assembled.
    monkeypatch.setattr(llm.models, "count_tokens", lambda text: 10)  # everything is tiny
    fake = _FakeChat(generate="ok")
    monkeypatch.setattr(llm, "OpenAI", fake)

    llm.generate("instructions and [CTX] and more", context="[CTX]")

    assert _prompt(fake) == "instructions and [CTX] and more"


def test_fit_condenses_only_the_context_when_over_budget(monkeypatch):
    # Over budget, the summarisable context is replaced in place by its condensation —
    # and only the context, so the instructions around it survive verbatim.
    monkeypatch.setattr(llm.models, "count_tokens", lambda text: 10**9)  # always over budget
    monkeypatch.setattr(llm, "_summarise", lambda context, target, model: "SHORT")
    fake = _FakeChat(generate="ok")
    monkeypatch.setattr(llm, "OpenAI", fake)

    llm.generate("keep these instructions [CTX] and these too", context="[CTX]")

    assert _prompt(fake) == "keep these instructions SHORT and these too"


def test_fit_raises_when_over_budget_with_nothing_summarisable(monkeypatch):
    # A prompt over budget with no context to condense is a bug to surface, not a prompt to send blind:
    # the guard refuses rather than hand the model something it would read badly or truncate silently.
    monkeypatch.setattr(llm.models, "count_tokens", lambda text: 10**9)
    monkeypatch.setattr(llm, "OpenAI",
                        _FakeChat(generate=_never("the guard must refuse before ever reaching the model")))

    with pytest.raises(RuntimeError):
        llm.generate("a prompt too large, with no context marked")


def test_fit_leaves_an_unmapped_model_untouched(monkeypatch):
    # A model not in the map has no optimal to hold to, so its prompt passes through — the budget check
    # (and count) is never consulted. An unmapped name routes to the ladder's local Ollama tier, so this
    # fakes that boundary and reads the Ollama-shaped payload.
    monkeypatch.setattr(llm.models, "count_tokens",
                        lambda text: (_ for _ in ()).throw(AssertionError("count must not be consulted")))
    fake = _FakeOllama(generate="ok")
    monkeypatch.setattr(llm.ollama, "Client", fake)

    llm.generate("PROMPT", model="some-model-not-in-the-map")

    assert fake.captured["json"]["prompt"] == "PROMPT"


def test_summarise_asks_to_condense_and_caps_the_result(monkeypatch):
    # The summariser names its token target and carries the context to condense, then the reply is truncated
    # to that target — so the budget is a promise kept, not a request the model may overshoot.
    fake = _FakeChat(generate="CONDENSED but still rather long")
    monkeypatch.setattr(llm, "OpenAI", fake)
    monkeypatch.setattr(llm.models, "truncate_tokens", lambda text, n: f"{text[:9]}<{n}>")

    out = llm._summarise("the big context block to shrink", 42, "glm-5.2")

    assert "the big context block to shrink" in _prompt(fake)
    assert "42" in _prompt(fake)
    assert out == "CONDENSED<42>"  # the model's summary, truncated to the target
    # The summariser asks for its own target as the output cap — more room than an ordinary reply — and here
    # the target (42) is under the tier's ceiling, so it passes through as named rather than the model default.
    assert fake.captured["json"]["max_tokens"] == 42
    assert fake.captured["json"]["temperature"] == 0  # a condensation is faithful, not warm


def test_summarise_target_is_clamped_to_the_tier_ceiling(monkeypatch):
    # A target sized to the context budget can run past what a provider accepts (Scaleway 400s over its cap,
    # and a 400 does not fall through). So a target above the tier's own ceiling is clamped down to it,
    # never sent as-is — the clamp only shortens the summary, the truncation after still holds the budget.
    fake = _FakeChat(generate="CONDENSED")
    monkeypatch.setattr(llm, "OpenAI", fake)
    monkeypatch.setattr(llm.models, "truncate_tokens", lambda text, n: text)

    ceiling = llm.models.MODELS["glm-5.2"].max_output_tokens
    llm._summarise("a context far larger than the tier can emit", ceiling + 50_000, "glm-5.2")

    assert fake.captured["json"]["max_tokens"] == ceiling  # clamped to what Scaleway accepts, not the huge target


# --- the fallback ladder: Scaleway → Mistral → local Ollama, per request --------------------


def test_ladder_falls_over_to_mistral_on_a_scaleway_outage(monkeypatch):
    # Scaleway unreachable: the same request is retried against Mistral, named the fallback model,
    # and Mistral answering ends the ladder before the local tier is ever touched.
    monkeypatch.setattr(llm, "OpenAI", _FakeChat(generate=_raises(_scaleway_outage())))
    mistral = _FakeMistral(complete="mistral answered")
    monkeypatch.setattr(llm, "Mistral", mistral)
    monkeypatch.setattr(llm.ollama, "Client",
                        _FakeOllama(generate=_never("the local tier must not run when Mistral answers")))

    out = llm.generate("hello")

    assert out == "mistral answered"
    assert mistral.captured["json"]["model"] == llm.config.GENERATIVE_FALLBACK_MODEL
    # The output ceiling is resolved per tier, from the model about to answer — so the Mistral tier is held
    # to Mistral's own ceiling, not the primary's, a cap it is guaranteed to support.
    assert mistral.captured["json"]["max_tokens"] == llm.models.MODELS[llm.config.GENERATIVE_FALLBACK_MODEL].max_output_tokens


def test_ladder_falls_to_local_ollama_when_both_clouds_are_down(monkeypatch):
    # Fort Alamo: Scaleway and Mistral both fail outage-class, so the loop still answers off the box,
    # through the local fallback model.
    monkeypatch.setattr(llm, "OpenAI", _FakeChat(generate=_raises(_scaleway_outage())))
    monkeypatch.setattr(llm, "Mistral", _FakeMistral(complete=_raises(httpx.ConnectError("mistral down"))))
    local = _FakeOllama(generate="local answered")
    monkeypatch.setattr(llm.ollama, "Client", local)

    out = llm.generate("hello")

    assert out == "local answered"
    assert local.captured["json"]["model"] == llm.config.GENERATIVE_LOCAL_FALLBACK_MODEL
    # The local tier caps output through Ollama's `num_predict` (unbounded by default), resolved from the
    # local model's own ceiling — like the cloud tiers above it, each held to its own.
    assert local.captured["json"]["options"]["num_predict"] == llm.models.MODELS[llm.config.GENERATIVE_LOCAL_FALLBACK_MODEL].max_output_tokens


def test_ladder_surfaces_a_scaleway_4xx_without_falling_over(monkeypatch):
    # A 4xx is our own bad request, not an outage: it raises at once, and neither fallback tier is tried —
    # the next provider would fail identically and hide the bug.
    monkeypatch.setattr(llm, "OpenAI", _FakeChat(generate=_raises(_scaleway_bad_request())))
    monkeypatch.setattr(llm, "Mistral",
                        _FakeMistral(complete=_never("a 4xx must surface, not fall through to Mistral")))
    monkeypatch.setattr(llm.ollama, "Client",
                        _FakeOllama(generate=_never("a 4xx must surface, not fall through to the local tier")))

    with pytest.raises(llm.openai.BadRequestError):
        llm.generate("hello")
