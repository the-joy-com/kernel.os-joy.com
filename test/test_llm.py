"""The free-text generate path and the context-budget guard, with Ollama faked at the network boundary.

generate_json's own contract is exercised in test_ontology.py alongside the router that leans on it;
here we cover the two things the read path added: a prose reply with no schema grammar, and the _fit guard
that holds a prompt to the model's optimal window by condensing only its context.
"""

import pytest

from services import llm


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _capturing_post(captured: dict):
    # A fake httpx.post that records the payload it was handed and answers a canned reply.
    def post(url, json, timeout):
        captured["json"] = json
        return _FakeResponse({"response": "ok"})

    return post


# --- the free-text generate path -----------------------------------------------------------


def test_generate_returns_free_text_with_no_schema_grammar(monkeypatch):
    # A spoken reply is prose, not JSON: no `format` grammar is sent, thinking and streaming are off,
    # and the model's text comes back as-is.
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse({"response": "here is a warm reply"})

    monkeypatch.setattr(llm.httpx, "post", fake_post)

    out = llm.generate("say something kind")

    assert out == "here is a warm reply"
    assert "format" not in captured["json"]  # free prose, unconstrained by a decode-time grammar
    assert captured["json"]["think"] is False
    assert captured["json"]["stream"] is False
    assert captured["url"].endswith("/api/generate")


def test_generate_raises_on_empty_response(monkeypatch):
    # An empty generation must fail loud, never reach the symbiot as silence.
    monkeypatch.setattr(llm.httpx, "post", lambda url, json, timeout: _FakeResponse({"response": ""}))

    with pytest.raises(RuntimeError):
        llm.generate("say something")


# --- the context-budget guard (_fit) -------------------------------------------------------


def test_fit_passes_a_prompt_through_untouched_when_under_budget(monkeypatch):
    # Under the model's optimal, the prompt reaches the model exactly as assembled.
    monkeypatch.setattr(llm.models, "count_tokens", lambda text: 10)  # everything is tiny
    captured = {}
    monkeypatch.setattr(llm.httpx, "post", _capturing_post(captured))

    llm.generate("instructions and [CTX] and more", context="[CTX]")

    assert captured["json"]["prompt"] == "instructions and [CTX] and more"


def test_fit_condenses_only_the_context_when_over_budget(monkeypatch):
    # Over budget, the summarisable context is replaced in place by its condensation —
    # and only the context, so the instructions around it survive verbatim.
    monkeypatch.setattr(llm.models, "count_tokens", lambda text: 10**9)  # always over budget
    monkeypatch.setattr(llm, "_summarise", lambda context, target, model: "SHORT")
    captured = {}
    monkeypatch.setattr(llm.httpx, "post", _capturing_post(captured))

    llm.generate("keep these instructions [CTX] and these too", context="[CTX]")

    assert captured["json"]["prompt"] == "keep these instructions SHORT and these too"


def test_fit_raises_when_over_budget_with_nothing_summarisable(monkeypatch):
    # A prompt over budget with no context to condense is a bug to surface, not a prompt to send blind:
    # the guard refuses rather than hand the model something it would read badly or truncate silently.
    monkeypatch.setattr(llm.models, "count_tokens", lambda text: 10**9)

    def boom(url, json, timeout):  # pragma: no cover - must never be reached
        raise AssertionError("the guard must refuse before ever reaching the model")

    monkeypatch.setattr(llm.httpx, "post", boom)

    with pytest.raises(RuntimeError):
        llm.generate("a prompt too large, with no context marked")


def test_fit_leaves_an_unmapped_model_untouched(monkeypatch):
    # A model not in the map has no optimal to hold to, so its prompt passes through — the budget check
    # (and count) is never consulted.
    monkeypatch.setattr(llm.models, "count_tokens",
                        lambda text: (_ for _ in ()).throw(AssertionError("count must not be consulted")))
    captured = {}
    monkeypatch.setattr(llm.httpx, "post", _capturing_post(captured))

    llm.generate("PROMPT", model="some-model-not-in-the-map")

    assert captured["json"]["prompt"] == "PROMPT"


def test_summarise_asks_to_condense_and_caps_the_result(monkeypatch):
    # The summariser names its token target and carries the context to condense, then the reply is truncated
    # to that target — so the budget is a promise kept, not a request the model may overshoot.
    captured = {}

    def fake_post(url, json, timeout):
        captured["json"] = json
        return _FakeResponse({"response": "CONDENSED but still rather long"})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    monkeypatch.setattr(llm.models, "truncate_tokens", lambda text, n: f"{text[:9]}<{n}>")

    out = llm._summarise("the big context block to shrink", 42, "qwen3.5:4b")

    assert "the big context block to shrink" in captured["json"]["prompt"]
    assert "42" in captured["json"]["prompt"]
    assert out == "CONDENSED<42>"  # the model's summary, truncated to the target
