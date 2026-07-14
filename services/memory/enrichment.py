"""Enrichment: the deep second pass that follows up on a fast answer — the composing half of Tier 2.

Tier 1 answers the moment a message lands, from the lexical diary reach and the running conversation.
This module is what happens a beat later, off the critical path:
once that answer is settled, the deep reach (deep_retrieval.py) gathers the facts that bear on the message by *meaning* rather than by shared words,
and — only when they add something the fast answer didn't — the machine sends an enriched follow-up.

Because the pass lands after the fast reply, and the conversation may have moved on since,
the composing call is given the whole *origin reference* the follow-up must situate itself against, all three legs of it:
  (a) the message that prompted the fast answer,
  (b) the fast answer itself — so the model can see what it already said and not merely restate it, and
  (c) the recent conversation around it — every turn since the Gist's cutoff other than this exchange's own,
      which crucially includes the follow-ups already sent, so the gate can see what it has said deeply and not say it again.
Legs (b) and (c) are the no-repeat basis:
(b) guards against restating the fast answer, (c) against repeating an earlier follow-up on the same ground —
the failure mode where two adjacent messages reach the same diary facts and each raises the same deep reply,
because a per-message pass judged novelty against its own fast answer alone and never saw the follow-ups it had already sent.
The compose prompt names both and forbids repeating either, so no-repeat is instructed, not left to chance.

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
from datetime import datetime

from pydantic import BaseModel

from core import config
from services.memory import conversation
from services.memory import deep_retrieval
from services.adapters import llm
from services.adapters import models
from services.loop import persona
from services.loop import zone

# The advisory-lock namespace the enrichment sweep claims a symbiot's pass under (claim).
# Its own number, distinct from the fold's (conversation._FOLD_LOCK_NAMESPACE), so the two never share a lock;
# paired with the symbiot id, it names "this symbiot's enrichment" and only that. Spells "TIER2" loosely in hex.
_ENRICH_LOCK_NAMESPACE = 0x71E2

# What the composing prompt reads where the deep facts or the recent conversation would go, when there are none —
# so the prompt always has a coherent line rather than a blank the model must puzzle over.
_NO_RECENT = "(nothing else has been said recently — this exchange stands alone)"
_NO_RELATED = "(nothing further in the diary that bears on this by meaning)"


@dataclass(frozen=True)
class Origin:
    """The origin reference a late follow-up must situate itself against — the three legs of Tier 2's spec.

    message is the human symbiot's line that prompted the fast answer;
    answer is the fast answer this pass may enrich;
    recent is the surrounding conversation — every turn newer than the Gist's cutoff except this exchange's own,
    oldest first, including any follow-ups already sent, so the gate can see what it has said before and not repeat it."""

    message: str
    answer: str
    recent: list[conversation.Turn]


class _EnrichmentReply(BaseModel):
    """The gate-and-compose verdict: whether the deep reach is worth surfacing, and the follow-up if so.

    surface is the gate — false when the deep facts add nothing the fast answer hadn't already covered.
    message is the follow-up's words when surfacing, empty otherwise;
    it defaults empty so the field is not forced on a suppressing reply,
    and a surface with no words is treated as a suppress by the caller (defensive).
    A plain module-level model — its shape never depends on the pool, so nothing is built per call."""

    surface: bool
    message: str = ""


def claim(conn, symbiot_id: int) -> bool:
    """Take exclusive ownership of this symbiot's enrichment for the current transaction; True if this caller got it.

    The same non-blocking advisory lock the compression fold uses (conversation.claim_fold),
    in its own namespace and keyed on the symbiot id, taken with the *try* form
    so a caller that finds it held comes back False at once rather than blocking.
    Keyed on the symbiot, not the message, on purpose: only one deep reply forms for a symbiot at a time,
    so a second message's pass cannot compose while the first is still in flight and unrecorded —
    which is what lets leg (c) of the origin reference see the earlier follow-up and refuse to repeat it.
    Without it, two adjacent messages could each reach the same diary facts, neither yet seeing the other's follow-up,
    and both deliver near-identical deep replies — the duplicate-missive twin of the duplicate-Gist race.
    With it, the second worker skips the symbiot this pass and its next message stays eligible for the following one,
    by which time the first follow-up is committed and on the stream for the gate to weigh against.
    Transaction-scoped, so it releases on commit or rollback with no unlock to remember and none stranded on the pool.
    It claims the enrichment *execution* only;
    the deep reach it guards is itself lock-free, reading the store and no more.
    """
    row = conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s, %s)",
        (_ENRICH_LOCK_NAMESPACE, symbiot_id),
    ).fetchone()
    return row[0]


def compose(
    origin: Origin,
    related: list[deep_retrieval.Related],
    *,
    zone_name: str | None = None,
    now_local: datetime | None = None,
) -> tuple[bool, str]:
    """Gate and, if it is worth it, compose the enriched follow-up — returns (surface, message).

    Short-circuits before any model call when the deep reach found nothing:
    there is no new ground to weigh, so the pass suppresses without spending the metered call.
    Otherwise one structured call on the heavy model reads the persona,
    the symbiot's local time now, the origin reference (the prompting message, the fast answer, and the recent conversation), and the deep facts,
    and returns whether to surface and the follow-up if so.
    A surface with an empty message is downgraded to a suppress —
    a model that flags "yes" but writes nothing has, in substance, nothing to add —
    so the caller never delivers an empty missive.
    zone_name is the symbiot's IANA zone, used to render each deep fact's date in the human's local day;
    absent (a by-hand call that names no zone), the dates fall back to UTC as they read off the store.
    now_local is the symbiot's current local time (zone.now_for), stated so the follow-up — composed a beat after the fast answer,
    when the conversation may have moved on — reasons about "now" against a real present rather than none at all,
    the same current-time line the fast reply already states;
    absent (no zone in hand), the line is simply omitted rather than asserting a wrong time.
    """
    if not related:
        return False, ""
    voice = persona.load()
    time_line = zone.render_now(now_local, zone_name) if now_local is not None and zone_name else None
    reply = llm.generate_json(
        _compose_prompt(origin, related, voice, zone_name or zone.DEFAULT_ZONE, time_line),
        _EnrichmentReply,
        model=models.role_name("enrich"),
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
    the third leg — the recent conversation around this exchange — is read live from the stream here,
    reusing the same verbatim tail the reply path sees (conversation.recent):
    every turn newer than the Gist's cutoff,
    resolved through the stream's pointer (intake.message / intake.answer / a missive body),
    with this exchange's own two turns excluded so the current message and its fast answer are not fed back as if they were surrounding context.
    Crucially the tail carries missive bodies, so a follow-up already sent on this ground is in view here —
    the one leg that lets the gate refuse to repeat an earlier deep reply.
    Empty when nothing else has been said recently — this exchange stands alone —
    which the compose prompt renders as its own line.
    """
    recent = conversation.recent(conn, symbiot_id, exclude_intake_id=intake_id).tail
    return Origin(message=message, answer=answer, recent=recent)


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


def _compose_prompt(
    origin: Origin, related: list[deep_retrieval.Related], voice: str, zone_name: str, time_line: str | None
) -> str:
    # voice first (who is speaking), then the situation, then the symbiot's local time now (when known),
    # then the origin reference (message, prior answer, the recent conversation), then the deep facts, then the gate-and-compose instruction.
    # The time line sits right after the framing that says the conversation may have moved on — it is the present that "where things now stand" is measured against,
    # the same current-time reference the fast reply states, so the follow-up composed a beat later does not reason about time in a void.
    # Both the prior answer and the recent conversation — which carries any follow-ups already sent — are named explicitly
    # and repeating either is forbidden, so the model adds only genuinely new ground and never echoes an earlier deep reply.
    now = f"{time_line}\n\n" if time_line else ""
    return (
        f"{voice}\n\n"
        "A moment ago you answered the human symbiot you live in symbiosis with. "
        "Since then, off to the side, you have had time to reach deeper into your diary — by meaning, not just wording — "
        "and you have found the entries below. Decide whether they let you add something genuinely worth saying now: "
        "a connection you missed, a fact that reframes your first answer, something that helps.\n\n"
        "Two things must hold. "
        "First, do not repeat yourself — you have your earlier answer and the recent conversation in front of you, "
        "including any follow-ups you have already sent; add only what is new, and say nothing if there is nothing new. "
        "Second, the conversation may have moved on since you answered, so read what was said and fit your follow-up to where things now stand.\n\n"
        f"{now}"
        f'What they first said:\n"{origin.message}"\n\n'
        f'What you already answered (do not restate this):\n"{origin.answer}"\n\n'
        f"The recent conversation, including any follow-ups you have already sent (do not repeat these):\n{_render_recent(origin.recent, zone_name)}\n\n"
        f"What your deeper reach into the diary turned up:\n{_render_related(related, zone_name)}\n\n"
        "Now decide. If there is something genuinely new and worth sending, set surface true and write the follow-up "
        "in your own voice — directly, as yourself, picking up the thread rather than starting over. "
        "If the deeper reach adds nothing your first answer and earlier follow-ups didn't already cover, set surface false and leave the message empty. "
        "Silence is the right answer more often than not; only surface when it earns the interruption."
    )


def _render_recent(recent: list[conversation.Turn], zone_name: str) -> str:
    # The recent conversation, each turn stamped with the local time it was said and role-tagged, oldest first,
    # so the model reads everything already said — its own earlier follow-ups among it, which it must not repeat —
    # and can tell the order of things said the same day rather than inferring it from how they read.
    if not recent:
        return _NO_RECENT
    return "\n".join(
        f"[{conversation._stamp(t.created_at, zone_name)}] {conversation._speaker(t.role)}: {t.text}" for t in recent
    )


def _render_related(related: list[deep_retrieval.Related], zone_name: str) -> str:
    # The deep facts as a plain block, one dated line each, rendered in time order, oldest first —
    # the same dated-line shape and the same reason the fast reply orders its facts by time:
    # deep_search picks them by meaning (vector distance, then the ontology-walked siblings),
    # but a relevance order read top-to-bottom looks like a timeline it isn't, so the order is made the true one.
    # Which facts appear is the reach's call; only their order is time's.
    # The date is read in the symbiot's local zone, not straight off the UTC column,
    # so a deep fact lands on the same calendar the symbiot lives in rather than the server's.
    if not related:
        return _NO_RELATED
    ordered = sorted(related, key=lambda r: r.effective_at)
    return "\n".join(f"- [{zone.local_date(r.effective_at, zone_name).isoformat()}] {r.raw_text}" for r in ordered)
