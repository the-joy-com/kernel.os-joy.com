"""Kernel logging — one timestamped stream for the whole `kernel.*` tree.

uvicorn configures only its own loggers, and its access line carries no
wall-clock — the one thing missing when reading a live tail to see *when* a
round trip landed. So the kernel owns its own logging: every module logs under
`kernel.<area>` (`kernel.db`, `kernel.intake`, …) and the whole tree surfaces
through the handlers configured here.

Two handlers, one format: a stream (uvicorn's stdout, what journalctl captures)
and a rotating file at the repo root (`kernel.log`), so an operator on the box
can `tail -f kernel.log` — or scp it off — without reaching for journalctl, and
the same wall-clocked lines stay on disk across restarts. The file rolls once,
to `kernel.log.1`, so the log on disk can never grow past two files.

The kernel runs in more than one process, and both of them log.
A unit of work is computed in a spawned child (services.loop.execution),
a fresh interpreter that never runs main.py,
so the child wires itself through `configure_child()`.
It differs from the parent in exactly one way: it appends to `kernel.log` instead of rotating it.
Rotation belongs to one process or to none, so the parent keeps it.
A child still holding the file across a rollover writes its last lines into `kernel.log.1`,
which is bounded and still on disk —
a better outcome than two processes renaming the file at each other.

A module wants two things from this file: whichever `configure` fits its process, called once,
and `get("<area>")` for its logger. Nothing reaches for `logging` directly, so
the format, the `kernel.` prefix, and the level all live in one place.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

# The root every module's logger hangs under, so one configure() wires them all.
ROOT = "kernel"

# The rotating file the tree also writes to, alongside the stream. This module is
# core/logs.py, so the repo root is one directory up — the file lands there.
LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kernel.log")

# One live file plus a single rotation: at the cap kernel.log rolls to kernel.log.1
# and starts fresh, so the log on disk is bounded at two files, never more.
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 1

# How much of the tree reaches the two handlers.
# INFO is the operator's level: what the kernel did, once per thing done.
# DEBUG adds the per-call detail beneath it,
# chiefly which model answered each generative call —
# several lines per message, and the only place the internal calls are named at all,
# since the durable ledger keeps only the ones made for a role (services.adapters.llm._served).
# Raised through the environment, so reaching that detail on a box is a restart rather than an edit here.
# An unrecognised name falls back to INFO rather than failing the boot:
# a typo in .env should cost the operator a level, never the kernel its startup.
LEVELS = {"CRITICAL", "DEBUG", "ERROR", "INFO", "WARNING"}
LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
if LEVEL not in LEVELS:
    LEVEL = "INFO"


def _wire(make_file_handler) -> None:
    """Attach a timestamped stream and a file handler to the `kernel` tree, at the configured level.

    Both entry points below come through here,
    so the format, the level, and the propagate=False that keeps these lines off the root logger
    are decided once rather than once per kind of process —
    a child's line and a parent's line about the same round trip read identically in the same file.
    Idempotent: a second call (the reload worker, a test re-import) is a no-op
    rather than stacking duplicate handlers that would print every line twice.
    The file handler arrives as a factory rather than as a handler,
    so an already-configured tree returns without having opened a second file to discard.
    """
    logger = logging.getLogger(ROOT)
    if logger.handlers:  # already configured
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    to_file = make_file_handler()
    to_file.setFormatter(formatter)
    logger.setLevel(LEVEL)
    logger.addHandler(stream)
    logger.addHandler(to_file)
    logger.propagate = False


def configure() -> None:
    """Wire the tree in the kernel's own process — the one that owns rotation.

    Called once at startup, from main.py.
    """
    _wire(lambda: RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT))


def configure_child() -> None:
    """Wire the tree inside a spawned child, which begins with no logging at all.

    A unit of work is computed in a fresh interpreter (services.loop.execution),
    and a fresh interpreter never runs main.py,
    so nothing had configured that process's `kernel` tree.
    Its INFO lines went nowhere at all;
    its warnings reached stderr only through logging's own last-resort handler,
    which is to say journalctl, and `kernel.log` never.
    That child is where the reply is composed —
    the generative ladder, the tools, the fold —
    so the most interesting half of the kernel's record was missing
    from the one file an operator reads to find out what happened.

    Appends rather than rotates: see the module header for why rotation stays with the parent.
    Opened lazily, so a child that logs nothing never touches the file.
    """
    _wire(lambda: logging.FileHandler(LOG_FILE, delay=True))


def get(area: str) -> logging.Logger:
    """The `kernel.<area>` logger a module should grab — e.g. get("intake")."""
    return logging.getLogger(f"{ROOT}.{area}")
