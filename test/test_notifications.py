"""The /notifications command: reading and flipping the symbiot's per-channel enable/disable.

Authed-gated like /timezone, so an anonymous caller is turned away rather than shown or allowed to write a
preference. The route is thin over notification_prefs; what these pin is the contract the shell reads — every
channel listed with its state, on by default, a flip that persists, and an unknown channel written nowhere.
"""

from core import db
from conftest import SYMBIOT_EMAIL, extract_code


def _token(client, fake_email, address=SYMBIOT_EMAIL) -> str:
    # Walk the real login flow to a session token, the way the shell does.
    client.post("/login", json={"address": address})
    code = extract_code(fake_email)
    return client.post(
        "/login/verify", json={"address": address, "code": code}
    ).json()["data"]["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_notifications_lists_every_channel_enabled_by_default(client, fake_email):
    token = _token(client, fake_email)
    body = client.get("/notifications", headers=_auth(token)).json()
    assert body["msg"] == "notifications"
    # One entry per real channel, all on until the symbiot turns one off.
    assert body["data"]["channels"] == {"email": True, "web_push": True}


def test_notifications_get_requires_a_session(client):
    body = client.get("/notifications").json()
    assert body["msg"] == "not authenticated"


def test_notifications_post_requires_a_session(client):
    body = client.post("/notifications", json={"channel": "email", "enabled": False}).json()
    assert body["msg"] == "not authenticated"


def test_set_notification_disables_a_channel_and_persists_it(client, fake_email):
    token = _token(client, fake_email)
    body = client.post(
        "/notifications", json={"channel": "email", "enabled": False}, headers=_auth(token)
    ).json()
    # The reply carries the full, current state so the shell re-renders from one source.
    assert body["data"]["channels"] == {"email": False, "web_push": True}
    with db.get_pool().connection() as conn:
        enabled = conn.execute(
            "SELECT enabled FROM notification_preference WHERE symbiot_id = 1 AND channel = 'email'"
        ).fetchone()[0]
    assert enabled is False


def test_set_notification_re_enables_a_channel(client, fake_email):
    token = _token(client, fake_email)
    client.post("/notifications", json={"channel": "email", "enabled": False}, headers=_auth(token))
    body = client.post(
        "/notifications", json={"channel": "email", "enabled": True}, headers=_auth(token)
    ).json()
    assert body["data"]["channels"] == {"email": True, "web_push": True}


def test_set_notification_ignores_an_unknown_channel(client, fake_email):
    token = _token(client, fake_email)
    body = client.post(
        "/notifications", json={"channel": "carrier_pigeon", "enabled": False}, headers=_auth(token)
    ).json()
    # A slug that names no real channel writes nothing — no phantom preference, state unchanged.
    assert body["data"]["channels"] == {"email": True, "web_push": True}
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT count(*) FROM notification_preference WHERE symbiot_id = 1"
        ).fetchone()[0]
    assert rows == 0
