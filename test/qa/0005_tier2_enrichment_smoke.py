"""By-hand smoke test: Tier 2 — the deep second pass and the enriched follow-up — against live models.

The pytest suite (test/test_enrichment.py, test/test_deep_retrieval.py) proves the eligibility, the claim, the origin reference,
the gate, and the ontology walk with the models faked.
It never proves the two things only a live run can:
that real embeddings make the *meaning-based* reach surface facts the *lexical* reach cannot,
and that the heavy model, handed those facts and its own earlier answer, writes a follow-up worth the interruption (or rightly stays silent).
This script is that other half.

It stages the situation Tier 2 exists for.
A short diary is filed through the real write path (ontology.ingest, so each fact earns a real embedding and real ontology links),
full of entries that circle one theme — being depleted, running on empty —
but worded so they share almost no words with the message to come.
Then a message lands that is about that same theme in entirely different words,
with a deliberately bland fast answer that missed the connection.
The run then shows:

  1. the contrast at the heart of Tier 2 — the fast lexical reach (retrieval.search) surfaces little or none of the themed facts,
     because they share no words with the message, while the deep reach (deep_retrieval.deep_search) surfaces them by meaning,
     and walks out along the ontology to their siblings;
  2. the enriched follow-up, composed live (enrichment.compose) from the deep facts and the origin reference —
     the message, the fast answer it must not repeat, and the (here empty) turns since —
     for a human to judge whether it connects the dots without restating the first answer;
  3. the delivery, mirrored exactly on worker._enrich_one:
     a surfaced follow-up is raised as a missive and recorded, the pass marked done so it is never re-run.

It is direct-run, not a pytest test, because it needs the live box:

    python test/qa/0005_tier2_enrichment_smoke.py            # rolls back at the end (default)
    python test/qa/0005_tier2_enrichment_smoke.py --keep     # commits, so you can inspect the rows

Prerequisites (see README, "Models" and "Database & migrations"):
  - Ollama serving the embedding model (`nomic-embed-text`) on the box — the deep reach embeds the message and the facts.
  - A generative provider for ingestion's routing and the follow-up: `SCALEWAY_API_KEY` for the primary (`glm-5.2`),
    or the ladder falls back to Mistral, then to the local `qwen3.5:4b`.
  - A reachable Postgres — this connects to config.DATABASE_URL (your dev database), migrated to 0015.

Everything is wrapped in one transaction, rolled back at the end by default,
so the dev store is left exactly as found while the live reach and follow-up are still proven end to end.
Pass --keep to commit and browse the rows.
"""

import argparse
import os
import sys

# Direct-run from anywhere: put the repo root on the path so `core`/`services` import cleanly.
# This file sits at test/qa/, so the repo root is three directories up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import psycopg

from core import config
from core import db
from services.memory import conversation
from services.memory import deep_retrieval
from services.memory import enrichment
from services.loop import missive
from services.memory import ontology
from services.memory import retrieval

# The themed diary: every entry circles being depleted / running on empty, but none uses the message's words —
# so the lexical reach, seeing no shared term, should mostly miss them, and only the meaning-based reach should surface them.
# Filed through the real write path, so each earns an embedding and ontology links the deep reach can read and walk.
THEMED = [
    "Stayed at the office past midnight again to get the release out.",
    "Skipped the gym all week — nothing left in the tank after work.",
    "Haven't slept a full night since the project kicked off.",
    "Snapped at a colleague over nothing today, then felt awful about it.",
]
# Plausible noise on other themes, so the deep reach is shown discriminating, not just returning everything.
NOISE = [
    "Baked a sourdough loaf on Sunday and it actually rose this time.",
    "Watched a slow, lovely documentary about the deep sea.",
]

# The message: the same theme — being worn down — in words the themed facts never use, so lexical overlap is near zero.
# Its fast answer is deliberately bland and diary-blind, the kind Tier 1 gives when the lexical reach came back thin —
# exactly the gap the deep pass is here to close.
MESSAGE = "I don't get it, I just can't seem to recharge no matter what I try."
FAST_ANSWER = "That sounds draining. Do you want to talk through what's been weighing on you?"


def _seed_symbiot(conn) -> int:
    # A dedicated smoke symbiot, so the run stands alone and doesn't lean on the seeded one; rolled back anyway.
    return conn.execute(
        "INSERT INTO symbiot (email) VALUES ('smoke-tier2@example.test') RETURNING id"
    ).fetchone()[0]


def _themed(texts) -> set:
    # The set of themed facts present in a list of results — the ones the deep reach should surface and the lexical shouldn't.
    return {t for t in texts if t in THEMED}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true",
        help="commit the writes instead of rolling back, so the rows can be inspected afterwards",
    )
    args = parser.parse_args()

    print(f"database  : {config.DATABASE_URL}")
    print(f"enrich    : {config.ENRICH_MODEL}  (the heavy hitter, same model as the reply)")
    print(f"embedding : {config.EMBEDDING_MODEL}")
    print(f"ollama    : {config.OLLAMA_BASE_URL}")

    pool = db.open_pool(config.DATABASE_URL)
    db.run_migrations(pool)  # idempotent — brings the dev database to 0015 if it isn't already

    with pool.connection() as conn:
        with conn.transaction():
            symbiot_id = _seed_symbiot(conn)

            # --- file the themed diary through the real write path ----------------------------
            print(f"\n=== filing {len(THEMED) + len(NOISE)} facts through the write path (live routing + embeddings) ===")
            for raw_text in THEMED + NOISE:
                ontology.ingest(conn, raw_text)
                print(f"  filed: {raw_text!r}")

            # The answered message being enriched — landed directly in its terminal state, its own fact left unfiled,
            # so the deep reach can be told to exclude it (a message must never enrich itself).
            intake_id = conn.execute(
                "INSERT INTO intake (message, answer, symbiot_id, status) "
                "VALUES (%s, %s, %s, 'answered') RETURNING id",
                (MESSAGE, FAST_ANSWER, symbiot_id),
            ).fetchone()[0]

            # --- 1. the contrast: lexical misses, meaning finds -------------------------------
            lexical = [f.raw_text for f in retrieval.search(conn, MESSAGE)]
            deep = deep_retrieval.deep_search(conn, MESSAGE, exclude_intake_ids=[intake_id])
            deep_texts = [r.raw_text for r in deep]

            print(f"\n=== the message ===\n  {MESSAGE!r}")
            print(f"\n=== Tier 1, lexical (retrieval.search) — by shared words ===")
            print(f"  themed facts surfaced: {sorted(_themed(lexical))}")
            print(f"  (all hits: {lexical})")
            print(f"\n=== Tier 2, deep (deep_retrieval.deep_search) — by meaning, then the ontology walk ===")
            for r in deep:
                how = f"recall d={r.distance:.3f}" if r.distance is not None else "ontology walk"
                marker = "  * themed" if r.raw_text in THEMED else ""
                print(f"  [{how:>14}] {r.raw_text!r}{marker}")

            deep_themed = _themed(deep_texts)
            lexical_themed = _themed(lexical)
            assert deep, "the deep reach surfaced nothing at all — the diary should hold facts near this message by meaning"
            assert deep_themed, "the deep reach surfaced no themed fact — the meaning-based recall isn't reaching the theme"
            newly_found = deep_themed - lexical_themed
            print(f"\n  themed facts the deep reach surfaced that the lexical reach missed: {sorted(newly_found)}")
            assert newly_found, (
                "the deep reach surfaced no themed fact the lexical reach hadn't already found — "
                "the whole point of Tier 2 is the facts recency and shared words can't reach"
            )
            walked = [r.raw_text for r in deep if r.distance is None]
            print(f"  facts reached through the ontology walk (not vector recall): {walked}")
            print("  ✓ the meaning-based reach surfaced themed facts the lexical reach could not")

            # --- 2. the enriched follow-up, composed live -------------------------------------
            # A single-message smoke, so the burst is this one message: exclude just its own id, and its message/answer are the whole legs.
            origin = enrichment.origin_reference(conn, symbiot_id, [intake_id], MESSAGE, FAST_ANSWER)
            surface, follow_up = enrichment.compose(origin, deep)
            print(f"\n=== the gate-and-compose (live {config.ENRICH_MODEL}) ===")
            print(f"  fast answer it must not repeat : {FAST_ANSWER!r}")
            print(f"  surface? {surface}")
            print(f"  follow-up: {follow_up!r}")
            if surface:
                assert follow_up.strip(), "surfaced but composed an empty follow-up"
                print("\n  ✓ a follow-up composed off the deep reach — read it above to judge whether it connects the dots "
                      "without restating the first answer")
            else:
                print("\n  the gate chose silence — read the deep facts above to judge whether that was right "
                      "(a fair verdict if they add nothing the fast answer didn't)")

            # --- 3. delivery, mirroring worker._enrich_one ------------------------------------
            missive_id = None
            if surface:
                missive_id = missive.raise_for(conn, symbiot_id, follow_up)
                conversation.record_utterance(conn, symbiot_id, "machine", follow_up, missive_id=missive_id)
            enrichment.record(conn, intake_id, symbiot_id, missive_id)
            recorded = conn.execute(
                "SELECT surfaced, missive_id FROM enrichment WHERE intake_id = %s", (intake_id,)
            ).fetchone()
            print(f"\n=== the record ===")
            print(f"  enrichment row: surfaced={recorded[0]}, missive_id={recorded[1]}")
            assert recorded[0] == (missive_id is not None), "the recorded verdict disagrees with what was sent"
            # The exactly-once mirror: the message now bears an enrichment row, so the sweep would never re-run it.
            # Read at settle 0, where each message is its own immediately-settled burst — so if this one were still
            # eligible it would come back as a burst carrying its id.
            still_eligible = enrichment.next_burst_to_enrich(conn, 0)
            assert still_eligible is None or intake_id not in [m.intake_id for m in still_eligible.members], \
                "the enriched message is still eligible — it would be re-run"
            print("  ✓ the pass is recorded, so the sweep considers this message exactly once")

            if not args.keep:
                raise psycopg.Rollback

        print("\nkept the writes." if args.keep else "\nrolled back — the dev store was left untouched.")

    db.close_pool()
    print("smoke run complete.")


if __name__ == "__main__":
    main()
