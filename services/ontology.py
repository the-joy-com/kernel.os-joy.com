"""Ontology routing: filing a diary fact into the vocabulary of concepts it touches.

A fact is rarely just one thing, so it is filed under every concept it genuinely touches, not one,
and each concept takes the same trip — recall, re-rank, mint-if-new — on its own.
This module owns that path over the ontology store (migration 0010).

The path opens with recall: a wide, approximate nomination of the concept types
nearest an embedded fact or concept, ordered by cosine distance.
Recall never decides.
Vector distance measures how *related* two things are, not whether they are the *same kind* of thing —
a sprint and a marathon land almost on top of each other and are still different kinds of act —
so the distance is only allowed to pull the plausible candidates into the room.
The re-rank that judges which of them truly fits — a generative model reads the fact and each
candidate's definition together and scores the fit — and the minting that coins a new type when
none does are the rest of this path, and read the pool recall hands back.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, create_model

from core import config
from services import llm

# The three bands the top re-rank score falls into, deciding what happens to the concept.
REUSE = "reuse"  # a clear enough fit: link the fact to that existing type
GREY = "grey"  # ambiguous: escalate to the one-shot LLM gate (Phase 1c)
MINT = "mint"  # nothing fits: coin a new type (Phase 1d)


@dataclass(frozen=True)
class Candidate:
    """One nominated ontology type and how near it fell to the query.

    distance is cosine distance through the active-model vectors — smaller is nearer.
    It orders the pool and nothing more, never the match-or-mint call, which is the re-ranker's."""

    ontology_id: int
    type_name: str
    definition: str
    distance: float


def recall_candidates(conn, embedding: list[float], limit: int | None = None) -> list[Candidate]:
    """The nominate pass: the `limit` nearest ontology types to `embedding`, nearest first.

    Reads the store through active_ontology_embedding — the view that always resolves to the live
    model's vectors — so a model swap costs this query nothing and it never names a versioned table.
    A merged type is excluded even if its vector lingers before the garbage pass drops it,
    so recall never nominates a concept already folded into another.
    An empty store returns an empty list: the first concept a diary ever sees has nothing to match,
    which is exactly the signal to mint it.

    ef_search — the HNSW working-set width — is opened per query, comfortably above the pool.
    The index answers approximately from a set of candidates it walks the graph to fill,
    and a set no wider than the result would cap the recall this pass exists to protect,
    from the very first fact rather than only once the store grows.
    It and the search run in one transaction so SET LOCAL holds across both regardless of the
    connection's autocommit mode, and reverts at transaction end rather than leaking onto the pool.
    """
    if limit is None:
        limit = config.RECALL_POOL
    # pgvector has no psycopg adapter installed, so the vector crosses as its text literal and casts ::vector.
    vector_literal = "[" + ",".join(repr(x) for x in embedding) + "]"
    with conn.transaction():
        # SET itself can't take a bind parameter; set_config(..., is_local => true) is the
        # parameter-safe equivalent of SET LOCAL, so the width stays confined to this transaction.
        conn.execute(
            "SELECT set_config('hnsw.ef_search', %s, true)",
            (str(config.RECALL_EF_SEARCH),),
        )
        rows = conn.execute(
            """
            SELECT o.id, o.type_name, o.definition,
                   e.embedding <=> %(q)s::vector AS distance
            FROM active_ontology_embedding e
            JOIN schema_ontology o ON o.id = e.ontology_id
            WHERE o.merged_into IS NULL
            ORDER BY e.embedding <=> %(q)s::vector
            LIMIT %(limit)s
            """,
            {"q": vector_literal, "limit": limit},
        ).fetchall()
    return [Candidate(r[0], r[1], r[2], r[3]) for r in rows]


@dataclass(frozen=True)
class Ranked:
    """One candidate and how well the re-rank judged it categorises the fact.

    score is 0.0–1.0: the generative model's read of fit, not a vector distance —
    it is what decides reuse vs mint, the call recall's distance was never allowed to make."""

    candidate: Candidate
    score: float


def _rerank_prompt(fact_text: str, candidates: list[Candidate]) -> str:
    # Each candidate is offered by name *and definition*, so the model judges meaning, not the label.
    lines = "\n".join(f"- {c.type_name} — {c.definition}" for c in candidates)
    return (
        "You classify a personal diary fact against candidate concept types.\n"
        f'Fact: "{fact_text}"\n\n'
        "Score each candidate type from 0.0 to 1.0 by how well it *categorises* this fact: "
        "1.0 means the fact is clearly an instance of that kind of thing, 0.0 means unrelated. "
        "Judge the kind of thing, not mere topical closeness — a sprint and a marathon are related "
        "yet are different kinds of act.\n\n"
        f"Candidates (name — definition):\n{lines}\n\n"
        'Return JSON only, one entry per candidate: '
        '{"scores": [{"type": "<name>", "score": <0.0-1.0>}]}'
    )


def _rerank_reply_model(candidates: list[Candidate]) -> type[BaseModel]:
    """Build — at runtime — the Pydantic model the re-rank reply must match for *this* candidate pool.

    Why built on the fly rather than written as a normal `class ...(BaseModel)`:
    the set of legal type names isn't known until we see the pool recall handed back,
    and we want the schema to name exactly those — so each call constructs a fresh model
    whose `type` field can only be one of the candidate names in front of us.

    The model describes a reply shaped like:

        {"scores": [{"type": "boxing_session", "score": 0.9},
                    {"type": "friends",        "score": 0.2}]}

    Two things it locks down by construction, not by cleanup afterwards:
      - `type` is a Literal over the exact candidate names,
        so the model can't score a type we never offered or invent a new one —
        that value simply isn't in the grammar.
      - `score` is a float pinned to 0.0–1.0, so an out-of-range number can't come back.

    The model does double duty: its JSON schema is handed to Ollama as the decoder's grammar,
    so the constraints hold *while* the tokens are generated, and the same model validates the
    reply on the way back — a violation raises at the boundary instead of being silently coerced.
    The one thing a schema can't force is *coverage* — that every candidate gets a score —
    so an omitted candidate is defaulted to 0.0 by the caller (rerank_candidates), not here.
    """
    # The closed set of names the reply's `type` field may take:
    # the names recall nominated, and nothing else. Fed to Literal below to become that constraint.
    names = tuple(c.type_name for c in candidates)
    # create_model defines a Pydantic model *dynamically* — the runtime equivalent of writing:
    #     class _RerankScore(BaseModel):
    #         type: Literal[<names>]
    #         score: float = Field(ge=0.0, le=1.0)
    # Each keyword is one field, valued as a (type, default) tuple;
    # `...` (Ellipsis) marks the field required, with no default.
    # `Literal[names]` turns the tuple of names into an enum-like type — `type` must equal one of them;
    # `Field(ge=0.0, le=1.0)` bounds the score to the inclusive range 0.0–1.0.
    score_entry = create_model(
        "_RerankScore",
        type=(Literal[names], ...),
        score=(float, Field(ge=0.0, le=1.0)),
    )
    # Wrap those entries in the top-level reply object: {"scores": [ <_RerankScore>, ... ]}.
    # One _RerankScore per candidate the model scored; the list itself is the required field.
    return create_model("_RerankReply", scores=(list[score_entry], ...))


def rerank_candidates(fact_text: str, candidates: list[Candidate]) -> list[Ranked]:
    """Score every recalled candidate for how well it fits the fact, and return them best first.

    A single LLM call scores the whole pool at once:
    the fact plus each candidate's definition go in,
    and a score from 0.0 to 1.0 per candidate comes back
    (see llm.generate_json and _rerank_reply_model for how the reply is shaped and checked).

    Two edge cases:
      - an empty pool returns an empty list — recall found nothing, so there is nothing to score;
      - a candidate the model forgot to score defaults to 0.0, so it just falls to the bottom.
    """
    if not candidates:
        return []
    reply = llm.generate_json(
        _rerank_prompt(fact_text, candidates), _rerank_reply_model(candidates)
    )
    by_name = {s.type: s.score for s in reply.scores}
    ranked = [Ranked(c, by_name.get(c.type_name, 0.0)) for c in candidates]
    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked


def decide(ranked: list[Ranked]) -> str:
    """Which band the top score falls in — REUSE, GREY, or MINT — the match-or-mint gate.

    An empty ranking is MINT: recall offered nothing (an empty or wholly-unrelated store),
    so there is nothing to reuse and the concept is coined.
    Otherwise the best score is read against the two configured thresholds; the band between
    them is the grey zone the one-shot LLM gate (Phase 1c) resolves.
    """
    if not ranked:
        return MINT
    top = ranked[0].score
    if top >= config.REUSE_THRESHOLD:
        return REUSE
    if top <= config.MINT_THRESHOLD:
        return MINT
    return GREY
