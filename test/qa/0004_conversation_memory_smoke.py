"""By-hand smoke test: short-term conversational memory — the gradient, against live models.

The pytest suite (test/test_conversation.py, test/test_compression.py, test/test_reply.py) proves the stream write, the uncapped read, the fold trigger, and the exactly-once cutoff,
with the model faked and hand-picked token counts.
It never proves the two things only a live run can:
that real `tiktoken` counts make the fold fire where you'd expect,
and that the real fold model folds a run of turns into a Gist worth reading.
This script is that other half.

It builds a short exchange the way the live path does — a symbiot line and its reply per turn, plus a machine-initiated missive — into the `conversation_item` stream, then:

  1. reads the whole verbatim tail back (conversation.recent) and shows it is uncapped — every turn, in order;
  2. folds the oldest turns into the Gist with a deliberately small trigger budget,
     using the live fold model (config.CONVERSATION_COMPRESS_MODEL, the same heavy hitter that composes the replies),
     and prints the Gist it produced — for a human to judge;
     the fold crosses the model boundary as a validated schema (conversation._FoldReply), not free text,
     so the summary is the only field the model may emit — this run also checks the live Gist carries no
     conversational wrapper (a "Here is the summary:" preamble, a ``` fence) that would compound into the anchor;
  3. proves the state-consistency invariant the fold must never break:
     after the fold, the folded turns and the verbatim tail *partition* the stream exactly —
     no turn in both, none in neither, the two buckets meeting at the cutoff with no gap;
  4. composes a live reply to a follow-up whose answer lives only in the Gist —
     the turns that named it were folded into the summary, and the shortened tail never repeats it —
     so the reply is right only by reaching up into the Gist to find it.

The fold here is orchestrated inline, on this one transaction's connection,
rather than through worker._compress_one —
the sweep uses its own pooled connections and would commit outside this transaction,
where a rolled-back smoke could not see its writes.
The logic mirrors _compress_one exactly (find the over-trigger symbiot, claim its fold, read the Gist and the oldest turns, fold, append),
only on the single connection so the whole run rolls back clean.

It is direct-run, not a pytest test, because it needs the live box:

    python test/qa/0004_conversation_memory_smoke.py            # rolls back at the end (default)
    python test/qa/0004_conversation_memory_smoke.py --keep     # commits, so you can inspect the rows

Prerequisites (see README, "Models" and "Database & migrations"):
  - A generative provider for both the fold and the composed reply —
    they run the same heavy model (config.CONVERSATION_COMPRESS_MODEL defaults to REPLY_MODEL, `glm-5.2`):
    `SCALEWAY_API_KEY` for the primary,
    or the ladder falls back to Mistral, then to the local `qwen3.5:4b`.
  - A reachable Postgres — this connects to config.DATABASE_URL (your dev database), already migrated to 0014.
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

# The deliberately small fold trigger: with the real turns below counting well over this, the tail is
# "over budget" and the fold fires, folding its oldest turns until the remaining tail is back within it.
# Small so a handful of short turns is enough to exercise the fold — the live budget is a share of a 131K
# window, far too large to trip with a toy conversation.
SMALL_BUDGET = 60

# The exchange, in the order it happened. Each (message, answer) is one intake turn — a symbiot line and the
# machine's reply — and the missive is a line the machine raised on its own. The content carries a referent
# worth resolving: two projects named up front, then a follow-up ("and the second one?") that points back to
# the second — the kind of continuity only the conversation memory can supply, never the relevance diary.
TURNS = [
    ("I'm juggling two side projects right now: a bread-baking blog and a small weather app.",
     "Two projects at once — a bread-baking blog and a weather app. Which one is eating more of your time?"),
    ("The weather app, by a mile. The forecasts keep fighting me.",
     "The app would; forecasts have far more moving parts than a blog post does."),
    ("Yeah. I nearly gave up on it twice this month.",
     "Twice in a month is a lot — but you didn't, which says something about how much it matters to you."),
]
MISSIVE = "By the way — you told me you wanted to post on the baking blog every week. This is your nudge."

# The follow-up whose answer lives only in the Gist, not the verbatim tail:
# the turns that named the weather app and its forecast trouble were folded into the summary,
# and the tail that remains never says which project was which —
# so a correct answer ("the weather app") can only come from reaching up into the Gist, which is exactly what this probe tests.
# Composed live at the end so the resolution can be read and judged.
FOLLOW_UP = "remind me — which project was giving me the forecast trouble?"


def _seed_symbiot(conn) -> int:
    # A dedicated smoke symbiot, so the run stands alone and doesn't lean on the seeded one; rolled back anyway.
    return conn.execute(
        "INSERT INTO symbiot (email) VALUES ('smoke-conversation@example.test') RETURNING id"
    ).fetchone()[0]


def _seed_exchange(conn, symbiot_id: int) -> None:
    # Write the exchange onto the stream the way the live path does: the symbiot line and its reply both point
    # at the one intake row (told apart by role), the missive at its own row — the words living in the source,
    # the stream carrying only the pointer, the role, and the write-time token count.
    for message, answer in TURNS:
        intake_id = conn.execute(
            "INSERT INTO intake (message, answer, symbiot_id, status) VALUES (%s, %s, %s, 'answered') RETURNING id",
            (message, answer, symbiot_id),
        ).fetchone()[0]
        conversation.record_utterance(conn, symbiot_id, "symbiot", message, intake_id=intake_id)
        conversation.record_utterance(conn, symbiot_id, "machine", answer, intake_id=intake_id)
    missive_id = conn.execute(
        "INSERT INTO missive (symbiot_id, body) VALUES (%s, %s) RETURNING id",
        (symbiot_id, MISSIVE),
    ).fetchone()[0]
    conversation.record_utterance(conn, symbiot_id, "machine", MISSIVE, missive_id=missive_id)


def _print_tail(label, conv) -> None:
    print(f"\n=== {label} ===")
    print(f"  gist : {conv.gist!r}")
    print(f"  tail ({len(conv.tail)} turns):")
    for t in conv.tail:
        print(f"    {conversation._speaker(t.role):18} {t.text!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true",
        help="commit the writes instead of rolling back, so the rows can be inspected afterwards",
    )
    args = parser.parse_args()

    print(f"database : {config.DATABASE_URL}")
    print(f"fold     : {config.CONVERSATION_COMPRESS_MODEL}  (the heavy hitter, same model as the reply)")
    print(f"reply    : {config.REPLY_MODEL}")
    print(f"ollama   : {config.OLLAMA_BASE_URL}")
    print(f"trigger  : {SMALL_BUDGET} tokens (deliberately small, so a toy exchange trips the fold)")

    pool = db.open_pool(config.DATABASE_URL)
    db.run_migrations(pool)  # idempotent — brings the dev database to 0014 if it isn't already

    with pool.connection() as conn:
        with conn.transaction():
            symbiot_id = _seed_symbiot(conn)
            _seed_exchange(conn, symbiot_id)

            # The stream as written, with the live tiktoken counts that drive the fold.
            items = conn.execute(
                "SELECT id, role, token_count, intake_id, missive_id FROM conversation_item "
                "WHERE symbiot_id = %s ORDER BY id",
                (symbiot_id,),
            ).fetchall()
            total_tokens = sum(r[2] for r in items)
            print(f"\n=== the stream: {len(items)} utterances, {total_tokens} tokens total ===")
            for r in items:
                src = f"intake={r[3]}" if r[3] is not None else f"missive={r[4]}"
                print(f"  id={r[0]:>3}  {r[1]:8}  {r[2]:>3} tok  ({src})")

            # --- 1. the uncapped read: the whole tail, back to the (absent) cutoff -------------
            before = conversation.recent(conn, symbiot_id)
            _print_tail("the verbatim tail before folding (uncapped — every turn)", before)
            assert before.gist is None, "no fold has happened yet, so there should be no Gist"
            assert len(before.tail) == len(items), "the read must return the whole tail, uncapped"
            print(f"  ✓ all {len(items)} turns returned verbatim, though {total_tokens} tokens far exceeds the "
                  f"{SMALL_BUDGET}-token budget — the read does not truncate")

            # --- 2. the fold, live (mirrors worker._compress_one on this one connection) -------
            symbiot_to_fold = conversation.next_symbiot_to_fold(conn, SMALL_BUDGET)
            assert symbiot_to_fold == symbiot_id, "the over-budget tail should have made this symbiot eligible to fold"
            assert conversation.claim_fold(conn, symbiot_id), "no other worker holds this symbiot's fold, so the claim should succeed"
            gist_row = conversation.current_gist(conn, symbiot_id)
            cutoff = gist_row[1] if gist_row is not None else 0
            to_fold, new_cutoff = conversation.pending_for_fold(conn, symbiot_id, SMALL_BUDGET, cutoff)
            print(f"\n=== the fold (live {config.CONVERSATION_COMPRESS_MODEL}) ===")
            print(f"  folding {len(to_fold)} of {len(items)} turns (the oldest, past the budget); "
                  f"new cutoff → item {new_cutoff}")
            merged = conversation.fold(gist_row[0] if gist_row is not None else None, to_fold)
            conversation.record_gist(conn, symbiot_id, merged, new_cutoff)
            print(f"  the Gist it produced:\n    {merged!r}")
            assert merged and merged.strip(), "the fold produced an empty Gist"
            # The isolation guarantee, checked against the live model: the schema boundary (conversation._FoldReply)
            # gives filler nowhere to land, so the raw Gist starts straight into the summary — no code fence, no
            # "Here is the summary:" preamble. This matters because each Gist seeds the next fold, so any wrapper
            # that slipped through would bake into the anchor and compound over time.
            opener = merged.lstrip().lower()
            assert not opener.startswith("```"), "the Gist opened with a code fence — meta-text bled into the anchor"
            assert not opener.startswith(("here is", "here's", "summary:", "sure,", "certainly")), (
                f"the Gist opened with a conversational preamble — the schema boundary should have prevented it: {merged!r}"
            )
            print("  ✓ the Gist is clean prose — no preamble, no fence — so nothing meta compounds into the anchor")

            # --- 3. the state-consistency invariant: folded + tail partition the stream --------
            after = conversation.recent(conn, symbiot_id)
            _print_tail("the memory after folding — Gist, then the shortened verbatim tail", after)
            assert after.gist == merged, "the current Gist should be the paragraph just folded"
            folded_count = len(to_fold)
            tail_count = len(after.tail)
            print(f"\n=== zero-gap check ===")
            print(f"  folded into the Gist : {folded_count} turns (id ≤ {new_cutoff})")
            print(f"  verbatim tail        : {tail_count} turns (id > {new_cutoff})")
            print(f"  stream total         : {len(items)} turns")
            assert folded_count + tail_count == len(items), (
                "gap or overlap: the folded turns and the verbatim tail must partition the stream exactly"
            )
            assert tail_count >= 1, "the fold should leave the most recent turns verbatim, not fold everything"
            print("  ✓ every turn is in exactly one bucket — the Gist and the tail meet at the cutoff, no gap")

            # --- 4. the payoff: a live reply that must reach back into the memory --------------
            # No diary facts here (this smoke isolates the conversation), so continuity can only come from the
            # short-term memory just assembled — the Gist and the tail. Read the reply to judge whether it did.
            from services.loop import reply

            answer = reply.compose(FOLLOW_UP, [], after)
            print(f"\n=== the composed reply (live {config.REPLY_MODEL}) ===")
            print(f"  follow-up : {FOLLOW_UP!r}")
            print(f"  reply     : {answer}")
            assert answer and answer.strip(), "the composed reply came back empty"
            print("\n  ✓ a non-empty reply composed off the conversation memory — read it above to judge whether "
                  "it reached up into the Gist to name the weather app")

            if not args.keep:
                raise psycopg.Rollback

        print("\nkept the writes." if args.keep else "\nrolled back — the dev store was left untouched.")

    db.close_pool()
    print("smoke run complete.")


if __name__ == "__main__":
    main()
