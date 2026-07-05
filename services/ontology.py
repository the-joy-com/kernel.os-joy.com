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
The re-ranker that judges which of them truly fits, and the minting that coins a new type
when none does, are the rest of this path and read the pool recall hands back.
"""

from dataclasses import dataclass

from core import config


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
