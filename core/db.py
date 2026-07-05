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
    """A pooled connection for the request,
    committed on success and rolled back on error when the `with` block closes after the response."""
    with get_pool().connection() as conn:
        yield conn


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
