"""By-hand smoke test: the /observe reminders lens, and the decision that feeds it, against live models.

The pytest suite (test/test_observe.py) proves the read behind the reminders card with a hand-inserted reminder:
the pairing resolves, the order is newest-first, the route gates on a session. It never runs the real tool path,
so it cannot prove the one thing that matters most here and that only a live run can — that the *sharpened
decision* actually behaves: an explicit "remind me…" schedules a reminder, and a line that merely mentions a
future task ("I need to call the dentist tomorrow") is declined rather than turned into a reminder no one asked for.
That is a claim about the model following the decision prompt, not about the code, so it lives here.

The script drives the real two-gate seam the worker uses (services/tools/tools.py) over a set of probe messages —
some explicit requests, some bare mentions, one plainly unrelated line. For each it runs the coarse recall
(search_catalog, the generous first gate) and then, only when a candidate surfaced, the decision (decide, the
precise second gate), executing the reminder tool when one is named — exactly as worker._answer sequences it.
It prints, per probe, which gate closed the door and what was decided, so the sharpening can be read in the open;
then it opens the reminders lens (observe.recent_reminders) and prints each scheduled reminder beside the line
that triggered it — the pairing the card exists for. Its hard assertions are the sharpening's claims:
every explicit request schedules, and every bare mention is declined (whether at the gate or the decision).
If a mention ever schedules, that is the over-eagerness back, and the decision prompt is the knob to move.

It is direct-run, not a pytest test, because it needs the live box:

    python test/qa/0010_observe_reminders_smoke.py            # rolls back at the end (default)
    python test/qa/0010_observe_reminders_smoke.py --keep     # commits, so you can open /observe and see it

Prerequisites (see README, "Models" and "Database & migrations"):
  - Ollama serving the embedding model (`nomic-embed-text`) and the tool-decision model on the box —
    the first gate embeds the message to recall candidates, the second gate is a structured model call.
  - A reachable Postgres — this connects to config.DATABASE_URL (your dev database), migrated to current.

Everything is wrapped in one transaction, rolled back at the end by default,
so the dev store is left exactly as found while the live decision is still proven end to end.
Pass --keep to commit and open /observe → reminders in the shell to see the same pairings rendered.
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
from services import observe
from services.loop import zone
from services.tools import reminder
from services.tools import tools

# The zone the decision resolves a named time against — a fixed, real zone so "tomorrow at 9" lands concretely.
ZONE_NAME = "Europe/Paris"

# The probes: each a message and what a sharpened decision should do with it.
# 'schedule' — an explicit request to be reminded, which must set a reminder.
# 'none'     — a line that only mentions a future task, or is plainly unrelated,
#              which must NOT set one (declined at the gate or the decision — either is a pass).
PROBES = [
    ("remind me to call the dentist tomorrow at 9", "schedule"),
    ("don't let me forget to email Sam tomorrow at 6pm", "schedule"),
    ("I need to call the dentist tomorrow", "none"),
    ("the meeting is at 3 this afternoon", "none"),
    ("I'm heading to the gym later", "none"),
    ("what's the weather like today?", "none"),
]


def _seed_symbiot(conn) -> int:
    # A dedicated smoke symbiot, so the run stands alone and doesn't lean on the seeded one; rolled back anyway.
    return conn.execute(
        "INSERT INTO symbiot (email) VALUES ('smoke-reminders@example.test') RETURNING id"
    ).fetchone()[0]


def _file_intake(conn, symbiot_id: int, message: str) -> int:
    # The triggering message as the loop leaves it: an answered intake row the reminder ties its exactly-once to,
    # and whose text the reminders card shows as the line that asked.
    return conn.execute(
        "INSERT INTO intake (message, answer, symbiot_id, status) VALUES (%s, NULL, %s, 'answered') RETURNING id",
        (message, symbiot_id),
    ).fetchone()[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true",
        help="commit the writes instead of rolling back, so /observe can be opened against them afterwards",
    )
    args = parser.parse_args()

    print(f"database  : {config.DATABASE_URL}")
    print(f"embedding : {config.EMBEDDING_MODEL}")
    print(f"decision  : {config.TOOL_DECISION_MODEL}  (the second gate's model)")
    print(f"ollama    : {config.OLLAMA_BASE_URL}")
    print(f"zone      : {ZONE_NAME}")

    pool = db.open_pool(config.DATABASE_URL)
    db.run_migrations(pool)  # idempotent — brings the dev database to the current schema if it isn't already

    now_local = zone.now_for(ZONE_NAME)
    print(f"now       : {now_local.strftime('%Y-%m-%d %H:%M')} ({ZONE_NAME})")

    with pool.connection() as conn:
        with conn.transaction():
            symbiot_id = _seed_symbiot(conn)
            # The catalog is the first gate's index; reconcile it so the tool descriptor is searchable.
            # Idempotent, so this is safe whether or not the running app already reconciled the dev store.
            tools.reconcile_catalog(conn)

            print(f"\n=== driving the two-gate seam over {len(PROBES)} probes ===")
            failures = []
            for message, expected in PROBES:
                # First gate: coarse recall. Generous by design — its job is to not miss a real request.
                candidates = tools.search_catalog(conn, message)
                if not candidates:
                    tool, via = tools.NO_TOOL, "gate closed — no candidate surfaced"
                    decision = None
                else:
                    # Second gate: the precise decision, over the shortlist and an empty tail (no prior turns here).
                    decision = tools.decide(message, candidates, [], now_local, ZONE_NAME)
                    tool = decision.tool
                    shortlist = ", ".join(f"{c.name}" for c in candidates)
                    via = f"decision over [{shortlist}]"

                effect = ""
                if tool != tools.NO_TOOL:
                    # A tool was named — run it exactly as the worker does, on this connection, in this transaction.
                    intake_id = _file_intake(conn, symbiot_id, message)
                    result = tools.execute(conn, decision, symbiot_id, intake_id, now_local, ZONE_NAME)
                    effect = f" → {'scheduled' if result.effected else 'asked for more'}: {result.summary}"

                got = "schedule" if tool == reminder.NAME else ("none" if tool == tools.NO_TOOL else tool)
                ok = got == expected
                mark = "✓" if ok else "✗"
                if not ok:
                    failures.append((message, expected, got))
                print(f"\n  {mark} [{expected:>8}]  {message!r}")
                print(f"       via {via}")
                print(f"       verdict: {got}{effect}")

            # The reminders lens over what actually got scheduled — the pairing the card renders.
            reminders = observe.recent_reminders(conn, symbiot_id)
            print(f"\n=== observe.recent_reminders — {len(reminders)} scheduled, each with its trigger ===")
            for r in reminders:
                fired = "fired" if r.fired else "pending"
                when = zone.local(r.fire_at, ZONE_NAME).strftime("%a %d %b %Y, %H:%M")
                print(f"  {fired} · {when}")
                print(f"     say : {r.body!r}")
                print(f"     from: {r.trigger!r}")

            # The sharpening's claims, asserted last so every probe is printed for reading before any failure bites.
            expected_scheduled = sum(1 for _, e in PROBES if e == "schedule")
            assert len(reminders) == expected_scheduled, (
                f"expected {expected_scheduled} reminders scheduled (the explicit requests), got {len(reminders)}"
            )
            assert not failures, (
                "the decision didn't match the sharpening's intent on: "
                + "; ".join(f"{m!r} expected {e}, got {g}" for m, e, g in failures)
                + " — if a bare mention scheduled, the over-eagerness is back and the decision prompt is the knob to move"
            )
            print("\n  ✓ every explicit request scheduled, every bare mention was declined")

            if not args.keep:
                raise psycopg.Rollback

        print("\nkept the writes." if args.keep else "\nrolled back — the dev store was left untouched.")

    db.close_pool()
    print("smoke run complete.")


if __name__ == "__main__":
    main()
