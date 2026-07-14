"""Presence: the shell heartbeat the /inbox poll leaves, and reading it back as "watching now".

The kernel holds no live connection, so "present" is inferred from a recent sign of life — the shell's
visibility-gated /inbox poll, which fires every ten seconds only while the tab is watched. These pin the two
halves of that inference: the store (mark_seen stamps, is_active reads against the window, null and stale both
read absent) and the route (a /inbox call leaves the stamp, an anonymous one leaves nothing to stamp). The
consequence for the fan-out — that a present symbiot's courtesy nudge is held — is pinned in test_notify.py;
here it's just the signal itself.
"""

from core import config
from core import db
from services.memory import presence
from conftest import SYMBIOT_EMAIL, extract_code

SEEDED_SYMBIOT_ID = 1  # conftest re-seeds exactly one symbiot with RESTART IDENTITY, so it's always id 1


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _token(client, fake_email) -> str:
    # Walk the real login flow to a session token, the way the shell does.
    client.post("/login", json={"address": SYMBIOT_EMAIL})
    code = extract_code(fake_email)
    return client.post(
        "/login/verify", json={"address": SYMBIOT_EMAIL, "code": code}
    ).json()["data"]["token"]


def test_is_active_false_when_never_seen(client):
    # A freshly seeded symbiot has never polled — last_seen_at is null, which reads as absent without a sentinel.
    with db.get_pool().connection() as conn:
        assert presence.is_active(conn, SEEDED_SYMBIOT_ID) is False


def test_mark_seen_makes_active(client):
    with db.get_pool().connection() as conn:
        presence.mark_seen(conn, SEEDED_SYMBIOT_ID)
        assert presence.is_active(conn, SEEDED_SYMBIOT_ID) is True


def test_stale_stamp_reads_absent(client):
    # Seen, but longer ago than the window — the tab has gone quiet, so the heartbeat no longer means present.
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE symbiot SET last_seen_at = now() - make_interval(secs => %s) WHERE id = %s",
            (config.PRESENCE_ACTIVE_WINDOW_SECONDS + 5, SEEDED_SYMBIOT_ID),
        )
        assert presence.is_active(conn, SEEDED_SYMBIOT_ID) is False


def test_mark_seen_moves_forward_only(client):
    # last_seen_at is a high-water mark: a later stamp advances it, and the window does the deciding about
    # recency. Two stamps in a row leave the newer one, and the symbiot reads present throughout.
    with db.get_pool().connection() as conn:
        presence.mark_seen(conn, SEEDED_SYMBIOT_ID)
        first = conn.execute(
            "SELECT last_seen_at FROM symbiot WHERE id = %s", (SEEDED_SYMBIOT_ID,)
        ).fetchone()[0]
        presence.mark_seen(conn, SEEDED_SYMBIOT_ID)
        second = conn.execute(
            "SELECT last_seen_at FROM symbiot WHERE id = %s", (SEEDED_SYMBIOT_ID,)
        ).fetchone()[0]
    assert second >= first


def test_inbox_poll_stamps_presence(client, fake_email):
    # The /inbox poll is the heartbeat: an authed call leaves the symbiot reading as present, so a missive
    # raised right after holds its out-of-app nudge. This is the wiring that makes presence-suppression real.
    token = _token(client, fake_email)
    with db.get_pool().connection() as conn:
        assert presence.is_active(conn, SEEDED_SYMBIOT_ID) is False  # not seen until the poll lands
    assert client.get("/inbox", headers=_auth(token)).json()["msg"] == "traffic waiting"
    with db.get_pool().connection() as conn:
        assert presence.is_active(conn, SEEDED_SYMBIOT_ID) is True


def test_inbox_without_a_session_stamps_nothing(client):
    # An anonymous /inbox is owed an empty list and names no symbiot, so there's no one to stamp — presence
    # stays what it was (absent), and the poll can't be turned into a way to mark a symbiot present unauthed.
    assert client.get("/inbox").json()["data"]["messages"] == []
    with db.get_pool().connection() as conn:
        assert presence.is_active(conn, SEEDED_SYMBIOT_ID) is False
