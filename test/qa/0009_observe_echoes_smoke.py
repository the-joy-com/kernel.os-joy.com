"""By-hand smoke test: the /observe echoes lens — semantic redundancy scoring — against live embeddings.

The pytest suite (test/test_observe.py) proves the gather, the union-find clustering, the degrade path,
and the route shape with the embedder faked — it stubs the vectors, so it never touches a real model.
It cannot prove the one thing only a live run can: that real nomic-embed-text vectors actually place two
*more-or-less-the-same* lines close enough to cluster, while keeping an unrelated line apart.
That is a claim about the embedding space and the default threshold, not about the code, so it lives here.

The script stages three machine replies that paraphrase one another (all about kicking off a reindex tonight)
and one that is plainly unrelated (a birthday wish), files each onto the conversation stream through a quick
reply, and then runs the real lens (observe.echoes). It prints two things a human tunes the threshold against:
the full pairwise cosine matrix, and the clusters the default ECHO_THRESHOLD produces over it.
Its one hard assertion is the invariant that holds whatever the threshold turns out to be —
the paraphrases sit nearer each other than any of them sits to the unrelated line —
because if that ordering ever failed, no threshold could separate an echo from noise and the lens is hopeless.

It is direct-run, not a pytest test, because it needs the live box:

    python test/qa/0009_observe_echoes_smoke.py            # rolls back at the end (default)
    python test/qa/0009_observe_echoes_smoke.py --keep     # commits, so you can open /observe and see it

Prerequisites (see README, "Models" and "Database & migrations"):
  - Ollama serving the embedding model (`nomic-embed-text`) on the box — the lens embeds each line to score it.
  - A reachable Postgres — this connects to config.DATABASE_URL (your dev database), migrated to 0015.

Everything is wrapped in one transaction, rolled back at the end by default,
so the dev store is left exactly as found while the live scoring is still proven end to end.
Pass --keep to commit and open /observe → echoes in the shell to see the same clusters rendered.
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
from services import echo
from services import observe
from services.adapters import embedding

# Three replies that say more or less the same thing in different words, and one that plainly doesn't —
# so a working lens clusters the first three and leaves the fourth alone.
PARAPHRASES = [
    "The reindex is the slow part — I can kick it off tonight if you want.",
    "The bottleneck is still the reindex; want me to run it this evening?",
    "It's the reindex that's dragging things; I'll start it after hours.",
]
UNRELATED = "Happy birthday! I hope your day is a wonderful one."
REPLIES = PARAPHRASES + [UNRELATED]


def _seed_symbiot(conn) -> int:
    # A dedicated smoke symbiot, so the run stands alone and doesn't lean on the seeded one; rolled back anyway.
    return conn.execute(
        "INSERT INTO symbiot (email) VALUES ('smoke-observe@example.test') RETURNING id"
    ).fetchone()[0]


def _file_reply(conn, symbiot_id: int, answer: str) -> None:
    # A quick reply as the loop leaves it: an answered intake row, mirrored onto the stream as a machine line.
    intake_id = conn.execute(
        "INSERT INTO intake (message, answer, symbiot_id, status) VALUES ('(a message)', %s, %s, 'answered') RETURNING id",
        (answer, symbiot_id),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO conversation_item (symbiot_id, role, token_count, intake_id) VALUES (%s, 'machine', 1, %s)",
        (symbiot_id, intake_id),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true",
        help="commit the writes instead of rolling back, so /observe can be opened against them afterwards",
    )
    args = parser.parse_args()

    print(f"database  : {config.DATABASE_URL}")
    print(f"embedding : {config.EMBEDDING_MODEL}")
    print(f"ollama    : {config.OLLAMA_BASE_URL}")
    print(f"threshold : {echo.ECHO_THRESHOLD}  (the default an echo must clear)")

    pool = db.open_pool(config.DATABASE_URL)
    db.run_migrations(pool)  # idempotent — brings the dev database to the current schema if it isn't already

    with pool.connection() as conn:
        with conn.transaction():
            symbiot_id = _seed_symbiot(conn)

            print(f"\n=== filing {len(REPLIES)} machine replies onto the stream ===")
            for answer in REPLIES:
                _file_reply(conn, symbiot_id, answer)
                print(f"  filed: {answer!r}")

            # The full pairwise cosine matrix, straight from live embeddings — the numbers a human tunes against.
            vectors = embedding.embed_many(REPLIES, task="document")
            print("\n=== pairwise cosine similarity (live nomic-embed-text) ===")
            pairs = []
            for i in range(len(REPLIES)):
                for j in range(i + 1, len(REPLIES)):
                    s = echo.cosine(vectors[i], vectors[j])
                    pairs.append((s, i, j))
            for s, i, j in sorted(pairs, reverse=True):
                mark = "  paraphrases" if i < len(PARAPHRASES) and j < len(PARAPHRASES) else ""
                print(f"  {s:.3f}  [{i}] × [{j}]{mark}")

            # The invariant no threshold can rescue if it fails: paraphrases are nearer each other than to the outlier.
            within = [s for s, i, j in pairs if i < len(PARAPHRASES) and j < len(PARAPHRASES)]
            across = [s for s, i, j in pairs if j == len(PARAPHRASES)]  # the unrelated line is the last index
            print(f"\n  closest paraphrase pair : {max(within):.3f}")
            print(f"  loosest paraphrase pair : {min(within):.3f}")
            print(f"  nearest the outlier gets: {max(across):.3f}")
            assert min(within) > max(across), (
                "a paraphrase pair sat further apart than a paraphrase and the unrelated line — "
                "the embedding space can't tell an echo from noise here, so no threshold would save the lens"
            )
            print("  ✓ the paraphrases sit nearer each other than any sits to the unrelated line")

            # The real lens over the same lines: what the default threshold actually clusters.
            result = observe.machine_echoes(conn, symbiot_id)
            print(f"\n=== observe.echoes at threshold {echo.ECHO_THRESHOLD} ===")
            print(f"  scored: {result.scored}")
            for c in result.clusters:
                print(f"  echo · {c.similarity:.3f}")
                for u in c.members:
                    print(f"     {u.mechanism} · {u.text!r}")
            for u in result.singles:
                print(f"  (alone) {u.mechanism} · {u.text!r}")
            assert result.scored, "the lens came back unscored — the embedder was unreachable"
            print(
                "\n  read the clusters above against the matrix: if the paraphrases didn't group at the default "
                "threshold (or the outlier did), ECHO_THRESHOLD is the knob to move — that is this smoke's whole point."
            )

            if not args.keep:
                raise psycopg.Rollback

        print("\nkept the writes." if args.keep else "\nrolled back — the dev store was left untouched.")

    db.close_pool()
    print("smoke run complete.")


if __name__ == "__main__":
    main()
