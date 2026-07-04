"""The worker: the loop that turns received messages into answered ones.

Several run at once as a small pool, each in its own background thread for the kernel's life —
pulling the oldest waiting message, producing a reply, recording it, again.
A pool, not a single worker, so one slow or wedged message can't block every message behind it;
the others keep draining.
claim_next is race-safe (FOR UPDATE SKIP LOCKED), so two workers never grab the same message.
The reply is computed in a killable child process (execution.run_with_deadline),
so a worker is never itself pinned by work that hangs —
the work is killed at the deadline and the message failed, and the worker moves on.
The reply it produces is a placeholder: the kernel has no real work to perform on a
message yet, so _produce_reply stands in for that answer until that work exists.

A message is claimed in its own transaction, so the moment work begins the row is
durably 'working'; the reply is written in a second transaction once it exists.
The loop guards itself against a failing iteration so one bad message can't take the
whole thread down with it.

Alongside the pool runs one reconcile sweep (run_reconcile_sweep) — the referee.
Where a worker turns received into answered, the sweep settles the rows a worker can't:
it fails an over-age 'working' row (a hang or dead loop),
re-queues a 'failed' row that still has attempts left,
and parks one that has spent them in 'abandoned'.
So every message reaches a terminal outcome — answered or abandoned —
retried a bounded number of times along the way, and never left waiting on an answer that will never come.
"""

import threading

import config
import db
import execution
import intake
import logs
import protocol
import push

# How long the loop waits before looking again once it finds nothing to do.
POLL_INTERVAL_SECONDS = 1.0

# How often the reconcile sweep runs. This is only the cadence of the check, not the
# deadline itself — the ceiling is config.INTAKE_DEADLINE_SECONDS. Fine-grained relative
# to the ceiling, so an overdue message is ruled failed, and a failed one retried or
# abandoned, promptly rather than lingering most of a sweep interval.
SWEEP_INTERVAL_SECONDS = 5.0


def _produce_reply(message: str) -> str:
    """The reply to a message.

    A placeholder: the kernel has nothing real to compute on a message yet,
    so this stands in for the answer until that work exists.
    """
    return protocol.STANDIN_ANSWER


def _process_one() -> bool:
    """Claim the oldest waiting message, produce its reply under a deadline, record it.

    Returns True if there was a message to process, False if none was waiting.
    The reply is computed in a killable child process (execution.run_with_deadline),
    so work that hangs or dead-loops is killed at the deadline instead of pinning this worker:
    a completed run is marked answered, a timeout or a crash is marked failed.
    An answer, being terminal, also nudges the message's push subscription (if it registered one).
    The claim and the outcome are separate transactions:
    a claimed message is durably 'working' before any work is attempted,
    so a crash mid-work leaves a visible 'working' row (which the deadline sweep will later fail)
    rather than a half-written answer.
    """
    pool = db.get_pool()
    with pool.connection() as conn:
        claimed = intake.claim_next(conn)
    if claimed is None:
        return False
    message_id, message = claimed
    result = execution.run_with_deadline(
        _produce_reply, message, config.INTAKE_DEADLINE_SECONDS
    )
    answered = False
    with pool.connection() as conn:
        if result.status == execution.COMPLETED:
            answered = intake.mark_answered(conn, message_id, result.value)
        elif result.status == execution.TIMED_OUT:
            # The work outran the deadline and was killed; the child left no reason behind.
            intake.mark_failed(conn, message_id, "deadline exceeded")
        else:
            # A crash: result.value is the child's full traceback, recorded as the reason.
            intake.mark_failed(conn, message_id, result.value)
    # Only on a move this call actually made (mark_answered is False if the sweep failed the row first):
    # 'answered' is terminal, so nudge whoever asked to be told.
    # Outside the transaction above — a slow push service must never hold a connection.
    if answered:
        push.notify(pool, message_id)
    return True


def run(stop: threading.Event) -> None:
    """Process messages until `stop` is set. Started from the kernel's lifespan.

    A processed message means looking again immediately, draining a backlog fast;
    an empty pass waits a beat so an idle kernel isn't spinning on the database.
    """
    log = logs.get("worker")
    log.info("worker started")
    while not stop.is_set():
        try:
            worked = _process_one()
        except Exception:
            # Keep the loop alive across a bad iteration so one message can't kill it.
            log.exception("worker iteration failed")
            worked = False
        if not worked:
            stop.wait(POLL_INTERVAL_SECONDS)
    log.info("worker stopped")


def run_reconcile_sweep(stop: threading.Event) -> None:
    """Reconcile the messages no worker is actively holding, until `stop` is set.

    The referee to the workers' players: one sweeper wakes on a fixed cadence and, each
    pass, settles the rows a worker can't settle itself —
      * a message stuck in working past config.INTAKE_DEADLINE_SECONDS is failed, so a
        hang or dead loop can't leave it waiting on an answer forever;
      * a failed message with attempts left is re-queued for another try;
      * a failed message that has spent its attempts is parked in abandoned.
    So a failure is retried a bounded number of times and then given a terminal verdict,
    without a worker having to stay alive across the wait between tries. An abandonment,
    being terminal, also nudges the message's push subscription (if it registered one).
    Every move is guarded in the database (each only touches rows in the state it expects),
    so the sweep never fights a worker finishing legitimately. Its own bad iteration can't
    take the loop down.
    """
    log = logs.get("worker")
    log.info("reconcile sweep started")
    while not stop.is_set():
        try:
            with db.get_pool().connection() as conn:
                failed = intake.fail_overdue(conn, config.INTAKE_DEADLINE_SECONDS)
                requeued = intake.requeue_failed(conn, config.MAX_INTAKE_ATTEMPTS)
                abandoned = intake.abandon_exhausted(conn, config.MAX_INTAKE_ATTEMPTS)
            if failed:
                log.warning("reconcile: failed %d overdue message(s)", failed)
            if requeued:
                log.info("reconcile: re-queued %d failed message(s) for retry", requeued)
            if abandoned:
                log.warning("reconcile: abandoned %d message(s) out of attempts", len(abandoned))
            # 'abandoned' is terminal: nudge each one's subscription, if it has one, that the kernel gave up.
            # Outside the transaction above, so a slow push can't hold a connection;
            # a failed nudge is swallowed and never breaks the sweep.
            for message_id in abandoned:
                push.notify(db.get_pool(), message_id)
        except Exception:
            log.exception("reconcile sweep iteration failed")
        stop.wait(SWEEP_INTERVAL_SECONDS)
    log.info("reconcile sweep stopped")
