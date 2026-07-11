"""The map of models the kernel talks to, and the local token counting the budget guard needs.

Every generative call is held to a budget (see llm._fit), and a budget needs two things this module owns:
a per-model figure for how much context that model reads *well*,
and a way to measure a prompt against it without leaving the box.

The figure is deliberately the model's *optimal* context, not its advertised maximum.
Long-context benchmarking (NVIDIA's RULER and its successors) finds a model reliably uses only half to two-thirds of the window it advertises:
past that its recall frays — facts in the middle get lost, details get invented — with no error to show for it.
So the number held here is the size past which quality quietly degrades,
not the size the model will accept before it hard-truncates.

The measuring is done locally with tiktoken, the industry-standard token counter.
It has no encoding for qwen, so o200k_base stands in — close enough for a conservative bound, not exact —
and every use keeps a margin (config.CONTEXT_SAFETY_MARGIN) for that approximation and for the reply's own output tokens.
If tiktoken can't load its encoding at all (an offline box with nothing cached),
counting falls back to a character estimate pitched high on purpose,
so the fallback never under-counts and waves an oversized prompt through.
"""

from dataclasses import dataclass

import tiktoken

# tiktoken carries no qwen encoding;
# o200k_base is a modern BPE whose token counts sit close enough to serve as a conservative estimate,
# which is all the budget guard needs — a bound, not an exact count.
_ENCODING_NAME = "o200k_base"

# Roughly how many characters one token is worth in the fallback estimate.
# Real English BPE runs nearer 4 chars/token, so dividing by 3 rounds the token count *up* —
# the fallback errs toward over-counting, never under, so it can't wave an oversized prompt past the budget.
_FALLBACK_CHARS_PER_TOKEN = 3

# The loaded tiktoken encoding, or None once we've tried and failed to load it (offline, nothing cached).
# "unset" is the third state: not yet attempted. Memoised so the load — which may hit the network once —
# happens at most once per process, and never at import time.
_encoding: object = "unset"


@dataclass(frozen=True)
class Model:
    """One model the kernel uses: who serves it, what it's called, and the window it reads *well*.

    optimal_context_tokens is the effective window, not the advertised maximum:
    it is the size past which recall frays, deliberately below the size the model will accept —
    so the budget guard holds a prompt to what the model answers well, not merely to what it swallows."""

    provider: str
    name: str
    optimal_context_tokens: int


# The models the kernel talks to, keyed by the exact name passed to the provider.
# The provider field is what llm._call dispatches on — "scaleway", "mistral", or "ollama" —
# and what makes pointing a model config at a local name the one-line rollback to on-box generation.
# The generative windows are each the model's *optimal*, deliberately below the advertised maximum:
# glm-5.2 and mistral-large-latest both advertise ~256K but recall frays past roughly half that,
# so 131072 (128K) is held for each — the same figure qwen3.5:4b (advertised ~262K) carries.
# The keys are the exact ids each provider answers to:
# "glm-5.2" is Scaleway's Generative APIs id (not the "zai-org/GLM-5.2" the model card uses),
# and "mistral-large-latest" is Mistral's own web-API id.
# nomic-embed-text is the embedder, not a generative model —
# it caps its own input via num_ctx (embedding.py),
# so it is listed here for the map's completeness but is not the budget guard's concern.
MODELS = {
    "glm-5.2": Model("scaleway", "glm-5.2", 131072),
    "mistral-large-latest": Model("mistral", "mistral-large-latest", 131072),
    "nomic-embed-text": Model("ollama", "nomic-embed-text", 8192),
    "qwen3.5:4b": Model("ollama", "qwen3.5:4b", 131072),
}


def _get_encoding():
    # Load the tiktoken encoding once, memoised, and remember a failure as None so we don't retry every call.
    # The load can reach the network the first time (to fetch the BPE file), so it is lazy, never at import.
    global _encoding
    if _encoding == "unset":
        try:
            _encoding = tiktoken.get_encoding(_ENCODING_NAME)
        except Exception:
            _encoding = None
    return _encoding


def count_tokens(text: str) -> int:
    """Estimate the token count of `text`, measured locally.

    Uses tiktoken when its encoding is available, and a deliberately-high character estimate when it isn't,
    so a box that can't load the encoding still gets a bound that errs toward over-counting rather than under.
    The count is an estimate either way — tiktoken's o200k_base is not qwen's tokeniser —
    which is why every caller holds it against a budget kept a margin below the true optimal,
    never against the optimal itself.
    """
    encoding = _get_encoding()
    if encoding is None:
        return -(-len(text) // _FALLBACK_CHARS_PER_TOKEN)  # ceil division: round the token count up
    return len(encoding.encode(text))


def truncate_tokens(text: str, max_tokens: int) -> str:
    """Cut `text` down to at most `max_tokens` tokens, returning it whole when it's already within.

    The hard guarantee behind the budget guard: a summariser can overshoot the length it was asked for,
    so after summarising, the context is truncated here to make the budget a promise rather than a request.
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
