"""The models the kernel talks to, which one plays each job,
and the local token counting the budget guard needs.

Two things live here, plus a third that always did.

The first is the *catalog* —
the set of models the kernel can talk to,
each with the characteristics it must be driven by:
who serves it, what it's called, the window it reads *well*,
and the reply length it's held to.
The second is the *assignment* —
which model, out of that catalog, plays each generative role
(the reply, the router's rerank, the enrichment gate, and so on).
Both used to be compile-time constants —
the catalog a hardcoded dict, the assignment a scatter of config constants —
and both are now durable, operator-editable state
(migration 0019, services/memory/model_config.py).
So what this module holds is no longer the catalog itself but a *resolver* over it:
BUILTIN_MODELS and BUILTIN_ROLES are the seed the durable store is reconciled from
and the fallback the resolver falls back to,
and role_name / spec / for_role read the store (through a small process-wide cache)
to answer "which model, and what are its characteristics?"
wherever a generative call is about to be made.

The cache is loaded once per process and refreshed when the /models command writes
(reload / set_config).
The subtlety it exists to solve:
a reply is composed inside a spawned child process (execution.run_with_deadline),
a fresh interpreter that inherits none of the parent's memory —
so the child cannot read a cache the parent loaded.
It loads its own, lazily, on the first resolution,
with a direct short-lived connection (config.DATABASE_URL),
one cheap read of two tiny tables per unit of work.
The parent loads eagerly at startup (main.py) and never takes the lazy path.
Either way, a name the store doesn't carry falls back to the builtin seed,
so a boot before the seed runs — or a degraded read —
still resolves a real model rather than nothing.

The third thing, unchanged: the local token counting the budget guard leans on (llm._fit).
The figure the guard holds a prompt to is the model's *optimal* context, not its advertised maximum.
Long-context benchmarking (NVIDIA's RULER and its successors)
finds a model reliably uses only half to two-thirds of the window it advertises:
past that its recall frays — facts in the middle get lost, details get invented —
with no error to show for it.
So the number held per model is the size past which quality quietly degrades,
not the size the model will accept before it hard-truncates.
The measuring is done locally with tiktoken, the industry-standard token counter.
It has no encoding for qwen, so o200k_base stands in —
close enough for a conservative bound, not exact —
and every use keeps a margin (config.CONTEXT_SAFETY_MARGIN)
for that approximation and for the reply's own output tokens.
If tiktoken can't load its encoding at all (an offline box with nothing cached),
counting falls back to a character estimate pitched high on purpose,
so the fallback never under-counts and waves an oversized prompt through.
"""

import threading
from dataclasses import dataclass

import psycopg
import tiktoken

from core import config

# tiktoken carries no qwen encoding;
# o200k_base is a modern BPE whose token counts sit close enough to serve as a conservative estimate,
# which is all the budget guard needs — a bound, not an exact count.
_ENCODING_NAME = "o200k_base"

# Roughly how many characters one token is worth in the fallback estimate.
# Real English BPE runs nearer 4 chars/token, so dividing by 3 rounds the token count *up* —
# the fallback errs toward over-counting, never under, so it can't wave an oversized prompt past the budget.
_FALLBACK_CHARS_PER_TOKEN = 3

# The loaded tiktoken encoding, or None once we've tried and failed to load it (offline, nothing cached).
# "unset" is the third state: not yet attempted.
# Memoised so the load — which may hit the network once —
# happens at most once per process, and never at import time.
_encoding: object = "unset"


@dataclass(frozen=True)
class Model:
    """One model the kernel uses:
    who serves it, what it's called, the window it reads *well*,
    and the reply length it's held to.

    optimal_context_tokens is the effective window, not the advertised maximum:
    it is the size past which recall frays,
    deliberately below the size the model will accept —
    so the budget guard holds a prompt to what the model answers well,
    not merely to what it swallows.

    max_output_tokens is the ceiling on a single reply's length,
    not a quality figure like the one above:
    unlike the input window it has no degradation curve to sit below
    (a reply is not worse for being allowed to run longer),
    so it is not sized below an optimum
    but set to the highest a provider actually permits —
    a guard that stops a runaway generation
    before it burns the latency and free-generation budget,
    while never truncating a real reply the provider would have let finish.
    The figure is the *verified* ceiling, not the model card's:
    Scaleway hard-caps glm-5.2's output well below its architectural maximum,
    and that verified cap is the number held here
    (see the note above BUILTIN_MODELS).
    The summariser asks for more room when it legitimately needs it (llm._summarise),
    but that request is clamped to this ceiling
    so it can never exceed what the tier accepts."""

    provider: str
    name: str
    optimal_context_tokens: int
    max_output_tokens: int


# The builtin catalog: the models the kernel ships knowing how to talk to.
# This is the seed the durable `model` table is reconciled from
# (model_config.reconcile_and_seed writes these rows, marked is_builtin),
# and the fallback the resolver falls back to when the store carries no row for a name —
# so a spawned child that hasn't loaded the store,
# or a boot before the seed runs, still resolves a builtin rather than nothing.
#
# The generative roles map onto three tiers (see the tier constants in core/config.py),
# and the models here are what those tiers default to:
#   flagship — glm-5.2 on Scaleway, the capable model kept for the reply and the load-bearing memory;
#   the two cheaper rungs — gpt-oss-120b on Scaleway, an order of magnitude cheaper per token;
#   and each rung's cross-cloud fallback on Mistral — mistral-large-latest, mistral-small-latest, ministral-8b-latest —
#   with qwen3.5:4b the single local floor beneath them all, the rollback target reached when both clouds are down.
#
# The context windows are each the model's *optimal*,
# deliberately below the advertised maximum,
# because a model reads well across only about half the window it will accept —
# NVIDIA's RULER and its successors find recall frays past that, with no error to show for it.
# The models advertising ~256K — glm-5.2, mistral-large-latest, mistral-small-latest — are held at 131072 (128K);
# gpt-oss-120b and ministral-8b-latest, whose native window *is* 128K, are held at half again — 65536 —
# past which a 128K model's own recall frays.
# Each Mistral fallback sits at or above the window of the Scaleway primary it catches,
# so a prompt fitted for the primary can never overflow the model that inherits it when the ladder falls.
# The output ceiling works the opposite way to the window:
# it isn't an optimal below a degradation point
# (a reply doesn't get worse because we let it run longer),
# it's a guard that stops a runaway generation
# before it burns the latency and free-generation budget.
# So it isn't sized below a quality curve —
# it's sized to the highest a provider actually permits,
# verified against each live endpoint rather than the architectural spec sheet,
# which is far larger and misleading:
#   glm-5.2's model card says ~131K output,
#     but Scaleway hard-caps max_completion_tokens at 16384 for it
#     (its 5-minute-response rule),
#     and a request over that is a 400 we would never fall through —
#     so 16384 is the true ceiling, and the other Scaleway generative models are held to the same verified cap;
#   the Mistral models enforce no request-time output cap at all
#     (they accept an absurd max_tokens and just stop when done),
#     so the guard there is entirely ours to set;
#   qwen3.5:4b caps output only through Ollama's num_predict, unbounded by default.
# 16384 is held across the whole generative catalog,
# so a fallback never truncates a reply shorter than the primary would have given —
# one ceiling at the highest the most-constrained tier allows.
# The keys are the exact ids each provider answers to:
# "glm-5.2" is Scaleway's Generative APIs id (not the "zai-org/GLM-5.2" the model card uses),
# and the Mistral names are Mistral's own web-API ids.
# nomic-embed-text is the embedder, not a generative model —
# it caps its own input via num_ctx (embedding.py) and never generates a reply,
# so its windows are listed for the catalog's completeness (0 output — never consulted)
# but are not the budget guard's concern.
BUILTIN_MODELS = {
    "glm-5.2": Model("scaleway", "glm-5.2", 131072, 16384),
    "gpt-oss-120b": Model("scaleway", "gpt-oss-120b", 65536, 16384),
    "ministral-8b-latest": Model("mistral", "ministral-8b-latest", 65536, 16384),
    "mistral-large-latest": Model("mistral", "mistral-large-latest", 131072, 16384),
    "mistral-small-latest": Model("mistral", "mistral-small-latest", 131072, 16384),
    "nomic-embed-text": Model("ollama", "nomic-embed-text", 8192, 0),
    "qwen3.5:4b": Model("ollama", "qwen3.5:4b", 131072, 16384),
}

# The default model for each generative role,
# used to seed the durable `model_role` table on first boot
# and as the fallback when the store names no model for a role.
# Read from config rather than hardcoded,
# so an existing .env override (REPLY_MODEL=…, RERANK_MODEL=…) is honoured on the first seed
# and the box behaves exactly as it did before these tables existed.
# These are the roles the /models command lets the operator reassign;
# the embedding model is deliberately not among them —
# nomic-embed-text is a hard requirement
# (its vector width is what the pgvector tables are typed to),
# so it stays config, not a role to swap.
BUILTIN_ROLES = {
    "reply": config.REPLY_MODEL,
    "rerank": config.RERANK_MODEL,
    "mint": config.MINT_MODEL,
    "enrich": config.ENRICH_MODEL,
    "tool_decision": config.TOOL_DECISION_MODEL,
    "tool_confirm": config.TOOL_CONFIRM_MODEL,
    "conversation_compress": config.CONVERSATION_COMPRESS_MODEL,
}

# The process-wide cache of the durable store, and the lock that guards loading it.
# None means "not loaded yet";
# a dict (possibly empty) means loaded,
# and the resolver falls back to the builtins above
# for anything the loaded dict doesn't carry.
_cache_lock = threading.Lock()
_catalog: dict[str, Model] | None = None
_roles: dict[str, str] | None = None


def count_tokens(text: str) -> int:
    """Estimate the token count of `text`, measured locally.

    Uses tiktoken when its encoding is available,
    and a deliberately-high character estimate when it isn't,
    so a box that can't load the encoding still gets a bound
    that errs toward over-counting rather than under.
    The count is an estimate either way —
    tiktoken's o200k_base is not qwen's tokeniser —
    which is why every caller holds it against a budget kept a margin below the true optimal,
    never against the optimal itself.
    """
    encoding = _get_encoding()
    if encoding is None:
        return -(-len(text) // _FALLBACK_CHARS_PER_TOKEN)  # ceil division: round the token count up
    return len(encoding.encode(text))


def for_role(role: str) -> Model:
    """The full characteristics of the model assigned to a role — role_name resolved through spec.

    Always returns a Model:
    if the assigned name is somehow in neither the store nor the builtins
    (a role pointing at a deleted model,
    which the FK and the /models command are built to prevent),
    it synthesises a conservative local default rather than raising,
    so a resolution never leaves a generative call without a window to fit to.
    """
    name = role_name(role)
    return spec(name) or Model("ollama", name, 131072, 16384)


def reload() -> None:
    """Force the cache to reload from the store with a direct connection — the /models route's refresh.

    After the route writes a model or reassigns a role, the parent's cache is stale;
    this re-reads it so the next resolution (and any child spawned after) sees the change.
    A child never calls this — it lazy-loads fresh on its first resolution regardless.
    """
    global _catalog, _roles
    with _cache_lock:
        _catalog, _roles = None, None
    _ensure_loaded()


def reload_from_conn(conn) -> None:
    """Load the cache from the store through an existing connection — the eager parent load at startup.

    main.py calls this once in the lifespan,
    after the schema is in place and the builtins are reconciled,
    so the parent's cache is warm before the first worker runs
    and it never pays the lazy direct connection.
    """
    catalog, roles = _read_store(conn)
    set_config(catalog, roles)


def role_name(role: str) -> str:
    """The name of the model assigned to a generative role, from the store or its config default.

    The replacement for reading config.REPLY_MODEL / RERANK_MODEL / … directly:
    a caller about to make a generative call resolves the role to a model name here,
    so an operator's reassignment through /models takes effect without a code change.
    Falls back to the role's config default (BUILTIN_ROLES) when the store names none,
    and to the rerank default for an unknown role,
    so this always returns a usable name.
    """
    _ensure_loaded()
    return (_roles or {}).get(role) or BUILTIN_ROLES.get(role) or BUILTIN_ROLES["rerank"]


def set_config(catalog: dict[str, Model], roles: dict[str, str]) -> None:
    """Replace the cache with an already-read catalog and role map — the eager parent load's landing point.

    Called at startup (main.py) with what model_config read through the pool,
    so the parent never takes the lazy direct-connection path (_ensure_loaded).
    Also the shape the /models route refreshes through after a write.
    """
    global _catalog, _roles
    with _cache_lock:
        _catalog = dict(catalog)
        _roles = dict(roles)


def spec(name: str) -> Model | None:
    """The characteristics of the model with this exact name, from the store or the builtin seed, or None.

    The lookup llm reaches for to drive a call (provider, windows, output cap)
    and _fit reaches for to hold a prompt to the model's optimal window.
    Falls back to the builtin seed for a name the store doesn't carry,
    and returns None for a name neither knows —
    which llm reads as the historical "unmapped model" case (uncapped, unbudgeted),
    the same as an unknown local name always meant.
    """
    _ensure_loaded()
    return (_catalog or {}).get(name) or BUILTIN_MODELS.get(name)


def truncate_tokens(text: str, max_tokens: int) -> str:
    """Cut `text` down to at most `max_tokens` tokens, returning it whole when it's already within.

    The hard guarantee behind the budget guard:
    a summariser can overshoot the length it was asked for,
    so after summarising, the context is truncated here
    to make the budget a promise rather than a request.
    Truncates on the same measure count_tokens uses —
    real tokens when tiktoken is loaded, characters when it isn't —
    so the cut agrees with the count that decided it was needed.
    """
    if max_tokens <= 0:
        return ""
    encoding = _get_encoding()
    if encoding is None:
        return text[: max_tokens * _FALLBACK_CHARS_PER_TOKEN]
    tokens = encoding.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return encoding.decode(tokens[:max_tokens])


def _ensure_loaded() -> None:
    """Load the cache from the store if it hasn't been yet — the lazy path a spawned child takes.

    The parent loads eagerly at startup and so never reaches the read here;
    a child (a fresh spawned interpreter with none of the parent's memory)
    reaches it on its first resolution and reads the store once
    with a direct short-lived connection, since it has no pool.
    A failed read leaves the cache empty rather than raising,
    so a generative call still resolves against the builtin seed
    instead of dying for want of the store —
    the store is how an operator *overrides* the builtins,
    never the only place a model can be found.
    """
    global _catalog, _roles
    if _catalog is not None:
        return
    with _cache_lock:
        if _catalog is not None:
            return
        try:
            with psycopg.connect(config.DATABASE_URL) as conn:
                catalog, roles = _read_store(conn)
        except Exception:
            catalog, roles = {}, {}
        _catalog, _roles = catalog, roles


def _get_encoding():
    # Load the tiktoken encoding once, memoised,
    # and remember a failure as None so we don't retry every call.
    # The load can reach the network the first time (to fetch the BPE file),
    # so it is lazy, never at import.
    global _encoding
    if _encoding == "unset":
        try:
            _encoding = tiktoken.get_encoding(_ENCODING_NAME)
        except Exception:
            _encoding = None
    return _encoding


def _read_store(conn) -> tuple[dict[str, Model], dict[str, str]]:
    """Read the catalog and the role assignments from the durable store on the given connection.

    The one place the two tables are read into the cache's shape,
    shared by the eager parent load (through the pool)
    and the lazy child load (through a direct connection) —
    so both populate the cache identically.
    """
    catalog = _rows_to_catalog(
        conn.execute(
            "SELECT name, provider, optimal_context_tokens, max_output_tokens FROM model"
        ).fetchall()
    )
    roles = {role: model_name for (role, model_name) in conn.execute(
        "SELECT role, model_name FROM model_role"
    ).fetchall()}
    return catalog, roles


def _rows_to_catalog(rows) -> dict[str, Model]:
    """Turn the `model` table rows into a name → Model map."""
    return {
        name: Model(provider, name, optimal_context_tokens, max_output_tokens)
        for (name, provider, optimal_context_tokens, max_output_tokens) in rows
    }
