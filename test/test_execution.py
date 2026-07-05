"""execution.run_with_deadline: work runs in a killable child, bounded by a deadline.

These are the proof of the inner timeout — the layer that frees the *worker* — so they
exercise the three real outcomes: a normal return, a hang that must be killed rather than
waited out, and a crash that comes back as its full traceback. The child functions are
module-level so the spawned process can import them by name.
"""

import time

from services import execution


def _ok(message):
    return f"reply to {message}"


def _hang(message):
    # Far longer than any test deadline: the child must be killed, never waited out.
    time.sleep(30)
    return "should never be returned"


def _boom(message):
    raise ValueError("work blew up")


def test_completed_returns_the_reply():
    result = execution.run_with_deadline(_ok, "hi", 5)
    assert result.status == execution.COMPLETED
    assert result.value == "reply to hi"


def test_timed_out_kills_a_hang_near_the_deadline():
    started = time.monotonic()
    result = execution.run_with_deadline(_hang, "hi", 0.3)
    elapsed = time.monotonic() - started
    assert result.status == execution.TIMED_OUT
    assert result.value is None
    # Returned near the deadline (plus kill grace), not after the child's 30s sleep —
    # proof the hang was killed, not merely abandoned to run on.
    assert elapsed < 5


def test_crashed_reports_the_full_traceback():
    result = execution.run_with_deadline(_boom, "hi", 5)
    assert result.status == execution.CRASHED
    # Not just the exception line — the whole traceback,
    # so a later reader (or a self-healing pass) can see where it broke, not only that it did.
    assert "Traceback" in result.value
    assert "ValueError" in result.value
    assert "work blew up" in result.value
    assert "_boom" in result.value  # the failing frame is named
