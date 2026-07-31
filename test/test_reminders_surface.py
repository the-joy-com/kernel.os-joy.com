"""The /reminders surface: reading the standing set, and the three writes over it.

The first thing in the kernel that lets anything read the reminders the machine is holding,
so what these pin is that contract — the set comes back with real ids and a state word,
a reminder can be typed straight in with no message behind it,
and the two writes land only on a reminder that is this symbiot's, unfired and uncancelled,
refusing legibly rather than silently when it isn't.
No model or embedding is reached: the terse path spends no model call, which is the point of it,
so the whole surface runs end to end against the test database.
"""

from datetime import timedelta

from core import db
from services.loop import zone
from services.tools import reminder
from conftest import SYMBIOT_EMAIL, extract_code

SEEDED_SYMBIOT_ID = 1  # conftest re-seeds exactly one symbiot with RESTART IDENTITY, so it's always id 1


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _reminders(client, token) -> list[dict]:
    return client.get("/reminders", headers=_auth(token)).json()["data"]["reminders"]


def _token(client, fake_email, address=SYMBIOT_EMAIL) -> str:
    # Walk the real login flow to a session token, the way the shell does.
    client.post("/login", json={"address": address})
    code = extract_code(fake_email)
    return client.post(
        "/login/verify", json={"address": address, "code": code}
    ).json()["data"]["token"]


def test_the_standing_set_is_authed_only(client):
    body = client.get("/reminders").json()
    # A reminder belongs to a particular symbiot, so there is no anonymous version to serve.
    assert body["msg"] == "not authenticated"


def test_a_reminder_typed_straight_in_needs_no_triggering_message(client, fake_email):
    token = _token(client, fake_email)
    body = client.post(
        "/reminders", json={"say": "call the dentist", "when": "+2h"}, headers=_auth(token)
    ).json()
    assert body["msg"] == "reminders"
    assert [r["body"] for r in body["data"]["reminders"]] == ["call the dentist"]
    with db.get_pool().connection() as conn:
        intake_id = conn.execute("SELECT intake_id FROM reminder").fetchone()[0]
    # Nothing was said to produce this one, so it carries no exactly-once pin — and needs none.
    assert intake_id is None


def test_two_directly_typed_reminders_both_stand(client, fake_email):
    token = _token(client, fake_email)
    client.post("/reminders", json={"say": "one", "when": "+1h"}, headers=_auth(token))
    client.post("/reminders", json={"say": "two", "when": "+2h"}, headers=_auth(token))
    # Both carry a null intake_id, and Postgres treats nulls as distinct under the UNIQUE index —
    # so the exactly-once pin guards a retried message without capping the directly-made reminders at one.
    assert [r["body"] for r in _reminders(client, token)] == ["one", "two"]


def test_the_listing_is_soonest_first_and_carries_real_ids(client, fake_email):
    token = _token(client, fake_email)
    client.post("/reminders", json={"say": "later", "when": "+3h"}, headers=_auth(token))
    client.post("/reminders", json={"say": "sooner", "when": "+1h"}, headers=_auth(token))
    listed = _reminders(client, token)
    assert [r["body"] for r in listed] == ["sooner", "later"]
    with db.get_pool().connection() as conn:
        ids = {r[0] for r in conn.execute("SELECT id FROM reminder").fetchall()}
    # The shell prints positions and holds these; a write names the id, never the position.
    assert {r["id"] for r in listed} == ids
    assert all(r["state"] == "pending" for r in listed)


def test_a_time_outside_the_grammar_is_refused_rather_than_guessed_at(client, fake_email):
    token = _token(client, fake_email)
    body = client.post(
        "/reminders", json={"say": "call the dentist", "when": "sometime next week-ish"}, headers=_auth(token)
    ).json()
    # Deterministic or nothing: this path spends no model call, so an unreadable time is a flat refusal.
    assert body["msg"] == "that reminder change didn't take"
    assert "couldn't read" in body["data"]["reason"]
    assert body["data"]["reminders"] == []


def test_a_past_time_is_refused(client, fake_email):
    token = _token(client, fake_email)
    yesterday = (zone.now_for("UTC") - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    body = client.post(
        "/reminders", json={"say": "call the dentist", "when": yesterday}, headers=_auth(token)
    ).json()
    # A reminder is for the future, so a past moment means it was typed wrong.
    assert body["msg"] == "that reminder change didn't take"
    assert body["data"]["reason"] == "that time has already passed"


def test_cancelling_stamps_the_row_rather_than_deleting_it(client, fake_email):
    token = _token(client, fake_email)
    client.post("/reminders", json={"say": "call the dentist", "when": "+2h"}, headers=_auth(token))
    reminder_id = _reminders(client, token)[0]["id"]
    body = client.post("/reminders/cancel", json={"id": reminder_id}, headers=_auth(token)).json()
    assert body["msg"] == "reminders"
    # Recorded, not dropped: the symbiot sees they called it off rather than finding a gap.
    assert [(r["id"], r["state"]) for r in body["data"]["reminders"]] == [(reminder_id, "cancelled")]
    with db.get_pool().connection() as conn:
        cancelled_at = conn.execute("SELECT cancelled_at FROM reminder WHERE id = %s", (reminder_id,)).fetchone()[0]
    assert cancelled_at is not None


def test_a_cancelled_reminder_never_fires(client, fake_email):
    token = _token(client, fake_email)
    client.post("/reminders", json={"say": "call the dentist", "when": "+1h"}, headers=_auth(token))
    reminder_id = _reminders(client, token)[0]["id"]
    client.post("/reminders/cancel", json={"id": reminder_id}, headers=_auth(token))
    with db.get_pool().connection() as conn:
        # Backdate it past due: were the sweep's claim blind to the cancel, this is where it would fire.
        conn.execute("UPDATE reminder SET fire_at = now() - interval '1 minute' WHERE id = %s", (reminder_id,))
        with conn.transaction():
            assert reminder.claim_due(conn) is None


def test_cancelling_twice_is_refused_legibly(client, fake_email):
    token = _token(client, fake_email)
    client.post("/reminders", json={"say": "call the dentist", "when": "+2h"}, headers=_auth(token))
    reminder_id = _reminders(client, token)[0]["id"]
    client.post("/reminders/cancel", json={"id": reminder_id}, headers=_auth(token))
    body = client.post("/reminders/cancel", json={"id": reminder_id}, headers=_auth(token)).json()
    assert body["msg"] == "that reminder change didn't take"
    assert body["data"]["reason"] == f"reminder {reminder_id} was already called off"


def test_a_fired_reminder_cannot_be_cancelled_or_edited(client, fake_email):
    token = _token(client, fake_email)
    client.post("/reminders", json={"say": "call the dentist", "when": "+2h"}, headers=_auth(token))
    reminder_id = _reminders(client, token)[0]["id"]
    with db.get_pool().connection() as conn:
        reminder.mark_fired(conn, reminder_id)
    # The ordinary case, not a corner one: a list printed minutes ago can name a reminder that has since fired.
    # The three conditions live in the statement, so the write simply doesn't land — and says which of them it was.
    for route, payload in (("/reminders/cancel", {}), ("/reminders/update", {"when": "+3h"})):
        body = client.post(route, json={"id": reminder_id, **payload}, headers=_auth(token)).json()
        assert body["msg"] == "that reminder change didn't take"
        assert body["data"]["reason"] == f"reminder {reminder_id} has already fired"


def test_editing_moves_the_time_the_line_or_both(client, fake_email):
    token = _token(client, fake_email)
    client.post("/reminders", json={"say": "call the dentist", "when": "+2h"}, headers=_auth(token))
    reminder_id = _reminders(client, token)[0]["id"]
    with db.get_pool().connection() as conn:
        before = conn.execute("SELECT fire_at FROM reminder WHERE id = %s", (reminder_id,)).fetchone()[0]
    # A time alone leaves the line where it was.
    client.post("/reminders/update", json={"id": reminder_id, "when": "+3d"}, headers=_auth(token))
    with db.get_pool().connection() as conn:
        body, moved = conn.execute(
            "SELECT body, fire_at FROM reminder WHERE id = %s", (reminder_id,)
        ).fetchone()
    assert body == "call the dentist"
    assert moved > before
    # A line alone leaves the time where it was.
    client.post(
        "/reminders/update", json={"id": reminder_id, "say": "call the dentist about the referral"}, headers=_auth(token)
    )
    with db.get_pool().connection() as conn:
        body, still = conn.execute(
            "SELECT body, fire_at FROM reminder WHERE id = %s", (reminder_id,)
        ).fetchone()
    assert body == "call the dentist about the referral"
    assert still == moved


def test_an_edit_naming_nothing_is_refused(client, fake_email):
    token = _token(client, fake_email)
    client.post("/reminders", json={"say": "call the dentist", "when": "+2h"}, headers=_auth(token))
    reminder_id = _reminders(client, token)[0]["id"]
    body = client.post("/reminders/update", json={"id": reminder_id}, headers=_auth(token)).json()
    # Nothing to change is refused rather than reported as a change that landed.
    assert body["msg"] == "that reminder change didn't take"
    assert "nothing to change" in body["data"]["reason"]


def test_an_edit_applied_twice_lands_in_the_same_place(client, fake_email):
    token = _token(client, fake_email)
    client.post("/reminders", json={"say": "call the dentist", "when": "+2h"}, headers=_auth(token))
    reminder_id = _reminders(client, token)[0]["id"]
    target = (zone.now_for("UTC") + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    client.post("/reminders/update", json={"id": reminder_id, "when": target}, headers=_auth(token))
    with db.get_pool().connection() as conn:
        once = conn.execute("SELECT fire_at FROM reminder WHERE id = %s", (reminder_id,)).fetchone()[0]
    client.post("/reminders/update", json={"id": reminder_id, "when": target}, headers=_auth(token))
    with db.get_pool().connection() as conn:
        twice = conn.execute("SELECT fire_at FROM reminder WHERE id = %s", (reminder_id,)).fetchone()[0]
    # Absolute values, never deltas — which is what makes a retried edit harmless with no second pin.
    assert once == twice


def test_a_reminder_that_is_not_the_callers_is_refused_as_absent(client, fake_email):
    token = _token(client, fake_email)
    with db.get_pool().connection() as conn:
        other = conn.execute(
            "INSERT INTO symbiot (email) VALUES ('elsewhere@example.com') RETURNING id"
        ).fetchone()[0]
        theirs = reminder.create(
            conn, other, "not yours", zone.now_for("UTC") + timedelta(hours=1)
        )
    body = client.post("/reminders/cancel", json={"id": theirs}, headers=_auth(token)).json()
    # Every read and write is scoped on symbiot_id, so an id outside the caller's own set resolves to nothing at all.
    assert body["msg"] == "that reminder change didn't take"
    assert body["data"]["reason"] == f"no reminder {theirs}"


def test_the_settled_tail_follows_the_live_ones(client, fake_email):
    token = _token(client, fake_email)
    client.post("/reminders", json={"say": "spent", "when": "+1h"}, headers=_auth(token))
    spent_id = _reminders(client, token)[0]["id"]
    with db.get_pool().connection() as conn:
        reminder.mark_fired(conn, spent_id)
    client.post("/reminders", json={"say": "still standing", "when": "+2h"}, headers=_auth(token))
    listed = _reminders(client, token)
    # The live ones lead — that is what the symbiot is holding;
    # the settled tail is there so a fired reminder reads as fired rather than as a gap where it used to be.
    assert [(r["body"], r["state"]) for r in listed] == [("still standing", "pending"), ("spent", "fired")]
