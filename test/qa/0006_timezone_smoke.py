"""By-hand smoke test: the symbiot's timezone — inference, storage, and the reply's local-time perception, against live models.

The pytest suite (test/test_zone.py, test/test_reply.py) proves the store round trip,
the validation that only a real IANA name is kept,
and that the reply prompt states the local time — all with the model faked.
It never proves the two things only a live run can:
that the real generative model turns a place named in plain words into the right IANA zone,
and that a live reply, handed that zone's "now", actually answers about time in the human's day rather than UTC.
This script is that other half.

It runs three movements, all on one rolled-back transaction:

  1. inference, live — a handful of places named casually go through zone.infer (the real model),
     and each result is checked to be a zone the system's timezone database actually carries;
     a deliberately unplaceable one ("the moon") must come back None, the honest "say again";
  2. the store round trip — a smoke symbiot's zone is set from "Tokyo" (zone.set_for) and read back (zone.of),
     proving the inferred name persists and reads as the symbiot's own;
  3. the payoff — a live reply is composed to "what time is it for me right now?",
     handed the local now for the zone just set,
     so a correct answer can only come from the local-time line the prompt now carries;
     the reply is printed for a human to judge it speaks Tokyo time, not UTC.

It is direct-run, not a pytest test, because it needs the live box:

    python test/qa/0006_timezone_smoke.py            # rolls back at the end (default)
    python test/qa/0006_timezone_smoke.py --keep     # commits, so you can inspect the row

Prerequisites (see README, "Models" and "Database & migrations"):
  - A generative provider for both the inference and the composed reply (config.RERANK_MODEL / REPLY_MODEL,
    `glm-5.2`): `SCALEWAY_API_KEY` for the primary, or the ladder falls back to Mistral, then local `qwen3.5:4b`.
  - A reachable Postgres — this connects to config.DATABASE_URL (your dev database), migrated to 0016.
"""

import argparse
import os
import sys

# Direct-run from anywhere: put the repo root on the path so `core`/`services` import cleanly.
# This file sits at test/qa/, so the repo root is three directories up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from zoneinfo import available_timezones

import psycopg

from core import config
from core import db
from services.memory import conversation
from services.loop import reply
from services.loop import zone

# Places named the way a human would, casually, plus one that names no place at all.
# The placeable ones must each resolve to a real IANA zone; the last must come back None.
PLACES = [
    "Tokyo",
    "just landed in New York",
    "back home in Strasbourg",
    "Sydney, Australia",
    "the moon",
]

# The place the smoke symbiot is moved to, and the question whose honest answer is the local hour.
HOME = "Tokyo"
QUESTION = "what time is it for me right now?"


def _seed_symbiot(conn) -> int:
    # A dedicated smoke symbiot, so the run stands alone and doesn't lean on the seeded one; rolled back anyway.
    return conn.execute(
        "INSERT INTO symbiot (email) VALUES ('smoke-timezone@example.test') RETURNING id"
    ).fetchone()[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true",
        help="commit the writes instead of rolling back, so the row can be inspected afterwards",
    )
    args = parser.parse_args()

    print(f"database : {config.DATABASE_URL}")
    print(f"infer    : {config.RERANK_MODEL}  (the model that reads a place into an IANA zone)")
    print(f"reply    : {config.REPLY_MODEL}")
    print(f"ollama   : {config.OLLAMA_BASE_URL}")

    all_zones = available_timezones()
    pool = db.open_pool(config.DATABASE_URL)
    db.run_migrations(pool)  # idempotent — brings the dev database to 0016 if it isn't already

    with pool.connection() as conn:
        with conn.transaction():
            # --- 1. inference, live: a place in plain words → a validated IANA zone -------------
            print(f"\n=== inference (live {config.RERANK_MODEL}) ===")
            for place in PLACES:
                resolved = zone.infer(place)
                print(f"  {place!r:32} → {resolved!r}")
                if place == "the moon":
                    assert resolved is None, "an unplaceable location must come back None, not a guessed zone"
                else:
                    assert resolved in all_zones, f"{place!r} resolved to {resolved!r}, not a real IANA zone"
            print("  ✓ every real place resolved to a zone the timezone database carries; the moon placed nothing")

            # --- 2. the store round trip: set from a place, read back as the symbiot's own -----
            symbiot_id = _seed_symbiot(conn)
            assert zone.of(conn, symbiot_id) == "UTC", "a fresh symbiot should carry the UTC default"
            resolved = zone.set_for(conn, symbiot_id, HOME)
            print(f"\n=== the store round trip ===")
            print(f"  set from {HOME!r} → {resolved!r}")
            assert resolved is not None, f"{HOME!r} should have resolved to a zone"
            assert zone.of(conn, symbiot_id) == resolved, "the stored zone should read back as set"
            print(f"  ✓ the symbiot's zone is now {resolved!r}, up from the UTC default")

            # --- 3. the payoff: a live reply that must speak the local hour, not UTC -----------
            # No diary and no conversation here (this smoke isolates the clock), so the only time reference the
            # reply has is the local-now line the prompt now carries — a UTC answer would be the old bug showing.
            now_local = zone.now_for(resolved)
            answer = reply.compose(QUESTION, [], conversation.Conversation(gist=None, tail=[]),
                                    now_local=now_local, zone_name=resolved)
            print(f"\n=== the composed reply (live {config.REPLY_MODEL}) ===")
            print(f"  local now : {now_local.strftime('%A %d %B %Y, %H:%M')} ({resolved})")
            print(f"  question  : {QUESTION!r}")
            print(f"  reply     : {answer}")
            assert answer and answer.strip(), "the composed reply came back empty"
            print("\n  ✓ a non-empty reply composed with the local clock in hand — read it above to judge it "
                  "speaks the symbiot's local time, not UTC")

            if not args.keep:
                raise psycopg.Rollback

        print("\nkept the writes." if args.keep else "\nrolled back — the dev store was left untouched.")

    db.close_pool()
    print("smoke run complete.")


if __name__ == "__main__":
    main()
