"""Test fixtures for the identity suite.

The kernel is pointed at a dedicated *test* database and a known symbiot before the app is imported,
so config reads these at import time.
Each test gets a clean database and a FakeEmailClient swapped in for the real Gmail one —
so the whole login flow is exercised end to end without a single network call.
"""

import os
import re

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

from fastapi.testclient import TestClient
import pytest

import db
from email_client import FakeEmailClient
from main import app, get_email_client
from rate_limit import limiter

SYMBIOT_EMAIL = "symbiot@example.com"
_CODE_RE = re.compile(r"\b(\d{6})\b")


@pytest.fixture(autouse=True)
def clean_db(client):
    # A clean slate before every test:
    # wipe all three tables, re-seed the one symbiot.
    pool = db.get_pool()
    with pool.connection() as conn:
        conn.execute("TRUNCATE symbiot, login_code, session RESTART IDENTITY CASCADE")
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
