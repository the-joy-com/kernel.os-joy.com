"""Composing the reply, with the persona and the model faked.

The composer's job is to fold three things — the persona's voice, the gathered facts, and the message —
into one prompt and hand it to the free-text model path, marking the facts as the summarisable context.
So the persona load and llm.generate are faked, and the assertions are about what reaches the model:
the voice, the dated facts, the message, and which slice is passed as context.
"""

from datetime import datetime, timezone

from core import config
from services import reply
from services import retrieval


def _fact(id: int, raw_text: str, effective_at: datetime, rank: float = 0.5) -> retrieval.Fact:
    return retrieval.Fact(id=id, raw_text=raw_text, payload={}, effective_at=effective_at, rank=rank)


def test_compose_folds_persona_facts_and_message_into_the_prompt(monkeypatch):
    monkeypatch.setattr(reply.persona, "load", lambda: "MACHINE VOICE")
    captured = {}

    def fake_generate(prompt, *, model=None, context=None):
        captured.update(prompt=prompt, model=model, context=context)
        return "here is your answer"

    monkeypatch.setattr(reply.llm, "generate", fake_generate)
    facts = [
        _fact(1, "boxing with Jeremy", datetime(2026, 7, 10, tzinfo=timezone.utc), rank=0.9),
        _fact(2, "I live in Strasbourg", datetime(2026, 7, 1, tzinfo=timezone.utc), rank=0.5),
    ]

    out = reply.compose("how have I been?", facts)

    assert out == "here is your answer"
    # The persona, the message, and each fact (with its effective date) are all in the prompt.
    assert "MACHINE VOICE" in captured["prompt"]
    assert "how have I been?" in captured["prompt"]
    assert "boxing with Jeremy" in captured["prompt"] and "2026-07-10" in captured["prompt"]
    assert "I live in Strasbourg" in captured["prompt"] and "2026-07-01" in captured["prompt"]
    # The reply model, and the facts block passed as the summarisable context — verbatim, as it sits in the prompt.
    assert captured["model"] == config.REPLY_MODEL
    assert captured["context"] in captured["prompt"]
    assert "boxing with Jeremy" in captured["context"]


def test_compose_over_an_empty_diary_says_so_and_marks_nothing_summarisable(monkeypatch):
    # No facts: the diary block reads the honest no-facts line, and nothing is offered as summarisable context.
    monkeypatch.setattr(reply.persona, "load", lambda: "VOICE")
    captured = {}
    monkeypatch.setattr(
        reply.llm, "generate",
        lambda prompt, *, model=None, context=None: captured.update(prompt=prompt, context=context) or "ok",
    )

    reply.compose("hello there", [])

    assert reply._NO_FACTS in captured["prompt"]
    assert "hello there" in captured["prompt"]
    assert captured["context"] is None
