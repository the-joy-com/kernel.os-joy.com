"""The worker: a received message becomes answered, with its reply stored.

The live loop is disabled in tests (WORKER_ENABLED=0 in conftest) so it can't race
the suite for received rows; here we drive one iteration at a time by calling
_process_one directly.
"""

import db
import execution
import intake
import worker


def _state_of(message_id):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT status, answer, failed_reason FROM intake WHERE id = %s", (message_id,)
        ).fetchone()


def _insert(message="a message") -> int:
    with db.get_pool().connection() as conn:
        intake.record_message(conn, message)
        return conn.execute("SELECT max(id) FROM intake").fetchone()[0]


def test_worker_answers_a_received_message(client):
    message_id = _insert("hello")
    assert worker._process_one() is True

    status, answer, failed_reason = _state_of(message_id)
    assert status == "answered"
    assert answer == worker._produce_reply("hello")  # the produced reply, stored
    assert failed_reason is None  # a success carries no failure reason


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
