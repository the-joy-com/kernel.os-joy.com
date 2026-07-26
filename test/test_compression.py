"""The compression sweep: folding aged-out turns into the Gist, off the critical path.

The sweep's own job is eligibility, the tail/pending boundary, and the append —
not the fold's model call (conversation.fold has the LLM behind it).
So conversation.fold is stubbed to a deterministic merge that records what it was handed,
and the assertions are about which turns get folded, that the cutoff advances, and that a folded turn is never folded twice.
The verbatim budget is monkeypatched small so the arithmetic that decides "overflowed" is exact.
"""

from core import db
from services.memory import conversation
from services.loop import worker

SEEDED_SYMBIOT_ID = 1  # conftest re-seeds exactly one symbiot with RESTART IDENTITY, so it's always id 1


def _intake(message, answer=None, symbiot_id=SEEDED_SYMBIOT_ID, status="answered") -> int:
    with db.get_pool().connection() as conn:
        return conn.execute(
            "INSERT INTO intake (message, answer, symbiot_id, status) VALUES (%s, %s, %s, %s) RETURNING id",
            (message, answer, symbiot_id, status),
        ).fetchone()[0]


def _item(role, token_count, *, intake_id, symbiot_id=SEEDED_SYMBIOT_ID) -> int:
    with db.get_pool().connection() as conn:
        return conn.execute(
            "INSERT INTO conversation_item (symbiot_id, role, token_count, intake_id) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (symbiot_id, role, token_count, intake_id),
        ).fetchone()[0]


def _gists():
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT gist_text, cutoff_item_id FROM conversation_gist "
            "WHERE symbiot_id = %s ORDER BY id",
            (SEEDED_SYMBIOT_ID,),
        ).fetchall()


def _stub_fold(monkeypatch, calls):
    # Stand in for the model call: record the prior Gist and the turns handed in,
    # and return a deterministic merged paragraph, so the sweep's boundary logic is exercised, not the LLM.
    def fake(gist_text, turns, zone_name):
        calls.append((gist_text, [(t.role, t.text) for t in turns]))
        return "FOLDED SUMMARY"

    monkeypatch.setattr(worker.conversation, "fold", fake)


def _small_budget(monkeypatch, budget=90):
    monkeypatch.setattr(worker.conversation, "verbatim_budget", lambda: budget)


def _backdate(item_id, seconds_ago):
    # Age an item into the past so the idle-collapse eligibility read sees the conversation as gone quiet.
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE conversation_item SET created_at = NOW() - make_interval(secs => %s) WHERE id = %s",
            (seconds_ago, item_id),
        )


def test_compress_folds_the_overflow_into_a_new_gist_and_advances_the_cutoff(client, monkeypatch):
    _small_budget(monkeypatch)
    calls = []
    _stub_fold(monkeypatch, calls)
    intake_id = _intake("m", answer="a")
    _item("symbiot", 50, intake_id=intake_id)          # running 150 → folded
    boundary = _item("machine", 50, intake_id=intake_id)  # running 100 → folded (over 90)
    _item("symbiot", 50, intake_id=_intake("newest"))  # running 50 → stays in the verbatim tail

    assert worker._compress_one() is True
    # The fold saw no prior Gist and the two overflowed turns, oldest first.
    assert calls == [(None, [("symbiot", "m"), ("machine", "a")])]
    # One Gist row now exists, carrying the merged paragraph and the cutoff at the last folded turn.
    assert _gists() == [("FOLDED SUMMARY", boundary)]


def test_compress_is_idle_when_nothing_has_overflowed(client, monkeypatch):
    _small_budget(monkeypatch)
    calls = []
    _stub_fold(monkeypatch, calls)
    intake_id = _intake("m", answer="a")
    _item("symbiot", 50, intake_id=intake_id)  # 50 tokens total, within the 90 budget → no overflow

    assert worker._compress_one() is False
    assert calls == []
    assert _gists() == []


def test_compress_skips_a_symbiot_whose_fold_another_worker_holds(client, monkeypatch):
    # The race guard, proven end to end: while one worker holds a symbiot's fold, a second sweep
    # over the same over-budget tail must claim nothing, fold nothing, and write no duplicate Gist —
    # then, once the lock is let go, the fold goes through as normal.
    _small_budget(monkeypatch)
    calls = []
    _stub_fold(monkeypatch, calls)
    intake_id = _intake("m", answer="a")
    _item("symbiot", 50, intake_id=intake_id)
    _item("machine", 50, intake_id=intake_id)
    _item("symbiot", 50, intake_id=_intake("newest"))

    # A stand-in for the other worker: hold this symbiot's fold on its own open transaction,
    # so the advisory lock is genuinely taken by a different session while the sweep runs.
    with db.get_pool().connection() as holder:
        with holder.transaction():
            assert conversation.claim_fold(holder, SEEDED_SYMBIOT_ID) is True

            assert worker._compress_one() is False  # couldn't claim — skipped this pass
            assert calls == []                       # the metered fold never ran
            assert _gists() == []                    # and no Gist was written

    # The holder's transaction has closed, releasing the lock; the fold now goes through.
    assert worker._compress_one() is True
    assert len(calls) == 1
    assert len(_gists()) == 1


def test_compress_never_folds_the_same_turn_twice(client, monkeypatch):
    # After a fold, the folded turns fall behind the cutoff,
    # so a second pass has nothing to fold until fresh overflow arrives —
    # exactly-once falls out of the cutoff only moving forward.
    _small_budget(monkeypatch)
    calls = []
    _stub_fold(monkeypatch, calls)
    intake_id = _intake("m", answer="a")
    _item("symbiot", 50, intake_id=intake_id)
    _item("machine", 50, intake_id=intake_id)
    _item("symbiot", 50, intake_id=_intake("newest"))

    assert worker._compress_one() is True
    assert worker._compress_one() is False  # the remaining tail is within budget, nothing left to fold
    assert len(calls) == 1
    assert len(_gists()) == 1


def test_collapse_idle_folds_the_whole_tail_of_a_gone_quiet_conversation(client, monkeypatch):
    # The idle half of the shrink: a conversation quiet past COMPRESS_IDLE_SECONDS has its whole verbatim tail
    # collapsed into the Gist, even though it never grew past the size budget, so the next session opens lean.
    calls = []
    _stub_fold(monkeypatch, calls)
    intake_id = _intake("m", answer="a")
    a = _item("symbiot", 50, intake_id=intake_id)
    b = _item("machine", 50, intake_id=intake_id)
    _backdate(a, 3600)
    _backdate(b, 3600)

    assert worker._collapse_idle_one() is True
    # The whole tail folded (both turns), oldest first, and the cutoff advanced to the newest turn.
    assert calls == [(None, [("symbiot", "m"), ("machine", "a")])]
    assert _gists() == [("FOLDED SUMMARY", b)]
    # Nothing left past the cutoff, so a second idle pass has nothing to collapse.
    assert worker._collapse_idle_one() is False


def test_collapse_idle_leaves_a_live_conversation_alone(client, monkeypatch):
    # A conversation whose newest turn is recent is still live and is never collapsed, however old its earlier turns.
    calls = []
    _stub_fold(monkeypatch, calls)
    intake_id = _intake("m", answer="a")
    a = _item("symbiot", 50, intake_id=intake_id)
    _backdate(a, 3600)
    _item("machine", 50, intake_id=intake_id)  # a fresh turn — the conversation is live

    assert worker._collapse_idle_one() is False
    assert calls == []
    assert _gists() == []
