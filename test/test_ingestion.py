"""Live diary ingestion: the sweep that files settled messages into the diary.

The sweep's own job is eligibility and exactly-once, not the write path's internals (ontology.ingest has its
own tests), so here ontology.ingest is stubbed to a minimal fact insert and the assertions are about which
messages get filed, and that each is filed once. The stub carries the intake_id through, so the database's
uniqueness — the real exactly-once guarantee — is exercised rather than mocked away.
"""

import json

from core import db
from services.loop import worker

SEEDED_SYMBIOT_ID = 1  # conftest re-seeds exactly one symbiot with RESTART IDENTITY, so it's always id 1


def _insert(message="a message", *, symbiot_id=SEEDED_SYMBIOT_ID, status="answered") -> int:
    # Land an intake row directly in the state a test needs — the message lifecycle has its own tests,
    # so this skips the walk and sets the terminal state the ingestion sweep reads.
    with db.get_pool().connection() as conn:
        return conn.execute(
            "INSERT INTO intake (message, symbiot_id, status) VALUES (%s, %s, %s) RETURNING id",
            (message, symbiot_id, status),
        ).fetchone()[0]


def _stub_ingest(monkeypatch, calls):
    # Stand in for the write path: record the call and file a minimal fact carrying the intake_id,
    # so eligibility (which mirrors the fact's intake_id) and the UNIQUE constraint both behave as in production.
    def fake(conn, raw_text, *, intake_id=None):
        calls.append((raw_text, intake_id))
        conn.execute(
            "INSERT INTO diary_facts (raw_text, payload, intake_id) VALUES (%s, %s::jsonb, %s)",
            (raw_text, json.dumps({"@type": [], "text": raw_text}), intake_id),
        )
        return None

    monkeypatch.setattr(worker.ontology, "ingest", fake)


def _fact_count_for(intake_id) -> int:
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT count(*) FROM diary_facts WHERE intake_id = %s", (intake_id,)
        ).fetchone()[0]


def test_ingest_files_an_authed_settled_message(client, monkeypatch):
    # The plain case: an answered message from the symbiot is filed, once, carrying its id.
    calls = []
    _stub_ingest(monkeypatch, calls)
    message_id = _insert("did boxing today", status="answered")

    assert worker._ingest_one() is True
    assert calls == [("did boxing today", message_id)]
    assert _fact_count_for(message_id) == 1


def test_ingest_skips_an_anonymous_message(client, monkeypatch):
    # The diary is the symbiot's; a message with no symbiot is never distilled into it.
    calls = []
    _stub_ingest(monkeypatch, calls)
    _insert("a stranger's line", symbiot_id=None, status="answered")

    assert worker._ingest_one() is False
    assert calls == []


def test_ingest_skips_a_non_terminal_message(client, monkeypatch):
    # Filing waits until the reply is done, so a message never lands in its own reply's retrieval context.
    calls = []
    _stub_ingest(monkeypatch, calls)
    _insert("still being worked", status="working")

    assert worker._ingest_one() is False
    assert calls == []


def test_ingest_files_an_abandoned_message_too(client, monkeypatch):
    # A message whose reply was given up on is still the symbiot's words, so it still joins the diary.
    calls = []
    _stub_ingest(monkeypatch, calls)
    message_id = _insert("something the kernel never answered", status="abandoned")

    assert worker._ingest_one() is True
    assert _fact_count_for(message_id) == 1


def test_ingest_files_each_message_exactly_once(client, monkeypatch):
    # A message filed is excluded from the next pass — the sweep never files it twice.
    calls = []
    _stub_ingest(monkeypatch, calls)
    message_id = _insert("a lunch with a friend", status="answered")

    assert worker._ingest_one() is True
    assert worker._ingest_one() is False  # nothing left eligible
    assert calls == [("a lunch with a friend", message_id)]
    assert _fact_count_for(message_id) == 1


def test_ingest_takes_the_oldest_first(client, monkeypatch):
    calls = []
    _stub_ingest(monkeypatch, calls)
    older = _insert("older", status="answered")
    _insert("newer", status="answered")

    worker._ingest_one()

    assert calls[0] == ("older", older)  # the older one went first


def test_ingest_idle_when_nothing_eligible(client, monkeypatch):
    calls = []
    _stub_ingest(monkeypatch, calls)

    assert worker._ingest_one() is False  # nothing to file, says so
    assert calls == []
