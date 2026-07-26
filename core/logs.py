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

A module wants two things from this file: call `configure()` once at startup,
and `get("<area>")` for its logger. Nothing reaches for `logging` directly, so
the format and the `kernel.` prefix live in one place.
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


def configure() -> None:
    """Attach a timestamped stream and rotating-file handler to the `kernel` tree.

    Idempotent: a second call (the reload worker, a test re-import) is a no-op
    rather than stacking duplicate handlers that would print every line twice.
    propagate=False keeps these lines off the root logger, so uvicorn's own
    config can never double-print them.
    """
    logger = logging.getLogger(ROOT)
    if logger.handlers:  # already configured
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    rotating = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
    rotating.setFormatter(formatter)
    logger.setLevel(logging.INFO)
    logger.addHandler(stream)
    logger.addHandler(rotating)
    logger.propagate = False


def get(area: str) -> logging.Logger:
    """The `kernel.<area>` logger a module should grab — e.g. get("intake")."""
    return logging.getLogger(f"{ROOT}.{area}")
