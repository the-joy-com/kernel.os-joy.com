"""Retrieval: the fast lexical reach into the diary — Tier 1 of the read path.

When the symbiot says something, the reply needs the facts that bear on it,
and it needs them without a pause — so this reach is lexical, not semantic,
and it runs on the critical path the deeper meaning-based pass never touches.
It is Postgres full-text search used to its full extent:
the symbiot's words become a tsquery, ranked against each fact's raw text (ts_rank),
with trigram similarity alongside it (pg_trgm) so a typo or a half-remembered word still surfaces the fact it meant.
It runs in both languages the diary is lived in — English and French — each under its own analyser,
so an emotive French entry folds and ranks on French rules rather than being read through English (migration 0012).
It searches raw_text and only raw_text — the durable words as they arrived —
so it can answer before the ontology holds anything, and it never walks the vocabulary or touches a vector.

It reads and never writes:
the store belongs to the write path (ontology.py), and this module only queries it,
returning each matching fact ordered on its effective time — happened_at when the fact named a moment,
otherwise created_at — the COALESCE the write path left for the reader to collapse (migrations 0011, 0012).
"""

from dataclasses import dataclass
from datetime import datetime

from core import config


@dataclass(frozen=True)
class Fact:
    """One retrieved diary fact: its durable words, its filed payload, and where it sits in time.

    effective_at is the fact's effective time — happened_at when it named a moment, otherwise created_at —
    the one clock the read path orders and reasons on.
    rank is the blended lexical relevance (full-text rank plus trigram similarity), higher is nearer;
    it orders the results and nothing more, the way recall's distance orders the router's pool."""

    id: int
    raw_text: str
    payload: dict
    effective_at: datetime
    rank: float


def search(conn, query_text: str, limit: int | None = None) -> list[Fact]:
    """The nearest diary facts to `query_text`, most relevant first — the fast lexical reach.

    The symbiot's words become a websearch tsquery, then loosened from AND to OR between terms,
    so a fact is a hit when it shares *any* of the words, not all of them —
    a question is a bag of clues, and ts_rank sorts by how many land and how densely, not all-or-nothing.
    This runs under both the English and French analysers, matched either way,
    so a French entry folds on French rules and an English one on English (migration 0012).
    A fact also hits when its raw text is trigram-similar enough to catch a near-miss the lexemes let slip.
    Hits are ranked by the two analysers' ts_ranks plus trigram similarity, ties broken by effective time, most recent first,
    so the fresher of two equally-relevant facts leads.
    Both the full-text match and the trigram operator read the indexes migration 0012 built,
    and the whole thing is one read — no lock, no write — so it never contends with a worker filing a fact.

    An empty store, or a query nothing matches, returns an empty list:
    the reply then composes with no facts to lean on, the honest answer over an unpopulated diary.
    limit defaults to config.RETRIEVAL_LIMIT — the fixed budget of facts folded into the reply.
    """
    if limit is None:
        limit = config.RETRIEVAL_LIMIT
    # websearch_to_tsquery ANDs its terms, which would demand every word of the question be present —
    # far too strict, so each language's query is rebuilt with OR between terms (& → |) in a CTE and reused below.
    # raw_text is matched under both the English and French analysers (migration 0012): a fact hits if either
    # analyser matches or trigram does, and its rank sums both ts_ranks so an entry folds on its own language's rules.
    # The one query text binds to a single named parameter reused across the statement.
    # `%%` is a literal `%` — the pg_trgm similarity operator — kept clear of psycopg's parameter marker.
    rows = conn.execute(
        """
        WITH q AS (
            SELECT replace(websearch_to_tsquery('english', %(q)s)::text, '&', '|')::tsquery AS q_en,
                   replace(websearch_to_tsquery('french',  %(q)s)::text, '&', '|')::tsquery AS q_fr
        )
        SELECT id, raw_text, payload,
               COALESCE(happened_at, created_at) AS effective_at,
               ts_rank(to_tsvector('english', raw_text), q.q_en)
                   + ts_rank(to_tsvector('french', raw_text), q.q_fr)
                   + similarity(raw_text, %(q)s) AS rank
        FROM diary_facts, q
        WHERE to_tsvector('english', raw_text) @@ q.q_en
           OR to_tsvector('french', raw_text) @@ q.q_fr
           OR raw_text %% %(q)s
        ORDER BY rank DESC, effective_at DESC
        LIMIT %(limit)s
        """,
        {"q": query_text, "limit": limit},
    ).fetchall()
    return [Fact(r[0], r[1], r[2], r[3], r[4]) for r in rows]
