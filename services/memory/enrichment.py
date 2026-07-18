"""Enrichment: the deep second pass that follows up on a fast answer — the composing half of Tier 2.

Tier 1 answers the moment a message lands, from the lexical diary reach and the running conversation.
This module is what happens a beat later, off the critical path:
the deep reach (deep_retrieval.py) gathers the facts that bear on what was said by *meaning* rather than by shared words,
and — only when they add something the fast answers didn't — the machine sends an enriched follow-up.

The pass does not fire per message. It fires per *lull*.
A message is enriched only once the conversation has gone quiet for a settling interval after it (config.ENRICH_SETTLE_SECONDS),
and then the whole burst it ended — the run of messages with no gap longer than that interval — is enriched as one unit (next_burst_to_enrich):
one deep reach over the burst's messages, one gate-and-compose, at most one follow-up, and a provenance row for every message in it.
This is the upstream half of not repeating itself: an active back-and-forth no longer spawns a deep pass per line over the same diary facts —
the soil the near-duplicate follow-ups grew in — and the follow-up never interrupts the exchange while it is still live.

Because the pass lands after the fast replies, and the conversation may have moved on since,
the composing call is given the whole *origin reference* the follow-up must situate itself against, all three legs of it:
  (a) the burst's messages that prompted the fast answers,
  (b) the fast answers themselves — so the model can see what it already said and not merely restate it, and
  (c) the recent conversation around the burst — every turn since the Gist's cutoff other than the burst's own,
      which crucially includes the follow-ups already sent, so the gate can see what it has said deeply and not say it again.
The compose prompt names (b) and (c) and forbids repeating either, so no-repeat is instructed, not left to chance.

One structured call both gates and composes:
it returns whether to surface at all and, if so, the follow-up's words —
strict-Pydantic-as-decoder-grammar, the same boundary discipline the router's calls keep.
When the deep reach found nothing, the call is skipped entirely —
there is nothing to weigh, so no metered model call is spent.

Downstream of the gate sits the guarantee the instruction alone can't make: the echo guard (is_echo_of_prior).
A composed follow-up is measured — by the same cosine closeness the /observe echoes lens reads (services/echo.py) —
against every deep reply the machine has ever sent this symbiot, and one that is near-identical to any of them is held back, whatever its age.
The redundancy is durable and deep replies are rare, so the check is all-time and cheap; it fails open, never suppressing when it cannot measure.

A follow-up worth sending is delivered as a missive (the kernel reaching out on its own), never as an inline reply,
because by now it is a new turn in the conversation, not the answer to the message that started it.

Delivery and the exactly-once record are the worker's to sequence (worker._enrich_one);
this module owns the pieces: eligibility, the non-blocking claim, the origin reference, the gate-and-compose, the echo guard, and the provenance write.
"""

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel

from core import config
from services import echo
from services.memory import conversation
from services.memory import deep_retrieval
from services.adapters import embedding
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
class BurstMember:
    """One answered message in a settled burst: its intake id and the exchange it holds.

    intake_id is the message's row — the exactly-once key the enrichment row will bear;
    message is the symbiot's line, answer the fast reply it already got.
    A burst is enriched as a unit, so the members' messages join into one deep query,
    and their answers into the "what you already said" the gate must not restate."""

    intake_id: int
    message: str
    answer: str


@dataclass(frozen=True)
class Burst:
    """A run of the symbiot's answered messages with no gap longer than the settling interval, taken as one enrichment unit.

    symbiot_id is whose burst it is — the key the claim locks on.
    members are the burst's messages, oldest first; the last is the anchor,
    the message the follow-up (if one surfaces) is recorded against.
    Every member gets a provenance row when the pass commits, so none is left eligible to fire again on its own."""

    symbiot_id: int
    members: list[BurstMember]


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


def is_echo_of_prior(
    conn, symbiot_id: int, follow_up: str, *, threshold: float = echo.ECHO_THRESHOLD
) -> bool:
    """Whether a composed follow-up echoes a deep reply already sent — the downstream guarantee, measured not hoped.

    The gate's own no-repeat is instruction to a model; this is the check that enforces it.
    Every deep reply the machine has ever sent this symbiot is gathered (prior_deep_replies) and, with the candidate,
    embedded in one call as documents — so two of the machine's own lines compare symmetrically, the same stance the echoes lens takes —
    and the candidate is an echo when the nearest of them sits at or above the threshold (services/echo.py).
    All-time, not a window: the redundancy is durable — the same diary facts get recalled by meaning for any similar message —
    and deep replies are rare, so the whole history is a short list and the check is cheap.
    No prior deep replies means nothing to echo — False, without a model call.
    It fails *open*: if the embedder is unreachable the guard does not suppress —
    a rare repeat getting through is a smaller harm than a good follow-up silently eaten,
    the same degrade-rather-than-lie stance the echoes lens keeps, pointed the other way.
    """
    priors = prior_deep_replies(conn, symbiot_id)
    if not priors:
        return False
    try:
        vectors = embedding.embed_many(priors + [follow_up], task="document")
    except Exception:
        return False
    return echo.max_similarity(vectors[-1], vectors[:-1]) >= threshold


def next_burst_to_enrich(conn, settle_seconds: float) -> Burst | None:
    """The oldest settled burst of the symbiot's answered-but-unenriched messages, or None when none has settled.

    The sweep's eligibility read (worker.run_enrichment_sweep), and the debounce that keeps a live exchange from firing a deep pass per line.
    It reads over the authed, 'answered', not-yet-enriched messages —
    enrichment reaches the symbiot's own diary, so an anonymous line is never enriched,
    and there must be a fast answer *to* enrich, which an 'abandoned' message never got —
    groups each symbiot's into bursts (a run with no gap longer than settle_seconds between consecutive messages),
    and returns the oldest burst that has *settled*: its last message followed by settle_seconds of quiet, with nothing newer.
    A burst still cooling — its last message too recent — is left whole for a later pass,
    so no member is enriched before the exchange it sits in has actually gone quiet.
    Members come back oldest first, the last being the anchor; None when no burst has settled yet.
    A pure read: it takes no lock and moves nothing, so it never contends with a worker.
    With settle_seconds = 0 every message is its own immediately-settled burst — the old per-message cadence, which the test suite runs under.
    """
    rows = conn.execute(
        """
        WITH eligible AS (
            SELECT i.id, i.symbiot_id, i.message, i.answer, i.created_at
            FROM intake i
            WHERE i.symbiot_id IS NOT NULL
              AND i.status = 'answered'
              AND NOT EXISTS (SELECT 1 FROM enrichment e WHERE e.intake_id = i.id)
        ),
        seq AS (
            SELECT *,
                   LAG(created_at)  OVER w AS prev_at,
                   LEAD(created_at) OVER w AS next_at
            FROM eligible
            WINDOW w AS (PARTITION BY symbiot_id ORDER BY created_at, id)
        ),
        marked AS (
            SELECT *,
                   (prev_at IS NULL OR created_at - prev_at > make_interval(secs => %(settle)s)) AS starts_burst,
                   (next_at IS NULL OR next_at - created_at > make_interval(secs => %(settle)s)) AS ends_burst
            FROM seq
        ),
        grouped AS (
            SELECT *,
                   SUM(CASE WHEN starts_burst THEN 1 ELSE 0 END)
                       OVER (PARTITION BY symbiot_id ORDER BY created_at, id) AS burst_no
            FROM marked
        ),
        target AS (
            SELECT symbiot_id, burst_no
            FROM grouped
            WHERE ends_burst
              AND now() - created_at >= make_interval(secs => %(settle)s)
            ORDER BY id
            LIMIT 1
        )
        SELECT g.id, g.symbiot_id, g.message, g.answer
        FROM grouped g
        JOIN target t ON t.symbiot_id = g.symbiot_id AND t.burst_no = g.burst_no
        ORDER BY g.created_at, g.id
        """,
        {"settle": settle_seconds},
    ).fetchall()
    if not rows:
        return None
    members = [BurstMember(intake_id=r[0], message=r[2], answer=r[3]) for r in rows]
    return Burst(symbiot_id=rows[0][1], members=members)


def origin_reference(
    conn, symbiot_id: int, exclude_intake_ids: list[int], message: str, answer: str
) -> Origin:
    """Assemble the three-legged origin reference for a burst's enrichment pass.

    The burst's messages and their fast answers are already in hand, joined by the caller into the message and answer legs;
    the third leg — the recent conversation around the burst — is read live from the stream here,
    reusing the same verbatim tail the reply path sees (conversation.recent):
    every turn newer than the Gist's cutoff,
    resolved through the stream's pointer (intake.message / intake.answer / a missive body),
    with the burst's own turns (exclude_intake_ids) excluded so its messages and their fast answers are not fed back as if they were surrounding context.
    Crucially the tail carries missive bodies, so a follow-up already sent on this ground is in view here —
    the one leg that lets the gate refuse to repeat an earlier deep reply.
    Empty when nothing else has been said recently — the burst stands alone —
    which the compose prompt renders as its own line.
    """
    recent = conversation.recent(conn, symbiot_id, exclude_intake_ids=exclude_intake_ids).tail
    return Origin(message=message, answer=answer, recent=recent)


def prior_deep_replies(conn, symbiot_id: int) -> list[str]:
    """Every deep follow-up the machine has already sent this symbiot, as plain text — the echo guard's comparison set.

    A surfaced enrichment row points at the missive it sent, whose body is the deep reply's words;
    this joins the two and returns those bodies, all-time, oldest first (order is immaterial to the max-similarity the guard takes).
    A suppressed pass carries no missive, so it is absent here — only replies actually sent can be echoed.
    A pure read: no lock, no write.
    """
    rows = conn.execute(
        "SELECT m.body FROM enrichment e JOIN missive m ON m.id = e.missive_id "
        "WHERE e.symbiot_id = %s AND e.surfaced "
        "ORDER BY e.id",
        (symbiot_id,),
    ).fetchall()
    return [row[0] for row in rows]


def record(conn, intake_id: int, symbiot_id: int, missive_id: int | None, *, echo_suppressed: bool = False) -> int:
    """Record that this message has been through the enrichment pass, and return the row's id.

    One row per message, written whether the pass surfaced a follow-up or suppressed it —
    so a message weighed and found not worth enriching is marked done and never reconsidered, the gate spent exactly once.
    surfaced is derived from whether a missive was sent (missive_id present), the one place that truth is decided,
    so it can never drift from the CHECK the schema holds.
    echo_suppressed records *why* a pass was silent when it was:
    true only when the gate composed a follow-up that the echo guard then held back as a near-duplicate,
    so /observe can tell that muzzled follow-up apart from a pass that simply had nothing to add.
    It never rides with a missive — a held echo was not sent — which the schema's CHECK enforces.
    The UNIQUE intake_id makes the write exactly-once:
    a re-run of an already-recorded pass conflicts and is refused rather than filing a second verdict.
    """
    row = conn.execute(
        "INSERT INTO enrichment (intake_id, symbiot_id, surfaced, missive_id, echo_suppressed) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (intake_id, symbiot_id, missive_id is not None, missive_id, echo_suppressed),
    ).fetchone()
    return row[0]


def record_burst(
    conn, members: list[BurstMember], symbiot_id: int, missive_id: int | None, *, echo_suppressed: bool = False
) -> None:
    """Record every message in an enriched burst as considered, exactly once each — the anchor carrying the outcome.

    One provenance row per member, so no message in the burst is ever left eligible to fire a deep pass on its own —
    the whole burst is spent together, the same "considered exactly once" the single-message pass gave each message before.
    The burst's one outcome — a follow-up sent, or one held back as an echo — is recorded against the anchor (its last message):
    the anchor carries the missive_id when one surfaced, or echo_suppressed when the guard held the composed follow-up.
    Every other member records a plain suppressed pass, since at most one follow-up speaks for the whole burst.
    surfaced falls out of missive presence in record, so the schema's CHECK can never disagree with the verdict.
    """
    anchor_id = members[-1].intake_id
    for member in members:
        is_anchor = member.intake_id == anchor_id
        record(
            conn,
            member.intake_id,
            symbiot_id,
            missive_id if is_anchor else None,
            echo_suppressed=echo_suppressed if is_anchor else False,
        )


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
