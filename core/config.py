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
# The private half is gitignored, never committed: it holds what the symbiot won't share with the World.
# An absent private file is fine — the token collapses to empty and the public persona stands alone.
# Paths are anchored to the repo root so they resolve the same whatever the working directory,
# and can still be pointed elsewhere by the environment.
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
# On by default; the test suite turns it off so it can't race the suite for received rows
# while the state machine is driven by hand.
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
# It must clear the generative fallback ladder's worst case:
# a single reply can try Scaleway, then Mistral, then local Ollama in sequence,
# each bounded by LLM_TIMEOUT_SECONDS,
# so a full three-tier fall-through of hard timeouts alone is 3 x LLM_TIMEOUT_SECONDS before any tokens are generated.
# Ten minutes keeps that chain (plus the composition it precedes) comfortably inside the deadline,
# while still killing an honest hang; tune it per the slowest honest job.
INTAKE_DEADLINE_SECONDS = float(os.getenv("INTAKE_DEADLINE_SECONDS", "600"))

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
# A contact URI (mailto: or https:) the push service can reach the app owner at,
# sent in every push's VAPID claims. Ignored when there's no key to sign with.
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "").strip()

# Ollama on the box (embedding.py, and the generative ladder's last-resort tier in llm.py).
# The embedding model runs locally — no external inference API for it —
# so these point at the host's Ollama, not a remote service.
# Generation now leans on the cloud (see the provider block below),
# and only falls back here when both cloud providers are down,
# but it reaches Ollama through this same base.
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
# How wide a candidate pool the vector search hands the re-ranker.
# Two forces size it.
# Wide enough that the right type is in the room even when it isn't yet at the front —
# recall, not precision, is this pass's job.
# But no wider than the re-ranker can weigh well in one call:
# it scores the whole pool in a single pass,
# and a small generative model grows sloppy asked to judge too many candidates at once —
# leaving some unscored, reading others loosely,
# the very slip the coverage default in rerank_candidates already absorbs.
# So the pool is capped where that judgement stays sharp,
# not opened as wide as the index could answer.
RECALL_POOL = int(os.getenv("RECALL_POOL", "20"))
# The HNSW working-set width, set per query.
# The index answers approximately from a set of candidates it walks the graph to fill;
# a set no wider than the pool we ask back would cap recall from the first fact,
# so it is opened comfortably above RECALL_POOL — cheap on a store the size of one life's concepts.
# Invariant: RECALL_EF_SEARCH >= RECALL_POOL, always.
RECALL_EF_SEARCH = int(os.getenv("RECALL_EF_SEARCH", "100"))

# The cloud generative providers and the fallback ladder behind every generative call (llm.py).
# Generation runs on a bigger, faster model than the box can serve:
# Scaleway (GPU-backed) is primary, reached through the OpenAI-compatible client Scaleway advertises;
# a call that hits an outage-class failure there falls to Mistral's own web API,
# and then — only if both clouds are down — to the local Ollama model,
# the last resort that keeps the loop answering.
# The keys and base URL come from .env;
# an empty key simply means that tier can't answer and the ladder falls through it.
SCALEWAY_API_BASE_URL = os.getenv("SCALEWAY_API_BASE_URL", "https://api.scaleway.ai/v1")
SCALEWAY_API_KEY = os.getenv("SCALEWAY_API_KEY", "").strip()
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "").strip()
# The two fallback models, tried in order after the primary (a Scaleway model) fails outage-class.
GENERATIVE_FALLBACK_MODEL = os.getenv("GENERATIVE_FALLBACK_MODEL", "mistral-large-latest")
GENERATIVE_LOCAL_FALLBACK_MODEL = os.getenv("GENERATIVE_LOCAL_FALLBACK_MODEL", "qwen3.5:4b")

# The generative model behind the router's judgments — re-ranking the recalled candidates,
# and breaking the tie in the grey zone when their top score is ambiguous.
# Its provider (Scaleway, Mistral, or local Ollama) is looked up from the model map (services.models),
# so pointing this at a local model name is the one-line rollback to on-box generation.
# Reached with thinking off and output constrained to the caller's JSON schema (llm.py).
RERANK_MODEL = os.getenv("RERANK_MODEL", "glm-5.2")
# How long to wait on a single generative attempt, applied per tier of the fallback ladder.
# Longer than an embedding — generation is token by token,
# and the first call after a cold load pays the load once.
# The intake deadline is sized to clear three of these in sequence (see it above).
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))

# The two thresholds that band the top re-rank score into reuse / grey / mint.
# At or above REUSE_THRESHOLD the top candidate fits well enough to reuse outright;
# at or below MINT_THRESHOLD nothing fits and a new type is coined;
# the grey band between the two escalates to the one-shot LLM gate.
# Tune against a hand-labelled set: too high mints needless duplicates, too low forces bad reuse.
REUSE_THRESHOLD = float(os.getenv("REUSE_THRESHOLD", "0.7"))
MINT_THRESHOLD = float(os.getenv("MINT_THRESHOLD", "0.3"))
# The bands only make sense with the mint floor strictly below the reuse floor and both inside 0.0–1.0;
# cross them and the grey zone vanishes and decide() tests MINT before REUSE,
# so a fact that clearly fits an existing type gets read as a mint and a needless duplicate is coined — silently, forever.
# Caught at import (before the pool opens or a worker starts),
# so a fat-fingered .env refuses to boot rather than quietly mis-filing every fact that follows.
if not 0.0 <= MINT_THRESHOLD < REUSE_THRESHOLD <= 1.0:
    raise ValueError(
        "ontology thresholds out of order: need 0.0 <= MINT_THRESHOLD < REUSE_THRESHOLD <= 1.0, "
        f"got MINT_THRESHOLD={MINT_THRESHOLD}, REUSE_THRESHOLD={REUSE_THRESHOLD}"
    )

# The offline duplicate garbage-collection pass (services/ontology_gc.py) that merges the semantic duplicates forward-only minting breeds —
# workout_action coined Tuesday, training_session Friday.
# GC_DISTANCE is the cosine-distance pre-filter:
# only type pairs nearer than this are even offered to the model as possible twins.
# It is a loose net, not the verdict — the model, reading both full definitions, makes the real same-or-not call —
# so it is set wide enough to catch true synonyms that embed a little apart,
# and tight enough not to ask the model about every unrelated pair.
# Tune against the store as it fills; the by-hand smoke prints the pairs it caught so it can be eyeballed.
GC_DISTANCE = float(os.getenv("GC_DISTANCE", "0.2"))
# How often the sweep wakes.
# Duplicates accrue slowly and the merge never sits on the read path,
# so this is a day, not the seconds the intake reconcile sweep runs on. 24 hours by default.
GC_SWEEP_INTERVAL_SECONDS = float(os.getenv("GC_SWEEP_INTERVAL_SECONDS", "86400"))
# On by default;
# the test suite turns it off so the sweep can't race the suite —
# the GC tests drive run_once by hand, the same stance WORKER_ENABLED takes for the intake workers.
GC_ENABLED = os.getenv("GC_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")

# The read path (retrieval.py, reply.py): assembling answer-time context and composing the reply.
# How many diary facts the fast lexical reach hands back — the fixed retrieval budget.
# Kept modest: these facts are folded into the reply prompt,
# so the point is the few that bear most on the question, not a wide net —
# the wide, meaning-based reach is the deep second pass, not this one.
RETRIEVAL_LIMIT = int(os.getenv("RETRIEVAL_LIMIT", "10"))
# The generative model that composes the reply.
# Defaults to the router's model (the primary cloud model),
# but named apart because composing prose is a different job from the router's classification calls,
# and it may want a different model without a code change.
# Reached through the same fallback ladder as the router (llm.generate), thinking off;
# the reply keeps the model's own default warmth rather than the router's temperature 0.
REPLY_MODEL = os.getenv("REPLY_MODEL", RERANK_MODEL)
# The headroom the context-budget guard (llm._fit) keeps below a model's optimal window.
# It covers two slacks at once: tiktoken only approximates qwen's tokeniser, so the count may run a little low,
# and a generative call spends some of its window on the reply it produces, not just the prompt it reads.
# A fraction of the optimal, held back from the input budget — 0.1 leaves a tenth of the window as margin.
CONTEXT_SAFETY_MARGIN = float(os.getenv("CONTEXT_SAFETY_MARGIN", "0.1"))

# Short-term conversational memory (services/conversation.py, worker.run_compression_sweep):
# the recent back-and-forth a reply sits inside, held as a gradient — near turns verbatim, far turns folded into one running summary.
# Two budgets size its share of the reply model's optimal window,
# each a fraction rather than an absolute so it "travels with" the model the way the rest of the prompt's budget does (llm._fit):
# if a provider drops and the fallback ladder switches models, the reserved share is recomputed against whichever model answers.
# In practice all three generative tiers share one optimal window (131072),
# so the figures are stable across a fallback.
# Neither budget caps a read — the reader carries the whole verbatim tail back to the Gist's cutoff, uncapped, so the two buckets never gap.
# The verbatim fraction (Bucket 1) is the *trigger* the background fold fires at, and the size it trims the tail back to:
# it carries whole turns, word-for-word, so it is the larger.
# The gist fraction (Bucket 2) is the *cap* on the single summary paragraph, re-compressed to this size every fold so it cannot creep upward;
# smaller than the verbatim slice, but not tiny — it must hold the concrete facts and open threads of an ever-longer history.
# The two, plus the persona, the diary facts, the current message, the instruction, and the output headroom, all sum to well under the window.
# On the rare overrun, the post-hoc backstop (llm._fit) condenses the whole remembered block (diary + conversation), never the persona, the instructions, or the live message.
CONVERSATION_VERBATIM_BUDGET_FRACTION = float(
    os.getenv("CONVERSATION_VERBATIM_BUDGET_FRACTION", "0.25")
)
CONVERSATION_GIST_BUDGET_FRACTION = float(
    os.getenv("CONVERSATION_GIST_BUDGET_FRACTION", "0.10")
)
# The model that folds the overflowed turns into the Gist.
# The same heavy hitter that composes the replies (REPLY_MODEL), not a cheap local tier —
# the Gist is load-bearing for the conversation:
# once a turn ages out of the verbatim tail, the Gist is all a later reply has to reach back through,
# so a weak fold that bloats the summary or garbles a fact quietly degrades every reply that leans on it.
# The local tier proved exactly that unreliable in practice (see the by-hand fold smoke),
# and the symbiot lives inside the result, so quality wins over saving the metered call —
# the fold is still background work no one waits on, and it fires only when a tail overflows, so it is far from every turn.
# It is looked up in the model map (services.models) the same way every generative model is,
# so pointing it back at the local name is a one-line change if the cost ever outweighs the quality.
CONVERSATION_COMPRESS_MODEL = os.getenv("CONVERSATION_COMPRESS_MODEL", REPLY_MODEL)
# On by default; the test suite turns it off so the live sweep can't race the suite —
# the compression tests drive _compress_one by hand, the same stance WORKER_ENABLED,
# GC_ENABLED, and INGEST_ENABLED take for their own background loops.
COMPRESS_ENABLED = os.getenv("COMPRESS_ENABLED", "true").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
# How often the compression sweep looks for a symbiot whose overflow it can fold when idle.
# It drains back-to-back while folds remain; this is only the idle poll, kept relaxed —
# a turn that overflows the verbatim tail sits in the "pending" band until the next fold,
# and the band staying small is a matter of the sweep chasing it closely, not instantly.
COMPRESS_SWEEP_INTERVAL_SECONDS = float(os.getenv("COMPRESS_SWEEP_INTERVAL_SECONDS", "10"))

# Live diary ingestion (worker.run_ingestion_sweep):
# the background sweep that files each settled message into the diary through the write path,
# so the store the read path leans on fills itself as messages arrive.
# On by default; the test suite turns it off so the live sweep can't race the suite —
# the ingestion tests drive _ingest_one by hand, the same stance WORKER_ENABLED and GC_ENABLED take.
INGEST_ENABLED = os.getenv("INGEST_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
# How often the ingestion sweep looks for a message to file when idle.
# It drains back-to-back while a backlog remains;
# this is only the idle poll, kept short so a just-answered message joins the diary promptly —
# fresh for the next reply's retrieval — without an idle kernel spinning.
INGEST_SWEEP_INTERVAL_SECONDS = float(os.getenv("INGEST_SWEEP_INTERVAL_SECONDS", "10"))
