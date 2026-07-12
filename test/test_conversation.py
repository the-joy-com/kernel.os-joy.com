"""Short-term conversational memory: the stream, the unbroken tail back to the cutoff, and the fold trigger.

The read (recent) carries the whole tail newer than the Gist's cutoff with no token cap,
so Bucket 1 and Bucket 2 always touch with no gap;
the budget lives only in the fold's trigger and the amount it trims (next_symbiot_to_fold, pending_for_fold),
where the stream rows' explicit token_counts make the arithmetic exact.
record_utterance's own token counting is checked against models.count_tokens directly,
and the "exactly one source" invariant is checked at the database, where the schema — not the calling code — holds it.
"""

import psycopg
import pytest

from core import db
from services import conversation
from services import models

SEEDED_SYMBIOT_ID = 1  # conftest re-seeds exactly one symbiot with RESTART IDENTITY, so it's always id 1


def _with_conn(fn):
    # Run one call against a fresh pooled connection and commit on the way out —
    # the connection stays live for the whole call (so a .fetchone() inside it is valid),
    # then returns to the pool, the way a route or a worker takes one connection per unit of work.
    with db.get_pool().connection() as conn:
        return fn(conn)


def _intake(message, answer=None, symbiot_id=SEEDED_SYMBIOT_ID, status="answered") -> int:
    return _with_conn(lambda c: c.execute(
        "INSERT INTO intake (message, answer, symbiot_id, status) VALUES (%s, %s, %s, %s) RETURNING id",
        (message, answer, symbiot_id, status),
    ).fetchone()[0])


def _missive(body, symbiot_id=SEEDED_SYMBIOT_ID) -> int:
    return _with_conn(lambda c: c.execute(
        "INSERT INTO missive (symbiot_id, body) VALUES (%s, %s) RETURNING id",
        (symbiot_id, body),
    ).fetchone()[0])


def _item(role, token_count, *, intake_id=None, missive_id=None, symbiot_id=SEEDED_SYMBIOT_ID) -> int:
    # Insert a stream row with an explicit token_count so the read tests' arithmetic is exact.
    return _with_conn(lambda c: c.execute(
        "INSERT INTO conversation_item (symbiot_id, role, token_count, intake_id, missive_id) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (symbiot_id, role, token_count, intake_id, missive_id),
    ).fetchone()[0])


def _row(item_id):
    return _with_conn(lambda c: c.execute(
        "SELECT symbiot_id, role, token_count, intake_id, missive_id FROM conversation_item WHERE id = %s",
        (item_id,),
    ).fetchone())


# --- the stream write --------------------------------------------------------


def test_record_utterance_counts_tokens_at_write_and_stores_the_pointer(client):
    # The row carries the role, the token count (computed here with the same local counter the budget guard uses),
    # and the pointer to where the words live — but never the words themselves.
    intake_id = _intake("boxing felt good today")
    item_id = _with_conn(lambda c: conversation.record_utterance(
        c, SEEDED_SYMBIOT_ID, "symbiot", "boxing felt good today", intake_id=intake_id
    ))
    symbiot_id, role, token_count, i_id, m_id = _row(item_id)
    assert symbiot_id == SEEDED_SYMBIOT_ID
    assert role == "symbiot"
    assert token_count == models.count_tokens("boxing felt good today")
    assert (i_id, m_id) == (intake_id, None)


def test_record_utterance_rejects_zero_sources(client):
    # The "exactly one source" invariant is the schema's, not the caller's: neither pointer set is refused.
    with pytest.raises(psycopg.errors.CheckViolation):
        _with_conn(lambda c: conversation.record_utterance(c, SEEDED_SYMBIOT_ID, "symbiot", "orphan words"))


def test_record_utterance_rejects_two_sources(client):
    # ...and so are both pointers set at once — a row must resolve to one place, never two.
    intake_id = _intake("a line")
    missive_id = _missive("a nudge")
    with pytest.raises(psycopg.errors.CheckViolation):
        _with_conn(lambda c: conversation.record_utterance(
            c, SEEDED_SYMBIOT_ID, "machine", "a line", intake_id=intake_id, missive_id=missive_id
        ))


# --- the verbatim tail: recent() --------------------------------------------


def test_recent_resolves_text_both_directions_and_from_missives(client):
    # The tail resolves each item's words through its pointer: intake.message for the symbiot side,
    # intake.answer for the machine side, and a missive's body for a machine-initiated line —
    # returned in chronological order, each tagged with who spoke.
    intake_id = _intake("what did I do?", answer="you boxed")
    missive_id = _missive("time to rest")
    _item("symbiot", 5, intake_id=intake_id)
    _item("machine", 5, intake_id=intake_id)
    _item("machine", 5, missive_id=missive_id)

    conv = _with_conn(lambda c: conversation.recent(c, SEEDED_SYMBIOT_ID))

    assert conv.gist is None
    assert [(t.role, t.text) for t in conv.tail] == [
        ("symbiot", "what did I do?"),
        ("machine", "you boxed"),
        ("machine", "time to rest"),
    ]


def test_recent_returns_the_whole_tail_back_to_the_cutoff_with_no_token_cap(client):
    # The state-consistency guarantee: the read carries EVERY turn newer than the cutoff, however many tokens that is — no truncation.
    # So Bucket 1 (this tail) and Bucket 2 (the Gist, ≤ cutoff) meet at the cutoff with no gap,
    # even when the tail is far larger than the fold's size budget (a lagging sweep only makes the tail fatter here, never blind).
    # Token counts dwarf any realistic budget:
    intake_id = _intake("m1", answer="a1")
    _item("symbiot", 100000, intake_id=intake_id)
    _item("machine", 100000, intake_id=intake_id)
    _item("symbiot", 100000, intake_id=_intake("m2"))

    conv = _with_conn(lambda c: conversation.recent(c, SEEDED_SYMBIOT_ID))

    assert [t.text for t in conv.tail] == ["m1", "a1", "m2"]  # all of it, in order — nothing left behind


def test_recent_ignores_what_the_gist_already_covers(client):
    # The tail is only the turns newer than the current Gist's cutoff;
    # everything folded already is the Gist's job, and the Gist text rides back alongside the tail.
    intake_id = _intake("old", answer="old-a")
    covered = _item("symbiot", 5, intake_id=intake_id)
    _item("machine", 5, intake_id=intake_id)
    _with_conn(lambda c: conversation.record_gist(
        c, SEEDED_SYMBIOT_ID, "everything up to the first turn, summarised", covered
    ))

    conv = _with_conn(lambda c: conversation.recent(c, SEEDED_SYMBIOT_ID))

    assert conv.gist == "everything up to the first turn, summarised"
    assert [t.text for t in conv.tail] == ["old-a"]  # only the turn newer than the cutoff


def test_recent_excludes_the_message_being_answered(client):
    # The current message was written onto the stream when it arrived;
    # excluding its intake id keeps it from showing up both as the last tail turn and as the "current message" the prompt states.
    prior = _intake("earlier question", answer="earlier answer")
    _item("symbiot", 5, intake_id=prior)
    _item("machine", 5, intake_id=prior)
    current = _intake("the message being answered now")
    _item("symbiot", 5, intake_id=current)

    conv = _with_conn(lambda c: conversation.recent(c, SEEDED_SYMBIOT_ID, exclude_intake_id=current))

    assert [t.text for t in conv.tail] == ["earlier question", "earlier answer"]


def test_recent_over_an_empty_stream_is_honestly_empty(client):
    conv = _with_conn(lambda c: conversation.recent(c, SEEDED_SYMBIOT_ID))
    assert conv.gist is None
    assert conv.tail == []


# --- the Gist and the fold's reads ------------------------------------------


def test_current_gist_is_the_newest_appended_row(client):
    # The append-only table is never overwritten, so "current" is the highest id — the last fold.
    intake_id = _intake("x", answer="y")
    first = _item("symbiot", 5, intake_id=intake_id)
    second = _item("machine", 5, intake_id=intake_id)
    _with_conn(lambda c: conversation.record_gist(c, SEEDED_SYMBIOT_ID, "first summary", first))
    _with_conn(lambda c: conversation.record_gist(c, SEEDED_SYMBIOT_ID, "second summary", second))

    assert _with_conn(lambda c: conversation.current_gist(c, SEEDED_SYMBIOT_ID)) == ("second summary", second)


def test_pending_for_fold_returns_the_overflow_oldest_first_and_the_new_cutoff(client):
    # The mirror of the tail read: the turns PAST the budget (older than the verbatim tail),
    # oldest first, with the id of the last one — the cutoff the new Gist row will carry.
    # Tail and pending partition the newer-than-cutoff set exactly, so a folded turn is never also carried verbatim.
    intake_id = _intake("m", answer="a")
    _item("symbiot", 50, intake_id=intake_id)              # running 150 → pending
    o2 = _item("machine", 50, intake_id=intake_id)         # running 100 → pending (over budget 90)
    _item("symbiot", 50, intake_id=_intake("newest"))      # running 50 → verbatim tail, not folded

    turns, new_cutoff = _with_conn(lambda c: conversation.pending_for_fold(c, SEEDED_SYMBIOT_ID, 90, 0))

    assert [t.text for t in turns] == ["m", "a"]  # oldest first, resolved through the pointer
    assert new_cutoff == o2  # the last folded item — the boundary the tail read will honour


def test_next_symbiot_to_fold_finds_overflow_and_is_silent_within_budget(client):
    intake_id = _intake("m", answer="a")
    _item("symbiot", 50, intake_id=intake_id)
    _item("machine", 50, intake_id=intake_id)

    # 120 tokens on the stream, newer than a zero cutoff: over a budget of 90, so there is a fold to make.
    assert _with_conn(lambda c: conversation.next_symbiot_to_fold(c, 90)) == SEEDED_SYMBIOT_ID
    # ...but under a budget of 200 the whole stream fits the verbatim tail, so nothing has overflowed.
    assert _with_conn(lambda c: conversation.next_symbiot_to_fold(c, 200)) is None


def test_next_symbiot_to_fold_measures_only_past_the_cutoff(client):
    # Once turns are folded (a Gist with a cutoff), they no longer count toward "is there overflow":
    # only the turns newer than the cutoff are weighed, so a symbiot fully caught up has no work.
    intake_id = _intake("m", answer="a")
    _item("symbiot", 50, intake_id=intake_id)
    o2 = _item("machine", 50, intake_id=intake_id)
    _with_conn(lambda c: conversation.record_gist(c, SEEDED_SYMBIOT_ID, "folded both", o2))

    assert _with_conn(lambda c: conversation.next_symbiot_to_fold(c, 90)) is None  # nothing newer than the cutoff


# --- the fold's LLM boundary -------------------------------------------------


def test_fold_crosses_the_boundary_as_a_schema_not_free_text(monkeypatch):
    # The isolation guarantee: the fold hands a mandatory _FoldReply schema to generate_json,
    # so the summary is the model's only emittable field and nothing else can reach the Gist.
    # A recursive store — each Gist seeds the next fold — makes this structural, not a prompt plea.
    seen = {}

    def _fake_generate_json(prompt, schema, *, model=None, context=None):
        seen["schema"] = schema
        seen["model"] = model
        return schema(summary="the merged paragraph")

    monkeypatch.setattr(conversation.llm, "generate_json", _fake_generate_json)
    # The free-text path is gone: if fold reaches for it, the test fails loudly rather than silently yapping.
    def _no_free_text(*a, **k):
        raise AssertionError("fold must not use the free-text generate — meta-commentary could bleed into the Gist")
    monkeypatch.setattr(conversation.llm, "generate", _no_free_text)

    out = conversation.fold("summary so far", [conversation.Turn(role="symbiot", text="hello")])

    assert out == "the merged paragraph"
    assert seen["schema"] is conversation._FoldReply
    assert seen["model"] == conversation.config.CONVERSATION_COMPRESS_MODEL


def test_fold_reply_rejects_an_empty_summary():
    # min_length=1 is the boundary re-check: a fold always has turns to summarise,
    # so an empty summary is a mis-read the model class refuses rather than filing a blank Gist.
    with pytest.raises(ValueError):
        conversation._FoldReply(summary="")
