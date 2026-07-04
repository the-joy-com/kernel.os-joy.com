"""Web push: storing a subscription, and nudging it when a message settles.

The database parts run for real against the test database.
The send itself — network I/O to an external push service — is never made for real here:
it's monkeypatched, or push is left off (the suite's default, pinned in conftest),
so the suite reaches no push service.
The one thing proven against a real key is that the public application server key derives from the private one;
signing and sending a real push needs a real browser, proven by hand.
"""

import config
import db
import intake
import push
from conftest import SYMBIOT_EMAIL, extract_code

# A valid base64url 32-octet private scalar — a throwaway VAPID key for the tests that
# need push switched on. Never a real credential; the suite never sends with it.
TEST_VAPID_KEY = "ASNSWLwop5XkSvQC4zmGN2wEpHTZbs56UQOrByFyZqk"


def _symbiot_id(email=SYMBIOT_EMAIL) -> int:
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT id FROM symbiot WHERE email = %s", (email,)
        ).fetchone()[0]


def _token(client, fake_email, address=SYMBIOT_EMAIL) -> str:
    # Walk the real login flow to a session token, the way the shell does.
    client.post("/login", json={"address": address})
    code = extract_code(fake_email)
    return client.post(
        "/login/verify", json={"address": address, "code": code}
    ).json()["data"]["token"]


def _channel_symbiot(channel_id: int):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT symbiot_id FROM reply_channel WHERE id = %s", (channel_id,)
        ).fetchone()[0]


def _answered_with_subscription(monkeypatch, endpoint="https://push.example/abc"):
    # A message linked to a subscription and driven to 'answered' — the state that owes a nudge.
    # Returns (message_id, subscription_id).
    with db.get_pool().connection() as conn:
        subscription_id = push.save_subscription(conn, endpoint, "p256", "authsecret")
        message_id = intake.record_message(conn, "a message", subscription_id)
        intake.claim_next(conn)
        intake.mark_answered(conn, message_id, "the reply")
    return message_id, subscription_id


# --- subscription storage -------------------------------------------------------------


def test_save_subscription_upserts_on_endpoint(client):
    # A browser re-subscribing with the same endpoint refreshes its row in place —
    # same id, keys updated — rather than piling up duplicates the kernel would push to.
    with db.get_pool().connection() as conn:
        first = push.save_subscription(conn, "https://push.example/x", "k1", "a1")
        again = push.save_subscription(conn, "https://push.example/x", "k2", "a2")
        assert again == first  # same endpoint, same row
        keys = conn.execute(
            "SELECT p256dh, auth FROM reply_channel WHERE id = %s", (first,)
        ).fetchone()
    assert keys == ("k2", "a2")  # refreshed in place


def test_push_subscribe_route_stores_and_returns_id(client):
    r = client.post(
        "/push/subscribe",
        json={"endpoint": "https://push.example/r", "keys": {"p256dh": "pk", "auth": "ak"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["msg"] == "subscribed"
    with db.get_pool().connection() as conn:
        endpoint = conn.execute(
            "SELECT endpoint FROM reply_channel WHERE id = %s", (body["data"]["id"],)
        ).fetchone()[0]
    assert endpoint == "https://push.example/r"


def test_intake_links_its_subscription(client):
    # The id the shell got from /push/subscribe, threaded through /intake, lands on the row
    # so the kernel knows whom to nudge when the message settles.
    with db.get_pool().connection() as conn:
        subscription_id = push.save_subscription(conn, "https://push.example/i", "pk", "ak")
    r = client.post("/intake", json={"line": "hi", "reply_channel_id": subscription_id})
    message_id = r.json()["data"]["id"]
    with db.get_pool().connection() as conn:
        link = conn.execute(
            "SELECT reply_channel_id FROM intake WHERE id = %s", (message_id,)
        ).fetchone()[0]
    assert link == subscription_id


# --- the application server key --------------------------------------------------------


def test_application_server_key_derives_from_the_private_key(client, monkeypatch):
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", TEST_VAPID_KEY)
    key = push.application_server_key()
    assert isinstance(key, str)
    assert len(key) > 80  # base64url of a 65-octet uncompressed point
    assert "=" not in key  # url-safe and unpadded, the way a browser wants it


def test_application_server_key_is_none_when_push_is_off(client):
    # Push is off in the suite (conftest pins VAPID_PRIVATE_KEY empty).
    assert push.application_server_key() is None


def test_push_key_route_reports_null_when_off(client):
    body = client.get("/push/key").json()
    assert body["msg"] == "push key"
    assert body["data"]["key"] is None  # the shell reads this as "no push, poll on open"


# --- notify ---------------------------------------------------------------------------


def test_notify_pushes_when_a_message_is_answered(client, monkeypatch):
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", TEST_VAPID_KEY)
    sent = []
    monkeypatch.setattr(
        push, "_send", lambda endpoint, p256dh, auth, payload: sent.append((endpoint, payload)) or False
    )
    message_id, _ = _answered_with_subscription(monkeypatch)
    push.notify(db.get_pool(), message_id)
    assert len(sent) == 1
    endpoint, payload = sent[0]
    assert endpoint == "https://push.example/abc"
    # kind names the nudge family; id + the shell-facing status word ride under it
    assert payload == {"kind": "reply", "id": message_id, "status": "answer"}


def test_notify_is_silent_when_push_is_off(client, monkeypatch):
    # VAPID key empty (conftest default) — notify must not even attempt a send.
    called = []
    monkeypatch.setattr(push, "_send", lambda *a: called.append(1) or False)
    message_id, _ = _answered_with_subscription(monkeypatch)
    push.notify(db.get_pool(), message_id)
    assert called == []


def test_notify_is_silent_when_no_subscription_linked(client, monkeypatch):
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", TEST_VAPID_KEY)
    called = []
    monkeypatch.setattr(push, "_send", lambda *a: called.append(1) or False)
    with db.get_pool().connection() as conn:
        message_id = intake.record_message(conn, "no one to tell")  # no subscription
        intake.claim_next(conn)
        intake.mark_answered(conn, message_id, "reply")
    push.notify(db.get_pool(), message_id)
    assert called == []  # nobody asked to be told


def test_notify_prunes_a_subscription_the_service_reports_gone(client, monkeypatch):
    # A 404/410 makes _send return True; the dead address is pruned, but the answer and
    # its message survive — ON DELETE SET NULL only nulls the link.
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", TEST_VAPID_KEY)
    monkeypatch.setattr(push, "_send", lambda *a: True)  # the push service says gone
    message_id, subscription_id = _answered_with_subscription(monkeypatch)
    push.notify(db.get_pool(), message_id)
    with db.get_pool().connection() as conn:
        remaining = conn.execute(
            "SELECT count(*) FROM reply_channel WHERE id = %s", (subscription_id,)
        ).fetchone()[0]
        status, link = conn.execute(
            "SELECT status, reply_channel_id FROM intake WHERE id = %s", (message_id,)
        ).fetchone()
    assert remaining == 0  # the dead subscription is gone
    assert status == "answered"  # the answer outlives the address it would have announced
    assert link is None  # the link nulled, not the message dropped


# --- subscription identity ------------------------------------------------------------


def test_save_subscription_is_anonymous_by_default(client):
    # No symbiot given — the channel is unlinked, and still serves per-message reply nudges.
    with db.get_pool().connection() as conn:
        cid = push.save_subscription(conn, "https://push.example/anon", "k", "a")
    assert _channel_symbiot(cid) is None


def test_save_subscription_links_the_symbiot_when_given(client):
    sid = _symbiot_id()
    with db.get_pool().connection() as conn:
        cid = push.save_subscription(conn, "https://push.example/me", "k", "a", sid)
    assert _channel_symbiot(cid) == sid


def test_resubscribe_adopts_identity_but_never_clears_it(client):
    # Anonymous first, then a logged-in refresh links it; a later logged-out refresh must
    # not un-link it — COALESCE keeps the existing symbiot when the new call is anonymous.
    sid = _symbiot_id()
    endpoint = "https://push.example/rotating"
    # Each save in its own transaction, so the read-back (a separate connection) sees it.
    with db.get_pool().connection() as conn:
        cid = push.save_subscription(conn, endpoint, "k1", "a1")  # anonymous
    assert _channel_symbiot(cid) is None
    with db.get_pool().connection() as conn:
        push.save_subscription(conn, endpoint, "k2", "a2", sid)  # links to the symbiot
    assert _channel_symbiot(cid) == sid
    with db.get_pool().connection() as conn:
        push.save_subscription(conn, endpoint, "k3", "a3")  # anonymous refresh
    assert _channel_symbiot(cid) == sid  # kept, not cleared


def test_push_subscribe_route_links_the_authenticated_symbiot(client, fake_email):
    token = _token(client, fake_email)
    r = client.post(
        "/push/subscribe",
        json={"endpoint": "https://push.example/auth", "keys": {"p256dh": "pk", "auth": "ak"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert _channel_symbiot(r.json()["data"]["id"]) == _symbiot_id()


def test_push_subscribe_route_stays_anonymous_without_a_session(client):
    r = client.post(
        "/push/subscribe",
        json={"endpoint": "https://push.example/noauth", "keys": {"p256dh": "pk", "auth": "ak"}},
    )
    assert _channel_symbiot(r.json()["data"]["id"]) is None


# --- notify_inbox: nudging a symbiot that a missive is waiting -------------------------


def test_notify_inbox_pushes_to_every_channel_the_symbiot_has(client, monkeypatch):
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", TEST_VAPID_KEY)
    sent = []
    monkeypatch.setattr(
        push, "_send", lambda endpoint, p256dh, auth, payload: sent.append((endpoint, payload)) or False
    )
    sid = _symbiot_id()
    with db.get_pool().connection() as conn:
        push.save_subscription(conn, "https://push.example/d1", "k", "a", sid)
        push.save_subscription(conn, "https://push.example/d2", "k", "a", sid)
    push.notify_inbox(db.get_pool(), sid)
    assert {endpoint for endpoint, _ in sent} == {
        "https://push.example/d1",
        "https://push.example/d2",
    }
    assert all(payload == {"kind": "traffic waiting"} for _, payload in sent)  # content-free nudge


def test_notify_inbox_is_silent_when_push_is_off(client, monkeypatch):
    called = []
    monkeypatch.setattr(push, "_send", lambda *a: called.append(1) or False)
    sid = _symbiot_id()
    with db.get_pool().connection() as conn:
        push.save_subscription(conn, "https://push.example/off", "k", "a", sid)
    push.notify_inbox(db.get_pool(), sid)
    assert called == []


def test_notify_inbox_is_silent_when_the_symbiot_has_no_channel(client, monkeypatch):
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", TEST_VAPID_KEY)
    called = []
    monkeypatch.setattr(push, "_send", lambda *a: called.append(1) or False)
    push.notify_inbox(db.get_pool(), _symbiot_id())  # nobody registered a channel
    assert called == []


def test_notify_inbox_prunes_a_dead_channel(client, monkeypatch):
    # A 404/410 makes _send return True; the dead address is pruned so the kernel stops
    # pushing into the void.
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", TEST_VAPID_KEY)
    monkeypatch.setattr(push, "_send", lambda *a: True)  # the push service says gone
    sid = _symbiot_id()
    with db.get_pool().connection() as conn:
        cid = push.save_subscription(conn, "https://push.example/dead", "k", "a", sid)
    push.notify_inbox(db.get_pool(), sid)
    with db.get_pool().connection() as conn:
        remaining = conn.execute(
            "SELECT count(*) FROM reply_channel WHERE id = %s", (cid,)
        ).fetchone()[0]
    assert remaining == 0