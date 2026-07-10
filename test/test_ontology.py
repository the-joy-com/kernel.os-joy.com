"""Ontology routing: the recall (nominate) pass, and the embedding client under it.

Everything here runs against the test database with hand-built vectors and a faked Ollama,
so a passing suite proves the recall SQL — its ordering, its bounds, its exclusions —
and the embedding client's contract, without a single call to a real model.
The live round trip (real Ollama embeddings ranked against real stored vectors) is proven
separately, by hand.
"""

import pytest
from pydantic import BaseModel, ValidationError

from core import config
from core import db
from services import embedding
from services import llm
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
        "INSERT INTO ontology_embedding_nomic_embed_text (ontology_id, model_id, embedding) "
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


# --- the re-rank (decide) pass, with the LLM faked -----------------------------------------


def _return_recall_candidate(name: str, definition: str = "a definition") -> ontology.Candidate:
    # A recall candidate; distance is irrelevant to the re-rank, which scores fit afresh.
    return ontology.Candidate(ontology_id=1, type_name=name, definition=definition, distance=0.1)


def test_rerank_scores_map_back_and_sort_best_first(monkeypatch):
    # The LLM scores the whole pool in one call; we sort by fit, best first.
    cands = [_return_recall_candidate("boxing_session"), _return_recall_candidate("phone_call"), _return_recall_candidate("sleep")]
    body = ('{"scores": [{"type": "phone_call", "score": 0.1}, '
            '{"type": "boxing_session", "score": 0.9}, {"type": "sleep", "score": 0.4}]}')
    monkeypatch.setattr(llm.httpx, "post", lambda url, json, timeout: _FakeResponse({"response": body}))

    ranked = ontology.rerank_candidates("hit the heavy bag", cands)

    assert [r.candidate.type_name for r in ranked] == ["boxing_session", "sleep", "phone_call"]
    assert ranked[0].score == 0.9


def test_rerank_sends_a_strict_schema_naming_only_the_candidates(monkeypatch):
    # Structured output: the schema pins `type` to an enum of exactly the candidates offered and
    # `score` to the 0.0–1.0 band, so Ollama's decoder can't emit an invented type or a wild score.
    cands = [_return_recall_candidate("boxing_session"), _return_recall_candidate("phone_call")]
    captured = {}

    def fake_post(url, json, timeout):
        captured["json"] = json
        return _FakeResponse({"response": '{"scores": [{"type": "boxing_session", "score": 0.9}, '
                                          '{"type": "phone_call", "score": 0.1}]}'})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    ontology.rerank_candidates("hit the heavy bag", cands)

    # The per-entry shape lives in the schema's single $def; grab it without hardcoding its name.
    (score_def,) = captured["json"]["format"]["$defs"].values()
    props = score_def["properties"]
    assert props["type"]["enum"] == ["boxing_session", "phone_call"]
    assert props["score"]["minimum"] == 0.0
    assert props["score"]["maximum"] == 1.0
    assert score_def["required"] == ["type", "score"]


def test_rerank_defaults_an_unscored_candidate_to_zero(monkeypatch):
    # Coverage is the one thing the schema can't compel: a candidate the model leaves out of its
    # scores defaults to 0.0 in code and simply falls to the bottom.
    cands = [_return_recall_candidate("a"), _return_recall_candidate("b")]
    monkeypatch.setattr(llm.httpx, "post", lambda url, json, timeout: _FakeResponse(
        {"response": '{"scores": [{"type": "a", "score": 0.6}]}'}))  # "b" omitted

    ranked = ontology.rerank_candidates("x", cands)

    assert (ranked[0].candidate.type_name, ranked[0].score) == ("a", 0.6)
    assert (ranked[1].candidate.type_name, ranked[1].score) == ("b", 0.0)


def test_rerank_rejects_a_reply_that_breaks_the_schema(monkeypatch):
    # A reply that invents a type or scores out of the 0.0–1.0 band violates the model and raises,
    # rather than being quietly coerced — no loose output survives the boundary.
    cands = [_return_recall_candidate("a")]
    monkeypatch.setattr(llm.httpx, "post", lambda url, json, timeout: _FakeResponse(
        {"response": '{"scores": [{"type": "ghost", "score": 0.5}]}'}))

    with pytest.raises(ValidationError):
        ontology.rerank_candidates("x", cands)


def test_rerank_empty_pool_never_calls_the_llm(monkeypatch):
    # Recall found nothing, so there is nothing to score — and no call to spend.
    # rerank_candidates returns [] on an empty pool *before* it ever reaches the LLM; this proves it.
    # We prove the skip with a landmine, not an after-the-fact check: swap the real HTTP call for a
    # fake that explodes the instant it is touched, so if the code did reach the LLM the test fails
    # loudly here rather than passing quietly.
    def boom(url, json, timeout):  # pragma: no cover - must never be reached
        raise AssertionError("re-rank must not call the LLM for an empty pool")

    # llm.httpx.post is the one call generate_json makes to reach Ollama, so this arms the whole path.
    monkeypatch.setattr(llm.httpx, "post", boom)

    # Passes only if both hold: the result is [] AND boom never fired (it would have thrown first).
    assert ontology.rerank_candidates("x", []) == []


def test_decide_bands_the_top_score(monkeypatch):
    # The two thresholds carve the top score into reuse / grey / mint; both boundaries are inclusive.
    monkeypatch.setattr(config, "REUSE_THRESHOLD", 0.7)
    monkeypatch.setattr(config, "MINT_THRESHOLD", 0.3)
    one = lambda s: [ontology.Ranked(_return_recall_candidate("a"), s)]

    assert ontology.decide(one(0.9)) == ontology.REUSE
    assert ontology.decide(one(0.7)) == ontology.REUSE   # at the reuse floor
    assert ontology.decide(one(0.5)) == ontology.GREY
    assert ontology.decide(one(0.3)) == ontology.MINT    # at the mint ceiling
    assert ontology.decide(one(0.1)) == ontology.MINT
    assert ontology.decide([]) == ontology.MINT          # empty pool → coin the concept


# --- the grey-zone binary gate, with the LLM faked -----------------------------------------


def test_resolve_grey_yes_reuses(monkeypatch):
    # On the fence, one yes/no call: a "fits" reuses the candidate type rather than minting a twin.
    monkeypatch.setattr(llm.httpx, "post", lambda url, json, timeout: _FakeResponse(
        {"response": '{"fits": true}'}))

    assert ontology.resolve_grey("hit the heavy bag", _return_recall_candidate("boxing_session")) == ontology.REUSE


def test_resolve_grey_no_mints(monkeypatch):
    # A "does not fit" sends the concept to minting instead of forcing an ill-fitting reuse.
    monkeypatch.setattr(llm.httpx, "post", lambda url, json, timeout: _FakeResponse(
        {"response": '{"fits": false}'}))

    assert ontology.resolve_grey("hit the heavy bag", _return_recall_candidate("phone_call")) == ontology.MINT


def test_resolve_grey_prompts_with_the_fact_and_candidate(monkeypatch):
    # The gate must see both the fact and the one candidate's name and definition to judge the fit,
    # and pin the reply to the fixed one-boolean schema so the decoder can't wander off it.
    captured = {}

    def fake_post(url, json, timeout):
        captured["json"] = json
        return _FakeResponse({"response": '{"fits": true}'})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    ontology.resolve_grey("hit the heavy bag", _return_recall_candidate("boxing_session", "a bout of boxing"))

    prompt = captured["json"]["prompt"]
    assert "hit the heavy bag" in prompt
    assert "boxing_session" in prompt and "a bout of boxing" in prompt
    assert captured["json"]["format"]["properties"]["fits"]["type"] == "boolean"


def test_resolve_grey_rejects_a_reply_that_breaks_the_schema(monkeypatch):
    # A non-boolean verdict violates the model and raises at the boundary rather than being coerced
    # into a silent reuse-or-mint — the same strict discipline the re-rank holds.
    monkeypatch.setattr(llm.httpx, "post", lambda url, json, timeout: _FakeResponse(
        {"response": '{"fits": "maybe"}'}))

    with pytest.raises(ValidationError):
        ontology.resolve_grey("x", _return_recall_candidate("a"))


# --- the generative client, with Ollama faked ----------------------------------------------


class _Scored(BaseModel):
    # A minimal Pydantic model to exercise generate_json's mandatory-schema contract.
    score: float


def test_generate_json_sends_the_fixed_flags_and_validates(monkeypatch):
    # Thinking off, deterministic, and output held to the caller's model schema — not loose JSON.
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse({"response": '{"score": 0.5}'})

    monkeypatch.setattr(llm.httpx, "post", fake_post)

    out = llm.generate_json("some prompt", _Scored)

    assert isinstance(out, _Scored) and out.score == 0.5
    assert captured["json"]["think"] is False
    assert captured["json"]["stream"] is False
    assert captured["json"]["format"] == _Scored.model_json_schema()
    assert captured["json"]["options"]["temperature"] == 0
    assert captured["url"].endswith("/api/generate")


def test_generate_json_raises_on_empty_response(monkeypatch):
    # An empty generation must fail loud, never pass as a half-read decision.
    monkeypatch.setattr(llm.httpx, "post", lambda url, json, timeout: _FakeResponse({"response": ""}))

    with pytest.raises(RuntimeError):
        llm.generate_json("some prompt", _Scored)
