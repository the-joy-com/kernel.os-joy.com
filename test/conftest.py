"""Test fixtures for the identity suite.

The kernel is pointed at a dedicated *test* database and a known symbiot before the app is imported,
so config reads these at import time.
Each test gets a clean database and a FakeEmailClient swapped in for the real Gmail one —
so the whole login flow is exercised end to end without a single network call.
"""

import os
import re

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

# Point the kernel at the test database and a known symbiot BEFORE importing the app —
# config.py reads the environment at import time.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://joy:joy@localhost:5432/joy_test"
)
os.environ["SYMBIOT_EMAIL"] = "symbiot@example.com"
os.environ["KERNEL_SECRET"] = "test-secret"
# Most tests re-issue codes back to back; a real cool-off would make that a no-op,
# so the default suite runs with no interval.
# The interval has its own focused test that turns it back on (monkeypatching config),
# where it's the thing under test.
os.environ["LOGIN_REISSUE_INTERVAL_SECONDS"] = "0"
# The intake worker would race the suite for received rows;
# the tests drive the state machine by hand, so the live loop stays off.
os.environ["WORKER_ENABLED"] = "0"
# Web push stays off unless a test opts in (by monkeypatching config), so no test can reach a real push service —
# even though a dev .env might carry a VAPID key, this pins it empty
# (load_dotenv won't override an env var already set, blank or not).
os.environ["VAPID_PRIVATE_KEY"] = ""


# The front-door lock: refuse a non-test database before the app is even imported,
# so its startup can't migrate or seed a live kernel on the way in.
# (The truncate fixture below holds a second, independent lock — it re-checks the
# *connected* database's own name right before wiping, trusting nothing about the URL.)
_db_name = os.environ["DATABASE_URL"].split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
if not _db_name.endswith("_test"):
    raise RuntimeError(
        f"refusing to run the test suite against database {_db_name!r}: "
        "its name must end in '_test'. Check DATABASE_URL / TEST_DATABASE_URL."
    )


def _ensure_test_database() -> None:
    # Make the suite self-sufficient: create the test database if it isn't there yet, so a
    # fresh clone — or a box where joy_test was dropped — just runs `pytest`, no manual
    # createdb step. Safe because the guard above has already proved the target name ends in
    # '_test', so this can only ever bring a *test* database into being, never a live one.
    # The migrations still build the schema inside it (the client fixture's lifespan); this
    # only ensures the empty database exists for that pool to connect to.
    # CREATE DATABASE can't run inside a transaction, so we connect to the 'postgres'
    # maintenance database in autocommit and create only on absence.
    params = conninfo_to_dict(os.environ["DATABASE_URL"])
    target = params["dbname"]
    admin = make_conninfo(**{**params, "dbname": "postgres"})
    with psycopg.connect(admin, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (target,)
        ).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))


_ensure_test_database()

from fastapi.testclient import TestClient
import pytest

import db
from email_client import FakeEmailClient
from main import app, get_email_client
from rate_limit import limiter

SYMBIOT_EMAIL = "symbiot@example.com"
_CODE_RE = re.compile(r"\b(\d{6})\b")


def _assert_test_database(conn) -> None:
    # The one hard stop between the suite and a live database.
    # The fixtures here TRUNCATE before every test, so running them against the real kernel would empty it mid-use.
    # We don't trust the URL to be the test one — a URL is editable and easy to get wrong —
    # we ask the *connected* database its own name and refuse unless it ends in '_test' (the suffix config guarantees).
    # Coupled to the wipe on purpose: the truncate can't run without passing this.
    name = conn.execute("SELECT current_database()").fetchone()[0]
    if not name.endswith("_test"):
        raise RuntimeError(
            f"refusing to run destructive tests against {name!r}: "
            "the test database name must end in '_test'. "
            "Check DATABASE_URL / TEST_DATABASE_URL before retrying."
        )


@pytest.fixture(autouse=True)
def clean_db(client):
    # A clean slate before every test:
    # wipe every table, re-seed the one symbiot.
    pool = db.get_pool()
    with pool.connection() as conn:
        _assert_test_database(conn)
        conn.execute(
            "TRUNCATE symbiot, login_code, session, intake, missive, reply_channel "
            "RESTART IDENTITY CASCADE"
        )
        conn.execute("INSERT INTO symbiot (email) VALUES (%s)", (SYMBIOT_EMAIL,))
    yield


@pytest.fixture(scope="session")
def client():
    # Entering the context runs the lifespan: open the pool, migrate, seed.
    with TestClient(app) as c:
        yield c


def count_codes() -> int:
    with db.get_pool().connection() as conn:
        return conn.execute("SELECT count(*) FROM login_code").fetchone()[0]


def extract_code(fake: FakeEmailClient) -> str:
    """Recover the 6-digit code a real client would have emailed."""
    assert fake.sent, "expected an email to have been sent"
    body = fake.sent[-1].body
    m = _CODE_RE.search(body)
    assert m, f"no 6-digit code found in email body: {body!r}"
    return m.group(1)


@pytest.fixture
def fake_email():
    fake = FakeEmailClient()
    app.dependency_overrides[get_email_client] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_email_client, None)


@pytest.fixture(autouse=True)
def reset_rate_limit():
    # The limiter's counters live for the whole process; wipe them before each test
    # so one test's burst can't spill into the next and trip a 429 out of nowhere.
    limiter.reset()
    yield
