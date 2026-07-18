"""Deep retrieval: the meaning-based reach into the diary — Tier 2 of the read path.

Where the lexical reach (retrieval.py) answers on the critical path by matching *words*,
this reach answers off it by matching *meaning*: it is the pass the fast reply deliberately never waits on.
It runs in two movements, both read-only:

  1. vector recall — the message is embedded and the nearest diary facts are pulled by cosine distance,
     so a fact bears on the message because it *means* something similar, even when it shares none of its words
     ("I'm drained" reaching a fact about burnout the lexical pass, seeing no common term, could never surface);
  2. the ontology walk — the concepts those recalled facts are filed under are read from the vocabulary,
     and the sibling facts filed under the same concepts are pulled alongside,
     so the reach follows the tree the write path grew, not raw vector distance alone.

The walk reads the ontology; it never writes it.
It never routes the message or mints a type — coining vocabulary from a read pass is the write path's job (ontology.py),
and doing it here would let a question quietly grow the diary's tree. So this module only ever reads: no lock, no write.

It is the mirror of the router's recall (ontology.recall_candidates), pointed the other way:
the router searches ontology vectors to *classify a new fact being filed*;
this searches diary-fact vectors to *fetch already-filed facts that bear on a question* — same vectors, opposite directions.
The store belongs to the write path; this module only queries it.
"""

from dataclasses import dataclass
from datetime import datetime

from core import config
from services.adapters import embedding


@dataclass(frozen=True)
class Related:
    """One diary fact the deep reach surfaced, and how it was reached.

    raw_text is the fact's durable words, effective_at its effective time (happened_at, else created_at) —
    the same one clock the lexical reach orders on.
    distance is the cosine distance from the vector recall when that is how the fact was reached,
    and None when it came in through the ontology walk instead —
    a sibling of a recalled fact, near by shared concept rather than by measured distance.
    It records provenance and orders the vector hits; it is never a threshold the pass acts on."""

    id: int
    raw_text: str
    effective_at: datetime
    distance: float | None


def deep_search(
    conn, query_text: str, *, exclude_intake_ids: list[int] | None = None
) -> list[Related]:
    """The whole deep reach for one message or burst: vector recall, then the ontology walk out from it.

    Recall the nearest facts by meaning, then expand along the concepts they are filed under to their siblings,
    and return the two joined — the recalled facts first, in distance order, then the walked-in siblings,
    de-duplicated so a fact reached both ways appears once (its recall entry, which carries the measured distance, wins).
    The facts the reaching message or burst became are excluded throughout, so a message never enriches itself —
    a single message excludes its own one fact, a burst enrichment excludes every member's fact.
    An empty list means the diary held nothing that bears on the message by meaning — the signal to send no enrichment.
    """
    recalled = recall_facts(conn, query_text, exclude_intake_ids=exclude_intake_ids)
    siblings = expand_by_concept(
        conn, [f.id for f in recalled], exclude_intake_ids=exclude_intake_ids
    )
    seen = {f.id for f in recalled}
    return recalled + [s for s in siblings if s.id not in seen]


def expand_by_concept(
    conn, seed_ids: list[int], *, exclude_intake_ids: list[int] | None = None, limit: int | None = None
) -> list[Related]:
    """The sibling facts filed under the same concepts as the seed facts — the ontology-walk movement.

    The seed facts (what vector recall found) are filed under concepts in the ontology;
    this reads those concepts from diary_fact_ontology and pulls the *other* facts filed under them,
    so a fact bears on the message because it shares a *kind of thing* with what was recalled, not a measured distance.
    Facts sharing more of the seeds' concepts come first — the ordering the walk has in place of a distance —
    with effective time breaking the tie, the fresher of two equally-connected facts leading.

    The seeds themselves are excluded (they are already in hand from recall), and so are the reaching messages' own facts.
    An empty seed set returns an empty list — nothing was recalled to walk out from — so the caller need not special-case it.
    Distance is None on everything here: these facts were reached through the tree, not measured against the query.
    """
    if not seed_ids:
        return []
    if limit is None:
        limit = config.DEEP_RETRIEVAL_EXPANSION_LIMIT
    rows = conn.execute(
        """
        WITH concepts AS (
            SELECT DISTINCT ontology_id
            FROM diary_fact_ontology
            WHERE diary_fact_id = ANY(%(seeds)s)
        )
        SELECT df.id, df.raw_text,
               COALESCE(df.happened_at, df.created_at) AS effective_at,
               count(*) AS shared
        FROM diary_fact_ontology link
        JOIN concepts    c  ON c.ontology_id = link.ontology_id
        JOIN diary_facts df ON df.id = link.diary_fact_id
        WHERE df.id <> ALL(%(seeds)s)
          AND (df.intake_id IS NULL OR df.intake_id <> ALL(%(exclude)s::bigint[]))
        GROUP BY df.id, df.raw_text, effective_at
        ORDER BY shared DESC, effective_at DESC
        LIMIT %(limit)s
        """,
        {"seeds": seed_ids, "exclude": exclude_intake_ids or [], "limit": limit},
    ).fetchall()
    return [Related(r[0], r[1], r[2], None) for r in rows]


def recall_facts(
    conn, query_text: str, *, exclude_intake_ids: list[int] | None = None, limit: int | None = None
) -> list[Related]:
    """The nearest diary facts to `query_text` by meaning, nearest first — the vector-recall movement.

    The message is embedded as a query (the matching search_query prefix, so the distance measures the right thing),
    and the active model's diary-fact vectors are searched for the nearest by cosine distance through the HNSW index.
    Reads through active_diary_fact_embedding — the view that always resolves to the live model's set —
    so a model swap costs this query nothing and it never names a versioned table, the same stance the router reads under.

    exclude_intake_ids drops the facts the reaching message or burst became, when they have already been filed:
    a message must never enrich itself with the fact it turned into, the same boundary the reply keeps for its own tail.
    (Ingestion may not have filed them yet; the filter is simply a no-op when a fact isn't there.)

    ef_search — the HNSW working-set width — is opened per query, comfortably above the pool,
    for the same reason the router opens it: a set no wider than the result would cap recall from the very first fact.
    It and the search run in one transaction so SET LOCAL holds across both regardless of the connection's autocommit mode,
    and reverts at transaction end rather than leaking onto the pool.
    An empty store, or a store with nothing near, returns an empty list — the honest nothing, not an error.
    """
    if limit is None:
        limit = config.DEEP_RETRIEVAL_LIMIT
    vector = embedding.embed(query_text, task="query")
    # pgvector has no psycopg adapter installed, so the vector crosses as its text literal and casts ::vector.
    vector_literal = "[" + ",".join(repr(x) for x in vector) + "]"
    with conn.transaction():
        # set_config(..., is_local => true) is the parameter-safe SET LOCAL, so the width stays confined to this transaction.
        conn.execute(
            "SELECT set_config('hnsw.ef_search', %s, true)",
            (str(config.DEEP_RETRIEVAL_EF_SEARCH),),
        )
        rows = conn.execute(
            """
            SELECT df.id, df.raw_text,
                   COALESCE(df.happened_at, df.created_at) AS effective_at,
                   e.embedding <=> %(q)s::vector AS distance
            FROM active_diary_fact_embedding e
            JOIN diary_facts df ON df.id = e.diary_fact_id
            WHERE (df.intake_id IS NULL OR df.intake_id <> ALL(%(exclude)s::bigint[]))
            ORDER BY e.embedding <=> %(q)s::vector
            LIMIT %(limit)s
            """,
            {"q": vector_literal, "exclude": exclude_intake_ids or [], "limit": limit},
        ).fetchall()
    return [Related(r[0], r[1], r[2], r[3]) for r in rows]
