"""By-hand smoke test: the reminder tool — retrieve, decide, act, speak, and fire — against live models.

The pytest suite (test/test_tools.py, test/test_reminder.py, test/test_worker.py) proves the catalog reconcile,
the gate, the decision schema, the executor's exactly-once, and the firing —
all with the model and the embedding faked.
It never proves the four things only a live run can:
that real recall surfaces the tool for a reminding message,
that the real decision reads a natural sentence into the right tool and a concrete time in the symbiot's zone,
that the executor stores what the decision extracted,
and that the confirmation the human sees is composed in the persona's voice.
This script is that other half, and it drives the whole retrieve → decide → act → speak fork end to end,
then fires the reminder it scheduled.

It runs on one rolled-back transaction:

  1. reconcile the catalog (live embed) and search it with a reminding message — the gate must surface schedule_reminder;
  2. decide, live — the real model reads "remind me to call the dentist tomorrow at 9" into the tool,
     resolving the arguments and the time against the symbiot's local now (a zone set to Europe/Paris here);
  3. act — the executor stores the reminder, and the row is read back to confirm the resolved instant;
  4. speak — the confirmation is composed live and printed, for a human to judge it confirms in the voice;
  5. fire — the reminder is backdated to due and delivered as a missive inline,
     mirroring worker._fire_one on this connection so a rolled-back smoke can see its own writes,
     and the missive is read back.

It is direct-run, not a pytest test, because it needs the live box:

    python test/qa/0007_reminder_smoke.py            # rolls back at the end (default)
    python test/qa/0007_reminder_smoke.py --keep     # commits, so you can inspect the rows

Prerequisites (see README, "Models" and "Database & migrations"):
  - Ollama on the box for the embeddings (the catalog reconcile and the gate).
  - A generative provider for the decision and the confirmation (config.TOOL_DECISION_MODEL / TOOL_CONFIRM_MODEL,
    `glm-5.2`): `SCALEWAY_API_KEY` for the primary, or the ladder falls back to Mistral, then local `qwen3.5:4b`.
  - A reachable Postgres — this connects to config.DATABASE_URL (your dev database), migrated to 0017.
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
from services.loop import missive
from services.tools import reminder
from services.tools import tools
from services.loop import zone

# The zone the smoke symbiot lives in, so the decision resolves "tomorrow at 9" to a Paris instant;
# set directly rather than inferred, since the inference is 0006's to prove, not this one's.
HOME_ZONE = "Europe/Paris"

# The reminding message driven through the whole fork — a natural sentence, no command syntax.
MESSAGE = "remind me to call the dentist tomorrow at 9am"


def _seed_symbiot(conn) -> int:
    # A dedicated smoke symbiot in a known zone, so the run stands alone; rolled back anyway.
    return conn.execute(
        "INSERT INTO symbiot (email, timezone) VALUES ('smoke-reminder@example.test', %s) RETURNING id",
        (HOME_ZONE,),
    ).fetchone()[0]


def _seed_intake(conn, symbiot_id: int) -> int:
    # The triggering message as a settled intake row — the reminder's intake_id references it.
    return conn.execute(
        "INSERT INTO intake (message, symbiot_id, status) VALUES (%s, %s, 'answered') RETURNING id",
        (MESSAGE, symbiot_id),
    ).fetchone()[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true",
        help="commit the writes instead of rolling back, so the rows can be inspected afterwards",
    )
    args = parser.parse_args()

    print(f"database : {config.DATABASE_URL}")
    print(f"decide   : {config.TOOL_DECISION_MODEL}")
    print(f"confirm  : {config.TOOL_CONFIRM_MODEL}")
    print(f"ollama   : {config.OLLAMA_BASE_URL}")
    print(f"zone     : {HOME_ZONE}")

    pool = db.open_pool(config.DATABASE_URL)
    db.run_migrations(pool)  # idempotent — brings the dev database to 0017 if it isn't already

    with pool.connection() as conn:
        with conn.transaction():
            symbiot_id = _seed_symbiot(conn)
            intake_id = _seed_intake(conn, symbiot_id)
            now_local = zone.now_for(HOME_ZONE)

            # --- 1. retrieve: the catalog reconcile, and the gate ------------------------------
            tools.reconcile_catalog(conn)
            candidates = tools.search_catalog(conn, MESSAGE)
            print(f"\n=== the gate (live embed) ===")
            print(f"  message    : {MESSAGE!r}")
            print(f"  candidates : {[c.name for c in candidates]}")
            assert any(c.name == "schedule_reminder" for c in candidates), (
                "the gate should surface schedule_reminder for a reminding message"
            )
            print("  ✓ the reminder surfaced — the gate opened")

            # --- 2. decide, live: the message → the tool and its arguments ---------------------
            decision = tools.decide(MESSAGE, candidates, [], now_local, HOME_ZONE)
            print(f"\n=== the decision (live {config.TOOL_DECISION_MODEL}) ===")
            print(f"  local now : {now_local.isoformat()} ({HOME_ZONE})")
            print(f"  tool      : {decision.tool!r}")
            print(f"  arguments : {decision.args!r}")
            assert decision.tool == "schedule_reminder", "the decision should name the reminder tool"
            assert decision.args.get("fire_at") is not None, "the decision should resolve a concrete time"
            print("  ✓ the tool was named and a concrete time resolved")

            # --- 3. act: the executor stores the reminder --------------------------------------
            result = tools.execute(conn, decision, symbiot_id, intake_id, now_local, HOME_ZONE)
            stored = conn.execute(
                "SELECT body, fire_at FROM reminder WHERE intake_id = %s", (intake_id,)
            ).fetchone()
            print(f"\n=== the act ===")
            print(f"  effected  : {result.effected}")
            print(f"  stored    : body={stored[0]!r}  fire_at={stored[1].astimezone(now_local.tzinfo).isoformat()}")
            assert result.effected, "the executor should have scheduled the reminder"
            print("  ✓ the reminder is in the store, at the resolved instant")

            # --- 4. speak: the confirmation, live ----------------------------------------------
            confirmation = tools.compose_confirmation(MESSAGE, result, now_local, HOME_ZONE)
            print(f"\n=== the confirmation (live {config.TOOL_CONFIRM_MODEL}) ===")
            print(f"  reply : {confirmation}")
            assert confirmation and confirmation.strip(), "the confirmation came back empty"
            print("\n  ✓ a confirmation composed — read it above to judge it confirms in the voice")

            # --- 5. fire: deliver the due reminder as a missive (inline, mirroring worker._fire_one) ---
            # Backdate the reminder to due, then fire it on this same connection,
            # so the rolled-back smoke can see its own writes
            # (worker._fire_one uses its own pooled connection, outside this transaction).
            conn.execute(
                "UPDATE reminder SET fire_at = now() - interval '1 minute' WHERE intake_id = %s", (intake_id,)
            )
            due = reminder.claim_due(conn)
            assert due is not None, "the backdated reminder should now be due"
            reminder_id, due_symbiot, due_body = due
            missive_id = missive.raise_for(conn, due_symbiot, due_body)
            conversation.record_utterance(conn, due_symbiot, "machine", due_body, missive_id=missive_id)
            reminder.mark_fired(conn, reminder_id)
            fired_body = conn.execute("SELECT body FROM missive WHERE id = %s", (missive_id,)).fetchone()[0]
            print(f"\n=== the firing ===")
            print(f"  delivered as a missive: {fired_body!r}")
            assert fired_body == due_body, "the missive should carry the stored reminder line"
            print("  ✓ the reminder fired — the kernel reached back out with the stored line")

            if not args.keep:
                raise psycopg.Rollback

        print("\nkept the writes." if args.keep else "\nrolled back — the dev store was left untouched.")

    db.close_pool()
    print("smoke run complete.")


if __name__ == "__main__":
    main()
