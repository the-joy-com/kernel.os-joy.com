"""Embedding: text into a vector, through local Ollama, done correctly.

The ontology router turns text into a vector twice —
an ontology definition on the way into the store, and an incoming fact on the way to a search —
and both go through here, the one place that talks to the embedding model.
This module exists mostly to carry the two quiet traps nomic-embed-text sets,
so nothing downstream has to remember them.

The first trap is the context window.
Ollama clips this model's window to a fraction of what it can actually read and truncates in silence,
so a long text embedded at the default hands back a vector computed from a cut-off text, with no error to show for it —
we open the window to its full width (config.EMBEDDING_NUM_CTX) on every call.

The second is the task prefix.
The model's distances only mean anything when each text wears a marker naming what it is for:
`search_document:` for text being stored, `search_query:` for a query being asked against the store.
A stored document and the query that should find it must be embedded under their matching prefixes,
or the distance between them measures the wrong thing.

Both live here; a caller picks `task` and never touches a prefix or a window itself.
No external inference API — Ollama serves the model on the box (see README, "Ollama (local models)"),
the same sovereignty stance as the rest of the kernel.
"""

import httpx

from core import config

# The nomic task prefixes, keyed by the caller's `task`.
# A stored definition is a "document"; a fact or concept being routed is a "query".
_PREFIX = {
    "document": "search_document: ",
    "query": "search_query: ",
}


def embed(text: str, *, task: str) -> list[float]:
    """One text to one embedding vector, via the local embedding model.

    task is "document" for text being stored or "query" for text being searched with —
    it picks the mandatory nomic prefix, the one thing the caller has to declare.
    The context window is opened to config.EMBEDDING_NUM_CTX so a long text is embedded whole,
    never silently truncated to the model's clipped default.
    Raises on anything but a clean response carrying a vector:
    a bad embedding must fail loud, never return a quietly wrong vector that would poison every distance measured against it.
    """
    if task not in _PREFIX:
        raise ValueError(f"unknown embed task {task!r}: expected 'document' or 'query'")
    resp = httpx.post(
        f"{config.OLLAMA_BASE_URL}/api/embed",
        json={
            "model": config.EMBEDDING_MODEL,
            "input": _PREFIX[task] + text,
            "options": {"num_ctx": config.EMBEDDING_NUM_CTX},
        },
        timeout=config.OLLAMA_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    # /api/embed answers with a list of vectors, one per input; we send one text, so we want the first.
    embeddings = resp.json().get("embeddings")
    if not embeddings or not embeddings[0]:
        raise RuntimeError(
            f"embedding model {config.EMBEDDING_MODEL!r} returned no vector for a {task!r} text"
        )
    return embeddings[0]
