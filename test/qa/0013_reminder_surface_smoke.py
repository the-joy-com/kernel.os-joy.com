"""By-hand smoke test: the standing reminder set over real HTTP — the read, and the three writes.

The pytest suite (test/test_reminders_surface.py) proves the whole surface against a TestClient:
the auth gate, the deterministic parse, the scoped writes, the legible refusals.
It never proves the one thing only a live run can —
that the routes answer over the wire the way the shell reads them,
against the dev database, in the symbiot's own real zone,
with the firing sweep actually running alongside rather than switched off.
That last part is the point: this smoke deliberately leaves a live kernel's reminder sweep running
and watches a cancelled reminder *not* fire when its moment comes.

It walks the surface the way a hand at the terminal walks it:

  1. mint a session for the smoke symbiot and read the standing set — empty to start;
  2. add two reminders through the terse grammar (+2h and a bare time), and read them back numbered;
  3. edit one — move its time, then reword it — and confirm each landed and the other column didn't move;
  4. cancel one, and confirm the row is stamped rather than gone;
  5. hold the cancelled one open past its moment and confirm the live sweep leaves it alone;
  6. refuse three ways, and read the reasons: a time outside the grammar, a past time, a second cancel.

It writes to the dev database through the running kernel,
so it cannot roll itself back the way the in-process smokes do — the kernel's own connections commit.
Instead it cleans up after itself:
every row it made is deleted at the end, unless --keep says to leave them for inspection.

It is direct-run, not a pytest test, because it needs a live kernel on the box:

    python test/qa/0013_reminder_surface_smoke.py            # cleans up after itself (default)
    python test/qa/0013_reminder_surface_smoke.py --keep     # leaves the rows, so they can be inspected

Prerequisites:
  - The kernel running and reachable (see README): by default http://127.0.0.1:9713, or pass --kernel.
  - A reachable Postgres — this connects to config.DATABASE_URL (your dev database) to mint the session
    and to read the raw rows back, the same database the running kernel is pointed at.
  - Nothing generative: this whole path spends no model call, which is the point of it.
"""

import argparse
import os
import sys
from datetime import timedelta

# Direct-run from anywhere: put the repo root on the path so `core`/`services` import cleanly.
# This file sits at test/qa/, so the repo root is three directories up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import secrets
import urllib.error
import urllib.request
import json

from core import config
from core import db
from core import logs
from services import identity
from services.loop import zone

# The zone the smoke symbiot lives in, so a bare time is read on a clock that isn't the box's UTC.
HOME_ZONE = "Europe/Paris"

EMAIL = "smoke-reminder-surface@example.test"


def _call(kernel: str, path: str, token: str, payload=None) -> dict:
    # One round trip in the envelope shape, over real HTTP, with the session token the shell sends.
    # A 4xx still carries a JSON body worth reading, so an HTTPError is unwrapped rather than raised.
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{kernel}{path}",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return json.loads(error.read())


def _mint_session(conn) -> tuple[int, str]:
    # A dedicated smoke symbiot in a known zone, and a session for it written the way identity writes one —
    # the token hashed at rest, so this mints a real session rather than a shape the route would reject.
    # Minted directly because /login emails the code to a real mailbox, which a script can't read.
    # Any leftover from a prior --keep run is cleared first, so the smoke is always re-runnable.
    conn.execute("DELETE FROM symbiot WHERE email = %s", (EMAIL,))
    symbiot_id = conn.execute(
        "INSERT INTO symbiot (email, timezone) VALUES (%s, %s) RETURNING id", (EMAIL, HOME_ZONE)
    ).fetchone()[0]
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO session (symbiot_id, token_hash, expires_at) VALUES (%s, %s, now() + interval '1 hour')",
        (symbiot_id, identity._hash(token)),
    )
    return symbiot_id, token


def _rows(conn, symbiot_id: int):
    return conn.execute(
        "SELECT id, body, fire_at, fired_at, cancelled_at FROM reminder WHERE symbiot_id = %s ORDER BY id",
        (symbiot_id,),
    ).fetchall()


def _show(listed: list[dict]) -> None:
    if not listed:
        print("    (nothing held)")
    for i, r in enumerate(listed, start=1):
        print(f"    {i}. [{r['id']}] {r['fire_at']}  {r['body']!r}  — {r['state']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", default="http://127.0.0.1:9713", help="the running kernel's base URL")
    parser.add_argument(
        "--keep", action="store_true",
        help="leave the rows behind instead of deleting them, so they can be inspected afterwards",
    )
    args = parser.parse_args()

    # Wire the kernel's own log stream, so a run names the model behind every generative call it makes —
    # a smoke that reads a reply back wrong is exactly when you need to know which tier wrote it.
    logs.configure()

    print(f"kernel   : {args.kernel}")
    print(f"database : {config.DATABASE_URL}")
    print(f"zone     : {HOME_ZONE}")

    pool = db.open_pool(config.DATABASE_URL)
    with pool.connection() as conn:
        symbiot_id, token = _mint_session(conn)
    print(f"symbiot  : {symbiot_id} ({EMAIL})")

    try:
        # --- 1. the read, before anything is held -----------------------------------------------
        body = _call(args.kernel, "/reminders", token)
        print(f"\n=== 1. the standing set, empty ===")
        print(f"  msg : {body['msg']!r}   zone: {body['data']['timezone']!r}")
        assert body["msg"] == "reminders", f"the read should answer 'reminders', got {body['msg']!r}"
        assert body["data"]["reminders"] == [], "a fresh symbiot holds nothing"
        assert body["data"]["timezone"] == HOME_ZONE, "the set is rendered on the symbiot's own clock"
        print("  ✓ the machine can be asked what it is holding, and answers honestly")

        # --- 2. two reminders typed straight in -------------------------------------------------
        now_local = zone.now_for(HOME_ZONE)
        bare = (now_local + timedelta(hours=5)).strftime("%H:%M")
        body = _call(args.kernel, "/reminders", token, {"say": "call the dentist", "when": "+2h"})
        assert body["msg"] == "reminders", body
        body = _call(args.kernel, "/reminders", token, {"say": "email Sam", "when": bare})
        print(f"\n=== 2. two reminders, typed straight in (+2h, and a bare {bare}) ===")
        _show(body["data"]["reminders"])
        assert body["msg"] == "reminders", body
        listed = body["data"]["reminders"]
        assert len(listed) == 2, "both should stand — a null intake_id never conflicts with another"
        assert [r["state"] for r in listed] == ["pending", "pending"]
        with pool.connection() as conn:
            pins = [r[0] for r in conn.execute(
                "SELECT intake_id FROM reminder WHERE symbiot_id = %s", (symbiot_id,)
            ).fetchall()]
        assert pins == [None, None], "nothing was said to produce these, so they carry no exactly-once pin"
        print("  ✓ two directly-made reminders stand, neither pinned to a message")

        dentist, sam = listed[0]["id"], listed[1]["id"]

        # --- 3. an edit: the time, then the line ------------------------------------------------
        with pool.connection() as conn:
            before = conn.execute("SELECT fire_at FROM reminder WHERE id = %s", (dentist,)).fetchone()[0]
        body = _call(args.kernel, "/reminders/update", token, {"id": dentist, "when": "+3d"})
        assert body["msg"] == "reminders", body
        with pool.connection() as conn:
            said, moved = conn.execute(
                "SELECT body, fire_at FROM reminder WHERE id = %s", (dentist,)
            ).fetchone()
        assert moved > before and said == "call the dentist", "a time alone leaves the line where it was"
        body = _call(
            args.kernel, "/reminders/update", token,
            {"id": dentist, "say": "call the dentist about the referral letter"},
        )
        assert body["msg"] == "reminders", body
        with pool.connection() as conn:
            said, still = conn.execute(
                "SELECT body, fire_at FROM reminder WHERE id = %s", (dentist,)
            ).fetchone()
        print(f"\n=== 3. the edit ===")
        _show(body["data"]["reminders"])
        assert still == moved, "a line alone leaves the time where it was"
        assert said == "call the dentist about the referral letter"
        print("  ✓ each column moves on its own — an absolute value into one column at a time")

        # --- 4. a cancel: stamped, not deleted --------------------------------------------------
        body = _call(args.kernel, "/reminders/cancel", token, {"id": sam})
        print(f"\n=== 4. the cancel ===")
        _show(body["data"]["reminders"])
        assert body["msg"] == "reminders", body
        with pool.connection() as conn:
            cancelled_at = conn.execute(
                "SELECT cancelled_at FROM reminder WHERE id = %s", (sam,)
            ).fetchone()[0]
        assert cancelled_at is not None, "cancelling stamps the row"
        assert any(r["id"] == sam and r["state"] == "cancelled" for r in body["data"]["reminders"]), (
            "the cancelled one is still visible, tagged — recorded, not dropped"
        )
        print("  ✓ recorded, not dropped — the symbiot sees they called it off")

        # --- 5. the live sweep leaves a cancelled reminder alone --------------------------------
        # The one thing only a live run proves.
        # Backdate the cancelled reminder past its moment,
        # and give the running sweep (REMINDER_SWEEP_INTERVAL_SECONDS, ~10s) more than one pass at it.
        with pool.connection() as conn:
            conn.execute(
                "UPDATE reminder SET fire_at = now() - interval '1 minute' WHERE id = %s", (sam,)
            )
            missives_before = conn.execute(
                "SELECT count(*) FROM missive WHERE symbiot_id = %s", (symbiot_id,)
            ).fetchone()[0]
        wait = config.REMINDER_SWEEP_INTERVAL_SECONDS * 2.5
        print(f"\n=== 5. the live sweep, given {wait:.0f}s at an overdue-but-cancelled reminder ===")
        import time

        time.sleep(wait)
        with pool.connection() as conn:
            fired_at, still_cancelled = conn.execute(
                "SELECT fired_at, cancelled_at FROM reminder WHERE id = %s", (sam,)
            ).fetchone()
            missives_after = conn.execute(
                "SELECT count(*) FROM missive WHERE symbiot_id = %s", (symbiot_id,)
            ).fetchone()[0]
        print(f"  fired_at : {fired_at}   cancelled_at: {still_cancelled}")
        print(f"  missives : {missives_before} → {missives_after}")
        assert fired_at is None, "a cancelled reminder must never fire"
        assert missives_after == missives_before, "and must raise no missive"
        print("  ✓ the sweep's claim honours the cancel — calling one off is real, not cosmetic")

        # --- 6. the three refusals, and their reasons ------------------------------------------
        print(f"\n=== 6. the refusals ===")
        cases = [
            ("/reminders", {"say": "x", "when": "sometime next week-ish"}, "a time outside the grammar"),
            (
                "/reminders",
                {"say": "x", "when": (now_local - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")},
                "a time already past",
            ),
            ("/reminders/cancel", {"id": sam}, "a second cancel"),
        ]
        for path, payload, label in cases:
            body = _call(args.kernel, path, token, payload)
            print(f"  {label:28} → {body['msg']!r}: {body['data'].get('reason')!r}")
            assert body["msg"] == "that reminder change didn't take", body
            assert body["data"].get("reason"), "a refusal has to say why, in words the shell can print"
        print("  ✓ each refusal is legible — the shell prints the reason as-is")

        with pool.connection() as conn:
            print(f"\n=== the rows, raw ===")
            for row in _rows(conn, symbiot_id):
                print(f"  {row}")

    finally:
        if args.keep:
            print(f"\nkept the rows — symbiot {symbiot_id}, {EMAIL}.")
        else:
            # The kernel's own connections committed, so there is no transaction to roll back;
            # the symbiot cascades onto its session, its reminders and its missives.
            with pool.connection() as conn:
                conn.execute("DELETE FROM symbiot WHERE id = %s", (symbiot_id,))
            print("\ncleaned up — the smoke symbiot and everything it made are gone.")
        db.close_pool()

    print("smoke run complete.")


if __name__ == "__main__":
    main()
