"""Database access: a connection pool, the migration runner, and the symbiot seed.

No ORM.
Raw, parameterised SQL over psycopg 3.
Migrations are ordered `.sql` files under migrations/,
each applied once inside its own transaction and recorded in the schema_migrations ledger the runner owns.
The runner is idempotent — already-applied files are skipped —
so startup can always call it, and so can the test suite, against the same code path.
"""

import logging
from pathlib import Path

from psycopg_pool import ConnectionPool

logger = logging.getLogger("kernel.db")

# This module lives in core/, so the migrations directory is one level up at the repo root.
MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

# The single pool for the process,
# opened at startup (or by the test fixture) and read back by request handlers via get_pool().
_pool: ConnectionPool | None = None


def _applied_versions(conn) -> set[str]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_conn():
    """A pooled connection for the request, in autocommit — so nothing a route writes is still pending when it answers.

    The subtlety this exists to fix, and it is the sort that hides.
    A FastAPI dependency that yields runs the code *after* its yield when the dependency unwinds,
    and that unwinding happens after the response has already gone out.
    So a connection whose transaction closed there would commit *after* the shell had its reply in hand —
    leaving a window in which the shell holds an acknowledgement for a write no other connection can see yet,
    and a second request made inside that window reads the state as it was before.
    Measured on the reminder routes, roughly half of back-to-back read-after-writes fell in it.
    Which is corrosive wherever the shell acts on what a reply told it:
    the reminders listing hands out display positions, so a reply one change stale points at the wrong row.

    Autocommit closes the window at the root rather than route by route:
    each statement commits as it runs, so by the time a handler returns there is nothing pending at all,
    and every route gets the same guarantee without having to remember it.

    A route or a store function that needs several statements to land together says so —
    `with conn.transaction():` — which under autocommit is a real transaction that commits at the block's end,
    still before the handler returns
    (the /intake route's message-and-mirror pair and identity.verify_login_code are where that matters).
    Nested inside a caller's own transaction it degrades to a savepoint,
    so the same store functions stay correct when a worker calls them instead of a route.

    Autocommit is set on the borrowed connection rather than on the pool, and restored on the way back,
    because the pool is shared with the background workers —
    and their `_process_one` leans on the implicit transaction to commit a reply and its conversation row together.
    A leaked autocommit would quietly take that guarantee away from them.
    """
    with get_pool().connection() as conn:
        previous = conn.autocommit
        conn.autocommit = True
        try:
            yield conn
        finally:
            conn.autocommit = previous


def get_pool() -> ConnectionPool:
    """The open pool. Raises if nothing opened it — a wiring bug, caught loud."""
    if _pool is None:
        raise RuntimeError("connection pool not opened — call open_pool() at startup")
    return _pool


def migrate_and_seed(pool: ConnectionPool, symbiot_email: str) -> None:
    """The full startup sequence: schema first, then the symbiot on top of it."""
    run_migrations(pool)
    seed_symbiot(pool, symbiot_email)


def open_pool(conninfo: str) -> ConnectionPool:
    """Open the process-wide pool against `conninfo` and return it."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(conninfo, min_size=1, max_size=10, open=True)
    return _pool


def run_migrations(pool: ConnectionPool) -> list[str]:
    """Apply every migration file not yet recorded, in filename order.

    Each file runs inside one transaction together with the ledger insert,
    so a half-applied migration can't be marked done.
    Returns the versions applied this call (empty when the database is already current).
    """
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    applied: list[str] = []
    with pool.connection() as conn:
        done = _applied_versions(conn)
        for path in files:
            version = path.name
            if version in done:
                continue
            sql = path.read_text()
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
                )
            applied.append(version)
            logger.info("applied migration %s", version)
    return applied


def seed_symbiot(pool: ConnectionPool, email: str) -> None:
    """Ensure the symbiot named by SYMBIOT_EMAIL exists. Idempotent — safe every startup.

    Today exactly one address is seeded this way,
    but the `symbiot` table and the /login lookup already hold and match many,
    so more symbiots are a matter of seeding, not a schema change.

    An empty email means the kernel is misconfigured:
    /login can never succeed, so we say so loudly rather than seed a blank row.
    """
    email = (email or "").strip().lower()
    if not email:
        logger.warning("SYMBIOT_EMAIL is unset — no symbiot seeded; /login cannot succeed")
        return
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO symbiot (email) VALUES (%s) ON CONFLICT (email) DO NOTHING",
            (email,),
        )
    logger.info("symbiot seeded: %s", email)
