"""Short-term conversational memory: the recent exchange, held as a gradient.

This is the second kind of memory the read path draws on, beside the long-term diary.
The diary (retrieval.py) is recall *by relevance* — the facts whose words bear on the message.
This is recall *by recency* — the running back-and-forth the current message is the next turn of,
the thread that lets "and the second one?" find what it points back to,
which no relevance search over the diary could recover
because a pronoun carries none of the words that would surface the thing it stands for.

The conversation is a gradient:
near turns carried word-for-word, far turns folded into one running summary as they age out,
and nothing ever thrown away. It lives in two structures.

Bucket 1, the present — the verbatim tail (recent):
    the recent turns carried word-for-word,
    so the model's own attention resolves the pronouns and the parallel threads exactly as they were said.
    The read carries every turn newer than the Gist's cutoff, with no token cap on it —
    the whole unbroken chain — so Bucket 1 and Bucket 2 meet at the cutoff with no gap and no overlap.
    The size budget does not live in this read; it lives in the fold below (verbatim_budget).

Bucket 2, the past — the Gist (current_gist / fold / record_gist):
    everything older than the tail, folded into one running summary paragraph.
    The store is append-only:
    each fold inserts a new row carrying the merged paragraph and the cutoff it reached
    (the id of the last item absorbed).
    The current Gist is the newest row; nothing is overwritten,
    so the table is also a durable history of how the summary grew.

The stream both buckets read (record_utterance) does not copy the words:
it points to where they already live durably
(intake for a symbiot message or its reply, missive for a machine-initiated line)
and carries what the read needs and the source doesn't hold —
the role, the timestamp, and the token count,
the last computed once at write with the same local counter the budget guard uses (services.models).
Anonymous callers never get a stream row —
the conversation is the symbiot's, the same boundary the diary keeps.

The budgets are reserved shares of the reply model's optimal window (gist_budget / verbatim_budget):
gist_budget caps the size of the Gist,
and verbatim_budget is the threshold the background fold trims the verbatim tail back to.
Neither caps the read — the read carries the whole tail — so a lagging fold only makes the tail fatter, never blind.

The members below are ordered alphabetically, as far as the code allows:
the two dataclasses come first and keep their dependency order
(Conversation's fields name Turn, so Turn is defined ahead of it),
then the module's functions in alphabetical order.
"""

from dataclasses import dataclass

from core import config
from services import llm
from services import models

# What the two short-term blocks read when there is nothing yet —
# a fresh symbiot, an empty store —
# so the prompt always has a coherent line where the conversation goes, never a blank the model must puzzle over.
_NO_GIST = "(nothing summarised yet — this conversation is still short)"
# The prior Gist stands in with this when a fold is the very first one,
# so the merge prompt reads coherently rather than splicing an empty string where a paragraph goes.
_NO_PRIOR_GIST = "(no earlier summary yet)"
_NO_TAIL = "(no earlier turns yet — this is the start of the conversation)"


@dataclass(frozen=True)
class Turn:
    """One utterance of the verbatim tail: who said it and the words they said.

    role is 'symbiot' or 'machine';
    text is resolved through the stream's pointer at read time (intake.message, intake.answer, or a missive body),
    never stored on the item itself.
    Frozen and made of plain strings so it crosses the reply's process boundary
    (the killable child, execution.run_with_deadline) as cleanly as a retrieval.Fact does."""

    role: str
    text: str


@dataclass(frozen=True)
class Conversation:
    """The short-term memory a reply sits inside: the Gist, then the verbatim tail.

    gist is the running summary of everything older than the tail, or None when nothing has been folded yet.
    tail is the recent turns word-for-word, oldest first — the whole chain back to the Gist's cutoff.
    Both are what reply.compose renders — the past before the present —
    and both cross into the killable child, so this stays a frozen holder of picklable parts."""

    gist: str | None
    tail: list[Turn]


def _speaker(role: str) -> str:
    # The tag each turn wears in the prompt, so the exchange reads top-to-bottom as it happened.
    # The machine reads these as a transcript of itself and the human it lives with.
    return "The human symbiot" if role == "symbiot" else "You"


def current_gist(conn, symbiot_id: int) -> tuple[str, int] | None:
    """The symbiot's current Gist as (gist_text, cutoff_item_id), or None if none folded yet.

    The current Gist is simply the newest row for the symbiot —
    the append-only table is never overwritten, so "current" is "highest id".
    The cutoff rides along because it is what every other read measures against:
    the reader gathers the whole tail newer than it,
    and the sweep gathers the oldest of those turns to fold when the tail has grown past its trigger.
    Read straight off the column, never parsed out of the summary prose."""
    row = conn.execute(
        "SELECT gist_text, cutoff_item_id FROM conversation_gist "
        "WHERE symbiot_id = %s ORDER BY id DESC LIMIT 1",
        (symbiot_id,),
    ).fetchone()
    return (row[0], row[1]) if row else None


def fold(gist_text: str | None, turns: list[Turn]) -> str:
    """Merge the current Gist and the overflowed turns into one fresh summary paragraph.

    The fold *re-compresses* rather than accumulates:
    each pass regenerates the whole paragraph from (current Gist + the pending turns) down to gist_budget,
    so the Gist is rewritten to the same size every pass and cannot creep upward over time.
    The same heavy model that composes the replies does it (config.CONVERSATION_COMPRESS_MODEL, defaulting to REPLY_MODEL),
    keeping the concrete facts and dropping the redundancy —
    the Gist is what a later reply reads back through once a turn has aged out of the verbatim tail,
    so its quality is load-bearing and worth the metered call, even though the fold itself is background work no one waits on.

    The prior Gist and the transcript are handed as the summarisable context:
    if that input ever overruns the model's window (a large pending band folded in one pass)
    the budget guard condenses it first rather than raising.
    The merged paragraph is then hard-truncated to gist_budget,
    so a model that overshoots the length it was asked for still cannot push the Gist past its cap:
    the cap is a guarantee, the same way llm._summarise already makes it for the diary facts.
    """
    target = gist_budget()
    transcript = "\n".join(f"{_speaker(t.role)}: {t.text}" for t in turns)
    context = (
        f"Summary so far:\n{gist_text or _NO_PRIOR_GIST}\n\n"
        f"Newer turns to fold in:\n{transcript}"
    )
    prompt = (
        "You keep a running summary of a conversation between you and the human symbiot you "
        "live in symbiosis with. Below is the summary so far, then the newer turns that have "
        f"aged out of the verbatim record. Merge them into one fresh summary of at most about "
        f"{target} tokens: keep every concrete fact, name, date, decision, and open thread; "
        "drop only redundancy and small talk; write it as continuous prose in the past tense, "
        "not a list. Return only the merged summary, nothing else.\n\n"
        f"{context}"
    )
    merged = llm.generate(prompt, model=config.CONVERSATION_COMPRESS_MODEL, context=context)
    return models.truncate_tokens(merged, target)


def gist_budget() -> int:
    """The token ceiling the Gist is re-compressed to every fold — Bucket 2's reserved share.

    A fraction of the reply model's optimal window (config), so it travels with the model a fallback might switch to.
    It is a cap the fold holds as a promise, not a target the model may drift past:
    the merge names it and the result is hard-truncated to it (fold)."""
    optimal = models.MODELS[config.REPLY_MODEL].optimal_context_tokens
    return int(optimal * config.CONVERSATION_GIST_BUDGET_FRACTION)


def next_symbiot_to_fold(conn, budget: int) -> int | None:
    """A symbiot whose verbatim tail has grown past the fold trigger, or None.

    The compression sweep's eligibility read — and the one place the budget acts as a *trigger*:
    a symbiot has work when the items newer than its Gist's cutoff sum to more than the budget,
    meaning the tail the reader carries verbatim has grown fatter than the size the fold keeps it to.
    Those turns are not a blind spot — the reader still returns every one of them until they are folded —
    so this only decides *when* the sweep should trim the tail back into the Gist.
    Returns the lowest-id such symbiot so the sweep is fair across symbiots and deterministic;
    None when no symbiot's tail is over the trigger.
    A pure read: no lock, no write, so it never contends with a reply gathering the same stream.
    """
    row = conn.execute(
        """
        WITH latest_gist AS (
            SELECT DISTINCT ON (symbiot_id) symbiot_id, cutoff_item_id
            FROM conversation_gist
            ORDER BY symbiot_id, id DESC
        )
        SELECT ci.symbiot_id
        FROM conversation_item ci
        LEFT JOIN latest_gist g ON g.symbiot_id = ci.symbiot_id
        WHERE ci.id > COALESCE(g.cutoff_item_id, 0)
        GROUP BY ci.symbiot_id
        HAVING SUM(ci.token_count) > %(budget)s
        ORDER BY ci.symbiot_id
        LIMIT 1
        """,
        {"budget": budget},
    ).fetchone()
    return row[0] if row else None


def pending_for_fold(
    conn, symbiot_id: int, budget: int, cutoff: int
) -> tuple[list[Turn], int]:
    """The oldest turns to fold out of an over-trigger tail, oldest first, and the new cutoff they reach.

    When the tail has grown past the fold trigger (next_symbiot_to_fold),
    this picks the turns to move into the Gist so the verbatim tail shrinks back to within the budget:
    over the items newer than the current cutoff,
    a reverse-chronological running sum keeps the newest turns that fit the budget as the tail-to-remain,
    and returns the older overflow — the turns past that sum —
    resolved to their words through the pointer and re-sorted to chronological order so the merge reads them as they were said,
    with the id of the last one (the cutoff the new Gist row will carry).

    These turns stay fully visible to the reader until the fold commits and the cutoff advances —
    the stream has no blind spot between read and fold, only a tail that is momentarily fatter than the budget.
    Returns an empty list and the given cutoff unchanged when the tail is within the budget.
    """
    rows = conn.execute(
        """
        SELECT t.id, t.role,
               CASE
                   WHEN t.intake_id IS NOT NULL AND t.role = 'symbiot' THEN i.message
                   WHEN t.intake_id IS NOT NULL AND t.role = 'machine' THEN i.answer
                   WHEN t.missive_id IS NOT NULL THEN m.body
               END AS text
        FROM (
            SELECT *, SUM(token_count) OVER (ORDER BY id DESC) AS running
            FROM conversation_item
            WHERE symbiot_id = %(symbiot)s
              AND id > %(cutoff)s
        ) t
        LEFT JOIN intake  i ON i.id = t.intake_id
        LEFT JOIN missive m ON m.id = t.missive_id
        WHERE t.running > %(budget)s
        ORDER BY t.id ASC
        """,
        {"symbiot": symbiot_id, "cutoff": cutoff, "budget": budget},
    ).fetchall()
    if not rows:
        return [], cutoff
    turns = [Turn(role=r[1], text=r[2]) for r in rows]
    new_cutoff = rows[-1][0]
    return turns, new_cutoff


def recent(conn, symbiot_id: int, *, exclude_intake_id: int | None = None) -> Conversation:
    """The short-term memory a reply should see: the current Gist, and the whole verbatim tail after it.

    The tail is every turn newer than the Gist's cutoff —
    the complete, unbroken chain from where the Gist ends up to now, no token cap on the read.
    This is the state-consistency guarantee:
    the Gist covers everything at or before the cutoff, the tail covers everything after it,
    so the two meet exactly at the cutoff with no gap and no overlap, whatever the token count.
    The size budget lives entirely in the background fold (verbatim_budget),
    which trims the tail back by folding its oldest turns into the Gist;
    if that fold lags, this read simply returns a fatter tail, never a hole.
    The rows come back in chronological order, each item's words resolved through its pointer
    (intake.message for the symbiot side, intake.answer for the machine side, a missive's body for a machine-initiated line).

    exclude_intake_id drops the message currently being answered from the tail:
    it was written onto the stream the instant it was received,
    so without this it would appear both as the last tail turn and as the "current message" the prompt states separately — the same line twice.
    Excluding it keeps the tail to what was said *before* this turn, which is exactly what "the conversation so far" means.

    A symbiot with no stream yet returns an empty tail and no gist —
    the honest empty, which reply.compose renders as a single line rather than a blank.
    """
    gist_row = current_gist(conn, symbiot_id)
    cutoff = gist_row[1] if gist_row is not None else 0
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
          AND ci.id > %(cutoff)s
          AND (%(exclude)s::bigint IS NULL OR ci.intake_id IS DISTINCT FROM %(exclude)s::bigint)
        ORDER BY ci.id ASC
        """,
        {"symbiot": symbiot_id, "cutoff": cutoff, "exclude": exclude_intake_id},
    ).fetchall()
    tail = [Turn(role=r[0], text=r[1]) for r in rows]
    return Conversation(gist=gist_row[0] if gist_row is not None else None, tail=tail)


def record_gist(conn, symbiot_id: int, gist_text: str, cutoff_item_id: int) -> int:
    """Append a folded Gist to the symbiot's history, and return the new row's id.

    A fold is a single INSERT —
    the table is append-only, so there is no flag to flip on the folded items and no row to overwrite.
    The new row becomes the current Gist (it has the highest id),
    and its cutoff_item_id is the boundary every later read measures against.
    Exactly-once falls out of this:
    a crash before this commit leaves the same turns eligible next pass,
    a crash after has already advanced the cutoff so those turns fall outside the next pass's gather —
    pinned by the cutoff only moving forward, not by the sweep being careful.
    """
    row = conn.execute(
        "INSERT INTO conversation_gist (symbiot_id, gist_text, cutoff_item_id) "
        "VALUES (%s, %s, %s) RETURNING id",
        (symbiot_id, gist_text, cutoff_item_id),
    ).fetchone()
    return row[0]


def record_utterance(
    conn,
    symbiot_id: int,
    role: str,
    text: str,
    *,
    intake_id: int | None = None,
    missive_id: int | None = None,
) -> int:
    """Add one utterance to the symbiot's conversation stream, and return its id.

    Called the moment an utterance is written durably elsewhere —
    a symbiot's message as it is received, the machine's reply as it is marked answered, a missive as it is raised —
    so the stream mirrors the exchange turn by turn.
    The words are not copied here:
    exactly one of intake_id / missive_id points at the row that already holds them durably
    (the CHECK the schema enforces makes "exactly one source" a database guarantee, not a caller's discipline),
    and role says which side of an intake row this is.

    token_count is computed once, here, with the same local counter the budget guard uses (models.count_tokens),
    so the read-time "how much fits?" is arithmetic over an integer column with no tokeniser call on the path the symbiot waits on.
    It is passed the text to measure but never stores it — the pointer is the only durable link to the words.
    Only a recognised symbiot's utterances reach the stream;
    an anonymous line is answered without a conversation, the same boundary the diary keeps, so callers pass a real symbiot_id.
    """
    row = conn.execute(
        "INSERT INTO conversation_item (symbiot_id, role, token_count, intake_id, missive_id) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (symbiot_id, role, models.count_tokens(text), intake_id, missive_id),
    ).fetchone()
    return row[0]


def verbatim_budget() -> int:
    """The size the verbatim tail is kept to — the compression sweep's *trigger*, not a read cap.

    A fraction of the reply model's optimal window (config), the same shape as gist_budget.
    It does not bound the read:
    recent() carries every turn back to the Gist's cutoff, however many tokens that is,
    so Bucket 1 and Bucket 2 always touch with no gap between them.
    This figure is instead the threshold the background fold fires at —
    once the turns past the cutoff sum beyond it (next_symbiot_to_fold),
    the sweep folds the oldest of them into the Gist (pending_for_fold) until the verbatim tail is back within this size.
    So the budget governs *when* to fold and *how much*, and a lagging sweep only makes the tail temporarily fatter — never blind."""
    optimal = models.MODELS[config.REPLY_MODEL].optimal_context_tokens
    return int(optimal * config.CONVERSATION_VERBATIM_BUDGET_FRACTION)
