"""Presence: whether the symbiot is actively watching the shell right now.

The kernel is stateless request-and-reply — it holds no live connection to the shell,
so "present" can't mean an open socket; it has to be inferred from a recent sign of life.
The sign it leans on is the shell's inbox poll:
the terminal, while its tab is visible, asks /inbox every ten seconds,
and only then (a backgrounded tab goes quiet — see the shell's visibility-gated poll),
so a fresh stamp is a near-perfect heartbeat for "someone is looking at the screen".

This store is the two halves of that:
mark_seen stamps the heartbeat from the /inbox route,
and is_active reads it back against a tolerant window to answer "present?".
It is deliberately thin — one column on the symbiot row (migration 0020),
moved forward and read —
because presence is only ever a courtesy signal:
its one consumer is notify.dispatch,
which holds a missive's out-of-app nudge back when the symbiot is present,
since the live inbox poll is already surfacing the record in front of them.
A wrong guess costs only immediacy, never the message —
the durable missive record stands regardless —
so the inference doesn't have to be certain, only cheap.
"""

from core import config


def mark_seen(conn, symbiot_id: int) -> None:
    """Stamp the symbiot seen now — the heartbeat behind the /inbox poll.

    A plain forward-only write:
    last_seen_at is a high-water mark, never cleared,
    so the window in is_active does all the deciding about whether it's still recent enough to mean present.
    Scoped to the one row by id, so it can only ever touch the caller's own symbiot.
    """
    conn.execute("UPDATE symbiot SET last_seen_at = now() WHERE id = %s", (symbiot_id,))


def is_active(conn, symbiot_id: int, within_seconds: float | None = None) -> bool:
    """Whether the symbiot has been seen within the presence window — the read behind the dispatcher's hold.

    True only when last_seen_at is both set and newer than now() minus the window
    (config.PRESENCE_ACTIVE_WINDOW_SECONDS unless the caller narrows it):
    a symbiot who has never polled (null) reads as not present without a sentinel,
    and one last seen longer ago than the window has let their tab go quiet
    (backgrounded, closed, or asleep),
    so the nudge is owed again.
    The comparison is the database's own clock against its own stamp,
    so it never depends on the caller's sense of now.
    """
    window = config.PRESENCE_ACTIVE_WINDOW_SECONDS if within_seconds is None else within_seconds
    row = conn.execute(
        "SELECT last_seen_at > now() - make_interval(secs => %s) FROM symbiot WHERE id = %s",
        (window, symbiot_id),
    ).fetchone()
    return bool(row and row[0])
