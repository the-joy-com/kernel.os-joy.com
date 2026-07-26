"""By-hand smoke test: a diary fact's time is resolved in the symbiot's own zone, against live models.

The pytest suite (test/test_ontology.py) fakes the model at the network boundary,
so it proves the prompt states the local clock
and that the returned reading is stamped with the symbiot's zone —
but with a canned reply, never that a live model,
told "your local now is such-and-such in Tokyo",
actually reads "this evening" or "3pm" as the hour of the human's day.
This script is that other half.

It is the ingestion mirror of 0006 (which proves the *reply* speaks local time):
here the concern is the *write* —
the one temporal particular the thin path promotes into structure.
The bug it guards against is the old behaviour,
where the extractor resolved every cue against the server's UTC clock,
so a symbiot far from Greenwich had "3pm today" filed as 15:00 UTC —
which, read back on their own clock, slid to midnight of the next day.
The fix threads the symbiot's zone into ingestion (worker._ingest_one → ontology.ingest),
states the reference as their local now,
and stamps the returned wall-clock with their zone.

It runs three facts for a smoke symbiot moved to Tokyo
(UTC+9, no daylight-saving shift, so the day boundary is unambiguous),
all on one rolled-back transaction:

  1. an explicit time of day — "dentist today at 3pm" —
     whose stored instant, read back on the Tokyo clock,
     must land at 15:00 on the local date
     (not 15:00 UTC, which would read as the small hours of the next day);
  2. a relative day — "boxing yesterday morning" —
     whose local date must be the day before the local now
     (the drift the old UTC framing introduced lands it a day early
     when the symbiot's morning is UTC's night);
  3. a fact with no time cue at all — "I live in Strasbourg" —
     which must file a null happened_at,
     so the read path stands created_at in for it
     rather than inventing a precision the fact never carried.

Storage stays UTC throughout — happened_at is an absolute instant (a TIMESTAMPTZ).
The zone enters only to find the *correct* instant at write time
and to read it back on the human's clock;
the run prints both the stored-UTC value and the local reading so the distinction is visible.

It is direct-run, not a pytest test, because it needs the live box:

    python test/qa/0011_ingestion_timezone_smoke.py            # rolls back at the end (default)
    python test/qa/0011_ingestion_timezone_smoke.py --keep     # commits, so you can inspect the rows

Prerequisites (see README, "Models" and "Database & migrations"):
  - Ollama running with the embedder pulled,
    and a generative provider reachable for the temporal extraction and the concept routing
    (the same ladder 0001 documents).
  - A reachable Postgres with pgvector —
    this connects to config.DATABASE_URL (your dev database), migrated to 0016.
"""

import argparse
import os
import sys
from datetime import timedelta, timezone

# Direct-run from anywhere: put the repo root on the path so `core`/`services` import cleanly.
# This file sits at test/qa/, so the repo root is three directories up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import psycopg

from core import config
from core import db
from services.loop import zone
from services.memory import ontology

# The place the smoke symbiot lives, chosen for a large, DST-free offset (UTC+9)
# so the day boundary is unambiguous:
# the fix and the bug land the facts on visibly different calendar days.
HOME_ZONE = "Asia/Tokyo"

# The three facts, one per branch the ingestion time path can take.
RAW_EXPLICIT = "Had a dentist appointment today at 3pm"
RAW_RELATIVE = "Went boxing yesterday morning"
RAW_NO_CUE = "I live in Strasbourg"


def _happened_at(conn, fact_id: int):
    # The absolute instant the fact was filed under (a TIMESTAMPTZ), or None when it named no moment.
    return conn.execute(
        "SELECT happened_at FROM diary_facts WHERE id = %s", (fact_id,)
    ).fetchone()[0]


def _seed_symbiot(conn) -> int:
    # A dedicated smoke symbiot moved straight to Tokyo, so the run stands alone; rolled back anyway.
    # The zone is set directly rather than through zone.set_for:
    # this smoke is about ingestion, not the place-to-zone inference 0006 already proves,
    # so it should not depend on that model call.
    symbiot_id = conn.execute(
        "INSERT INTO symbiot (email) VALUES ('smoke-ingestion-tz@example.test') RETURNING id"
    ).fetchone()[0]
    conn.execute("UPDATE symbiot SET timezone = %s WHERE id = %s", (HOME_ZONE, symbiot_id))
    return symbiot_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true",
        help="commit the writes instead of rolling back, so the rows can be inspected afterwards",
    )
    args = parser.parse_args()

    print(f"database : {config.DATABASE_URL}")
    print(f"embed    : {config.EMBEDDING_MODEL}")
    print(f"extract  : {config.RERANK_MODEL}  (also the model that reads a fact's time)")
    print(f"ollama   : {config.OLLAMA_BASE_URL}")

    pool = db.open_pool(config.DATABASE_URL)
    db.run_migrations(pool)  # idempotent — brings the dev database to 0016 if it isn't already

    with pool.connection() as conn:
        with conn.transaction():
            symbiot_id = _seed_symbiot(conn)
            zone_name = zone.of(conn, symbiot_id)
            assert zone_name == HOME_ZONE, f"the smoke symbiot's zone should read as {HOME_ZONE!r}"
            # The reference clock the facts are read against, captured once here for the assertions below;
            # ontology.ingest reads its own now_for a beat later, so these agree except exactly at midnight.
            now_local = zone.now_for(zone_name)
            print(f"\nsymbiot zone : {zone_name}")
            print(f"local now    : {now_local.strftime('%A %d %B %Y, %H:%M')} ({zone_name})")

            # --- 1. an explicit time of day: "3pm today" must read as 15:00 on the local date ----------
            # This is the sharpest catch: a UTC-framed extractor would file 15:00 UTC,
            # which on the Tokyo clock is 00:00 the following day — wrong hour and wrong day at once.
            # The threaded zone files the instant whose Tokyo reading is 15:00 today.
            explicit_id = ontology.ingest(conn, RAW_EXPLICIT, zone_name=zone_name)
            stored = _happened_at(conn, explicit_id)
            assert stored is not None, "an explicit time of day should have filed a happened_at, not null"
            local = zone.local(stored, zone_name)
            print(f"\n=== explicit time of day ===")
            print(f"  raw text     : {RAW_EXPLICIT!r}")
            print(f"  stored (UTC) : {stored.astimezone(timezone.utc).isoformat()}")
            print(f"  local read   : {local.strftime('%A %d %B %Y, %H:%M')} ({zone_name})")
            assert local.hour == 15, f"'3pm' should read as 15:00 local, got {local.hour:02d}:00 — the old UTC framing?"
            assert local.date() == now_local.date(), (
                f"'today' should read as the local date {now_local.date()}, got {local.date()} — a day-boundary drift"
            )
            print("  ✓ '3pm today' filed as the instant that reads 15:00 on the local date, not 15:00 UTC")

            # --- 2. a relative day: "yesterday" must read as the day before the local now ---------------
            relative_id = ontology.ingest(conn, RAW_RELATIVE, zone_name=zone_name)
            stored = _happened_at(conn, relative_id)
            assert stored is not None, "a relative day cue should have filed a happened_at, not null"
            local = zone.local(stored, zone_name)
            print(f"\n=== relative day ===")
            print(f"  raw text     : {RAW_RELATIVE!r}")
            print(f"  stored (UTC) : {stored.astimezone(timezone.utc).isoformat()}")
            print(f"  local read   : {local.strftime('%A %d %B %Y, %H:%M')} ({zone_name})")
            assert local.date() == now_local.date() - timedelta(days=1), (
                f"'yesterday' should read as {now_local.date() - timedelta(days=1)} local, got {local.date()}"
            )
            print("  ✓ 'yesterday' filed on the local day before now (the day-boundary drift the old UTC framing caused)")

            # --- 3. no time cue: happened_at stays null, so the read path uses created_at ---------------
            no_cue_id = ontology.ingest(conn, RAW_NO_CUE, zone_name=zone_name)
            stored = _happened_at(conn, no_cue_id)
            print(f"\n=== no time cue ===")
            print(f"  raw text     : {RAW_NO_CUE!r}")
            print(f"  stored       : {stored!r}")
            assert stored is None, "a fact with no time expression must file a null happened_at, never a guessed one"
            print("  ✓ no cue → null happened_at; the read path will stand created_at in for it")

            if not args.keep:
                raise psycopg.Rollback

        print("\nkept the writes." if args.keep else "\nrolled back — the dev store was left untouched.")

    db.close_pool()
    print("smoke run complete.")


if __name__ == "__main__":
    main()
