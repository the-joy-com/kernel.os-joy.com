"""Retrieval: the fast lexical reach into the diary, against the real test database.

No model is faked here because none is called — Tier 1's reach is pure Postgres (full-text plus trigram),
so a passing suite proves the search itself: what it matches, how it ranks, the effective time it orders on,
and the bounds it honours. Facts are inserted straight into diary_facts (the write path has its own tests),
carrying only what this read touches — raw_text, a payload, and the two clocks.
"""

import json
from datetime import datetime, timezone

from core import config
from core import db
from services import retrieval


def _add_fact(conn, raw_text: str, *, happened_at=None, payload=None) -> int:
    # Land one diary fact the way the write path eventually will, minus the embedding this read never touches.
    # payload is NOT NULL, so a thin stand-in stands in when a test doesn't care about it.
    payload = payload if payload is not None else {"@type": [], "text": raw_text}
    return conn.execute(
        "INSERT INTO diary_facts (raw_text, payload, happened_at) VALUES (%s, %s::jsonb, %s) RETURNING id",
        (raw_text, json.dumps(payload), happened_at),
    ).fetchone()[0]


def test_search_finds_a_lexical_match_and_leaves_the_unrelated(client):
    # The plain case: a query word present in one fact and absent from another returns only the first.
    with db.get_pool().connection() as conn:
        hit = _add_fact(conn, "hit the heavy bag at the gym")
        _add_fact(conn, "a quiet evening reading on the couch")

        got = retrieval.search(conn, "gym")

    assert [f.id for f in got] == [hit]


def test_search_ranks_a_denser_match_higher(client):
    # ts_rank rewards how strongly a fact answers the query; the fact naming the term thrice outranks the one
    # naming it once, and rank comes back descending.
    with db.get_pool().connection() as conn:
        dense = _add_fact(conn, "the gym, back to the gym, a good gym day")
        sparse = _add_fact(conn, "stopped by the gym once")

        got = retrieval.search(conn, "gym")

    assert [f.id for f in got] == [dense, sparse]
    assert got[0].rank >= got[1].rank


def test_search_fuzzy_trigram_catches_a_typo_full_text_would_miss(client):
    # Full-text matches whole lexemes, so "Strasborg" would never match "Strasbourg" on its own.
    # Trigram similarity closes that gap, so a misspelt query still surfaces the fact it was reaching for.
    with db.get_pool().connection() as conn:
        strasbourg = _add_fact(conn, "I live in Strasbourg")
        _add_fact(conn, "the weather was mild today")

        got = retrieval.search(conn, "Strasborg")

    assert [f.id for f in got] == [strasbourg]


def test_search_matches_french_inflections_via_the_french_analyser(client):
    # The symbiot slips into French for the emotive entries; a French query in a related form must still
    # find them. "énervé" and "énervement" fold to one stem ("énerv") under the french analyser, never the
    # english one — so the entry is reached, the whole point of indexing raw_text under both languages.
    with db.get_pool().connection() as conn:
        angry = _add_fact(conn, "ce matin au travail j'étais vraiment énervé")
        _add_fact(conn, "a calm morning by the lake")

        got = retrieval.search(conn, "énervement")

    assert [f.id for f in got] == [angry]


def test_search_reads_effective_time_as_happened_at_then_created_at(client):
    # The COALESCE the write path left for the reader: a fact that named a moment orders on happened_at,
    # one that didn't stands its created_at in — and neither is ever None.
    dated = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with db.get_pool().connection() as conn:
        _add_fact(conn, "a gym session long ago", happened_at=dated)
        _add_fact(conn, "a gym session with no stated date", happened_at=None)

        got = {f.raw_text: f for f in retrieval.search(conn, "gym session")}

    assert got["a gym session long ago"].effective_at == dated
    # The undated fact falls back to created_at — the telling time, filled now, this year — never None.
    undated = got["a gym session with no stated date"]
    assert undated.effective_at is not None
    assert undated.effective_at.year == datetime.now(timezone.utc).year


def test_search_breaks_a_rank_tie_by_recency(client):
    # Two facts with identical text score identically, so the tie falls to effective time, most recent first.
    older = datetime(2021, 6, 1, tzinfo=timezone.utc)
    newer = datetime(2023, 6, 1, tzinfo=timezone.utc)
    with db.get_pool().connection() as conn:
        older_id = _add_fact(conn, "the gym", happened_at=older)
        newer_id = _add_fact(conn, "the gym", happened_at=newer)

        got = retrieval.search(conn, "gym")

    assert [f.id for f in got] == [newer_id, older_id]


def test_search_empty_store_returns_nothing(client):
    # Nothing filed yet: the reply then composes over an empty shelf, the honest answer on a fresh diary.
    with db.get_pool().connection() as conn:
        assert retrieval.search(conn, "anything at all") == []


def test_search_no_match_returns_nothing(client):
    # A query sharing neither a lexeme nor enough trigrams with any fact returns empty, not a weak match.
    with db.get_pool().connection() as conn:
        _add_fact(conn, "hit the heavy bag at the gym")

        assert retrieval.search(conn, "xylophone") == []


def test_search_honours_the_limit(client):
    # The fixed retrieval budget: an explicit limit caps how many facts come back.
    with db.get_pool().connection() as conn:
        for i in range(3):
            _add_fact(conn, f"gym session number {i}")

        assert len(retrieval.search(conn, "gym session", limit=2)) == 2


def test_search_defaults_its_limit_to_config(client, monkeypatch):
    # With no explicit limit, the budget is config.RETRIEVAL_LIMIT.
    monkeypatch.setattr(config, "RETRIEVAL_LIMIT", 1)
    with db.get_pool().connection() as conn:
        _add_fact(conn, "a gym session")
        _add_fact(conn, "another gym session")

        assert len(retrieval.search(conn, "gym session")) == 1
