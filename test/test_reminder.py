"""The reminder tool: the executor that stores it (exactly once, or asks when unclear), and the due firing.

No model or embedding is reached here — the executor and the firing are pure store-and-deliver —
so these run end to end against the test database.
The live decision that fills the arguments, and the whole retrieve → decide → act → speak fork,
are the by-hand smoke's to prove (test/qa/0007).
"""

from datetime import datetime, timezone

from core import db
from services.tools import reminder
from services.tools import tools
from services.loop import worker

SEEDED_SYMBIOT_ID = 1  # conftest re-seeds exactly one symbiot with RESTART IDENTITY, so it's always id 1


def _intake(message: str = "remind me to call the dentist") -> int:
    # A settled intake row for the reminder to hang off — the reminder's intake_id references it.
    with db.get_pool().connection() as conn:
        return conn.execute(
            "INSERT INTO intake (message, symbiot_id, status) VALUES (%s, %s, 'answered') RETURNING id",
            (message, SEEDED_SYMBIOT_ID),
        ).fetchone()[0]


def _seed_reminder(body: str, fire_sql: str, channels=None) -> int:
    # A reminder due (or not) per fire_sql, unfired — the row the firing sweep reads.
    # channels null (the default) is "the symbiot named none", which fires over the tool's whole supported set.
    intake_id = _intake()
    with db.get_pool().connection() as conn:
        return conn.execute(
            f"INSERT INTO reminder (intake_id, symbiot_id, body, fire_at, channels) "
            f"VALUES (%s, %s, %s, {fire_sql}, %s) RETURNING id",
            (intake_id, SEEDED_SYMBIOT_ID, body, channels),
        ).fetchone()[0]


def test_executor_stores_a_reminder_exactly_once_against_the_message(client):
    intake_id = _intake()
    fire_at = datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc)
    decision = tools.Decision("schedule_reminder", {"reminder_message": "call the dentist", "fire_at": fire_at})
    now = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
    with db.get_pool().connection() as conn:
        first = tools.execute(conn, decision, SEEDED_SYMBIOT_ID, intake_id, now, "Europe/Paris")
        second = tools.execute(conn, decision, SEEDED_SYMBIOT_ID, intake_id, now, "Europe/Paris")
    assert first.effected and second.effected
    with db.get_pool().connection() as conn:
        count = conn.execute("SELECT count(*) FROM reminder WHERE intake_id = %s", (intake_id,)).fetchone()[0]
    # A retried message re-runs the executor; the UNIQUE intake_id makes the second write a no-op.
    assert count == 1


def test_executor_asks_rather_than_guesses_when_the_time_is_unclear(client):
    intake_id = _intake()
    decision = tools.Decision("schedule_reminder", {"reminder_message": "call the dentist", "fire_at": None})
    now = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
    with db.get_pool().connection() as conn:
        result = tools.execute(conn, decision, SEEDED_SYMBIOT_ID, intake_id, now, "UTC")
        stored = conn.execute("SELECT count(*) FROM reminder WHERE intake_id = %s", (intake_id,)).fetchone()[0]
    # No clear time: nothing is stored, and the result asks rather than confirming (the reactive-ambiguity law).
    assert result.effected is False
    assert stored == 0


def test_claim_due_takes_a_reminder_whose_moment_has_come_and_leaves_a_future_one(client):
    _seed_reminder("the past one", "now() - interval '1 minute'")
    _seed_reminder("the future one", "now() + interval '1 hour'")
    with db.get_pool().connection() as conn:
        with conn.transaction():
            due = reminder.claim_due(conn)
    assert due is not None
    assert due[2] == "the past one"  # only the due one, not the future one


def test_fire_one_delivers_a_due_reminder_as_a_missive_exactly_once(client):
    reminder_id = _seed_reminder("call the dentist", "now() - interval '1 minute'")

    assert worker._fire_one() is True
    with db.get_pool().connection() as conn:
        fired_at = conn.execute("SELECT fired_at FROM reminder WHERE id = %s", (reminder_id,)).fetchone()[0]
        missives = conn.execute(
            "SELECT body FROM missive WHERE symbiot_id = %s", (SEEDED_SYMBIOT_ID,)
        ).fetchall()
        items = conn.execute(
            "SELECT count(*) FROM conversation_item WHERE symbiot_id = %s AND role = 'machine'",
            (SEEDED_SYMBIOT_ID,),
        ).fetchone()[0]
    # The reminder fired: stamped delivered, raised as a missive, and mirrored onto the conversation stream.
    assert fired_at is not None
    assert [m[0] for m in missives] == ["call the dentist"]
    assert items == 1

    # Nothing due now — the fired reminder is not delivered a second time.
    assert worker._fire_one() is False
    with db.get_pool().connection() as conn:
        missives = conn.execute(
            "SELECT count(*) FROM missive WHERE symbiot_id = %s", (SEEDED_SYMBIOT_ID,)
        ).fetchone()[0]
    assert missives == 1


def test_executor_stores_the_channels_the_symbiot_named(client):
    intake_id = _intake()
    fire_at = datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc)
    decision = tools.Decision(
        "schedule_reminder",
        {"reminder_message": "call the dentist", "fire_at": fire_at, "channels": ["email"]},
    )
    now = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
    with db.get_pool().connection() as conn:
        tools.execute(conn, decision, SEEDED_SYMBIOT_ID, intake_id, now, "Europe/Paris")
        stored = conn.execute(
            "SELECT channels FROM reminder WHERE intake_id = %s", (intake_id,)
        ).fetchone()[0]
    # Narrowed to the one they asked for; the firing sweep will fan over exactly this.
    assert stored == ["email"]


def test_fire_one_fans_over_the_channels_the_reminder_stored(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        worker.notify, "dispatch",
        lambda pool, sid, notification, channels: captured.update(channels=channels, body=notification.body),
    )
    _seed_reminder("call the dentist", "now() - interval '1 minute'", ["email"])
    assert worker._fire_one() is True
    assert captured["channels"] == ["email"]  # only the channel the symbiot named
    assert captured["body"] == "call the dentist"


def test_fire_one_fans_over_all_supported_channels_when_none_were_named(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        worker.notify, "dispatch",
        lambda pool, sid, notification, channels: captured.update(channels=channels),
    )
    _seed_reminder("call the dentist", "now() - interval '1 minute'")  # channels null — none named
    assert worker._fire_one() is True
    # No narrowing means the whole set the tool supports.
    assert captured["channels"] == list(reminder.SUPPORTED_CHANNELS)
