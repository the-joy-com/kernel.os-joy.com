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

# The machine symbiot's persona, kept as two files split by who may read them (persona.py).
# The public half is versioned in the repo — the character and the stance, in the open —
# and carries a {{ INJECT_SYMBIOSIS_CORE_PRIVATE }} token where the private half is spliced in.
# The private half is gitignored, never committed: it holds what the symbiot won't share
# with the World. An absent private file is fine — the token collapses to empty and the
# public persona stands alone. Paths are anchored to the repo root so they resolve the same
# whatever the working directory, and can still be pointed elsewhere by the environment.
# This module lives in core/, one level down from the repo root, so we climb one directory.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERSONA_PUBLIC_FILE = os.getenv(
    "PERSONA_PUBLIC_FILE", os.path.join(_REPO_ROOT, "persona", "public.md")
)
PERSONA_PRIVATE_FILE = os.getenv(
    "PERSONA_PRIVATE_FILE", os.path.join(_REPO_ROOT, "persona", "private.md")
)

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

# The intake worker (worker.py): the background loop that answers received messages.
# On by default; the test suite turns it off so it can't race the suite for received
# rows while the state machine is driven by hand.
WORKER_ENABLED = os.getenv("WORKER_ENABLED", "true").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

# How many workers run at once.
# More than one so a single slow or wedged message can't block every message behind it —
# the others keep draining the queue.
# When a worker claims a message it locks that row, 
# and any other worker reaching for it steps over the locked row to the next free one — 
# so two workers never take the same message.
# Kept well under the connection pool's size so workers can't starve the API of connections.
WORKER_CONCURRENCY = int(os.getenv("WORKER_CONCURRENCY", "4"))

# The hard ceiling on how long a message's work may run before it is failed.
# This is what kills dead loops and hangs: no message runs forever, every one reaches an outcome.
# It bounds both layers of the timeout —
# the worker kills its own work process at this deadline (execution.run_with_deadline),
# and the deadline sweep fails any row still 'working' past it as the backstop (worker.run_deadline_sweep).
# Five minutes is generous for the placeholder work today and a sane default for the real work to come;
# tune it per the slowest honest job.
INTAKE_DEADLINE_SECONDS = float(os.getenv("INTAKE_DEADLINE_SECONDS", "300"))

# How many times a message may be attempted before the kernel gives up and parks it in 'abandoned'.
# A failing message is retried — a transient hiccup shouldn't be a death sentence —
# but only a bounded number of times, so the retrying itself can't become a new way to loop forever.
# Counts total attempts, not retries: 3 means one try and two more.
MAX_INTAKE_ATTEMPTS = int(os.getenv("MAX_INTAKE_ATTEMPTS", "3"))

# Web Push (VAPID): the signing key for the kernel's reply channel —
# the notification it sends the shell when a message reaches a terminal outcome (answered or abandoned).
# The private key is the raw 32-octet scalar in base64url;
# the public application server key the browser subscribes with is derived from it at runtime (push.py),
# so only the private half is configured here.
# Unset means push is simply off: answers still store and /answers still serves them,
# only the out-of-band nudge is skipped —
# so a missing key degrades the reply channel to poll-on-open rather than breaking anything.
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
# A contact URI (mailto: or https:) the push service can reach the app owner at, sent in
# every push's VAPID claims. Ignored when there's no key to sign with.
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "").strip()

# Ollama and the ontology router (embedding.py, ontology.py).
# The embedding model runs locally on the box — no external inference API, the same sovereignty
# stance as the rest of the kernel — so these point at the host's Ollama, not a remote service.
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
# The embedding model the router routes with.
# Its 768-dimensional output is what the ontology_embedding_nomic_embed_text tables are typed to,
# and it is the model seeded active in embedding_model (migration 0010) —
# the two must agree, or an embedded fact is searched against vectors from a different model.
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
# The context window opened on every embed call.
# Ollama clips nomic-embed-text to 2048 tokens by default and truncates in silence,
# so a long text embedded at the default would lose its tail with no error;
# 8192 is the model's native window, so the whole text reaches its own vector.
EMBEDDING_NUM_CTX = int(os.getenv("EMBEDDING_NUM_CTX", "8192"))
# How long to wait on Ollama before giving up.
# Generous, because the first call after a cold model load pays the load time once.
OLLAMA_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "60"))

# The recall (nominate) pass of the ontology router.
# How wide a candidate pool the vector search hands the re-ranker:
# tuned for recall, not precision, so the right type is in the room even if it isn't yet at the front.
RECALL_POOL = int(os.getenv("RECALL_POOL", "40"))
# The HNSW working-set width, set per query.
# The index answers approximately from a set of candidates it walks the graph to fill;
# a set no wider than the pool we ask back would cap recall from the first fact,
# so it is opened comfortably above RECALL_POOL — cheap on a store the size of one life's concepts.
# Invariant: RECALL_EF_SEARCH >= RECALL_POOL, always.
RECALL_EF_SEARCH = int(os.getenv("RECALL_EF_SEARCH", "100"))

# The generative model behind the router's judgments — the re-rank now (Phase 1b),
# and later the grey-zone tie-break, the minting, and the JSON-LD synthesis.
# Reached through Ollama's /api/generate with thinking off and output constrained to JSON (llm.py).
RERANK_MODEL = os.getenv("RERANK_MODEL", "qwen3.5:4b")
# How long to wait on a generative call.
# Longer than an embedding — generation is token by token, and the first call after a cold load
# pays the load once — but still well inside the intake deadline that bounds a fact's whole run.
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))

# The two thresholds that band the top re-rank score into reuse / grey / mint.
# At or above REUSE_THRESHOLD the top candidate fits well enough to reuse outright;
# at or below MINT_THRESHOLD nothing fits and a new type is coined;
# the grey band between the two escalates to the one-shot LLM gate (Phase 1c).
# Tune against a hand-labelled set: too high mints needless duplicates, too low forces bad reuse.
REUSE_THRESHOLD = float(os.getenv("REUSE_THRESHOLD", "0.7"))
MINT_THRESHOLD = float(os.getenv("MINT_THRESHOLD", "0.3"))
# The bands only make sense with the mint floor strictly below the reuse floor and both inside 0.0–1.0;
# cross them and the grey zone vanishes and decide() tests MINT before REUSE, so a fact that clearly
# fits an existing type gets read as a mint and a needless duplicate is coined — silently, forever.
# Caught at import (before the pool opens or a worker starts), so a fat-fingered .env refuses to boot
# rather than quietly mis-filing every fact that follows.
if not 0.0 <= MINT_THRESHOLD < REUSE_THRESHOLD <= 1.0:
    raise ValueError(
        "ontology thresholds out of order: need 0.0 <= MINT_THRESHOLD < REUSE_THRESHOLD <= 1.0, "
        f"got MINT_THRESHOLD={MINT_THRESHOLD}, REUSE_THRESHOLD={REUSE_THRESHOLD}"
    )
