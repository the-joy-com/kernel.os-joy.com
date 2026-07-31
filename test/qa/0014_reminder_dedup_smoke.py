"""By-hand smoke test: the machine looking before it leaps — deduplication, and resolving "the dentist one".

The pytest suite (test/test_reminder_agentic.py) proves the arc with the judging call faked:
the hook's window and ranking, the fail-open, the decline row, the writes.
It cannot prove the one thing the whole feature turns on, which is whether a real judge *judges well* —
whether it says "same intent" to a rephrasing of the same errand
and "not the same" to a more specific one that deserves its own reminder.
That is a matter of live judgment, and watching it decide is the whole reason smokes exist.

So this drives the arc end to end against live models, four times, and prints every step:

  1. the same sentence into an empty store — nothing held, so it acts. The control.
  2. the same sentence again, now that the store holds it — the judge should call it the same intent and refuse,
     writing a decline instead of a second reminder.
  3. a *more specific* errand about the same subject —
     "call the dentist about the referral letter" against a held "call the dentist".
     This is the failure mode worth catching: collapsing these two is the mistake,
     so the judge should say not-the-same and the reminder should be set.
  4. "move the dentist one to Thursday at nine" — genuinely ambiguous by this point,
     since two held reminders are about the dentist at the same hour.
     It should be *asked about*, not guessed at:
     being asked which one is far better than the wrong one being changed.
     Nothing is written, so 5 still has both to choose between.
  5. "move the referral letter one to Thursday at nine" — the same ask made precise,
     so the phrase resolves to one row and the edit lands, by an absolute value.

Every judgment is printed with its verdict, so a run can be read rather than merely passed.
The judgments in 2, 3 and 5 are the ones worth watching —
a judge that is too generous, or too shy, is exactly what this surfaces,
so a failure prints what it decided rather than only that it was wrong.

It runs on one rolled-back transaction:

    python test/qa/0014_reminder_dedup_smoke.py            # rolls back at the end (default)
    python test/qa/0014_reminder_dedup_smoke.py --keep     # commits, so the rows can be inspected

Prerequisites (see README, "Models" and "Database & migrations"):
  - Ollama on the box for the embeddings (the catalog reconcile and the gate).
  - A generative provider for the decision, the judgment and the confirmation
    (config.TOOL_DECISION_MODEL / TOOL_OBSERVATION_JUDGE_MODEL / TOOL_CONFIRM_MODEL).
  - A reachable Postgres — this connects to config.DATABASE_URL (your dev database), migrated to 0025.
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
from core import logs
from services.tools import reminder
from services.tools import tools
from services.loop import zone

# The zone the smoke symbiot lives in, so "tomorrow at 9" resolves to a Paris instant.
HOME_ZONE = "Europe/Paris"

FIRST = "remind me to call the dentist tomorrow at 9am"
AGAIN = "don't let me forget to ring the dentist tomorrow morning"
SPECIFIC = "remind me to call the dentist about the referral letter tomorrow at 9am"
MOVE = "move the referral letter one to Thursday at nine"
MOVE_AMBIGUOUS = "move the dentist one to Thursday at nine"


def _seed_symbiot(conn) -> int:
    # A dedicated smoke symbiot in a known zone, so the run stands alone; rolled back anyway.
    return conn.execute(
        "INSERT INTO symbiot (email, timezone) VALUES ('smoke-reminder-dedup@example.test', %s) RETURNING id",
        (HOME_ZONE,),
    ).fetchone()[0]


def _seed_intake(conn, symbiot_id: int, message: str) -> int:
    return conn.execute(
        "INSERT INTO intake (message, symbiot_id, status) VALUES (%s, %s, 'answered') RETURNING id",
        (message, symbiot_id),
    ).fetchone()[0]


def _arc(conn, symbiot_id: int, message: str, label: str) -> tools.ToolResult:
    """One message all the way through retrieve → decide → look → judge → act → speak, printed as it goes.

    The same sequence worker._answer runs, inlined here on one connection so a rolled-back smoke sees its
    own writes, and with each step's result printed so the run can be read.
    """
    print(f"\n=== {label} ===")
    print(f"  said       : {message!r}")
    now_local = zone.now_for(HOME_ZONE)
    intake_id = _seed_intake(conn, symbiot_id, message)

    candidates = tools.search_catalog(conn, message)
    print(f"  gate       : {[c.name for c in candidates]}")
    assert candidates, "the gate should surface at least one reminder tool for a reminding message"

    decision = tools.decide(message, candidates, [], now_local, HOME_ZONE)
    print(f"  decided    : {decision.tool}  {decision.args!r}")
    assert decision.tool != tools.NO_TOOL, "the decision should name a tool for this message"

    observation = tools.observe(conn, decision, symbiot_id, now_local, HOME_ZONE)
    verdict = None
    if observation is None:
        print("  looked     : nothing to judge (no hook, or nothing held near it)")
    else:
        print(f"  looked     : {len(observation.candidates)} candidate(s)")
        for candidate in observation.candidates:
            print(f"               [{candidate.ref}] {candidate.description}")
        verdict = tools.judge_observation(message, observation, now_local, HOME_ZONE)
        named = "nothing — not the same" if verdict.match is None else f"[{verdict.match}]"
        print(f"  judged     : {named}")

    result = tools.execute(conn, decision, symbiot_id, intake_id, now_local, HOME_ZONE, verdict)
    print(f"  outcome    : {result.outcome}")
    print(f"  facts      : {result.summary}")

    confirmation = tools.compose_confirmation(message, result, now_local, HOME_ZONE)
    print(f"  said back  : {confirmation}")
    assert confirmation and confirmation.strip(), "the confirmation came back empty"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true",
        help="commit the writes instead of rolling back, so the rows can be inspected afterwards",
    )
    args = parser.parse_args()

    # Wire the kernel's own log stream, so a run names the model behind every decision and every judgment it makes —
    # a smoke that reads a reply back wrong is exactly when you need to know which tier wrote it.
    logs.configure()

    print(f"database : {config.DATABASE_URL}")
    print(f"decide   : {config.TOOL_DECISION_MODEL}")
    print(f"judge    : {config.TOOL_OBSERVATION_JUDGE_MODEL}")
    print(f"confirm  : {config.TOOL_CONFIRM_MODEL}")
    print(f"ollama   : {config.OLLAMA_BASE_URL}")
    print(f"zone     : {HOME_ZONE}")
    print(f"window   : ±{config.REMINDER_DEDUP_WINDOW_HOURS}h · cap {config.REMINDER_DEDUP_LIMIT}")

    pool = db.open_pool(config.DATABASE_URL)
    db.run_migrations(pool)  # idempotent — brings the dev database to 0024 if it isn't already

    verdicts = []
    with pool.connection() as conn:
        with conn.transaction():
            symbiot_id = _seed_symbiot(conn)
            tools.reconcile_catalog(conn)

            # 1. The control: nothing held, so there is nothing to be a duplicate of.
            first = _arc(conn, symbiot_id, FIRST, "1. into an empty store — the control")
            assert first.outcome == "ACTED", "the first reminder should simply be set"
            held = conn.execute(
                "SELECT count(*) FROM reminder WHERE symbiot_id = %s", (symbiot_id,)
            ).fetchone()[0]
            assert held == 1, f"expected one reminder held, found {held}"

            # 2. The same errand, differently worded. This is the judgment the feature exists for.
            second = _arc(conn, symbiot_id, AGAIN, "2. the same errand, reworded — should be refused")
            verdicts.append(("rewording judged a duplicate", second.outcome == "SATISFIED", second.outcome))
            if second.outcome == "SATISFIED":
                decline = conn.execute(
                    "SELECT i.message, r.body FROM reminder_decline d "
                    "JOIN intake i ON i.id = d.intake_id JOIN reminder r ON r.id = d.reminder_id "
                    "WHERE r.symbiot_id = %s",
                    (symbiot_id,),
                ).fetchone()
                print(f"  decline    : asked {decline[0]!r} · matched {decline[1]!r}")
                assert decline is not None, "a refused duplicate must leave a decline row"

            # 3. A more specific errand about the same subject. Collapsing these two is the mistake.
            third = _arc(conn, symbiot_id, SPECIFIC, "3. a more specific errand — should NOT be refused")
            verdicts.append(("more specific errand kept apart", third.outcome == "ACTED", third.outcome))

            # 4. The other reader of the standing set, on a phrase that genuinely does not resolve:
            # two held reminders are about the dentist at the same hour, so it must ask rather than pick.
            # It writes nothing, which is what leaves 5 with both to choose between.
            fourth = _arc(conn, symbiot_id, MOVE_AMBIGUOUS, "4. an ambiguous phrase — should be asked about")
            verdicts.append(("ambiguous phrase asked about", fourth.outcome == "UNCLEAR", fourth.outcome))

            # 5. The same ask made precise, so the phrase resolves to one row and the edit lands.
            fifth = _arc(conn, symbiot_id, MOVE, "5. a phrase pointing at one held reminder, moved by language")
            verdicts.append(("phrase resolved to a held reminder", fifth.outcome == "ACTED", fifth.outcome))

            print("\n=== the rows, raw ===")
            for row in conn.execute(
                "SELECT id, body, fire_at, intake_id FROM reminder WHERE symbiot_id = %s ORDER BY id",
                (symbiot_id,),
            ).fetchall():
                print(f"  {row}")

            print("\n=== what the live judge decided ===")
            for label, ok, got in verdicts:
                print(f"  {'✓' if ok else '✗'} {label:34} → {got}")
            missed = [(label, got) for label, ok, got in verdicts if not ok]

            if not args.keep:
                raise psycopg.Rollback

        print("\nkept the writes." if args.keep else "\nrolled back — the dev store was left untouched.")

    db.close_pool()
    if missed:
        # Printed, not swallowed:
        # a judge that is too generous (or too shy) is exactly what this smoke is for surfacing,
        # so the run says which judgment went the wrong way rather than only that something did.
        raise AssertionError(
            "the live judge went the wrong way on: "
            + "; ".join(f"{label} (got {got})" for label, got in missed)
        )
    print("smoke run complete — every judgment went the right way.")


if __name__ == "__main__":
    main()
