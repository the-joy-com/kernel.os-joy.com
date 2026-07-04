"""Missives: messages the kernel raises for a symbiot on its own.

A missive owes nothing to a prior /intake — it's a nudge, or a line relayed from the
World, that the kernel authored and addressed to a symbiot. It lives apart from intake
because it doesn't share intake's shape: an intake row is a question walking toward an
answer, and a missive has neither a question nor a walk — the kernel wrote it, it *is*
the content, and there's nothing to compute. So there's no state machine here, only a
record, a read, and an acknowledgement.

The shell never sent a missive, so it holds no id to ask about one — the way it does for
its own lines (see intake.read_outcome and the /answers route). Discovery is the whole
point of this module: /inbox lists a symbiot's unseen missives, and /inbox/seen marks the
ones the shell has shown, so each surfaces exactly once across opens and devices.

The record and the reads below are pure data access. `deliver` sits one layer up: it's the
complete act of the kernel reaching out — record the missive, then nudge — and is what a
producer (or a QA session) calls to send one.
"""

import push


# The functions below are ordered by layer, not alphabetically:
# the three pure data-access primitives first, in the arc the module docstring narrates —
# raise_for (the record), unseen_for_symbiot (the read), mark_seen (the acknowledgement) —
# and then deliver, which sits one layer up and composes them into the kernel's act of reaching out.
# The order mirrors the "record, read, acknowledgement … deliver one layer up" the docstring describes,
# so reading top to bottom is reading from primitive to composite.
def raise_for(conn, symbiot_id: int, body: str) -> int:
    """Raise a missive addressed to a symbiot, and return its id.

    Persisted unseen (seen_at null), which is what /inbox reads. There's no work to walk
    toward — a missive is the message, not a question — so it's ready the instant it's
    written; nothing moves it forward, because there's nowhere forward to move.
    """
    row = conn.execute(
        "INSERT INTO missive (symbiot_id, body) VALUES (%s, %s) RETURNING id",
        (symbiot_id, body),
    ).fetchone()
    return row[0]


def unseen_for_symbiot(conn, symbiot_id: int) -> list[tuple[int, str]]:
    """A symbiot's unseen missives as (id, body), oldest first.

    The read behind /inbox — the messages the shell couldn't have discovered on its own,
    since it never sent them. Already-seen ones are out of scope.
    """
    rows = conn.execute(
        "SELECT id, body FROM missive "
        "WHERE symbiot_id = %s AND seen_at IS NULL "
        "ORDER BY id",
        (symbiot_id,),
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def mark_seen(conn, symbiot_id: int, ids: list[int]) -> int:
    """Mark a symbiot's missives shown, so /inbox won't offer them again.

    Returns how many rows this changed.
    Scoped to the caller's own still-unseen missives: an id that isn't theirs or is already
    seen changes nothing — so acking is safe, idempotent, and can't be turned into a way to
    touch another symbiot's missives.
    """
    if not ids:
        return 0
    cursor = conn.execute(
        "UPDATE missive SET seen_at = now() "
        "WHERE symbiot_id = %s AND seen_at IS NULL AND id = ANY(%s)",
        (symbiot_id, ids),
    )
    return cursor.rowcount


def deliver(pool, symbiot_id: int, body: str) -> int:
    """Raise a missive for a symbiot and nudge them it's waiting. Returns its id.

    The whole act of the kernel reaching out on its own, over two channels so first contact
    never rides on one holding up:
      • the record — raise_for writes the missive durably, so /inbox surfaces it on the next
        open no matter what. This is the guarantee; it always lands.
      • the nudge — a best-effort push (push.notify_inbox) so it arrives promptly rather than
        only on next open. This is the speed; it may be skipped (push off, no channel, a dead
        one) without weakening the guarantee, because the record already stands.
    Takes a pool, not a connection: the write is its own quick transaction, and the push
    runs outside any transaction (it's slow external I/O and must not hold one open).
    """
    with pool.connection() as conn:
        missive_id = raise_for(conn, symbiot_id, body)
    push.notify_inbox(pool, symbiot_id)
    return missive_id
