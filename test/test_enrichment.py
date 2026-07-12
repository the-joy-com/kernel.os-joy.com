"""Tier 2 enrichment: the deep second pass, its eligibility, its gate, and its exactly-once.

The pass's own job is eligibility, the claim, the origin reference, the surface-or-not decision, and the record —
not the deep reach's vectors (deep_retrieval has the live smoke for those) nor the model's judgement (that is the gate's call).
So deep_retrieval.deep_search and the model behind enrichment.compose are stubbed to deterministic stand-ins,
and the assertions are about which messages are eligible, that a surfaced pass sends exactly one missive and records it,
that a suppressed pass records itself too so it is never reconsidered, and that a held claim makes a second sweep skip.
"""

from datetime import datetime, timezone

from core import db
from services import conversation
from services import deep_retrieval
from services import enrichment
from services import worker

SEEDED_SYMBIOT_ID = 1  # conftest re-seeds exactly one symbiot with RESTART IDENTITY, so it's always id 1


def _intake(message="a message", answer="an answer", *, symbiot_id=SEEDED_SYMBIOT_ID, status="answered") -> int:
    # Land an intake row directly in the state a test needs — the message lifecycle has its own tests,
    # so this skips the walk and sets the terminal state the enrichment sweep reads.
    with db.get_pool().connection() as conn:
        return conn.execute(
            "INSERT INTO intake (message, answer, symbiot_id, status) VALUES (%s, %s, %s, %s) RETURNING id",
            (message, answer, symbiot_id, status),
        ).fetchone()[0]


def _item(role, *, intake_id=None, missive_id=None, symbiot_id=SEEDED_SYMBIOT_ID) -> int:
    # A conversation_item pointing at where its words live (an intake row or a missive), told apart by role.
    # token_count is arbitrary here — the origin reference reads text and order, not the count.
    with db.get_pool().connection() as conn:
        return conn.execute(
            "INSERT INTO conversation_item (symbiot_id, role, token_count, intake_id, missive_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (symbiot_id, role, 1, intake_id, missive_id),
        ).fetchone()[0]


def _enrichments():
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT intake_id, surfaced, missive_id FROM enrichment "
            "WHERE symbiot_id = %s ORDER BY id",
            (SEEDED_SYMBIOT_ID,),
        ).fetchall()


def _missives():
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT id, body FROM missive WHERE symbiot_id = %s ORDER BY id",
            (SEEDED_SYMBIOT_ID,),
        ).fetchall()


def _one_related():
    # A single stand-in deep-reach hit — enough that compose isn't short-circuited on an empty reach.
    return [
        deep_retrieval.Related(
            id=42, raw_text="a related fact",
            effective_at=datetime(2026, 1, 1, tzinfo=timezone.utc), distance=0.1,
        )
    ]


def _stub_reach(monkeypatch, related, decision):
    # Stand in for the two model-bearing halves of the pass:
    # the deep reach returns a fixed list, and the gate-and-compose returns a fixed (surface, message).
    # So the orchestration — eligibility, claim, delivery, record — is exercised without vectors or a model call.
    monkeypatch.setattr(worker.deep_retrieval, "deep_search", lambda conn, message, **kw: related)
    monkeypatch.setattr(worker.enrichment, "compose", lambda origin, rel: decision)


def test_enrich_sends_a_follow_up_when_the_pass_surfaces(client, monkeypatch):
    # The plain surface case: the gate says yes, so a missive is sent, mirrored onto the stream, and recorded.
    _stub_reach(monkeypatch, _one_related(), (True, "one more thing — the weather app"))
    intake_id = _intake()

    assert worker._enrich_one() is True
    assert _missives() == [(1, "one more thing — the weather app")]
    assert _enrichments() == [(intake_id, True, 1)]  # surfaced, pointing at the missive
    # The follow-up joined the conversation stream as a machine turn pointing at that missive.
    with db.get_pool().connection() as conn:
        streamed = conn.execute(
            "SELECT count(*) FROM conversation_item WHERE missive_id = 1 AND role = 'machine'"
        ).fetchone()[0]
    assert streamed == 1


def test_enrich_records_a_suppressed_pass_without_a_missive(client, monkeypatch):
    # The gate says no: nothing is sent, but the pass is still recorded so it is never reconsidered.
    _stub_reach(monkeypatch, _one_related(), (False, ""))
    intake_id = _intake()

    assert worker._enrich_one() is True
    assert _missives() == []
    assert _enrichments() == [(intake_id, False, None)]  # considered, suppressed, no missive


def test_enrich_skips_an_anonymous_message(client, monkeypatch):
    # Enrichment reaches the symbiot's own diary; a message with no symbiot is never enriched.
    _stub_reach(monkeypatch, _one_related(), (True, "should never send"))
    _intake(symbiot_id=None)

    assert worker._enrich_one() is False
    assert _missives() == []
    assert _enrichments() == []


def test_enrich_skips_a_message_that_never_got_an_answer(client, monkeypatch):
    # Enrichment enriches a fast *answer*, so it waits for 'answered' specifically —
    # narrower than ingestion, which also files an 'abandoned' message that never got one.
    _stub_reach(monkeypatch, _one_related(), (True, "should never send"))
    _intake(status="abandoned")
    _intake(status="working")
    _intake(status="received")

    assert worker._enrich_one() is False
    assert _missives() == []
    assert _enrichments() == []


def test_enrich_considers_each_message_exactly_once(client, monkeypatch):
    # A message passed is excluded from the next round — the sweep never enriches it twice.
    _stub_reach(monkeypatch, _one_related(), (True, "the one follow-up"))
    intake_id = _intake()

    assert worker._enrich_one() is True
    assert worker._enrich_one() is False  # nothing left eligible (an enrichment row now bears its id)
    assert _missives() == [(1, "the one follow-up")]
    assert _enrichments() == [(intake_id, True, 1)]


def test_enrich_takes_the_oldest_first(client, monkeypatch):
    sent = []
    monkeypatch.setattr(worker.deep_retrieval, "deep_search", lambda conn, message, **kw: _one_related())
    # Record which message's text the compose saw, so we can prove the older one went first.
    monkeypatch.setattr(worker.enrichment, "compose", lambda origin, rel: (sent.append(origin.message), (False, ""))[1])
    _intake(message="older")
    _intake(message="newer")

    worker._enrich_one()

    assert sent == ["older"]


def test_enrich_skips_a_message_whose_pass_another_worker_holds(client, monkeypatch):
    # The race guard, proven end to end: while one worker holds a message's pass, a second sweep must claim nothing,
    # send nothing, and record nothing — then, once the claim is let go, the pass goes through as normal.
    _stub_reach(monkeypatch, _one_related(), (True, "the follow-up"))
    intake_id = _intake()

    with db.get_pool().connection() as holder:
        with holder.transaction():
            assert enrichment.claim(holder, intake_id) is True

            assert worker._enrich_one() is False  # couldn't claim — skipped this pass
            assert _missives() == []               # nothing sent
            assert _enrichments() == []            # nothing recorded

    # The holder's transaction has closed, releasing the lock; the pass now goes through.
    assert worker._enrich_one() is True
    assert len(_missives()) == 1
    assert len(_enrichments()) == 1


def test_enrich_idle_when_nothing_eligible(client, monkeypatch):
    _stub_reach(monkeypatch, _one_related(), (True, "should never send"))

    assert worker._enrich_one() is False  # nothing to enrich, says so
    assert _missives() == []


def test_compose_suppresses_without_a_model_call_when_the_reach_found_nothing(client, monkeypatch):
    # No deep facts means no new ground to weigh, so the gate suppresses before spending the metered model call.
    called = []
    monkeypatch.setattr(enrichment.llm, "generate_json", lambda *a, **k: called.append(1))
    origin = enrichment.Origin(message="m", answer="a", since=[])

    assert enrichment.compose(origin, []) == (False, "")
    assert called == []  # the model was never reached


def test_compose_surfaces_when_the_model_says_so(client, monkeypatch):
    monkeypatch.setattr(enrichment.persona, "load", lambda: "VOICE")
    monkeypatch.setattr(
        enrichment.llm, "generate_json",
        lambda *a, **k: enrichment._EnrichmentReply(surface=True, message="  the follow-up  "),
    )
    origin = enrichment.Origin(message="m", answer="a", since=[])

    assert enrichment.compose(origin, _one_related()) == (True, "the follow-up")  # trimmed


def test_compose_downgrades_an_empty_surface_to_a_suppress(client, monkeypatch):
    # A model that flags "yes" but writes nothing has, in substance, nothing to add — never deliver an empty missive.
    monkeypatch.setattr(enrichment.persona, "load", lambda: "VOICE")
    monkeypatch.setattr(
        enrichment.llm, "generate_json",
        lambda *a, **k: enrichment._EnrichmentReply(surface=True, message="   "),
    )
    origin = enrichment.Origin(message="m", answer="a", since=[])

    assert enrichment.compose(origin, _one_related()) == (False, "")


def test_compose_suppresses_when_the_model_declines(client, monkeypatch):
    monkeypatch.setattr(enrichment.persona, "load", lambda: "VOICE")
    monkeypatch.setattr(
        enrichment.llm, "generate_json",
        lambda *a, **k: enrichment._EnrichmentReply(surface=False, message="ignored"),
    )
    origin = enrichment.Origin(message="m", answer="a", since=[])

    assert enrichment.compose(origin, _one_related()) == (False, "")


def test_origin_reference_gathers_the_turns_since_the_answer(client):
    # The three legs of the origin reference: the prompting message, its fast answer, and everything said since.
    # Seed the exchange being enriched (M, both its stream turns), then two later turns and a missive —
    # the "since" must be exactly the three later utterances, in order, and never M's own two.
    m = _intake(message="which project?", answer="the weather app")
    _item("symbiot", intake_id=m)   # M's own turns — must be excluded from "since"
    _item("machine", intake_id=m)
    later = _intake(message="thanks", answer="anytime")
    _item("symbiot", intake_id=later)
    _item("machine", intake_id=later)
    with db.get_pool().connection() as conn:
        missive_id = conn.execute(
            "INSERT INTO missive (symbiot_id, body) VALUES (%s, %s) RETURNING id",
            (SEEDED_SYMBIOT_ID, "a nudge"),
        ).fetchone()[0]
    _item("machine", missive_id=missive_id)

    with db.get_pool().connection() as conn:
        origin = enrichment.origin_reference(conn, SEEDED_SYMBIOT_ID, m, "which project?", "the weather app")

    assert origin.message == "which project?"
    assert origin.answer == "the weather app"
    assert origin.since == [
        conversation.Turn("symbiot", "thanks"),
        conversation.Turn("machine", "anytime"),
        conversation.Turn("machine", "a nudge"),
    ]
