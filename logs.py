"""Kernel logging — one timestamped stream for the whole `kernel.*` tree.

uvicorn configures only its own loggers, and its access line carries no
wall-clock — the one thing missing when reading a live tail to see *when* a
round trip landed. So the kernel owns its own logging: every module logs under
`kernel.<area>` (`kernel.db`, `kernel.intake`, …) and the whole tree surfaces
through the single handler configured here.

A module wants two things from this file: call `configure()` once at startup,
and `get("<area>")` for its logger. Nothing reaches for `logging` directly, so
the format and the `kernel.` prefix live in one place.
"""

import logging

# The root every module's logger hangs under, so one configure() wires them all.
ROOT = "kernel"


def configure() -> None:
    """Attach a timestamped stream handler to the `kernel` logger tree.

    Idempotent: a second call (the reload worker, a test re-import) is a no-op
    rather than stacking duplicate handlers that would print every line twice.
    propagate=False keeps these lines off the root logger, so uvicorn's own
    config can never double-print them.
    """
    logger = logging.getLogger(ROOT)
    if logger.handlers:  # already configured
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False


def get(area: str) -> logging.Logger:
    """The `kernel.<area>` logger a module should grab — e.g. get("intake")."""
    return logging.getLogger(f"{ROOT}.{area}")
