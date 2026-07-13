"""By-hand smoke test: the offline duplicate garbage-collection pass against the *live* models.

The pytest suite (test/test_ontology_gc.py) fakes the model clients at the network boundary,
so it proves the detection SQL, the idempotent re-point, the parent guards, the tombstone and the dropped vector —
but never that the real embedder places true synonyms near each other,
nor that the real generative model confirms them the same kind and picks a sane survivor.
This script is the other half:
it seeds a few types with *real* `nomic-embed-text` vectors (the embedder is still local),
then runs the whole merge pass — detect → confirm → cluster → pick → collapse —
against real `nomic-embed-text` and the real generative model (`glm-5.2` on Scaleway),
and prints what the models decided so a human can eyeball it.

It doubles as the distance-calibration tool GC_DISTANCE needs:
it prints the real pairwise cosine distances among the seeded types,
so the pre-filter threshold can be set against real numbers rather than guessed.
Pass --distance to override config.GC_DISTANCE for the run.

It is direct-run, not a pytest test, because it needs the live box:

    python test/qa/0002_ontology_gc_smoke.py                 # rolls back at the end (default)
    python test/qa/0002_ontology_gc_smoke.py --keep          # commits, so you can inspect the rows
    python test/qa/0002_ontology_gc_smoke.py --distance 0.5  # widen the pre-filter for this run

Prerequisites (see README, "Ollama (local models)" and "Database & migrations"):
  - Ollama running on the box with both models pulled: `nomic-embed-text` and `qwen3.5:4b`.
  - A reachable Postgres with pgvector — this connects to config.DATABASE_URL (your dev database).

By default every write is wrapped in one transaction that is rolled back at the end, so the run leaves
the dev ontology store exactly as it found it while still proving the live round trip end to end.
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
from services.adapters import embedding
from services.memory import ontology_gc

# Two genuinely synonymous types the pass should merge, plus one unrelated type it must leave alone.
# Definitions are what actually get embedded and what the re-ranker reads, so they carry the meaning.
SEED = [
    ("workout_action", "a session of physical exercise or training"),
    ("training_session", "a bout of physical training or working out"),
    ("phone_call", "a telephone conversation held with another person"),
]


def _seed_type(conn, type_name: str, definition: str) -> int:
    # Land a type the way the minter does, but with a *real* embedding of its definition,
    # so the smoke exercises whether the live embedder actually places the synonyms near each other.
    vector = embedding.embed(definition, task="document")
    literal = "[" + ",".join(repr(x) for x in vector) + "]"
    oid = conn.execute(
        "INSERT INTO schema_ontology (type_name, definition) VALUES (%s, %s) RETURNING id",
        (type_name, definition),
    ).fetchone()[0]
    model_id = conn.execute("SELECT id FROM embedding_model WHERE is_active").fetchone()[0]
    conn.execute(
        "INSERT INTO ontology_embedding_nomic_embed_text (ontology_id, model_id, embedding) "
        "VALUES (%s, %s, %s::vector)",
        (oid, model_id, literal),
    )
    return oid


def _links(conn, fact_id: int) -> set[int]:
    return {r[0] for r in conn.execute(
        "SELECT ontology_id FROM diary_fact_ontology WHERE diary_fact_id = %s", (fact_id,)
    ).fetchall()}


def _print_distance_matrix(conn, ids: dict[int, str]) -> None:
    # The real pairwise cosine distances among the seeded types — the numbers GC_DISTANCE is set against.
    print("\n=== real pairwise cosine distances (calibrate GC_DISTANCE against these) ===")
    id_list = list(ids)
    for i, a in enumerate(id_list):
        for b in id_list[i + 1:]:
            dist = conn.execute(
                "SELECT ea.embedding <=> eb.embedding "
                "FROM active_ontology_embedding ea, active_ontology_embedding eb "
                "WHERE ea.ontology_id = %s AND eb.ontology_id = %s",
                (a, b),
            ).fetchone()[0]
            print(f"  {ids[a]:>18}  ↔  {ids[b]:<18}  {dist:.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true",
                        help="commit the writes instead of rolling back, so the rows can be inspected")
    parser.add_argument("--distance", type=float, default=config.GC_DISTANCE,
                        help="override GC_DISTANCE (the cosine pre-filter) for this run")
    args = parser.parse_args()

    print(f"database  : {config.DATABASE_URL}")
    print(f"embed     : {config.EMBEDDING_MODEL}")
    print(f"re-rank   : {config.RERANK_MODEL}")
    print(f"ollama    : {config.OLLAMA_BASE_URL}")
    print(f"distance  : {args.distance} (GC_DISTANCE pre-filter for this run)")

    pool = db.open_pool(config.DATABASE_URL)
    db.run_migrations(pool)  # idempotent — no-op if the dev database is already current

    with pool.connection() as conn:
        with conn.transaction():
            # Seed the three types with live embeddings, and two facts: one on the likely loser alone,
            # one on both synonyms at once (the case the idempotent re-point exists for).
            ids = {}
            for type_name, definition in SEED:
                ids[_seed_type(conn, type_name, definition)] = type_name
            by_name = {name: oid for oid, name in ids.items()}
            only_loser = conn.execute(
                "INSERT INTO diary_facts (raw_text, payload) VALUES (%s, %s::jsonb) RETURNING id",
                ("did a training session", "{}"),
            ).fetchone()[0]
            conn.execute("INSERT INTO diary_fact_ontology VALUES (%s, %s)",
                         (only_loser, by_name["training_session"]))
            both = conn.execute(
                "INSERT INTO diary_facts (raw_text, payload) VALUES (%s, %s::jsonb) RETURNING id",
                ("a workout, a session", "{}"),
            ).fetchone()[0]
            for name in ("workout_action", "training_session"):
                conn.execute("INSERT INTO diary_fact_ontology VALUES (%s, %s)", (both, by_name[name]))

            _print_distance_matrix(conn, ids)

            # Which pairs the pre-filter offers at this distance — reported so the threshold can be tuned.
            pairs = ontology_gc.candidate_pairs(conn, distance=args.distance)
            print("\n=== candidate pairs (vector pre-filter) ===")
            for a, b in pairs:
                print(f"  {ids.get(a, a)}  ↔  {ids.get(b, b)}")
            if not pairs:
                print("  none within the distance — widen --distance to see the pass do anything")

            # The whole pass against the live models. run_once reads its own threshold from config,
            # so pin config.GC_DISTANCE to the run's value first.
            config.GC_DISTANCE = args.distance
            report = ontology_gc.run_once(conn)

            print("\n=== what the pass did ===")
            if not report:
                print("  merged nothing — the model did not confirm any pair the same kind")
            for merge in report:
                survivor = merge["survivor"]
                print(f"  survivor : {ids.get(survivor, survivor)}")
                print(f"  merged   : {[ids.get(m, m) for m in merge['merged']]}")

            # Mechanical invariants: whatever the model judged, a reported merge must hold these.
            for merge in report:
                survivor = merge["survivor"]
                for loser in merge["merged"]:
                    assert conn.execute(
                        "SELECT merged_into FROM schema_ontology WHERE id = %s", (loser,)
                    ).fetchone()[0] == survivor, "loser was not tombstoned onto the survivor"
                    assert conn.execute(
                        "SELECT count(*) FROM active_ontology_embedding WHERE ontology_id = %s", (loser,)
                    ).fetchone()[0] == 0, "loser vector was not dropped"
            # Every fact now points only at live (unmerged) types — no link left on a tombstone.
            dangling = conn.execute(
                "SELECT count(*) FROM diary_fact_ontology l "
                "JOIN schema_ontology o ON o.id = l.ontology_id WHERE o.merged_into IS NOT NULL"
            ).fetchone()[0]
            assert dangling == 0, f"{dangling} fact link(s) still point at a merged type"
            print("\n  ✓ every reported merge tombstoned its loser, dropped its vector, and left no dangling link")
            print(f"  fact 'training session' now filed under : {sorted(ids.get(o, o) for o in _links(conn, only_loser))}")
            print(f"  fact 'both'              now filed under : {sorted(ids.get(o, o) for o in _links(conn, both))}")

            if not args.keep:
                raise psycopg.Rollback

        print("\nkept the writes." if args.keep else "\nrolled back — the dev store was left untouched.")

    db.close_pool()
    print("smoke run complete.")


if __name__ == "__main__":
    main()
