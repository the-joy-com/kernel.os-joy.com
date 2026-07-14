"""By-hand smoke test: a reminder's fire time is the human's wall clock — never shifted by a timezone.

This guards the invariant a live incident broke: the symbiot asked, at ~18:35 Paris, for a reminder
about a cafe closing ~90 minutes out, and the reminder was stored two hours early (18:05 Paris instead
of ~20:05) and fired at once. The decision had handed back the fire time as a bare UTC reading, and the
executor's old rule — "a fire_at with no timezone is the local wall clock" — stamped that UTC value as
local, shifting it two hours early. A sibling failure shifts two hours *late*: a model that labels a
correct local reading with a UTC offset, which a trusting astimezone then converts forward.

The pytest suite proves the executor's exactly-once and the schema with the model faked; it never proves
what only a live run can — that the whole decide → act path lands a real model's time on the right instant,
in the human's day, whatever offset shape the model chose. This is that half, and it holds two things:

  1. THE CONTRACT (deterministic, no model): for one intended local time (20:05 Paris), every offset shape a
     model might emit — bare, correctly offset, or local-components-with-a-UTC-label — must store the same
     20:05 Paris instant, because the executor reads the clock face and stamps the symbiot's zone, discarding
     the model's offset guess. And the exact incident value (the intended time as a bare UTC reading, which
     lands in the past) must be REFUSED, not stored — a reminder is for the future, so a non-future instant
     means it was read wrong, and the executor asks again rather than firing garbage the moment it is written.

  2. THE LIVE PATH: the real decision, on a plain relative sentence ("remind me in 30 minutes"), must resolve
     to an instant close to now + 30 min and strictly in the future — never shifted an hour or two off.

It runs on one rolled-back transaction, and is direct-run, not pytest, because leg 2 needs the live box:

    python test/qa/0008_reminder_timezone_smoke.py            # rolls back at the end (default)
    python test/qa/0008_reminder_timezone_smoke.py --keep     # commits, so you can inspect the rows

Prerequisites (see README, "Models" and "Database & migrations"):
  - A generative provider for the live decision (config.TOOL_DECISION_MODEL): `SCALEWAY_API_KEY` for the
    primary, or the ladder falls back to Mistral, then local `qwen3.5:4b`. Leg 1 needs no model at all.
  - A reachable Postgres — this connects to config.DATABASE_URL (your dev database), migrated to 0017.
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Direct-run from anywhere: put the repo root on the path so `core`/`services` import cleanly.
# This file sits at test/qa/, so the repo root is three directories up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import psycopg

from core import config
from core import db
from services.loop import zone
from services.tools import reminder
from services.tools import tools

# The zone the smoke symbiot lives in, so a resolved time is read against a real, offset-bearing zone.
HOME_ZONE = "Europe/Paris"

# Leg 1 works one intended local time from every angle a model might phrase it.
# A fixed "now" and date keep the contract wholly deterministic: the incident value sits in the past of it.
FIXED_NOW = datetime(2026, 7, 14, 18, 35, tzinfo=ZoneInfo(HOME_ZONE))
INTENDED_LOCAL = datetime(2026, 7, 14, 20, 5)  # 20:05 on the human's own clock — the one right answer

# The live leg drives a plain relative sentence; 30 minutes is unambiguous and easy to check tightly.
LIVE_MESSAGE = "remind me in 30 minutes to stretch"
LIVE_TOLERANCE_MIN = 4  # the resolved instant must sit within this of now + 30 min


def _seed_symbiot(conn) -> int:
    # A dedicated smoke symbiot in a known zone, so the run stands alone; rolled back anyway.
    return conn.execute(
        "INSERT INTO symbiot (email, timezone) VALUES ('smoke-tz@example.test', %s) RETURNING id",
        (HOME_ZONE,),
    ).fetchone()[0]


def _seed_intake(conn, symbiot_id: int) -> int:
    # A fresh triggering message per stored reminder — the reminder's intake_id is unique, so each case needs its own.
    return conn.execute(
        "INSERT INTO intake (message, symbiot_id, status) VALUES ('tz smoke', %s, 'answered') RETURNING id",
        (symbiot_id,),
    ).fetchone()[0]


def _store(conn, symbiot_id: int, fire_at, now_local):
    # Drive the real dispatch (tools.execute re-validates through the tool's args_model, exactly as the worker does),
    # then read back the stored instant expressed on the human's clock. Returns (result, stored-local or None).
    intake_id = _seed_intake(conn, symbiot_id)
    decision = tools.Decision("schedule_reminder", {"reminder_message": "stand up and stretch", "fire_at": fire_at, "channels": None})
    result = tools.execute(conn, decision, symbiot_id, intake_id, now_local, HOME_ZONE)
    row = conn.execute("SELECT fire_at FROM reminder WHERE intake_id = %s", (intake_id,)).fetchone()
    stored_local = row[0].astimezone(ZoneInfo(HOME_ZONE)) if row else None
    return result, stored_local


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true",
        help="commit the writes instead of rolling back, so the rows can be inspected afterwards",
    )
    args = parser.parse_args()

    print(f"database : {config.DATABASE_URL}")
    print(f"decide   : {config.TOOL_DECISION_MODEL}")
    print(f"zone     : {HOME_ZONE}")

    pool = db.open_pool(config.DATABASE_URL)
    db.run_migrations(pool)  # idempotent — brings the dev database to 0017 if it isn't already

    with pool.connection() as conn:
        with conn.transaction():
            symbiot_id = _seed_symbiot(conn)
            want = INTENDED_LOCAL.strftime("%H:%M")

            # --- 1. the contract (deterministic): every offset shape lands on the one local instant ------
            # The three shapes a model legitimately emits for "20:05 on the human's clock", plus the
            # incident: the intended time as a bare UTC reading, which lands in the past and must be refused.
            utc_of_intended = INTENDED_LOCAL.replace(tzinfo=ZoneInfo(HOME_ZONE)).astimezone(timezone.utc).replace(tzinfo=None)
            cases = [
                ("bare local reading",                 INTENDED_LOCAL,                                          want),
                ("correct zone offset",                INTENDED_LOCAL.replace(tzinfo=ZoneInfo(HOME_ZONE)),      want),
                ("local components, UTC label",        INTENDED_LOCAL.replace(tzinfo=timezone.utc),             want),
                ("incident: intended as bare UTC",     utc_of_intended,                                         None),
            ]
            print(f"\n=== 1. the contract (no model) — one intended time {want} Paris, now {FIXED_NOW.strftime('%H:%M')} ===")
            for label, fire_at, expected in cases:
                result, stored = _store(conn, symbiot_id, fire_at, FIXED_NOW)
                got = stored.strftime("%H:%M") if stored else None
                if expected is None:
                    ok = result.effected is False and stored is None
                    verdict = "refused (past)" if ok else "STORED A PAST INSTANT"
                else:
                    ok = result.effected and got == expected
                    verdict = f"stored {got}" if ok else f"stored {got}, wanted {expected}"
                print(f"  {'✓' if ok else '✗'} {label:32} emitted={fire_at!r} -> {verdict}")
                assert ok, f"contract broken for '{label}': {verdict}"
            print(f"  ✓ every shape stored {want} Paris; the incident value was refused, not shifted")

            # --- 2. the live path: the real decision resolves to a near, future instant -------------------
            now_local = zone.now_for(HOME_ZONE)
            target = now_local + timedelta(minutes=30)
            candidates = [tools.ToolCandidate(reminder.NAME, reminder.DESCRIPTION, None)]
            decision = tools.decide(LIVE_MESSAGE, candidates, [], now_local, HOME_ZONE)
            print(f"\n=== 2. the live decision ({config.TOOL_DECISION_MODEL}) — {LIVE_MESSAGE!r} ===")
            print(f"  local now : {now_local.isoformat()}")
            print(f"  emitted   : {decision.args.get('fire_at')!r}")
            assert decision.tool == "schedule_reminder", "the decision should name the reminder tool"
            result, stored = _store(conn, symbiot_id, decision.args.get("fire_at"), now_local)
            assert result.effected and stored is not None, f"the reminder was not stored: {result.summary}"
            drift_min = (stored - target).total_seconds() / 60.0
            print(f"  stored    : {stored.isoformat()}  (target ~{target.strftime('%H:%M')}, drift {drift_min:+.1f} min)")
            assert stored > now_local, "the resolved instant is in the past — it would fire immediately"
            assert abs(drift_min) <= LIVE_TOLERANCE_MIN, (
                f"the resolved instant is {drift_min:+.0f} min off — a timezone shift is the usual cause"
            )
            print(f"  ✓ resolved within {LIVE_TOLERANCE_MIN} min of the target, and safely in the future")

            if not args.keep:
                raise psycopg.Rollback

        print("\nkept the writes." if args.keep else "\nrolled back — the dev store was left untouched.")

    db.close_pool()
    print("smoke run complete — a reminder's time stays the human's, whatever the model emits.")


if __name__ == "__main__":
    main()
