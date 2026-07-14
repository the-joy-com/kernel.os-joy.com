"""The worker: the loop that turns received messages into answered ones.

Several run at once as a small pool, each in its own background thread for the kernel's life —
pulling the oldest waiting message, producing a reply, recording it, again.
A pool, not a single worker, so one slow or wedged message can't block every message behind it;
the others keep draining.
claim_next is race-safe (FOR UPDATE SKIP LOCKED), so two workers never grab the same message.
The reply is computed in a killable child process (execution.run_with_deadline),
so a worker is never itself pinned by work that hangs —
the work is killed at the deadline and the message failed, and the worker moves on.
The reply is real for a recognised symbiot —
composed from the diary facts the message bears on, and the persona (the read path, Tier 1) —
while an anonymous caller gets a stand-in, answered without the symbiot's private memory.
The context is gathered on the worker's own thread; only the composing LLM call runs in the killable child, where the deadline bites.

A message is claimed in its own transaction, so the moment work begins the row is
durably 'working'; the reply is written in a second transaction once it exists.
The loop guards itself against a failing iteration so one bad message can't take the
whole thread down with it.

Alongside the pool runs one reconcile sweep (run_reconcile_sweep) — the referee.
Where a worker turns received into answered, the sweep settles the rows a worker can't:
it fails an over-age 'working' row (a hang or dead loop),
re-queues a 'failed' row that still has attempts left,
and parks one that has spent them in 'abandoned'.
So every message reaches a terminal outcome — answered or abandoned —
retried a bounded number of times along the way, and never left waiting on an answer that will never come.
"""

import threading
import traceback
from datetime import datetime

from core import config
from core import db
from core import logs
from core import protocol
from services.memory import conversation
from services.memory import deep_retrieval
from services.memory import enrichment
from services.loop import execution
from services.memory import intake
from services.loop import missive
from services.loop import notify
from services.memory import ontology
from services.adapters import push
from services.tools import reminder
from services.loop import reply
from services.memory import retrieval
from services.tools import tools
from services.loop import zone

# How long the loop waits before looking again once it finds nothing to do.
POLL_INTERVAL_SECONDS = 1.0

# How often the reconcile sweep runs. This is only the cadence of the check, not the
# deadline itself — the ceiling is config.INTAKE_DEADLINE_SECONDS. Fine-grained relative
# to the ceiling, so an overdue message is ruled failed, and a failed one retried or
# abandoned, promptly rather than lingering most of a sweep interval.
SWEEP_INTERVAL_SECONDS = 5.0


def _answer(
    pool,
    message_id: int,
    message: str,
    symbiot_id: int | None,
    facts: list[retrieval.Fact],
    conv: conversation.Conversation,
    now_local: datetime,
    zone_name: str,
    shortlist: list[tools.ToolCandidate],
) -> execution.Result:
    """Produce a message's answer, forking into the tool seam only when the gate surfaced a candidate.

    This is the read path's one additive fork —
    invisible to the overwhelming majority of messages, which ask for nothing to be done.
    With no tool candidate (the gate closed), or an anonymous caller (no symbiot to act for),
    the answer is the ordinary reply — one composing call in the killable child, exactly as before.
    When a candidate surfaced, the flow is decide, act, speak:
    a decision call names a tool and extracts its arguments, or answers "none";
    a "none" falls back to the ordinary reply, which has the full memory to answer well;
    a named tool's executor runs on this thread, and a second call composes the confirmation.
    Each composing call is its own killable child under the deadline;
    the executor runs here, on the worker's own thread, never in a child that could be severed mid-effect.
    Returns the execution.Result the caller records — completed with the answer, or failed to be retried.
    """
    if symbiot_id is None or not shortlist:
        return execution.run_with_deadline(
            _produce_reply,
            (message, symbiot_id, facts, conv, now_local, zone_name),
            config.INTAKE_DEADLINE_SECONDS,
        )
    # A candidate surfaced — decide, in the child, on the shortlist and the recent tail (not the full diary).
    decided = execution.run_with_deadline(
        _decide_tool,
        (message, shortlist, conv.tail, now_local, zone_name),
        config.INTAKE_DEADLINE_SECONDS,
    )
    if decided.status != execution.COMPLETED:
        # A timeout or a crash deciding fails the message, to be retried like any other — nothing was done yet.
        return decided
    decision = decided.value
    if decision.tool == tools.NO_TOOL:
        # Coarse recall surfaced a candidate, but nothing truly fit — hand off to the ordinary reply.
        return execution.run_with_deadline(
            _produce_reply,
            (message, symbiot_id, facts, conv, now_local, zone_name),
            config.INTAKE_DEADLINE_SECONDS,
        )
    # A tool was named — run its executor on this thread, never the killable child, exactly-once.
    try:
        result = _execute_tool(pool, decision, symbiot_id, message_id, now_local, zone_name)
    except Exception:
        # A failed effect fails the message; the retry re-runs,
        # and the executor's exactly-once guard (the reminder's ON CONFLICT) makes that second run harmless,
        # so nothing is ever done twice.
        return execution.Result(execution.CRASHED, traceback.format_exc())
    # Speak — compose the confirmation, or the clarifying question, in the child, in the persona's voice.
    return execution.run_with_deadline(
        _compose_confirmation,
        (message, result, now_local, zone_name),
        config.INTAKE_DEADLINE_SECONDS,
    )


def _compose_confirmation(task: tuple[str, tools.ToolResult, datetime, str]) -> str:
    """Compose the confirmation the human sees after a tool ran — the speak step, in the killable child.

    Pure model work, like the ordinary reply, so it runs in the child under the deadline (see _answer).
    task is (message, result, now_local, zone_name), one tuple because the child takes a single arg;
    it speaks the tool's own result in the persona's voice (tools.compose_confirmation) —
    confirming when the tool acted, asking for what was missing when it could not.
    """
    message, result, now_local, zone_name = task
    return tools.compose_confirmation(message, result, now_local, zone_name)


def _compress_one() -> bool:
    """Trim one symbiot's over-trigger verbatim tail back into its Gist. True if there was a fold to make.

    The short-term memory gradient's background half, cut from the ingestion sweep's cloth:
    when the verbatim tail grows past its size budget, its oldest turns are folded into the Gist —
    the single running summary of everything older than the tail —
    and this is where that fold happens, off the path the symbiot waits on, so a reply carries zero latency from it.
    Nothing is dropped and nothing goes dark meanwhile:
    those turns stay fully visible in the reader's tail until this commits.
    Each pass finds a symbiot whose tail is over the trigger (next_symbiot_to_fold),
    claims that symbiot's fold so no other worker runs it at the same time (claim_fold),
    reads its current Gist and the oldest turns to fold out (current_gist, pending_for_fold),
    asks the same heavy model that composes the replies to merge them into one fresh paragraph (conversation.fold),
    and appends the result with the new cutoff (record_gist).

    The whole fold runs inside one transaction on one connection, under a non-blocking advisory lock claimed up front.
    That lock is what keeps a multi-worker deployment honest:
    without it two sweeps could both spot the same over-budget tail, both run the metered fold, and both append a Gist for the one cutoff.
    With it, the second worker's claim comes back False and it skips the symbiot this pass rather than blocking on the first.
    Holding the connection across the model call is the cost of a transaction-scoped lock, and a fair one here:
    only this one symbiot's fold is in flight at a time, so it pins a single pooled slot briefly, never the reply path.
    Exactly-once no longer leans on timing alone:
    the lock serialises would-be duplicates, and the single transaction makes the fold atomic —
    a crash or rollback before the append releases the lock and leaves the same turns eligible next pass,
    a commit advances the cutoff so they fall outside it, the boundary only ever moving forward.
    """
    budget = conversation.verbatim_budget()
    pool = db.get_pool()
    with pool.connection() as conn:
        with conn.transaction():
            symbiot_id = conversation.next_symbiot_to_fold(conn, budget)
            if symbiot_id is None:
                return False
            if not conversation.claim_fold(conn, symbiot_id):
                # Another worker owns this symbiot's fold; skip it cleanly rather than
                # block on its model call or race it to a duplicate Gist. It stays eligible for next pass.
                return False
            gist = conversation.current_gist(conn, symbiot_id)
            cutoff = gist[1] if gist is not None else 0
            turns, new_cutoff = conversation.pending_for_fold(conn, symbiot_id, budget, cutoff)
            # next_symbiot_to_fold proved the tail is over the trigger, and this reads the same boundary
            # in the same transaction, so turns is normally non-empty;
            # the guard is defence against a boundary edge case, leaving nothing written and the lock released on exit.
            if not turns:
                return False
            # The fold stamps each turn with its local time,
            # so it reads the symbiot's zone the same way the reply path does —
            # the clock is what lets temporal ordering survive the compression.
            zone_name = zone.of(conn, symbiot_id)
            merged = conversation.fold(gist[0] if gist is not None else None, turns, zone_name)
            conversation.record_gist(conn, symbiot_id, merged, new_cutoff)
    return True


def _decide_tool(
    task: tuple[str, list[tools.ToolCandidate], list[conversation.Turn], datetime, str],
) -> tools.Decision:
    """Decide which tool a message is asking for, and extract its arguments — the decide step, in the child.

    Pure model work under the deadline, like the ordinary reply (see _answer).
    task is (message, shortlist, tail, now_local, zone_name), one tuple because the child takes a single arg;
    it builds the flat decision schema from the shortlist and calls the model (tools.decide),
    returning a Decision — a named tool with its arguments, or "none".
    """
    message, shortlist, tail, now_local, zone_name = task
    return tools.decide(message, shortlist, tail, now_local, zone_name)


def _enrich_one() -> bool:
    """Run the deep second pass on one answered message, sending a follow-up if it earns one. True if there was one to run.

    Tier 2's background half, cut from the ingestion sweep's cloth:
    once a message's fast reply is settled, this reaches deeper into the diary by meaning (deep_retrieval.deep_search) —
    vector recall plus the ontology walk out from it — and, only when that reach adds something the fast answer didn't,
    the machine sends an enriched follow-up. All of it off the path the symbiot waited on, so the fast reply carries no cost from it.
    Each pass finds an answered, authed message with no enrichment yet (next_to_enrich),
    claims the symbiot's enrichment so no other worker forms a deep reply for it at the same time (claim),
    gathers the deep facts and the origin reference the follow-up must situate itself against (deep_search, origin_reference),
    and gates-and-composes on the heavy model (compose) — which stays silent unless the enrichment is worth the interruption.

    The whole pass runs inside one transaction on one connection, under a non-blocking advisory lock claimed up front —
    the same shape, and the same reason, as the compression fold, but keyed on the symbiot rather than the message:
    without the lock, two adjacent messages could each run the deep reach and the composing call at once,
    neither seeing the other's follow-up yet, and both deliver near-identical deep replies.
    With it, the second worker's claim comes back False and it skips the symbiot this pass rather than blocking,
    so its next message stays eligible until the first is committed and on the stream for the gate to weigh against.
    Holding the connection across the model call is the cost of a transaction-scoped lock, and a fair one here:
    only one deep reply for a symbiot is in flight at a time, so it pins a single pooled slot briefly, never the reply path.
    A follow-up and its provenance row commit together — sent-and-recorded atomically — so a crash before the commit
    leaves nothing sent and the message still eligible, and a commit records the pass so it is never re-run;
    exactly-once falls out of the UNIQUE intake_id the same way ingestion's does, the lock only keeping concurrent passes from racing to it.
    """
    pool = db.get_pool()
    missive_id = None
    with pool.connection() as conn:
        with conn.transaction():
            pending = enrichment.next_to_enrich(conn)
            if pending is None:
                return False
            intake_id, symbiot_id, message, answer = pending
            if not enrichment.claim(conn, symbiot_id):
                # Another worker is already forming a deep reply for this symbiot;
                # skip cleanly rather than race it to a duplicate follow-up — this message stays eligible for the next pass.
                return False
            related = deep_retrieval.deep_search(conn, message, exclude_intake_id=intake_id)
            origin = enrichment.origin_reference(conn, symbiot_id, intake_id, message, answer)
            # The symbiot's zone and local now, so the deep facts' dates render in the human's local day
            # and the follow-up — composed a beat after the fast answer — reasons about "now" against a real present,
            # the same current-time reference the fast reply already states rather than the void the deep pass had before.
            zone_name = zone.of(conn, symbiot_id)
            now_local = zone.now_for(zone_name)
            surface, follow_up = enrichment.compose(origin, related, zone_name=zone_name, now_local=now_local)
            if surface:
                # Raise the missive and mirror it onto the conversation stream in this same transaction,
                # so the follow-up and the provenance row that records it commit together — sent and recorded atomically.
                # missive.deliver is deliberately not called: it opens its own transaction and would split the send
                # from the record, so its two data-access steps are replicated here to land under this pass's one commit.
                missive_id = missive.raise_for(conn, symbiot_id, follow_up)
                conversation.record_utterance(
                    conn, symbiot_id, "machine", follow_up, missive_id=missive_id
                )
            enrichment.record(conn, intake_id, symbiot_id, missive_id)
    # Nudge the symbiot that a missive is waiting — outside the transaction, so a slow push never holds a connection,
    # and best-effort, because the record already stands: the follow-up surfaces on the next /inbox open regardless.
    if missive_id is not None:
        # A follow-up the kernel raised on its own — no tool, no request behind it —
        # so it fans out to every channel there is
        # (the dispatcher then drops any the symbiot has globally disabled).
        # Titled as the symbiot's own reaching-out,
        # not by the internal pass that produced it.
        # suppress_when_present, like every unprompted missive:
        # if the symbiot is watching the shell,
        # the live /inbox poll already surfaces this record,
        # so the out-of-app nudge is held rather than doubling up.
        notification = notify.Notification(title="The Joy", body=follow_up, pointer="/inbox")
        notify.dispatch(pool, symbiot_id, notification, list(notify.ALL_CHANNELS), suppress_when_present=True)
    return True


def _execute_tool(
    pool, decision: tools.Decision, symbiot_id: int, message_id: int, now_local: datetime, zone_name: str
) -> tools.ToolResult:
    """Run the named tool's executor on the worker's own thread, in its own transaction — the act step.

    Never in the killable child:
    the child can be severed at the deadline mid-run,
    and a half-done side effect is exactly what the rest of the kernel is careful never to leave behind,
    so the effect runs here, where nothing kills it.
    One transaction, so the effect and its exactly-once guard commit together (tools.execute → the executor);
    the message id is that guard's key, so a retried message re-runs this harmlessly.
    """
    with pool.connection() as conn:
        with conn.transaction():
            return tools.execute(conn, decision, symbiot_id, message_id, now_local, zone_name)


def _fire_one() -> bool:
    """Fire one due reminder as a missive, exactly once. True if there was one to fire.

    The reminder tool's due side, cut from the enrichment sweep's cloth.
    It claims the oldest unfired reminder whose moment has come
    (reminder.claim_due, under FOR UPDATE SKIP LOCKED so two sweeps never fire the same one),
    raises the stored line as a missive, mirrors it onto the conversation stream, and stamps the reminder fired —
    all in one transaction, so the send and the record commit together.
    A crash before the commit leaves the reminder unfired and simply due again;
    a commit sends it and stamps it, so it is never delivered twice —
    exactly-once on the firing side, pinned in the database the way the rest of the kernel pins it,
    not by the sweep being careful.
    The nudge is best-effort and outside the transaction,
    since the missive already stands to be read on the next inbox open regardless.
    """
    pool = db.get_pool()
    symbiot_id = None
    with pool.connection() as conn:
        with conn.transaction():
            due = reminder.claim_due(conn)
            if due is None:
                return False
            reminder_id, symbiot_id, body, channels = due
            # Raise the missive and mirror it onto the stream in this same transaction, then stamp the reminder,
            # so the line the kernel says on its own is remembered and the reminder is recorded delivered —
            # sent and recorded atomically, the same shape the enrichment follow-up commits under.
            missive_id = missive.raise_for(conn, symbiot_id, body)
            conversation.record_utterance(conn, symbiot_id, "machine", body, missive_id=missive_id)
            reminder.mark_fired(conn, reminder_id)
    # Fan the reminder out over the channels the symbiot chose when they set it, or — when they named none —
    # the whole set the tool supports. Empty and null both mean "they didn't narrow it", so both fall back to
    # the full set; the dispatcher then drops any channel they've since globally disabled.
    fire_channels = list(channels) if channels else list(reminder.SUPPORTED_CHANNELS)
    notification = notify.Notification(title="Reminder", body=body, pointer="/inbox")
    notify.dispatch(pool, symbiot_id, notification, fire_channels)
    return True


def _gather_context(
    pool, message: str, message_id: int, symbiot_id: int | None
) -> tuple[list[retrieval.Fact], conversation.Conversation, str, list[tools.ToolCandidate]]:
    """The memories, the timezone, and the tool shortlist a reply draws on, gathered before it is composed.

    The long-term diary facts that bear on the message (retrieval.search, recall by relevance),
    the short-term conversation the message sits inside (conversation.recent, recall by recency),
    the symbiot's IANA timezone (zone.of), so the reply reasons about time in the human's day, not UTC,
    and the tools the message might be reaching for (tools.search_catalog, the gate before the tool seam).
    All run on the worker's own thread, not in the killable child:
    they are bounded, indexed reads that carry no hang risk to protect against,
    and doing them here keeps the child — where the deadline bites — to the calls that can run long, the LLM's.
    They share one connection: reads of the same symbiot's state, taken together.
    Only a recognised symbiot's message reaches any of this:
    an anonymous line is answered without it, so the symbiot's memory is never read to answer a stranger,
    it never composes a real reply — so the UTC default handed back for it is a placeholder the stand-in ignores —
    and no tool is offered it, because a tool acts on the symbiot's behalf and there is no symbiot here to act for.
    The message being answered is excluded from the conversation tail —
    it was written onto the stream when it arrived,
    so without this it would show up both as the last turn and as the current message the prompt states.
    """
    if symbiot_id is None:
        return [], conversation.Conversation(gist=None, tail=[]), zone.DEFAULT_ZONE, []
    with pool.connection() as conn:
        facts = retrieval.search(conn, message)
        conv = conversation.recent(conn, symbiot_id, exclude_intake_id=message_id)
        zone_name = zone.of(conn, symbiot_id)
        shortlist = tools.search_catalog(conn, message)
    return facts, conv, zone_name, shortlist


def _ingest_one() -> bool:
    """File the next of the symbiot's settled messages into the diary. True if there was one to file.

    The read that finds an eligible message and the write that files it use separate connections:
    ontology.ingest is a long run of model calls,
    and holding the eligibility read's connection across it would pin a pooled slot for no reason.
    Exactly-once does not depend on holding anything —
    the UNIQUE intake_id, and the eligibility that mirrors it, make a re-file a no-op —
    so nothing is lost by letting go between the two.
    """
    pool = db.get_pool()
    with pool.connection() as conn:
        pending = intake.next_uningested(conn)
    if pending is None:
        return False
    message_id, message = pending
    with pool.connection() as conn:
        ontology.ingest(conn, message, intake_id=message_id)
    return True


def _process_one() -> bool:
    """Claim the oldest waiting message, produce its reply under a deadline, record it.

    Returns True if there was a message to process, False if none was waiting.
    The reply is computed in a killable child process (execution.run_with_deadline),
    so work that hangs or dead-loops is killed at the deadline instead of pinning this worker:
    a completed run is marked answered, a timeout or a crash is marked failed.
    An answer, being terminal, also nudges the message's push subscription (if it registered one).
    The claim and the outcome are separate transactions:
    a claimed message is durably 'working' before any work is attempted,
    so a crash mid-work leaves a visible 'working' row (which the deadline sweep will later fail)
    rather than a half-written answer.
    """
    pool = db.get_pool()
    with pool.connection() as conn:
        claimed = intake.claim_next(conn)
    if claimed is None:
        return False
    message_id, message, symbiot_id = claimed
    # Assemble the answer-time memory here, on the worker's thread, before any killable child runs:
    # the reads are fast and bounded, so only the composing LLM calls need the deadline's protection.
    # The symbiot's local "now" is resolved here too, from the zone gathered alongside the memory,
    # so the reply reasons about time in the human's day rather than the server's UTC —
    # and it is a concrete instant the child reads, not a clock the child would have to consult mid-compose.
    # The tool shortlist is gathered here too — the gate before the tool seam _answer may fork into.
    facts, conv, zone_name, shortlist = _gather_context(pool, message, message_id, symbiot_id)
    now_local = zone.now_for(zone_name)
    result = _answer(pool, message_id, message, symbiot_id, facts, conv, now_local, zone_name, shortlist)
    answered = False
    with pool.connection() as conn:
        if result.status == execution.COMPLETED:
            answered = intake.mark_answered(conn, message_id, result.value)
            # The reply is now durable on the intake row; mirror it onto the conversation stream
            # so the next turn sees this exchange as short-term memory.
            # Only a recognised symbiot's reply joins the stream (the conversation is theirs),
            # and only if this call made the move —
            # if the sweep failed the row first, mark_answered is False and there is no reply to mirror.
            # Same transaction as mark_answered, so the reply and its stream row commit together,
            # and both point at the one place the words live durably (this intake row).
            if answered and symbiot_id is not None:
                conversation.record_utterance(
                    conn, symbiot_id, "machine", result.value, intake_id=message_id
                )
        elif result.status == execution.TIMED_OUT:
            # The work outran the deadline and was killed; the child left no reason behind.
            intake.mark_failed(conn, message_id, "deadline exceeded")
        else:
            # A crash: result.value is the child's full traceback, recorded as the reason.
            intake.mark_failed(conn, message_id, result.value)
    # Only on a move this call actually made (mark_answered is False if the sweep failed the row first):
    # 'answered' is terminal, so nudge whoever asked to be told.
    # Outside the transaction above — a slow push service must never hold a connection.
    if answered:
        push.notify(pool, message_id)
    return True


def _produce_reply(
    task: tuple[str, int | None, list[retrieval.Fact], conversation.Conversation, datetime, str],
) -> str:
    """The reply to a message, given the line, who sent it, the memory gathered for it, and the symbiot's local now.

    A recognised symbiot gets a real answer,
    composed from the persona, the diary facts, the running conversation, and its local time (reply.compose) —
    read off long- and short-term memory rather than a canned line, and clocked to the human's day, not UTC.
    An anonymous caller gets the stand-in still:
    the memory is the symbiot's, so a stranger is answered without it,
    and told to authenticate rather than handed a conversation that isn't theirs.
    task is (message, symbiot_id, facts, conversation, now_local, zone_name):
    it arrives as one tuple because the work runs in a child process (see run_with_deadline), which passes a single arg —
    facts, conversation, and the local now are what _gather_context and now_for already pulled on the worker thread.
    """
    message, symbiot_id, facts, conv, now_local, zone_name = task
    if symbiot_id is None:
        return protocol.STANDIN_ANSWER_ANON
    return reply.compose(message, facts, conv, now_local=now_local, zone_name=zone_name)


def run(stop: threading.Event) -> None:
    """Process messages until `stop` is set. Started from the kernel's lifespan.

    A processed message means looking again immediately, draining a backlog fast;
    an empty pass waits a beat so an idle kernel isn't spinning on the database.
    """
    log = logs.get("worker")
    log.info("worker started")
    while not stop.is_set():
        try:
            worked = _process_one()
        except Exception:
            # Keep the loop alive across a bad iteration so one message can't kill it.
            log.exception("worker iteration failed")
            worked = False
        if not worked:
            stop.wait(POLL_INTERVAL_SECONDS)
    log.info("worker stopped")


def run_compression_sweep(stop: threading.Event) -> None:
    """Fold aged-out turns into each symbiot's Gist until `stop` is set. Started from the kernel's lifespan.

    The fourth background loop beside the worker pool, the reconcile sweep, and the ingestion sweep,
    and the non-blocking half of the memory gradient's promise:
    a reply is composed and sent the instant it is ready,
    and when the verbatim tail grows past its size budget its oldest turns are summarised into the Gist here,
    off to the side, on this thread —
    so the symbiot never waits on a fold, and the tail is trimmed back rather than the reader ever going blind.
    It drains the way the others do: a fold made means looking again at once, an idle pass waits a beat,
    so a tail grown fat clears back to its budget quickly without an idle kernel spinning on the database.
    One bad fold can't take the loop down —
    a failed fold is logged and left eligible for the next pass, since the cutoff has not moved,
    tried again if it was a transient hiccup off the critical path.
    Its own on/off switch (config.COMPRESS_ENABLED), like the ingestion sweep and the GC,
    because a machine might reasonably want the verbatim tail without the folding, or the reverse;
    it shares only worker_stop.
    """
    log = logs.get("worker")
    log.info("compression sweep started")
    while not stop.is_set():
        try:
            worked = _compress_one()
        except Exception:
            # Keep the loop alive across a bad fold so one symbiot's overflow can't kill it.
            log.exception("compression sweep iteration failed")
            worked = False
        if not worked:
            stop.wait(config.COMPRESS_SWEEP_INTERVAL_SECONDS)
    log.info("compression sweep stopped")


def run_enrichment_sweep(stop: threading.Event) -> None:
    """Run the deep second pass on answered messages until `stop` is set. Started from the kernel's lifespan.

    The fifth background loop beside the worker pool, the reconcile sweep, the ingestion sweep, and the compression sweep,
    and the whole of Tier 2's non-blocking promise:
    the fast reply is composed and sent the instant it is ready,
    and only afterwards, here, does the machine reach deeper into the diary by meaning and — if it finds something worth saying —
    follow up, so the symbiot never waits on the deep reach's embedding and model calls to get their answer.
    It drains the way the others do: a pass made means looking again at once, an idle pass waits a beat,
    so a backlog of just-answered messages is worked through promptly without an idle kernel spinning on the database.
    One bad pass can't take the loop down —
    a failed pass is logged and left eligible for the next round, since no enrichment row was committed for it,
    tried again if it was a transient hiccup off the critical path, or failing harmlessly if it is a poison line.
    Its own on/off switch (config.ENRICH_ENABLED), like the ingestion and compression sweeps and the GC,
    because a machine might reasonably want the fast replies without the deep follow-ups, or the reverse;
    it shares only worker_stop, so the whole process winds down together.
    """
    log = logs.get("worker")
    log.info("enrichment sweep started")
    while not stop.is_set():
        try:
            worked = _enrich_one()
        except Exception:
            # Keep the loop alive across a bad pass so one message can't kill it.
            log.exception("enrichment sweep iteration failed")
            worked = False
        if not worked:
            stop.wait(config.ENRICH_SWEEP_INTERVAL_SECONDS)
    log.info("enrichment sweep stopped")


def run_ingestion_sweep(stop: threading.Event) -> None:
    """File settled messages into the diary until `stop` is set. Started from the kernel's lifespan.

    The parallel, non-blocking half of the read path's promise:
    the reply is sent the instant it is ready, and the message is distilled into the diary here, off to the side,
    on its own thread — so the symbiot never waits on ingestion's several model calls to get their answer.
    It drains the way the workers do: a filed message means looking again at once, an idle pass waits a beat,
    so a backlog clears quickly without an idle kernel spinning on the database.
    One bad message can't take the loop down — a failed filing is logged and left eligible for the next pass,
    tried again if it was a transient hiccup, or failing harmlessly off the critical path if it is a poison line.
    """
    log = logs.get("worker")
    log.info("ingestion sweep started")
    while not stop.is_set():
        try:
            worked = _ingest_one()
        except Exception:
            # Keep the loop alive across a bad filing so one message can't kill it.
            log.exception("ingestion sweep iteration failed")
            worked = False
        if not worked:
            stop.wait(config.INGEST_SWEEP_INTERVAL_SECONDS)
    log.info("ingestion sweep stopped")


def run_reconcile_sweep(stop: threading.Event) -> None:
    """Reconcile the messages no worker is actively holding, until `stop` is set.

    The referee to the workers' players: one sweeper wakes on a fixed cadence and, each
    pass, settles the rows a worker can't settle itself —
      * a message stuck in working past config.INTAKE_DEADLINE_SECONDS is failed, so a
        hang or dead loop can't leave it waiting on an answer forever;
      * a failed message with attempts left is re-queued for another try;
      * a failed message that has spent its attempts is parked in abandoned.
    So a failure is retried a bounded number of times and then given a terminal verdict,
    without a worker having to stay alive across the wait between tries. An abandonment,
    being terminal, also nudges the message's push subscription (if it registered one).
    Every move is guarded in the database (each only touches rows in the state it expects),
    so the sweep never fights a worker finishing legitimately. Its own bad iteration can't
    take the loop down.
    """
    log = logs.get("worker")
    log.info("reconcile sweep started")
    while not stop.is_set():
        try:
            with db.get_pool().connection() as conn:
                failed = intake.fail_overdue(conn, config.INTAKE_DEADLINE_SECONDS)
                requeued = intake.requeue_failed(conn, config.MAX_INTAKE_ATTEMPTS)
                abandoned = intake.abandon_exhausted(conn, config.MAX_INTAKE_ATTEMPTS)
            if failed:
                log.warning("reconcile: failed %d overdue message(s)", failed)
            if requeued:
                log.info("reconcile: re-queued %d failed message(s) for retry", requeued)
            if abandoned:
                log.warning("reconcile: abandoned %d message(s) out of attempts", len(abandoned))
            # 'abandoned' is terminal: nudge each one's subscription, if it has one, that the kernel gave up.
            # Outside the transaction above, so a slow push can't hold a connection;
            # a failed nudge is swallowed and never breaks the sweep.
            for message_id in abandoned:
                push.notify(db.get_pool(), message_id)
        except Exception:
            log.exception("reconcile sweep iteration failed")
        stop.wait(SWEEP_INTERVAL_SECONDS)
    log.info("reconcile sweep stopped")


def run_reminder_sweep(stop: threading.Event) -> None:
    """Deliver due reminders as missives until `stop` is set. Started from the kernel's lifespan.

    The sixth background loop beside the worker pool and the other sweeps,
    and the firing half of the reminder tool:
    the tool schedules a reminder on the reply path,
    and here, off it, the kernel reaches back out at the appointed moment and says the stored line.
    It drains the way the others do: a reminder fired means looking again at once, an idle pass waits a beat,
    so a batch of reminders that came due together is delivered promptly without an idle kernel spinning.
    One bad firing can't take the loop down —
    a failed pass is logged and left for the next round, since the reminder stays unfired until its send commits,
    tried again if it was a transient hiccup off the critical path.
    Its own on/off switch (config.REMINDER_ENABLED), like the other sweeps,
    so a box can want the reply path without the firing loop, or the reverse; it shares only worker_stop.
    """
    log = logs.get("worker")
    log.info("reminder sweep started")
    while not stop.is_set():
        try:
            worked = _fire_one()
        except Exception:
            # Keep the loop alive across a bad firing so one reminder can't kill it.
            log.exception("reminder sweep iteration failed")
            worked = False
        if not worked:
            stop.wait(config.REMINDER_SWEEP_INTERVAL_SECONDS)
    log.info("reminder sweep stopped")
