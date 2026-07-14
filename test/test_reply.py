"""Composing the reply, with the persona and the model faked.

The composer's job is to fold four things — the persona's voice, the diary facts (long-term memory),
the recent conversation (short-term memory, the Gist then the verbatim tail), and the message —
into one prompt and hand it to the free-text model path.
Two of those are sacred and never marked summarisable — the persona and the live message;
everything the reply remembers (diary + Gist + tail) is assembled into the one compressible context block.
So the persona load and llm.generate are faked,
and the assertions are about what reaches the model: the voice, the dated facts, the conversation, the message,
and that the summarisable region is exactly the remembered block — never the persona or the message.
"""

from datetime import datetime, timezone

from core import config
from services.memory import conversation
from services.loop import reply
from services.memory import retrieval


def _fact(id: int, raw_text: str, effective_at: datetime, rank: float = 0.5) -> retrieval.Fact:
    return retrieval.Fact(id=id, raw_text=raw_text, payload={}, effective_at=effective_at, rank=rank)


def _convo(gist=None, tail=()) -> conversation.Conversation:
    return conversation.Conversation(gist=gist, tail=list(tail))


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

    out = reply.compose("how have I been?", facts, _convo())

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


def test_compose_orders_diary_facts_oldest_first_regardless_of_relevance(monkeypatch):
    # The librarian hands facts most-relevant-first; the prompt must render them in time order instead,
    # so the model reads position as chronology rather than mistaking relevance rank for recency.
    monkeypatch.setattr(reply.persona, "load", lambda: "VOICE")
    captured = {}
    monkeypatch.setattr(
        reply.llm, "generate",
        lambda prompt, *, model=None, context=None: captured.update(prompt=prompt) or "ok",
    )
    facts = [  # relevance order: the newer fact ranked higher, so it arrives first
        _fact(1, "the newer thing", datetime(2026, 7, 10, tzinfo=timezone.utc), rank=0.9),
        _fact(2, "the older thing", datetime(2026, 7, 1, tzinfo=timezone.utc), rank=0.4),
    ]

    reply.compose("what happened?", facts, _convo())

    prompt = captured["prompt"]
    assert prompt.index("the older thing") < prompt.index("the newer thing")  # oldest first, not most-relevant first


def test_compose_folds_short_term_memory_gist_then_verbatim_tail(monkeypatch):
    # Short-term memory reaches the prompt as the Gist followed by the role-tagged verbatim tail,
    # and it is part of the one compressible context block — diary and conversation alike.
    monkeypatch.setattr(reply.persona, "load", lambda: "VOICE")
    captured = {}
    monkeypatch.setattr(
        reply.llm, "generate",
        lambda prompt, *, model=None, context=None: captured.update(prompt=prompt, context=context) or "ok",
    )
    conv = _convo(
        gist="Earlier they told me about two projects.",
        tail=[
            conversation.Turn(role="symbiot", text="show me the projects", created_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)),
            conversation.Turn(role="machine", text="here are Alpha and Beta", created_at=datetime(2026, 7, 1, 12, 1, tzinfo=timezone.utc)),
        ],
    )
    facts = [_fact(1, "a fact", datetime(2026, 7, 1, tzinfo=timezone.utc))]

    reply.compose("and the second one?", facts, conv)

    prompt = captured["prompt"]
    assert "Earlier they told me about two projects." in prompt
    assert "show me the projects" in prompt and "here are Alpha and Beta" in prompt
    # The gist precedes the verbatim tail, which precedes the current message.
    assert prompt.index("Earlier they told me") < prompt.index("show me the projects")
    assert prompt.index("here are Alpha and Beta") < prompt.index("and the second one?")
    # Each turn is tagged with who spoke.
    assert f"{conversation._speaker('symbiot')}: show me the projects" in prompt
    assert f"{conversation._speaker('machine')}: here are Alpha and Beta" in prompt
    # The whole remembered block is the compressible context — the conversation belongs to it too now.
    assert "here are Alpha and Beta" in captured["context"]
    assert "a fact" in captured["context"]


def test_compose_marks_the_whole_memory_summarisable_and_never_the_persona_or_message(monkeypatch):
    # The uniform rule: everything remembered (diary + Gist + tail) is the one compressible region;
    # the persona and the live message are sacred and never reach the summarisable context.
    monkeypatch.setattr(reply.persona, "load", lambda: "SACRED PERSONA VOICE")
    captured = {}
    monkeypatch.setattr(
        reply.llm, "generate",
        lambda prompt, *, model=None, context=None: captured.update(prompt=prompt, context=context) or "ok",
    )
    conv = _convo(
        gist="a summarised backstory",
        tail=[conversation.Turn(role="symbiot", text="a recent verbatim turn", created_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc))],
    )
    facts = [_fact(1, "a dated fact", datetime(2026, 7, 1, tzinfo=timezone.utc))]

    reply.compose("THE LIVE MESSAGE", facts, conv)

    context = captured["context"]
    # Everything remembered is inside the compressible block...
    assert "a dated fact" in context
    assert "a summarised backstory" in context
    assert "a recent verbatim turn" in context
    # ...and the two sacred parts never are.
    assert "SACRED PERSONA VOICE" not in context
    assert "THE LIVE MESSAGE" not in context
    # The block sits verbatim in the prompt, so the guard can splice a condensed version back in its place.
    assert context in captured["prompt"]


def test_compose_states_the_symbiot_local_time_when_the_zone_is_known(monkeypatch):
    # The fix for the UTC-perception bug: when the worker hands compose the symbiot's local now and zone,
    # the prompt states that local time so the reply reasons about time in the human's day, not the server's UTC.
    monkeypatch.setattr(reply.persona, "load", lambda: "VOICE")
    captured = {}
    monkeypatch.setattr(
        reply.llm, "generate",
        lambda prompt, *, model=None, context=None: captured.update(prompt=prompt, context=context) or "ok",
    )
    now_local = datetime(2026, 7, 13, 18, 30, tzinfo=timezone.utc)

    reply.compose("what time is it?", [], _convo(), now_local=now_local, zone_name="Asia/Tokyo")

    prompt = captured["prompt"]
    # The local date, the hour, and the zone are all stated for the model to reason against.
    assert "Monday 13 July 2026, 18:30" in prompt
    assert "Asia/Tokyo" in prompt
    # The time reference is a sacred one-liner, never folded into the compressible memory block.
    assert "18:30" not in captured["context"]


def test_compose_stamps_each_recent_turn_with_its_local_time(monkeypatch):
    # Every tail turn carries the local time it was said, so the model can order things said the same day
    # instead of guessing from how they read — and the stamp is in the symbiot's zone, not the server's UTC.
    # The turn was said at 18:30 UTC, which in Tokyo (+9) is 03:30 the next day — so the weekday flips too,
    # which is exactly why the stamp carries the weekday and not a bare clock.
    monkeypatch.setattr(reply.persona, "load", lambda: "VOICE")
    captured = {}
    monkeypatch.setattr(
        reply.llm, "generate",
        lambda prompt, *, model=None, context=None: captured.update(prompt=prompt) or "ok",
    )
    conv = _convo(tail=[
        conversation.Turn(
            role="symbiot", text="waiting at the laundromat",
            created_at=datetime(2026, 7, 13, 18, 30, tzinfo=timezone.utc),  # a Monday in UTC
        ),
    ])

    reply.compose("done yet?", [], conv, now_local=datetime(2026, 7, 14, 3, 40, tzinfo=timezone.utc), zone_name="Asia/Tokyo")

    prompt = captured["prompt"]
    assert "[Tue 03:30] The human symbiot: waiting at the laundromat" in prompt  # the local stamp
    assert "Mon 18:30" not in prompt  # never the UTC reading


def test_compose_omits_the_time_line_when_the_zone_is_unknown(monkeypatch):
    # No zone handed in (an anon stand-in never reaches here, a by-hand call that names no clock):
    # the prompt asserts no time at all rather than a wrong one — the honest silence over a fabricated hour.
    monkeypatch.setattr(reply.persona, "load", lambda: "VOICE")
    captured = {}
    monkeypatch.setattr(
        reply.llm, "generate",
        lambda prompt, *, model=None, context=None: captured.update(prompt=prompt) or "ok",
    )

    reply.compose("hello", [], _convo())

    assert "local date and time" not in captured["prompt"]


def test_compose_over_an_empty_diary_and_no_conversation_still_composes_over_the_honest_empty(monkeypatch):
    # No facts and no conversation yet:
    # the memory block reads its honest empty lines and is still the compressible region (tiny, so the guard never fires) —
    # the persona and the message stay out of it.
    monkeypatch.setattr(reply.persona, "load", lambda: "VOICE")
    captured = {}
    monkeypatch.setattr(
        reply.llm, "generate",
        lambda prompt, *, model=None, context=None: captured.update(prompt=prompt, context=context) or "ok",
    )

    reply.compose("hello there", [], _convo())

    assert reply._NO_FACTS in captured["prompt"]
    assert conversation._NO_GIST in captured["prompt"]
    assert conversation._NO_TAIL in captured["prompt"]
    assert "hello there" in captured["prompt"]
    # The memory block is the context even when empty; the honest-empty lines are what it carries.
    assert reply._NO_FACTS in captured["context"]
    assert "hello there" not in captured["context"]
