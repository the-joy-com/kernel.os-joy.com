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

from core import config
from core import db
from core import logs
from core import protocol
from services import conversation
from services import execution
from services import intake
from services import ontology
from services import push
from services import reply
from services import retrieval

# How long the loop waits before looking again once it finds nothing to do.
POLL_INTERVAL_SECONDS = 1.0

# How often the reconcile sweep runs. This is only the cadence of the check, not the
# deadline itself — the ceiling is config.INTAKE_DEADLINE_SECONDS. Fine-grained relative
# to the ceiling, so an overdue message is ruled failed, and a failed one retried or
# abandoned, promptly rather than lingering most of a sweep interval.
SWEEP_INTERVAL_SECONDS = 5.0


def _compress_one() -> bool:
    """Trim one symbiot's over-trigger verbatim tail back into its Gist. True if there was a fold to make.

    The short-term memory gradient's background half, cut from the ingestion sweep's cloth:
    when the verbatim tail grows past its size budget, its oldest turns are folded into the Gist —
    the single running summary of everything older than the tail —
    and this is where that fold happens, off the path the symbiot waits on, so a reply carries zero latency from it.
    Nothing is dropped and nothing goes dark meanwhile:
    those turns stay fully visible in the reader's tail until this commits.
    Each pass finds a symbiot whose tail is over the trigger (next_symbiot_to_fold),
    reads its current Gist and the oldest turns to fold out (current_gist, pending_for_fold),
    asks the same heavy model that composes the replies to merge them into one fresh paragraph (conversation.fold),
    and appends the result with the new cutoff (record_gist).

    The eligibility read and the append use separate connections, the way ingestion's do:
    the fold is a model call, and holding the read's connection across it would pin a pooled slot for no reason.
    Exactly-once does not depend on holding anything — the cutoff only ever moves forward,
    so a crash before the append leaves the same turns eligible next pass,
    and a crash after has already advanced the boundary —
    so nothing is lost by letting go between the read and the write.
    """
    budget = conversation.verbatim_budget()
    pool = db.get_pool()
    with pool.connection() as conn:
        symbiot_id = conversation.next_symbiot_to_fold(conn, budget)
        if symbiot_id is None:
            return False
        gist = conversation.current_gist(conn, symbiot_id)
        cutoff = gist[1] if gist is not None else 0
        turns, new_cutoff = conversation.pending_for_fold(conn, symbiot_id, budget, cutoff)
    # next_symbiot_to_fold already proved the tail is over the trigger, so turns is never empty here
    # (the two read the same boundary);
    # the guard keeps a torn race between them from writing a no-op Gist.
    if not turns:
        return False
    merged = conversation.fold(gist[0] if gist is not None else None, turns)
    with pool.connection() as conn:
        conversation.record_gist(conn, symbiot_id, merged, new_cutoff)
    return True


def _gather_context(
    pool, message: str, message_id: int, symbiot_id: int | None
) -> tuple[list[retrieval.Fact], conversation.Conversation]:
    """Both memories a reply draws on, gathered synchronously before it is composed.

    The long-term diary facts that bear on the message (retrieval.search, recall by relevance),
    and the short-term conversation the message sits inside (conversation.recent, recall by recency).
    Runs on the worker's own thread, not in the killable child:
    both are bounded, indexed reads that carry no hang risk to protect against,
    and doing them here keeps the child — where the deadline bites — to the one call that can run long, the LLM.
    They share one connection: two reads of the same symbiot's memory, taken together.
    Only a recognised symbiot's message reaches either memory:
    an anonymous line is answered without them, so the symbiot's own memory is never read to answer a stranger.
    The message being answered is excluded from the conversation tail —
    it was written onto the stream when it arrived,
    so without this it would show up both as the last turn and as the current message the prompt states.
    """
    if symbiot_id is None:
        return [], conversation.Conversation(gist=None, tail=[])
    with pool.connection() as conn:
        facts = retrieval.search(conn, message)
        conv = conversation.recent(conn, symbiot_id, exclude_intake_id=message_id)
    return facts, conv


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
    # Assemble the answer-time memory here, on the worker's thread, before the killable child runs:
    # the reads are fast and bounded, so only the composing LLM call needs the deadline's protection.
    facts, conv = _gather_context(pool, message, message_id, symbiot_id)
    result = execution.run_with_deadline(
        _produce_reply, (message, symbiot_id, facts, conv), config.INTAKE_DEADLINE_SECONDS
    )
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


def _produce_reply(task: tuple[str, int | None, list[retrieval.Fact], conversation.Conversation]) -> str:
    """The reply to a message, given the line, who sent it, and the memory gathered for it.

    A recognised symbiot gets a real answer,
    composed from the persona, the diary facts, and the running conversation (reply.compose) —
    read off long- and short-term memory rather than a canned line.
    An anonymous caller gets the stand-in still:
    the memory is the symbiot's, so a stranger is answered without it,
    and told to authenticate rather than handed a conversation that isn't theirs.
    task is (message, symbiot_id, facts, conversation):
    it arrives as one tuple because the work runs in a child process (see run_with_deadline), which passes a single arg —
    facts and conversation are what _gather_context already pulled.
    """
    message, symbiot_id, facts, conv = task
    if symbiot_id is None:
        return protocol.STANDIN_ANSWER_ANON
    return reply.compose(message, facts, conv)


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
