"""By-hand smoke test: the read path — the fast lexical reach and the composed reply — against live models.

The pytest suite (test/test_retrieval.py, test/test_reply.py, test/test_llm.py) proves the search SQL and
the composer's wiring with the model faked, but never that a real reply actually reads well off the diary.
This script is the other half: it seeds a small diary, runs the librarian (retrieval.search) over it, and
composes a real reply with the live reply model (`glm-5.2` on Scaleway) through the persona,
printing what came back for a human to judge.

It is direct-run, not a pytest test, because it needs the live box:

    python test/qa/0003_read_path_smoke.py            # rolls back at the end (default)
    python test/qa/0003_read_path_smoke.py --keep     # commits, so you can inspect the rows

Prerequisites (see README, "Models" and "Database & migrations"):
  - A generative provider reachable for the reply: `SCALEWAY_API_KEY` set in .env for the primary (`glm-5.2`),
    or the ladder falls back to Mistral, then to a local `qwen3.5:4b`.
    The librarian (the lexical reach) needs no model at all.
  - A reachable Postgres — this connects to config.DATABASE_URL (your dev database).

The seeded facts are written straight into diary_facts, not filed through the write path (ingestion has its
own smoke, 0001): this run isolates the read path, so it proves the reach and the reply without leaning on
the router. Every write is wrapped in one transaction, rolled back at the end by default, so the dev store is
left exactly as found while the live reply is still proven end to end. Pass --keep to commit and browse.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# Direct-run from anywhere: put the repo root on the path so `core`/`services` import cleanly.
# This file sits at test/qa/, so the repo root is three directories up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import psycopg

from core import config
from core import db
from services import conversation
from services import models
from services import reply
from services import retrieval

# A small diary with a shape worth reading back:
# two facts share words with the boxing/gym question below (so the reach ranks across both),
# one carries no time cue at all (so its effective time must fall back to created_at),
# and the rest are there as plausible noise the lexical reach should mostly leave alone.
# happened_at is set where the fact named a moment, None where it didn't — the two clocks, as the write path files them.
_NOW = datetime.now(timezone.utc)
SEED = [
    ("Hit the heavy bag for 45 minutes at the gym", datetime(2026, 7, 9, tzinfo=timezone.utc)),
    ("Boxing with my friend Jeremy during the heat wave", datetime(2026, 7, 2, tzinfo=timezone.utc)),
    ("I live in Strasbourg", None),
    ("Read a slow, good book about the sea", None),
    ("Went for a long run along the river this morning", datetime(2026, 7, 11, tzinfo=timezone.utc)),
    # The French entries — the emotive ones the symbiot slips into their first language for.
    # The first carries "énervé", which the French query's "énervement" folds to under the french analyser.
    ("Ce matin j'étais énervé, alors j'ai couru le long du fleuve pour me calmer", datetime(2026, 7, 8, tzinfo=timezone.utc)),
    ("Grosse séance de musculation à la salle hier soir, épuisé mais fier", datetime(2026, 7, 6, tzinfo=timezone.utc)),
]

# The question the symbiot asks — used both as the search query and as the message the reply answers.
# It shares "boxing" with one seeded fact and "gym" with another, so the lexical reach should surface both.
QUESTION = "How has my boxing and gym training been lately?"

# A French question whose "énervement" folds to the "énervé" entry under the french analyser —
# a stem the English analyser could never reach.
# (French Snowball folds many forms but not all — "couru" stays put, for one — so trigram is there for the rest.)
FRENCH_QUESTION = "Comment ça va niveau sport et énervement ces derniers temps ?"


def _seed(conn) -> None:
    for raw_text, happened_at in SEED:
        conn.execute(
            "INSERT INTO diary_facts (raw_text, payload, happened_at) VALUES (%s, %s::jsonb, %s)",
            (raw_text, json.dumps({"@type": [], "text": raw_text}), happened_at),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true",
        help="commit the writes instead of rolling back, so the rows can be inspected afterwards",
    )
    args = parser.parse_args()

    print(f"database  : {config.DATABASE_URL}")
    print(f"reply     : {config.REPLY_MODEL}")
    print(f"ollama    : {config.OLLAMA_BASE_URL}")

    pool = db.open_pool(config.DATABASE_URL)
    db.run_migrations(pool)  # idempotent — brings the dev database to 0012 if it isn't already

    with pool.connection() as conn:
        with conn.transaction():
            _seed(conn)

            # --- the fast lexical reach -------------------------------------------------------
            hits = retrieval.search(conn, QUESTION)
            print(f"\n=== the reach: {QUESTION!r} ===")
            for f in hits:
                print(f"  [{f.effective_at.date().isoformat()}] rank={f.rank:.4f}  {f.raw_text!r}")

            assert hits, "the reach found nothing for a query that shares words with two seeded facts"
            # Deterministic, model-free checks: ranked best-first, and every fact carries a usable time.
            assert all(a.rank >= b.rank for a, b in zip(hits, hits[1:])), "results are not ranked best-first"
            assert all(f.effective_at is not None for f in hits), "a fact came back with no effective time"
            texts = [f.raw_text for f in hits]
            assert "Hit the heavy bag for 45 minutes at the gym" in texts, "the gym fact was not reached"
            assert "Boxing with my friend Jeremy during the heat wave" in texts, "the boxing fact was not reached"
            print("  ✓ ranked best-first, both boxing/gym facts reached, every fact has an effective time")

            # --- the effective-time fallback, live --------------------------------------------
            strasbourg = retrieval.search(conn, "Strasbourg")
            assert strasbourg, "the reach did not find the Strasbourg fact"
            # It named no moment, so happened_at is NULL and effective time stands created_at in — never None.
            assert strasbourg[0].effective_at is not None
            print(f"\n=== effective-time fallback ===")
            print(f"  'I live in Strasbourg' named no moment; effective time fell back to created_at: "
                  f"{strasbourg[0].effective_at.isoformat()}")

            # --- the fuzzy trigram reach ------------------------------------------------------
            fuzzy = retrieval.search(conn, "Strasborg")  # a typo full-text alone would miss
            print(f"\n=== fuzzy reach: 'Strasborg' (misspelt) ===")
            print(f"  found: {[f.raw_text for f in fuzzy]}")
            assert any(f.raw_text == "I live in Strasbourg" for f in fuzzy), "trigram did not catch the typo"
            print("  ✓ a misspelt query still surfaced the fact, via trigram similarity")

            # --- a query that should match nothing --------------------------------------------
            assert retrieval.search(conn, "xylophone quantum saxophone") == [], "an unrelated query matched something"
            print("\n=== no-match ===\n  ✓ a query sharing nothing with the diary returned []")

            # --- the reach, in French ---------------------------------------------------------
            # "énervement" in the question folds to the "énervé" in an entry under the french analyser —
            # a stem the English analyser could never reach, so the emotive French entry is surfaced, not left behind.
            french_hits = retrieval.search(conn, FRENCH_QUESTION)
            print(f"\n=== the reach, in French: {FRENCH_QUESTION!r} ===")
            for f in french_hits:
                print(f"  [{f.effective_at.date().isoformat()}] rank={f.rank:.4f}  {f.raw_text!r}")
            french_texts = [f.raw_text for f in french_hits]
            assert any("énervé" in t for t in french_texts), "the emotive French entry was not reached via the french analyser"
            print("  ✓ a French question reached the emotive French entry by its stem — the french analyser earns its place")

            # --- the composed reply, live -----------------------------------------------------
            # An eyeball on the budget guard: the reply's context is far under the model's optimal, so it won't fire.
            spec = models.MODELS[config.REPLY_MODEL]
            context_tokens = models.count_tokens(reply._render(hits))
            print(f"\n=== budget headroom ===")
            print(f"  reply model optimal: {spec.optimal_context_tokens} tokens; "
                  f"retrieved-context block: {context_tokens} tokens — guard will not fire")

            # This smoke isolates the diary reach, so it composes with an empty conversation —
            # short-term memory has its own smoke (0004). compose now takes it as a third argument.
            answer = reply.compose(QUESTION, hits, conversation.Conversation(gist=None, tail=[]))
            print(f"\n=== the composed reply (live {config.REPLY_MODEL}) ===")
            print(f"  question : {QUESTION!r}")
            print(f"  reply    : {answer}")
            assert answer and answer.strip(), "the composed reply came back empty"
            print("\n  ✓ a non-empty reply composed off the retrieved diary — read it above to judge the voice")

            if not args.keep:
                raise psycopg.Rollback

        print("\nkept the writes." if args.keep else "\nrolled back — the dev store was left untouched.")

    db.close_pool()
    print("smoke run complete.")


if __name__ == "__main__":
    main()
