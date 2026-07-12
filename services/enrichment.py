"""Enrichment: the deep second pass that follows up on a fast answer — the composing half of Tier 2.

Tier 1 answers the moment a message lands, from the lexical diary reach and the running conversation.
This module is what happens a beat later, off the critical path:
once that answer is settled, the deep reach (deep_retrieval.py) gathers the facts that bear on the message by *meaning* rather than by shared words,
and — only when they add something the fast answer didn't — the machine sends an enriched follow-up.

Because the pass lands after the fast reply, and the conversation may have moved on since,
the composing call is given the whole *origin reference* the follow-up must situate itself against, all three legs of it:
  (a) the message that prompted the fast answer,
  (b) the fast answer itself — so the model can see what it already said and not merely restate it, and
  (c) the turns exchanged since — so a delayed follow-up reads as caught up, not as arriving out of order.
Leg (b) is load-bearing twice over:
it is how the gate judges whether the enrichment adds new ground,
and the compose prompt names it explicitly and forbids repeating it, so no-repeat is instructed, not left to chance.

One structured call both gates and composes:
it returns whether to surface at all and, if so, the follow-up's words —
strict-Pydantic-as-decoder-grammar, the same boundary discipline the router's calls keep.
When the deep reach found nothing, the call is skipped entirely —
there is nothing to weigh, so no metered model call is spent.
A follow-up worth sending is delivered as a missive (the kernel reaching out on its own), never as an inline reply,
because by now it is a new turn in the conversation, not the answer to the message that started it.

Delivery and the exactly-once record are the worker's to sequence (worker._enrich_one);
this module owns the pieces: eligibility, the non-blocking claim, the origin reference, the gate-and-compose, and the provenance write.
"""

from dataclasses import dataclass

from pydantic import BaseModel

from core import config
from services import conversation
from services import deep_retrieval
from services import llm
from services import persona

# The advisory-lock namespace the enrichment sweep claims a message's pass under (claim).
# Its own number, distinct from the fold's (conversation._FOLD_LOCK_NAMESPACE), so the two never share a lock;
# paired with the intake id, it names "this message's enrichment" and only that. Spells "TIER2" loosely in hex.
_ENRICH_LOCK_NAMESPACE = 0x71E2

# What the composing prompt reads where the deep facts or the turns-since would go, when there are none —
# so the prompt always has a coherent line rather than a blank the model must puzzle over.
_NO_RELATED = "(nothing further in the diary that bears on this by meaning)"
_NO_TURNS_SINCE = "(nothing has been said since — this is still the last exchange)"


@dataclass(frozen=True)
class Origin:
    """The origin reference a late follow-up must situate itself against — the three legs of Tier 2's spec.

    message is the human symbiot's line that prompted the fast answer;
    answer is the fast answer this pass may enrich;
    since is the turns exchanged after that answer, oldest first (empty when the exchange is still the latest)."""

    message: str
    answer: str
    since: list[conversation.Turn]


class _EnrichmentReply(BaseModel):
    """The gate-and-compose verdict: whether the deep reach is worth surfacing, and the follow-up if so.

    surface is the gate — false when the deep facts add nothing the fast answer hadn't already covered.
    message is the follow-up's words when surfacing, empty otherwise;
    it defaults empty so the field is not forced on a suppressing reply,
    and a surface with no words is treated as a suppress by the caller (defensive).
    A plain module-level model — its shape never depends on the pool, so nothing is built per call."""

    surface: bool
    message: str = ""


def claim(conn, intake_id: int) -> bool:
    """Take exclusive ownership of this message's enrichment for the current transaction; True if this caller got it.

    The same non-blocking advisory lock the compression fold uses (conversation.claim_fold),
    in its own namespace and keyed on the intake id, taken with the *try* form
    so a caller that finds it held comes back False at once rather than blocking.
    Without it, two sweeps could both spot the same eligible message,
    both run the deep reach and the composing call,
    and both deliver a follow-up for the one message —
    the duplicate-missive twin of the duplicate-Gist race.
    With it, the second worker skips the message this pass and it stays eligible for the next.
    Transaction-scoped, so it releases on commit or rollback with no unlock to remember and none stranded on the pool.
    It claims the enrichment *execution* only;
    the deep reach it guards is itself lock-free, reading the store and no more.
    """
    row = conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s, %s)",
        (_ENRICH_LOCK_NAMESPACE, intake_id),
    ).fetchone()
    return row[0]


def compose(origin: Origin, related: list[deep_retrieval.Related]) -> tuple[bool, str]:
    """Gate and, if it is worth it, compose the enriched follow-up — returns (surface, message).

    Short-circuits before any model call when the deep reach found nothing:
    there is no new ground to weigh, so the pass suppresses without spending the metered call.
    Otherwise one structured call on the heavy model reads the persona,
    the origin reference (the prompting message, the fast answer, and the turns since), and the deep facts,
    and returns whether to surface and the follow-up if so.
    A surface with an empty message is downgraded to a suppress —
    a model that flags "yes" but writes nothing has, in substance, nothing to add —
    so the caller never delivers an empty missive.
    """
    if not related:
        return False, ""
    voice = persona.load()
    reply = llm.generate_json(
        _compose_prompt(origin, related, voice), _EnrichmentReply, model=config.ENRICH_MODEL
    )
    surface = reply.surface and bool(reply.message.strip())
    return surface, reply.message.strip() if surface else ""


def next_to_enrich(conn) -> tuple[int, int, str, str] | None:
    """The oldest answered message from the symbiot that hasn't been through the enrichment pass yet.

    The sweep's eligibility read (worker.run_enrichment_sweep), the mirror of the enrichment table's UNIQUE intake_id:
    an authed message — enrichment reaches the symbiot's own diary, so an anonymous line is never enriched —
    that reached 'answered' specifically (narrower than ingestion's terminal set: there must be a fast answer *to* enrich,
    which an 'abandoned' message never got),
    and that has no enrichment row yet.
    Returns (intake_id, symbiot_id, message, answer), or None when none is waiting.
    A message just passed is excluded next time (an enrichment row now bears its id, surfaced or not),
    and one whose pass crashed before it committed is still eligible and simply picked up again — no drop, no double.
    A pure read: it takes no lock and moves nothing, so it never contends with a worker.
    """
    row = conn.execute(
        "SELECT id, symbiot_id, message, answer FROM intake "
        "WHERE symbiot_id IS NOT NULL "
        "AND status = 'answered' "
        "AND NOT EXISTS (SELECT 1 FROM enrichment WHERE enrichment.intake_id = intake.id) "
        "ORDER BY id LIMIT 1"
    ).fetchone()
    return (row[0], row[1], row[2], row[3]) if row else None


def origin_reference(conn, symbiot_id: int, intake_id: int, message: str, answer: str) -> Origin:
    """Assemble the three-legged origin reference for a message's enrichment pass.

    The message and its fast answer are already in hand (the eligibility read carried them);
    the third leg — the turns exchanged since — is read live from the conversation stream here,
    every utterance the symbiot's stream carries newer than this exchange's own turns,
    resolved through the stream's pointer the same way the reply's tail is (intake.message / intake.answer / a missive body).
    "Newer than this exchange" is anchored on the id of the last conversation_item pointing at this intake row:
    the symbiot's message and the machine's reply both point at it,
    so everything past the later of the two is what came after.
    Empty when nothing has been said since — this exchange is still the latest —
    which the compose prompt renders as its own line.
    """
    rows = conn.execute(
        """
        SELECT ci.role,
               CASE
                   WHEN ci.intake_id IS NOT NULL AND ci.role = 'symbiot' THEN i.message
                   WHEN ci.intake_id IS NOT NULL AND ci.role = 'machine' THEN i.answer
                   WHEN ci.missive_id IS NOT NULL THEN m.body
               END AS text
        FROM conversation_item ci
        LEFT JOIN intake  i ON i.id = ci.intake_id
        LEFT JOIN missive m ON m.id = ci.missive_id
        WHERE ci.symbiot_id = %(symbiot)s
          AND ci.id > COALESCE(
              (SELECT max(id) FROM conversation_item WHERE symbiot_id = %(symbiot)s AND intake_id = %(intake)s),
              0
          )
        ORDER BY ci.id ASC
        """,
        {"symbiot": symbiot_id, "intake": intake_id},
    ).fetchall()
    since = [conversation.Turn(role=r[0], text=r[1]) for r in rows]
    return Origin(message=message, answer=answer, since=since)


def record(conn, intake_id: int, symbiot_id: int, missive_id: int | None) -> int:
    """Record that this message has been through the enrichment pass, and return the row's id.

    One row per message, written whether the pass surfaced a follow-up or suppressed it —
    so a message weighed and found not worth enriching is marked done and never reconsidered, the gate spent exactly once.
    surfaced is derived from whether a missive was sent (missive_id present), the one place that truth is decided,
    so it can never drift from the CHECK the schema holds.
    The UNIQUE intake_id makes the write exactly-once:
    a re-run of an already-recorded pass conflicts and is refused rather than filing a second verdict.
    """
    row = conn.execute(
        "INSERT INTO enrichment (intake_id, symbiot_id, surfaced, missive_id) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (intake_id, symbiot_id, missive_id is not None, missive_id),
    ).fetchone()
    return row[0]


def _compose_prompt(origin: Origin, related: list[deep_retrieval.Related], voice: str) -> str:
    # voice first (who is speaking), then the situation, then the origin reference (message, prior answer, turns since),
    # then the deep facts, then the gate-and-compose instruction.
    # The prior answer is named explicitly and repeating it is forbidden, so the model adds only genuinely new ground.
    return (
        f"{voice}\n\n"
        "A moment ago you answered the human symbiot you live in symbiosis with. "
        "Since then, off to the side, you have had time to reach deeper into your diary — by meaning, not just wording — "
        "and you have found the entries below. Decide whether they let you add something genuinely worth saying now: "
        "a connection you missed, a fact that reframes your first answer, something that helps.\n\n"
        "Two things must hold. "
        "First, do not repeat what you already said — you have your earlier answer in front of you; "
        "add only what is new, and say nothing if there is nothing new. "
        "Second, the conversation may have moved on since you answered, so read what was said after and fit your follow-up to where things now stand.\n\n"
        f'What they first said:\n"{origin.message}"\n\n'
        f'What you already answered (do not restate this):\n"{origin.answer}"\n\n'
        f"What has been said since:\n{_render_since(origin.since)}\n\n"
        f"What your deeper reach into the diary turned up:\n{_render_related(related)}\n\n"
        "Now decide. If there is something genuinely new and worth sending, set surface true and write the follow-up "
        "in your own voice — directly, as yourself, picking up the thread rather than starting over. "
        "If the deeper reach adds nothing your first answer didn't already cover, set surface false and leave the message empty. "
        "Silence is the right answer more often than not; only surface when it earns the interruption."
    )


def _render_related(related: list[deep_retrieval.Related]) -> str:
    # The deep facts as a plain block, one dated line each, in the order deep_search returned them
    # (the vector hits by distance, then the ontology-walked siblings) —
    # the same dated-line shape the reply renders facts in.
    if not related:
        return _NO_RELATED
    return "\n".join(f"- [{r.effective_at.date().isoformat()}] {r.raw_text}" for r in related)


def _render_since(since: list[conversation.Turn]) -> str:
    # The turns exchanged since the fast answer, role-tagged, oldest first, so the model reads what it has to catch up on.
    if not since:
        return _NO_TURNS_SINCE
    return "\n".join(f"{conversation._speaker(t.role)}: {t.text}" for t in since)
