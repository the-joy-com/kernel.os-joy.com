"""Running a unit of work under a hard deadline, in a process we can actually kill.

A worker computes a message's reply by calling the work in a *child process*, not inline in its own thread.
Python can't force-stop a thread, so a reply that hangs or dead-loops would pin its worker forever —
but the operating system can always kill a process.
So the work runs where it can be interrupted:
overrun the deadline and the child is killed and the message failed, freeing the worker the instant that happens.

This is the inner half of the runtime guarantee's timeout — it frees the *worker* the moment work overruns.
The deadline sweep (worker.run_deadline_sweep) is the outer half:
the backstop for when a worker, or the whole kernel, dies mid-work and can't fail the row itself.
Together no message hangs forever and no worker is held hostage by one that would.
"""

from collections import namedtuple
import multiprocessing
import traceback

# Empty is the exception queue.Queue raises when a non-blocking get finds nothing:
# get_nowait(), get(block=False), or get(timeout=...) after the timeout expires.
# In CPython it normally comes from the C extension _queue; queue.py falls back to
# a pure-Python Exception subclass if that import fails.
from queue import Empty

from core import logs

# A fresh interpreter per unit of work, never a fork: forking a multi-threaded process
# (the worker pool is threads) can inherit locks other threads hold and deadlock the
# child. spawn starts clean, at the cost of interpreter startup — cheap next to work that
# can run for seconds.
_context = multiprocessing.get_context("spawn")

# How long a killed child is given to end on SIGTERM before SIGKILL ends it outright.
_TERMINATION_GRACE_SECONDS = 2.0

# The three ways a unit of work can end.
COMPLETED = "completed"  # the work returned; value is the reply
TIMED_OUT = "timed_out"  # the work outran the deadline and was killed; value is None
CRASHED = "crashed"  # the work raised or the process died; value is the full traceback

Result = namedtuple("Result", "status value")


def _entry(fn, arg, queue):
    """The child's entry point: run the work, hand its outcome back over the queue.

    A raised exception is caught and reported as its full traceback,
    so a crash comes back as the whole story — every frame, not just the exception line —
    for the parent to record rather than a child that merely dies without a word.
    The traceback is captured here, inside the child, because it's the only place the live stack still exists;
    once the exception crosses the process boundary the frames are gone.
    Stored on the row, it's the raw material a later self-healing pass can read to understand what actually broke.
    """
    try:
        queue.put((COMPLETED, fn(arg)))
    except BaseException:  # the child reports every failure, never swallows one
        queue.put((CRASHED, traceback.format_exc()))


def run_with_deadline(fn, arg, deadline_seconds) -> Result:
    """Run fn(arg) in a child process, killing it if it outruns deadline_seconds.

    Returns COMPLETED with the reply, TIMED_OUT if the deadline passed (the child is terminated, then killed if it clings),
    or CRASHED with the reason if the work raised.
    The parent never blocks past the deadline plus a short kill grace.
    fn must be importable by name (a module-level function), so the spawned child can reach it;
    arg and the return value must be picklable, to cross the process boundary.
    """
    log = logs.get("execution")
    queue = _context.Queue()
    child = _context.Process(target=_entry, args=(fn, arg, queue), daemon=True)
    child.start()
    child.join(deadline_seconds)

    if child.is_alive():
        # Past the deadline and still running: interrupt it, escalate to a kill if it clings.
        child.terminate()
        child.join(_TERMINATION_GRACE_SECONDS)
        if child.is_alive():
            child.kill()
            child.join()
        log.warning("work exceeded the %.0fs deadline and was killed", deadline_seconds)
        return Result(TIMED_OUT, None)

    # The child ended on its own.
    # A child flushes its queue before it exits, so a result it produced is available now;
    # nothing on the queue means it died without producing one
    # (a hard crash — a segfault, os._exit — the try/except in the child couldn't catch).
    try:
        status, value = queue.get(timeout=_TERMINATION_GRACE_SECONDS)
        return Result(status, value)
    except Empty:  # grace timeout elapsed with nothing on the queue
        return Result(CRASHED, f"work process exited without a result (code {child.exitcode})")
