"""Ontology routing: the recall (nominate) pass, and the embedding client under it.

Everything here runs against the test database with hand-built vectors and a faked Ollama,
so a passing suite proves the recall SQL — its ordering, its bounds, its exclusions —
and the embedding client's contract, without a single call to a real model.
The live round trip (real Ollama embeddings ranked against real stored vectors) is proven
separately, by hand.
"""

import pytest

from core import config
from core import db
from services import embedding
from services import ontology

# The active model's output width; every hand-built vector must match it or the ::vector cast rejects it.
_DIM = 768


def _vec(**index_value: float) -> list[float]:
    # A _DIM-long vector, zero everywhere except the named indices — enough to place a point
    # in the first couple of dimensions and let cosine distance order a handful of them.
    v = [0.0] * _DIM
    for i, x in index_value.items():
        v[int(i)] = x
    return v


def _add_type(conn, type_name: str, definition: str, vec: list[float], merged_into=None) -> int:
    # Land one ontology type and its embedding the way the minter eventually will:
    # the durable text in schema_ontology, the vector in the active model's table stamped with that model.
    oid = conn.execute(
        "INSERT INTO schema_ontology (type_name, definition, merged_into) "
        "VALUES (%s, %s, %s) RETURNING id",
        (type_name, definition, merged_into),
    ).fetchone()[0]
    model_id = conn.execute("SELECT id FROM embedding_model WHERE is_active").fetchone()[0]
    literal = "[" + ",".join(repr(x) for x in vec) + "]"
    conn.execute(
        "INSERT INTO ontology_embedding_nomic (ontology_id, model_id, embedding) "
        "VALUES (%s, %s, %s::vector)",
        (oid, model_id, literal),
    )
    return oid


def test_recall_on_empty_store_returns_nothing(client):
    # The cold start: no type has ever been coined, so there is nothing to nominate —
    # which is exactly the signal for the caller to mint the first concept.
    with db.get_pool().connection() as conn:
        assert ontology.recall_candidates(conn, _vec(**{"0": 1.0})) == []


def test_recall_orders_by_cosine_distance_nearest_first(client):
    # Three types at falling cosine similarity to the query; recall must return them nearest-first.
    with db.get_pool().connection() as conn:
        _add_type(conn, "near", "almost the query", _vec(**{"0": 1.0, "1": 0.1}))
        _add_type(conn, "mid", "half turned away", _vec(**{"0": 1.0, "1": 1.0}))
        _add_type(conn, "far", "orthogonal", _vec(**{"1": 1.0}))

        got = ontology.recall_candidates(conn, _vec(**{"0": 1.0}))

    assert [c.type_name for c in got] == ["near", "mid", "far"]
    # Distances are real cosine distances, sorted ascending, and the orthogonal one sits at 1.0.
    assert got[0].distance < got[1].distance < got[2].distance
    assert got[-1].distance == pytest.approx(1.0)


def test_recall_honours_the_pool_limit(client):
    # A limit caps the pool at the k nearest, dropping the rest — this is the wide-net knob.
    with db.get_pool().connection() as conn:
        _add_type(conn, "a", "closest", _vec(**{"0": 1.0, "1": 0.1}))
        _add_type(conn, "b", "middle", _vec(**{"0": 1.0, "1": 1.0}))
        _add_type(conn, "c", "farthest", _vec(**{"1": 1.0}))

        got = ontology.recall_candidates(conn, _vec(**{"0": 1.0}), limit=2)

    assert [c.type_name for c in got] == ["a", "b"]


def test_recall_excludes_a_merged_type(client):
    # A collapsed type stays in schema_ontology as a redirect (merged_into set) but must never be
    # nominated again, even if the garbage pass hasn't yet dropped its vector.
    with db.get_pool().connection() as conn:
        survivor = _add_type(conn, "survivor", "the keeper", _vec(**{"0": 1.0, "1": 1.0}))
        # The merged type is the *nearest* to the query, so only the merged_into filter can hide it.
        _add_type(conn, "merged", "folded away", _vec(**{"0": 1.0, "1": 0.1}), merged_into=survivor)

        got = ontology.recall_candidates(conn, _vec(**{"0": 1.0}))

    assert [c.type_name for c in got] == ["survivor"]


def test_recall_defaults_its_pool_width_to_config(client, monkeypatch):
    # With no explicit limit, recall pulls config.RECALL_POOL candidates.
    monkeypatch.setattr(config, "RECALL_POOL", 1)
    with db.get_pool().connection() as conn:
        _add_type(conn, "a", "closest", _vec(**{"0": 1.0, "1": 0.1}))
        _add_type(conn, "b", "farther", _vec(**{"0": 1.0, "1": 1.0}))

        got = ontology.recall_candidates(conn, _vec(**{"0": 1.0}))

    assert [c.type_name for c in got] == ["a"]


# --- the embedding client, with Ollama faked ---------------------------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_embed_sends_the_task_prefix_and_full_window(monkeypatch):
    # The two traps the client exists to carry: the search_query:/search_document: prefix,
    # and the num_ctx that stops Ollama silently truncating a long text.
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse({"embeddings": [[0.1] * _DIM]})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)

    vec = embedding.embed("hit the heavy bag", task="query")

    assert len(vec) == _DIM
    assert captured["json"]["input"] == "search_query: hit the heavy bag"
    assert captured["json"]["options"]["num_ctx"] == config.EMBEDDING_NUM_CTX
    assert captured["url"].endswith("/api/embed")


def test_embed_rejects_an_unknown_task(monkeypatch):
    # A caller must declare document-or-query; anything else is a bug, caught before the network.
    def fake_post(url, json, timeout):  # pragma: no cover - must never be reached
        raise AssertionError("embed must not call Ollama for an unknown task")

    monkeypatch.setattr(embedding.httpx, "post", fake_post)

    with pytest.raises(ValueError):
        embedding.embed("whatever", task="banana")


def test_embed_raises_when_no_vector_comes_back(monkeypatch):
    # An empty answer must fail loud, never return a quietly wrong vector.
    monkeypatch.setattr(embedding.httpx, "post", lambda url, json, timeout: _FakeResponse({"embeddings": []}))

    with pytest.raises(RuntimeError):
        embedding.embed("hit the heavy bag", task="document")
