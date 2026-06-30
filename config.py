"""Configuration: every environment-sourced value the kernel reads, in one place.

`.env` is loaded once here so the rest of the code reads plain module-level constants and never touches `os.environ` directly.
The same `.env` file is read identically on a dev box and on the server —
only the values differ (see .env.example for the local-vs-server database URL, in particular).
"""

import os

from dotenv import load_dotenv

# Load `.env` from the working directory (the repo root,
# both locally and under the systemd unit).
# Real environment variables already set take precedence,
# so the server can also inject config the systemd way if it ever wants to.
load_dotenv()

# Where the data lives.
# Defaults to the local docker-compose Postgres so a fresh clone develops with zero .env;
# the server overrides this with its peer-auth socket URL.
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://joy:joy@localhost:5432/joy")


def _derive_test_url(url: str) -> str:
    """The test database is the configured one with a `_test` suffix,
    unless TEST_DATABASE_URL says otherwise —
    so the suite can truncate freely without ever touching development data."""
    base, _, _query = url.partition("?")
    return base.rstrip("/") + "_test"


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL") or _derive_test_url(DATABASE_URL)

# The human symbiot seeded at startup.
# Today this is the single seeded address,
# but any registered symbiot may log in — the schema and /login already support more than one.
# Empty means the seed is skipped (and /login can never succeed) —
# a misconfiguration the startup logs will call out rather than fail silently.
SYMBIOT_EMAIL = os.getenv("SYMBIOT_EMAIL", "").strip().lower()

# Server secret that HMACs login codes and session tokens before they're stored,
# so the database never holds a usable code or token in the clear.
KERNEL_SECRET = os.getenv("KERNEL_SECRET", "dev-insecure-secret")

# Gmail API: path to the service-account key and the mailbox it sends as.
# When unset, the real email client refuses to send rather than pretend to.
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "").strip()
GMAIL_SENDER = os.getenv("GMAIL_SENDER", "").strip()

# Lifetimes for the two short-lived secrets.
# Codes are deliberately brief;
# a session lasts a day — long enough that the shell needn't re-ask for a login on every reload,
# short enough that a forgotten open tab doesn't stay authed indefinitely.
LOGIN_CODE_TTL_SECONDS = 10 * 60
SESSION_TTL_SECONDS = 24 * 60 * 60

# Abuse limits enforced in the strict layer (the database), not by request timing.
# The smallest gap between two issued codes for one symbiot:
# a second /login inside this window keeps the code already in the inbox and emails nothing,
# so a flood of taps can't become a flood of mail (the test suite sets this to 0).
LOGIN_REISSUE_INTERVAL_SECONDS = int(os.getenv("LOGIN_REISSUE_INTERVAL_SECONDS", "60"))
# How many wrong guesses a single live code absorbs before the database burns it.
# Bounds brute force to a fixed budget per code, immune to which IP does the guessing.
MAX_VERIFY_ATTEMPTS = int(os.getenv("MAX_VERIFY_ATTEMPTS", "5"))

# The edge rate limiter (rate_limit.py). On by default;
# the suite leaves it on and resets its counters between tests.
# Set RATE_LIMIT_ENABLED=0 to turn it off entirely.
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
