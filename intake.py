"""Intake: a received message and the path it walks to an answer.

The first half of the answer guarantee is durability at the point of receipt —
a message is recorded *before* /intake returns "roger",
so a crash a microsecond after the acknowledgement can't lose what was promised kept.

From there a message walks toward one of two ends.
The happy path is received → working → answered.
A failing attempt lands in 'failed', which is not the end of the road:
while it has attempts left it is re-queued (failed → received) for another try,
and only once its budget is spent is it parked in 'abandoned' — the terminal give-up.
So the two resting states are 'answered' and 'abandoned', with 'failed' a way-station between tries.
Each move is guarded — the UPDATE names the state it's allowed to move *from* —
so a transition that doesn't fit the message's current state simply doesn't happen,
and "one row, one outcome" is a guarantee the row layer enforces rather than one the calling order has to be careful to preserve.
Should the kernel die while a message is 'working', a restart doesn't orphan it:
recover_orphaned sends it back to 'received' for a fresh claim,
because a process that fell over mid-flight is not the same as work that failed —
only the latter has a failure to record.
A terminal outcome carries one mark past its status: delivered_at,
set once the shell confirms it showed the answer (or the abandonment) to the symbiot.
'answered' means the reply exists and is durable; delivered_at means it actually reached the human —
the reply's honest counterpart, on the way back, to the outbox's COPY, never a hopeful guess.
It rides orthogonal to status, the way a missive's seen_at does,
so the resting states stay 'answered' and 'abandoned' and delivery is a separate fact about them.
The worker (worker.py) drives a message along this path;
this module owns the durable write and the legal moves it makes, not the deciding.

One row per request — the message, never the lines inside it.
The kernel stores what it can honestly stand behind, nothing it would have to guess.

Every row here is the symbiot's, walking the path above.
A message the kernel raises *for* a symbiot — a nudge, a line relayed from the World —
is a different thing with a different shape (no question, no walk), so it lives in its own
table and module (missive.py), not folded into this one.
"""

# The reason recorded when the deadline sweep — not a worker — fails a row.
# A worker that fails a message writes a specific reason (a traceback, or that it outran the deadline);
# the sweep only knows a row overstayed its ceiling with no worker left to speak for it,
# so it says exactly that, and the two are told apart by their reason.
SWEEP_REASON = "deadline exceeded (swept: no worker reported an outcome)"

# The reason recorded when restart recovery abandons a message rather than re-queuing it:
# it was caught mid-work by a kernel that died, re-run as far as its budget allowed, and never once completed —
# so there is no traceback to keep, only this.
# Written anyway because an abandoned row must always say why,
# the same invariant a swept or worker failure keeps.
ORPHAN_ABANDON_REASON = (
    "abandoned in restart recovery (caught mid-work, retry budget spent, no attempt completed)"
)


# The functions below are ordered by the message's lifecycle, not alphabetically:
# record_message and read_outcome first (the write at receipt and the read back off it),
# then the state transitions in the order a message walks them —
# claim, answer, fail, sweep, requeue, abandon, deliver —
# and last, off the per-message path, recover_orphaned:
# the once-at-startup reconcile of rows a dead process left mid-work.
# The order mirrors the path the module docstring narrates,
# so reading top to bottom is reading a message's life start to finish.
def record_message(
    conn,
    message: str,
    reply_channel_id: int | None = None,
    symbiot_id: int | None = None,
) -> int:
    """Persist a received message, status 'received', and return its id once it's durable.

    Called inside the /intake request's transaction,
    so the row is committed in lockstep with the "roger" the handler returns —
    the acknowledgement and the durable write stand or fall together.
    status, answer, created_at and updated_at all take their defaults;
    nothing here moves the message forward, that's the worker's job.
    The id it returns is the handle the shell keeps: the message crossed the wire as a batch with no identity of its own,
    so this is the one token by which the shell can later ask what became of it (see read_outcome and the /answers route).
    reply_channel_id, when given, is the channel to notify once this message reaches a terminal outcome —
    the shell passes the id it got from /push/subscribe (its reply channel), so the kernel knows where to send the nudge.
    None (the default) means no one asked to be told: the answer still stands to be read on next open, there's just no nudge.
    symbiot_id is who sent the line, as the kernel read it from the request's session — None when there's no live session.
    It's stamped here, at the one moment identity is in hand, so the worker (which has no request) can answer by it later.
    reply_channel_id is resolved through a subquery rather than inserted raw:
    a client can carry a stale id (its channel was pruned, or the database was reset out from under a browser that still remembers one),
    and a raw insert of a dangling foreign key would raise, turning an accepted-by-design line into a 500.
    The subquery collapses an id that names no live channel to NULL — the same outcome as ON DELETE SET NULL —
    so a stale channel costs the message its nudge, never its acceptance.
    """
    row = conn.execute(
        "INSERT INTO intake (message, reply_channel_id, symbiot_id) "
        "VALUES (%s, (SELECT id FROM reply_channel WHERE id = %s), %s) RETURNING id",
        (message, reply_channel_id, symbiot_id),
    ).fetchone()
    return row[0]


def read_outcome(conn, message_id: int) -> tuple[str, str | None] | None:
    """Read where a message has got to: its status and its answer, if it has one yet.

    Returns (status, answer), or None when no message carries that id.
    This is the read behind the /answers route — the other end of record_message:
    the shell holds the id it was handed at intake and asks here whether the kernel has finished.
    It returns the raw status word and lets the route decide what of that is fit to cross the wire —
    the answer is returned, a failure's stored traceback never is,
    since that's the kernel's own diagnostic and not the symbiot's to read.
    A pure read: it takes no lock and moves nothing, so it never contends with a worker.
    """
    row = conn.execute(
        "SELECT status, answer FROM intake WHERE id = %s",
        (message_id,),
    ).fetchone()
    return (row[0], row[1]) if row else None


def claim_next(conn) -> tuple[int, str, int | None] | None:
    """Claim the oldest waiting message for work: received → working, atomically.

    Returns (id, message, symbiot_id) for the message claimed, or None when none is waiting.
    symbiot_id rides along because answering is the worker's job and the reply turns on who sent the line —
    it was stamped at intake (the one moment identity was in hand), and the claim hands it to the worker.
    The oldest received row is selected and flipped in a single statement, under
    FOR UPDATE SKIP LOCKED, so two workers can never claim the same message —
    a second worker skips the locked row and takes the next one instead.
    updated_at moves with the claim, so the clock starts the moment work begins.
    attempts is bumped here, on the claim, so it counts every try — a retry of a failed
    message is a fresh claim, and the count is the budget the retry logic spends.
    """
    row = conn.execute(
        "UPDATE intake SET status = 'working', attempts = attempts + 1, updated_at = now() "
        "WHERE id = ("
        "SELECT id FROM intake WHERE status = 'received' "
        "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
        ") "
        "RETURNING id, message, symbiot_id"
    ).fetchone()
    return (row[0], row[1], row[2]) if row else None


def mark_answered(conn, message_id: int, answer: str) -> bool:
    """Finish a message: working → answered, storing its reply.

    True only if this call made the move.
    Reachable only from working — the guard in the WHERE clause —
    so a message can't be answered before it's claimed, nor answered twice.
    """
    cursor = conn.execute(
        "UPDATE intake SET status = 'answered', answer = %s, updated_at = now() "
        "WHERE id = %s AND status = 'working'",
        (answer, message_id),
    )
    return cursor.rowcount == 1


def mark_failed(conn, message_id: int, reason: str) -> bool:
    """Give up on a message: working → failed, recording why. True only if this call moved it.

    Reachable only from working, the same guard as answering —
    a message ends working in exactly one direction, never both.
    The reason is stored so no failure is silent: a crash records its full traceback,
    a timeout records that it outran the deadline —
    enough for a later pass to see what broke rather than only that something did.
    """
    cursor = conn.execute(
        "UPDATE intake SET status = 'failed', failed_reason = %s, updated_at = now() "
        "WHERE id = %s AND status = 'working'",
        (reason, message_id),
    )
    return cursor.rowcount == 1


def fail_overdue(conn, ceiling_seconds: float) -> int:
    """Fail every message stuck in working past the ceiling: working → failed in bulk.

    Returns how many rows were failed.
    "How long in working" is now() - updated_at — the clock step two wired in —
    so a row is overdue once it has sat in working longer than ceiling_seconds.
    Guarded on status = 'working' exactly like a single fail,
    so this races cleanly with a worker finishing the same row at the same instant:
    whichever UPDATE commits first changes the state, the other then matches nothing — one row, one outcome, always.
    This gives a hung or dead-looping message a verdict;
    it does not stop the worker still holding it — that thread runs on until it returns, harmlessly,
    since its later mark_answered will find the row no longer 'working' and change nothing.
    Every row it fails records SWEEP_REASON,
    so a swept failure is never silent and is distinguishable from one a worker reported.
    """
    cursor = conn.execute(
        "UPDATE intake SET status = 'failed', failed_reason = %s, updated_at = now() "
        "WHERE status = 'working' AND updated_at < now() - make_interval(secs => %s)",
        (SWEEP_REASON, ceiling_seconds),
    )
    return cursor.rowcount


def requeue_failed(conn, max_attempts: int) -> int:
    """Send failed messages that still have attempts left back for another try: failed → received.

    Returns how many were re-queued.
    A transient failure — a flaky moment, a crash that won't recur — shouldn't be a death sentence,
    so a failed row with fewer than max_attempts tries behind it goes back to received for a worker to claim again.
    The claim will bump attempts, so the budget spends down with each try.
    failed_reason is cleared on the way back,
    so a re-queued message is a clean received row again (attempts alone records that it has failed before)
    and no working or answered row ever carries a stale reason.
    Guarded on status = 'failed', so it can't disturb a message that isn't there.
    """
    cursor = conn.execute(
        "UPDATE intake SET status = 'received', failed_reason = NULL, updated_at = now() "
        "WHERE status = 'failed' AND attempts < %s",
        (max_attempts,),
    )
    return cursor.rowcount


def abandon_exhausted(conn, max_attempts: int) -> list[int]:
    """Give up on failed messages that have spent their attempts: failed → abandoned.

    Returns the ids of the messages abandoned this call.
    The counterpart to requeue_failed: a message that has failed max_attempts times has used its budget,
    so it's parked in the terminal 'abandoned' state rather than retried forever —
    the retrying itself must not become a new way to loop.
    failed_reason is kept, so an abandoned message still says why its last attempt failed.
    Guarded on status = 'failed', the same guard as re-queuing, and on the opposite side of the attempts test —
    so every failed row goes to exactly one of the two, never both.
    It returns the ids (not just a count) because 'abandoned' is a terminal outcome the reconcile sweep has to announce:
    each one is a message whose subscription, if it has one, is owed a push saying the kernel gave up.
    """
    cursor = conn.execute(
        "UPDATE intake SET status = 'abandoned', updated_at = now() "
        "WHERE status = 'failed' AND attempts >= %s "
        "RETURNING id",
        (max_attempts,),
    )
    return [row[0] for row in cursor.fetchall()]


def mark_delivered(conn, ids: list[int]) -> int:
    """Record that the shell has shown a message's outcome to the symbiot: stamp delivered_at.

    Returns how many rows this marked delivered.
    A reply is 'truly out' — the honest counterpart, on the way back, to the outbox's COPY —
    only once the terminal it was emitted to confirms it displayed it.
    The shell reports that here, after rendering an answer (or an abandonment) it read off /answers,
    so 'answered' means the reply was produced and delivered_at means it actually reached the human.
    Guarded on a terminal, still-undelivered row (status in answered/abandoned, delivered_at null),
    so a re-ack, an in-flight id, or an unknown one changes nothing:
    idempotent and safe on an unauthed route where the id is the only capability.
    A lost ack simply leaves delivered_at null — the signal errs toward not-yet-delivered,
    never toward a delivery that didn't happen, so a set delivered_at is always trustworthy.
    """
    if not ids:
        return 0
    cursor = conn.execute(
        "UPDATE intake SET delivered_at = now() "
        "WHERE delivered_at IS NULL AND status IN ('answered', 'abandoned') AND id = ANY(%s)",
        (ids,),
    )
    return cursor.rowcount


def recover_orphaned(conn, max_attempts: int) -> tuple[int, list[int]]:
    """Reconcile rows a dead process left mid-work: working → received, or → abandoned.

    Returns (how many were re-queued, the ids abandoned).
    Run once at startup, before any worker of this process begins.
    At that instant every row still in 'working' is an orphan by definition —
    the worker that claimed it belonged to a process that has since exited,
    so no one is left to finish it.
    That is why this needs no heartbeat or lease to spot an orphan: the process boundary is the signal.
    It does assume a single kernel against the database —
    a second live instance's in-flight rows would look orphaned from here,
    and telling those apart is high-availability work (OS-16), not this.

    An orphan is re-queued, not failed, and that distinction is the whole point of this step.
    Nothing about the work failed — the kernel did, mid-flight —
    so there is no outcome to record and no traceback to keep;
    marking it 'failed' would invent a failure that never happened.
    A row with attempts to spare goes straight back to 'received' for a fresh claim,
    as if it had never been taken.

    The reclaim is bounded by the same budget as every other retry (requeue_failed),
    so a message that crashes the whole kernel on each try can't loop across restarts forever:
    a row that has already spent its attempts is parked in the terminal 'abandoned' instead,
    with a reason of its own (ORPHAN_ABANDON_REASON) — it has no traceback to carry,
    but an abandoned row must still say why.

    Both moves are guarded on status = 'working' and split on the attempts test,
    so a row goes to exactly one of them, the same disjoint shape as requeue_failed / abandon_exhausted.
    The abandoned ids come back (not just a count) because 'abandoned' is terminal:
    each one's subscription, if it registered one, is owed the give-up nudge.
    """
    requeued = conn.execute(
        "UPDATE intake SET status = 'received', updated_at = now() "
        "WHERE status = 'working' AND attempts < %s",
        (max_attempts,),
    ).rowcount
    cursor = conn.execute(
        "UPDATE intake SET status = 'abandoned', failed_reason = %s, updated_at = now() "
        "WHERE status = 'working' AND attempts >= %s "
        "RETURNING id",
        (ORPHAN_ABANDON_REASON, max_attempts),
    )
    return requeued, [row[0] for row in cursor.fetchall()]
