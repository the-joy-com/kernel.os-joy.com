"""By-hand smoke test: the diary ingestion path against the *live* models.

The pytest suite (test/test_ontology.py) fakes the model clients at the network boundary,
so it proves the SQL, the schema constraints, and the control flow —
but never that the real embedder and the real re-ranker actually behave.
This script is the other half:
it runs the whole write path — name the concepts, route each, synthesize the thin payload, persist —
against the real embedder (`nomic-embed-text`, local) and the real re-ranker (`glm-5.2` on Scaleway),
and prints what the models decided so a human can eyeball it.

It is direct-run, not a pytest test, because it needs the live box:

    python test/qa/0001_ontology_ingestion_smoke.py            # rolls back at the end (default)
    python test/qa/0001_ontology_ingestion_smoke.py --keep     # commits, so you can inspect the rows

Prerequisites (see README, "Models" and "Database & migrations"):
  - Ollama running on the box with `nomic-embed-text` pulled (the embedder is still local).
  - A generative provider reachable: `SCALEWAY_API_KEY` set in .env for the primary (`glm-5.2`),
    or the ladder falls back to Mistral, then to a local `qwen3.5:4b` if you have it pulled.
  - A reachable Postgres with pgvector — this connects to config.DATABASE_URL (your dev database).

By default every write is wrapped in one transaction that is rolled back at the end, so the run
leaves the dev ontology store exactly as it found it while still proving the live round trip end to
end (concept 2 still sees concept 1's freshly-coined type, because both run inside that one
transaction on one connection). Pass --keep to commit instead and browse the persisted rows.
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
from services.memory import ontology

# The acceptance-criteria example, and a second fact that deliberately shares one concept (boxing)
# with the first while adding two it has never seen (a friend, a heat wave) — so the run exercises
# both halves of the vocabulary law: reuse the type already coined, coin the genuinely new ones.
RAW_FIRST = "Hit the heavy bag for 45 minutes today"
RAW_SECOND = "Boxing with my friend Jeremy during the heat wave"


def _links(conn, fact_id: int) -> list[tuple[int, str]]:
    # The (ontology_id, type_name) pairs a fact is filed under — one row per concept it resolved to.
    return conn.execute(
        "SELECT o.id, o.type_name "
        "FROM diary_fact_ontology l JOIN schema_ontology o ON o.id = l.ontology_id "
        "WHERE l.diary_fact_id = %s ORDER BY o.id",
        (fact_id,),
    ).fetchall()


def _report(conn, label: str, raw: str, fact_id: int) -> set[int]:
    # Print what the pipeline made of one fact, and hand back the set of type ids it linked to.
    payload = conn.execute(
        "SELECT payload FROM diary_facts WHERE id = %s", (fact_id,)
    ).fetchone()[0]
    links = _links(conn, fact_id)

    print(f"\n=== {label} ===")
    print(f"  raw text : {raw!r}")
    print(f"  filed as : {[name for _, name in links]}")
    print(f"  payload  : {payload}")

    # Structural checks — these are deterministic and must hold whatever the models named.
    assert set(payload.keys()) == {"@type", "text"}, f"payload is not thin: {payload.keys()}"
    assert payload["text"] == raw, "raw text was not kept verbatim in the payload"
    # @type is the linked types, sorted alphabetically
    # (the link query above orders by id, so sort both sides to compare):
    # the payload names exactly the concepts the fact was filed under.
    assert payload["@type"] == sorted(name for _, name in links), "@type does not match the linked types"
    assert conn.execute(
        "SELECT count(*) FROM active_diary_fact_embedding WHERE diary_fact_id = %s", (fact_id,)
    ).fetchone()[0] == 1, "the fact's embedding did not land in the active set"
    print("  ✓ payload is thin ({@type, text}), text is verbatim, one embedding row, links agree")
    return {oid for oid, _ in links}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true",
        help="commit the writes instead of rolling back, so the rows can be inspected afterwards",
    )
    args = parser.parse_args()

    print(f"database : {config.DATABASE_URL}")
    print(f"embed    : {config.EMBEDDING_MODEL}")
    print(f"re-rank  : {config.RERANK_MODEL}")
    print(f"ollama   : {config.OLLAMA_BASE_URL}")

    pool = db.open_pool(config.DATABASE_URL)
    db.run_migrations(pool)  # idempotent — no-op if the dev database is already current

    with pool.connection() as conn:
        active = conn.execute(
            "SELECT name, version, dimension FROM embedding_model WHERE is_active"
        ).fetchone()
        print(f"active model row: {active[0]} {active[1]} ({active[2]}-d)")

        # One outer transaction around the whole run. Each mint/persist inside opens its own
        # nested block (a savepoint), so a coined type is visible to the next concept on this
        # connection, yet the whole lot vanishes on rollback and the dev store stays pristine.
        with conn.transaction():
            first_id = ontology.ingest(conn, RAW_FIRST)
            first_types = _report(conn, "first fact (novel — should coin)", RAW_FIRST, first_id)

            second_id = ontology.ingest(conn, RAW_SECOND)
            second_types = _report(conn, "second fact (shares boxing — should reuse it)", RAW_SECOND, second_id)

            # The vocabulary law, observed live: the shared concept should route both facts to the
            # SAME type id (reuse, not a coined twin); the second fact should also carry types the
            # first never did (the friend, the heat wave). These depend on the model's judgment, so
            # they are reported for the human to confirm rather than asserted.
            shared = first_types & second_types
            fresh = second_types - first_types
            print("\n=== the vocabulary law, live ===")
            print(f"  shared type ids (reused across both facts) : {sorted(shared) or 'NONE — the re-ranker did not reuse'}")
            print(f"  ids only the second fact coined            : {sorted(fresh) or 'none'}")

            # JSONB queryability — the acceptance criterion that the thin payload is reachable with
            # plain Postgres operators, proven against the rows just written.
            hits = conn.execute(
                "SELECT id FROM diary_facts WHERE payload -> '@type' ?| %s::text[] ORDER BY id",
                ([name for _, name in _links(conn, second_id)],),
            ).fetchall()
            print("\n=== payload queryable by JSONB operators ===")
            print(f"  facts whose @type overlaps the second fact's types: {[r[0] for r in hits]}")
            assert second_id in [r[0] for r in hits], "the JSONB @type query did not find the fact"
            print("  ✓ `payload -> '@type' ?| ...` reaches into the stored JSON-LD")

            if not args.keep:
                # Roll this transaction back: the run proved itself without leaving a mark.
                raise psycopg.Rollback

        print("\nkept the writes." if args.keep else "\nrolled back — the dev store was left untouched.")

    db.close_pool()
    print("smoke run complete.")


if __name__ == "__main__":
    main()
