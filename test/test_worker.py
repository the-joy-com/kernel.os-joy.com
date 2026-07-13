"""The worker: a received message becomes answered, with its reply stored.

The live loop is disabled in tests (WORKER_ENABLED=0 in conftest) so it can't race
the suite for received rows; here we drive one iteration at a time by calling
_process_one directly.
"""

from datetime import datetime, timezone

from core import db
from services import conversation
from services import execution
from services import intake
from core import protocol
from services import worker

SEEDED_SYMBIOT_ID = 1  # conftest re-seeds exactly one symbiot with RESTART IDENTITY, so it's always id 1
_EMPTY_CONVO = conversation.Conversation(gist=None, tail=[])
# The symbiot's local now and zone the worker resolves before composing (fixed here so the reply is deterministic).
_NOW = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)
_ZONE = "UTC"


def _state_of(message_id):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT status, answer, failed_reason FROM intake WHERE id = %s", (message_id,)
        ).fetchone()


def _insert(message="a message", symbiot_id=None) -> int:
    with db.get_pool().connection() as conn:
        intake.record_message(conn, message, symbiot_id=symbiot_id)
        return conn.execute("SELECT max(id) FROM intake").fetchone()[0]


def test_worker_answers_a_received_message(client):
    message_id = _insert("hello")  # no symbiot_id — an anonymous line
    assert worker._process_one() is True

    status, answer, failed_reason = _state_of(message_id)
    assert status == "answered"
    assert answer == worker._produce_reply(("hello", None, [], _EMPTY_CONVO, _NOW, _ZONE))  # the produced reply, stored
    assert failed_reason is None  # a success carries no failure reason


def test_produce_reply_distinguishes_the_caller(client, monkeypatch):
    # The whole point of this rung: a recognized symbiot and an anonymous caller get different replies,
    # and it's the kernel that draws the line — the reply turns on symbiot_id, nothing the caller sends.
    # A recognized symbiot gets a real reply composed off memory (reply.compose, faked here);
    # an anonymous caller gets the stand-in, answered without the symbiot's memory.
    monkeypatch.setattr(
        worker.reply, "compose",
        lambda message, facts, conv, *, now_local=None, zone_name=None: f"composed:{message}",
    )
    assert worker._produce_reply(("hi", SEEDED_SYMBIOT_ID, [], _EMPTY_CONVO, _NOW, _ZONE)) == "composed:hi"
    assert worker._produce_reply(("hi", None, [], _EMPTY_CONVO, _NOW, _ZONE)) == protocol.STANDIN_ANSWER_ANON


def test_worker_answers_an_authed_line_as_the_symbiot(client, monkeypatch):
    # End to end through the claim: a line stamped with a symbiot is answered by composing a real reply,
    # proving symbiot_id survives record → claim → gather → produce, not just the branch in isolation.
    # The composition is faked (reply.compose), and the work is run in-process (not the killable child)
    # so that fake takes effect — the child spawn is proven separately in test_execution.
    monkeypatch.setattr(
        worker.reply, "compose",
        lambda message, facts, conv, *, now_local=None, zone_name=None: f"reply to {message!r}",
    )
    monkeypatch.setattr(
        worker.execution, "run_with_deadline",
        lambda fn, arg, deadline: execution.Result(execution.COMPLETED, fn(arg)),
    )
    message_id = _insert("who am I", symbiot_id=SEEDED_SYMBIOT_ID)
    assert worker._process_one() is True

    status, answer, _ = _state_of(message_id)
    assert status == "answered"
    assert answer == "reply to 'who am I'"


def test_worker_answers_an_anonymous_line_as_a_stranger(client):
    # The other side of the branch, end to end: a line with no symbiot gets the anonymous reply.
    message_id = _insert("who am I")  # no symbiot_id — an unauthed line
    assert worker._process_one() is True

    status, answer, _ = _state_of(message_id)
    assert status == "answered"
    assert answer == protocol.STANDIN_ANSWER_ANON


def test_worker_idle_when_nothing_waiting(client):
    assert worker._process_one() is False  # nothing to do, says so


def test_worker_takes_oldest_first(client):
    older = _insert("older")
    newer = _insert("newer")
    worker._process_one()

    assert _state_of(older)[0] == "answered"  # the older one went first
    assert _state_of(newer)[0] == "received"  # the newer one still waits


def test_worker_records_crash_traceback(client, monkeypatch):
    # A crash comes back from the child as its full traceback;
    # the worker stores it on the row, so the failure carries what broke, not just that something did.
    # (The child really producing that traceback is proven in test_execution;
    # here we feed the worker a crash Result and check it maps to a failed row with the reason kept.)
    trace = "Traceback (most recent call last):\n  ...\nValueError: work blew up"
    monkeypatch.setattr(
        worker.execution, "run_with_deadline",
        lambda *a, **k: execution.Result(execution.CRASHED, trace),
    )
    message_id = _insert("boom")
    assert worker._process_one() is True

    status, answer, failed_reason = _state_of(message_id)
    assert status == "failed"
    assert answer is None
    assert failed_reason == trace


def test_worker_records_timeout_reason(client, monkeypatch):
    # A timeout leaves no reason from the child (the work was killed), so the worker
    # supplies its own: the row records that it outran the deadline, told apart from a crash.
    monkeypatch.setattr(
        worker.execution, "run_with_deadline",
        lambda *a, **k: execution.Result(execution.TIMED_OUT, None),
    )
    message_id = _insert("hang")
    assert worker._process_one() is True

    status, answer, failed_reason = _state_of(message_id)
    assert status == "failed"
    assert answer is None
    assert failed_reason == "deadline exceeded"
