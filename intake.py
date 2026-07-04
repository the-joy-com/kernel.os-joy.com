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
The worker (worker.py) drives a message along this path;
this module owns the durable write and the legal moves it makes, not the deciding.

One row per request — the message, never the lines inside it.
The kernel stores what it can honestly stand behind, nothing it would have to guess.
"""

# The reason recorded when the deadline sweep — not a worker — fails a row.
# A worker that fails a message writes a specific reason (a traceback, or that it outran the deadline);
# the sweep only knows a row overstayed its ceiling with no worker left to speak for it,
# so it says exactly that, and the two are told apart by their reason.
SWEEP_REASON = "deadline exceeded (swept: no worker reported an outcome)"


# The functions below are ordered by the message's lifecycle, not alphabetically:
# record_message and read_outcome first (the write at receipt and the read back off it),
# then the state transitions in the order a message walks them —
# claim, answer, fail, sweep, requeue, abandon.
# The order mirrors the path the module docstring narrates,
# so reading top to bottom is reading a message's life start to finish.
def record_message(conn, message: str, reply_channel_id: int | None = None) -> int:
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
    """
    row = conn.execute(
        "INSERT INTO intake (message, reply_channel_id) VALUES (%s, %s) RETURNING id",
        (message, reply_channel_id),
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
        "SELECT status, answer FROM intake WHERE id = %s", (message_id,)
    ).fetchone()
    return (row[0], row[1]) if row else None


def claim_next(conn) -> tuple[int, str] | None:
    """Claim the oldest waiting message for work: received → working, atomically.

    Returns (id, message) for the message claimed, or None when none is waiting.
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
        "RETURNING id, message"
    ).fetchone()
    return (row[0], row[1]) if row else None


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
