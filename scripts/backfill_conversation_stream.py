"""Deploy backfill: seed the conversation stream from the history already on the box.

Short-term conversational memory (services/conversation.py, migration 0014) fills itself going forward —
a symbiot's line joins the stream at intake, its reply when the worker answers it, a missive when it is raised.
But at the moment the feature ships, every exchange that happened *before* it existed is invisible to it:
the intake and missive rows are there, holding the words durably,
with no conversation_item pointing at them.
This script closes that gap once, on deploy,
so the first reply after the feature lands already sits inside the conversation that came before.

It reconstructs the stream from the three sources the live path writes from, in the order they happened,
and computes each utterance's token_count with the same local counter the live write and the budget guard use (services.models) —
the "backfill token count on deploy" step the design calls for.
Each row points at where its words already live (never a copy):
the intake row for a symbiot message or its reply, the missive row for a machine-initiated line.

It is idempotent:
an utterance that already has a conversation_item (a re-run, or history written after the feature shipped) is skipped by a NOT EXISTS guard,
so running it twice adds nothing the second time.
Anonymous intake rows are left out — the conversation is the symbiot's, the same boundary the diary and the live write keep.

Direct-run, and a *dry run by default* —
it prints what it would insert and rolls back,
so you can look before you leap.
Pass --commit to actually write:

    python scripts/backfill_conversation_stream.py            # dry run: report and roll back
    python scripts/backfill_conversation_stream.py --commit   # write the stream rows durably

It connects to config.DATABASE_URL (the box's database), so run it there, after the migration.
"""

import argparse
import os
import sys

# Run from anywhere: put the repo root on the path so `core` and `services` import.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg

from core import config
from services import models

# The three sources, unioned in the order the live path writes them,
# with the sort key that reconstructs true chronology across all of them:
#   - a symbiot's message, ordered by when it arrived (intake.created_at);
#   - the machine's reply, ordered by when it was produced (intake.updated_at, the answer time),
#     so a reply that landed between two other utterances is placed where it actually happened;
#   - a missive, ordered by when it was raised (missive.created_at).
# The `ord` column breaks a tie within one intake row so the message precedes its own reply even if the two timestamps coincide.
# Each source carries a NOT EXISTS guard against an already-present conversation_item,
# so the backfill only ever adds what is missing.
_GATHER = """
    SELECT symbiot_id, role, text, intake_id, missive_id
    FROM (
        SELECT i.symbiot_id, i.created_at AS sort_ts, 0 AS ord,
               'symbiot' AS role, i.message AS text, i.id AS intake_id, NULL::bigint AS missive_id
        FROM intake i
        WHERE i.symbiot_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM conversation_item ci WHERE ci.intake_id = i.id AND ci.role = 'symbiot'
          )
        UNION ALL
        SELECT i.symbiot_id, i.updated_at, 1,
               'machine', i.answer, i.id, NULL
        FROM intake i
        WHERE i.symbiot_id IS NOT NULL AND i.status = 'answered' AND i.answer IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM conversation_item ci WHERE ci.intake_id = i.id AND ci.role = 'machine'
          )
        UNION ALL
        SELECT m.symbiot_id, m.created_at, 1,
               'machine', m.body, NULL, m.id
        FROM missive m
        WHERE NOT EXISTS (
            SELECT 1 FROM conversation_item ci WHERE ci.missive_id = m.id
        )
    ) u
    ORDER BY sort_ts, ord, intake_id, missive_id
"""


def backfill(conn) -> int:
    """Insert a conversation_item for every historical utterance not yet on the stream. Returns the count.

    Runs inside the caller's transaction, so the caller decides commit or rollback.
    The rows are inserted in chronological order,
    so their ids reflect the order the utterances happened —
    the same ordering the reader (conversation.recent) and the compression sweep walk the stream in.
    """
    with psycopg.ServerCursor(conn, "backfill_cursor") as cursor:
        cursor.execute(_GATHER)
        
        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
                
            for symbiot_id, role, text, intake_id, missive_id in rows:
                conn.execute(
                    "INSERT INTO conversation_item (symbiot_id, role, token_count, intake_id, missive_id) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (symbiot_id, role, models.count_tokens(text), intake_id, missive_id),
                )
            total_inserted += len(rows)
            
    return total_inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill the conversation stream from existing history.")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="write the stream rows durably (default: dry run — report the count and roll back)",
    )
    args = parser.parse_args()

    with psycopg.connect(config.DATABASE_URL) as conn:
        try:
            with conn.transaction():
                inserted = backfill(conn)
                if args.commit:
                    print(f"committing {inserted} conversation_item row(s) reconstructed from history")
                else:
                    print(f"dry run: would insert {inserted} conversation_item row(s) — rolling back")
                    # Raise the transaction's own sentinel to roll back cleanly, with no error exit.
                    raise psycopg.Rollback
        except psycopg.Rollback:
            pass


if __name__ == "__main__":
    main()
