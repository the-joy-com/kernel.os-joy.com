"""The inbox: missives the kernel raises for a symbiot, and how the shell discovers them.

A symbiot learns of an answer to its *own* line from the id it kept at intake. But a
missive — a message the kernel originates, addressed to a symbiot, owing nothing to a
prior send — was never sent from the shell, so there's no id to have kept. /inbox is where
the shell discovers those, and it's identity-gated: a missive belongs to a symbiot, so
only a live session sees them, and only its own. A missive lives in its own table, not in
intake — it has no question and no walk to an answer — so the worker never sees one and
/answers can't reach one. Everything runs against the test database.
"""

import db
import intake
import missive
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


def _symbiot_id(email=SYMBIOT_EMAIL) -> int:
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT id FROM symbiot WHERE email = %s", (email,)
        ).fetchone()[0]


def _raise(symbiot_id: int, body: str) -> int:
    with db.get_pool().connection() as conn:
        return missive.raise_for(conn, symbiot_id, body)


def _row(missive_id: int):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT symbiot_id, body, seen_at FROM missive WHERE id = %s",
            (missive_id,),
        ).fetchone()


# --- the producer ---------------------------------------------------------------------


def test_missive_is_addressed_and_unseen(client):
    # A raised missive lands addressed to a symbiot, with its text in body, unseen until
    # the shell shows it. There's no status and no walk — a missive is the message.
    sid = _symbiot_id()
    missive_id = _raise(sid, "a message from the kernel")
    symbiot_id, body, seen_at = _row(missive_id)
    assert symbiot_id == sid
    assert body == "a message from the kernel"
    assert seen_at is None  # not shown yet


def test_worker_never_sees_a_missive(client):
    # A missive isn't an intake row, so the worker — which claims from intake — can't see
    # it. Raising one leaves nothing claimable.
    _raise(_symbiot_id(), "kernel says hi")
    with db.get_pool().connection() as conn:
        assert intake.claim_next(conn) is None  # nothing in intake to claim


def test_deliver_records_the_missive_so_inbox_surfaces_it(client, fake_email):
    # deliver is the producer entry point: record the missive, then nudge. Push is off in
    # the suite, so the nudge is a silent no-op — but the record is the guarantee, and it
    # stands: the missive is there for /inbox to surface on the next open.
    token = _token(client, fake_email)
    sid = _symbiot_id()
    missive_id = missive.deliver(db.get_pool(), sid, "reaching out")
    assert _row(missive_id)[1] == "reaching out"  # body persisted
    messages = client.get("/inbox", headers=_auth(token)).json()["data"]["messages"]
    assert messages == [{"id": missive_id, "body": "reaching out"}]


# --- /inbox: discovery ----------------------------------------------------------------


def test_inbox_lists_unseen_missives_oldest_first(client, fake_email):
    token = _token(client, fake_email)
    sid = _symbiot_id()
    first = _raise(sid, "first")
    second = _raise(sid, "second")

    body = client.get("/inbox", headers=_auth(token)).json()
    assert body["msg"] == "traffic waiting"
    assert body["data"]["messages"] == [
        {"id": first, "body": "first"},
        {"id": second, "body": "second"},
    ]


def test_inbox_excludes_the_symbiots_own_answers(client, fake_email):
    # A symbiot-origin message, even once answered, is discovered from the id kept at
    # intake — not through the inbox. Only missives surface here.
    token = _token(client, fake_email)
    with db.get_pool().connection() as conn:
        own = intake.record_message(conn, "a line I typed")
        intake.claim_next(conn)
        intake.mark_answered(conn, own, "the reply")

    messages = client.get("/inbox", headers=_auth(token)).json()["data"]["messages"]
    assert messages == []  # my own answer is not inbox traffic


def test_inbox_is_empty_without_a_session(client):
    # Identity-gated: no token, or a token that names no live session, is owed nothing —
    # an empty list, never an error that would hint something was there.
    assert client.get("/inbox").json()["data"]["messages"] == []
    assert (
        client.get("/inbox", headers=_auth("not-a-real-token")).json()["data"]["messages"]
        == []
    )


def test_missive_never_leaks_through_answers(client):
    # /answers is unauthed and reads by a guessable id — but it reads *intake*, and a
    # missive lives in its own table. So a missive is structurally unreachable there;
    # it crosses only through the identity-gated /inbox.
    missive_id = _raise(_symbiot_id(), "private nudge")
    body = client.get(f"/answers?id={missive_id}").json()
    assert body["msg"] == "unknown"  # no intake row carries that id
    assert "private nudge" not in str(body)


# --- /inbox/seen: acknowledgement -----------------------------------------------------


def test_seen_stops_a_missive_from_returning(client, fake_email):
    token = _token(client, fake_email)
    sid = _symbiot_id()
    a, b = _raise(sid, "one"), _raise(sid, "two")

    client.post("/inbox/seen", json={"ids": [a, b]}, headers=_auth(token))
    assert client.get("/inbox", headers=_auth(token)).json()["data"]["messages"] == []


def test_seen_reports_how_many_it_marked(client, fake_email):
    token = _token(client, fake_email)
    a = _raise(_symbiot_id(), "only one")
    r = client.post("/inbox/seen", json={"ids": [a]}, headers=_auth(token))
    assert r.json()["data"]["seen"] == 1
    # Acking the same id again changes nothing — idempotent.
    again = client.post("/inbox/seen", json={"ids": [a]}, headers=_auth(token))
    assert again.json()["data"]["seen"] == 0


def test_empty_ack_is_a_clean_noop(client, fake_email):
    token = _token(client, fake_email)
    r = client.post("/inbox/seen", json={"ids": []}, headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["data"]["seen"] == 0


# --- ownership: one symbiot can't touch another's inbox -------------------------------


def _seed_symbiot(email: str) -> int:
    with db.get_pool().connection() as conn:
        return conn.execute(
            "INSERT INTO symbiot (email) VALUES (%s) RETURNING id", (email,)
        ).fetchone()[0]


def test_inbox_is_scoped_to_the_caller(client, fake_email):
    # A missive for another symbiot is invisible to me, and my ack can't reach it.
    mine = _token(client, fake_email)
    other_id = _seed_symbiot("other@example.com")
    theirs = _raise(other_id, "not for you")

    # It doesn't show in my inbox...
    assert client.get("/inbox", headers=_auth(mine)).json()["data"]["messages"] == []
    # ...and acking its id from my session marks nothing (it isn't mine).
    r = client.post("/inbox/seen", json={"ids": [theirs]}, headers=_auth(mine))
    assert r.json()["data"]["seen"] == 0
    assert _row(theirs)[2] is None  # still unseen — untouched by another's ack
