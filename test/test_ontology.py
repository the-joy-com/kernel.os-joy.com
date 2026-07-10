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


# --- the mint pass, with the LLM and the embedder faked ------------------------------------


def _fake_ollama(monkeypatch, *, generate: str, embed: list[float] | None = None):
    # Mint calls both Ollama endpoints — /api/generate for the reply, /api/embed for the vector —
    # and llm and embedding share the one httpx module, so a single post dispatches on the URL:
    # the generative reply for one, the embedding vector for the other.
    vec = embed if embed is not None else [0.1] * _DIM

    def dispatch(url, json, timeout):
        if url.endswith("/api/generate"):
            return _FakeResponse({"response": generate})
        return _FakeResponse({"embeddings": [vec]})

    monkeypatch.setattr(llm.httpx, "post", dispatch)


def _ranked(name: str, ontology_id: int, definition: str = "a definition") -> ontology.Ranked:
    # A re-ranked candidate; only its name and ontology_id matter to the mint (the parent it may point at).
    return ontology.Ranked(ontology.Candidate(ontology_id, name, definition, 0.1), 0.5)


def test_mint_inserts_a_new_type_and_embedding_as_a_root(client, monkeypatch):
    # The plain mint: no parent among the neighbours, so a root type with parent_id NULL,
    # its coined definition embedded and landed in the active model's table for the next recall.
    _fake_ollama(monkeypatch, generate='{"type_name": "boxing_session", "definition": "a bout of boxing", "parent": "none"}')

    with db.get_pool().connection() as conn:
        new_id = ontology.mint(conn, "hit the heavy bag", [])

        row = conn.execute(
            "SELECT type_name, definition, parent_id FROM schema_ontology WHERE id = %s", (new_id,)
        ).fetchone()
        assert row == ("boxing_session", "a bout of boxing", None)
        # The vector landed in the active set, keyed back to the new type.
        assert conn.execute(
            "SELECT count(*) FROM active_ontology_embedding WHERE ontology_id = %s", (new_id,)
        ).fetchone()[0] == 1


def test_mint_sets_the_parent_from_the_context(client, monkeypatch):
    # When the model places the new type under a neighbour, that neighbour's id becomes parent_id —
    # the sub-type edge that keeps the vocabulary a tree rather than a flat scatter.
    with db.get_pool().connection() as conn:
        parent_oid = _add_type(conn, "workout_action", "any bout of physical training", _vec(**{"0": 1.0}))
        context = [_ranked("workout_action", parent_oid), _ranked("errand", 999)]
        _fake_ollama(monkeypatch, generate='{"type_name": "boxing_session", "definition": "a bout of boxing", "parent": "workout_action"}')

        new_id = ontology.mint(conn, "hit the heavy bag", context)

        assert conn.execute(
            "SELECT parent_id FROM schema_ontology WHERE id = %s", (new_id,)
        ).fetchone()[0] == parent_oid


def test_mint_reuses_an_existing_type_on_a_name_collision(client, monkeypatch):
    # The model names a type that already exists: mint returns that row's id and inserts nothing,
    # and never even reaches the embedder — a clash resolves to reuse, not a suffixed duplicate.
    def dispatch(url, json, timeout):
        if url.endswith("/api/embed"):  # pragma: no cover - must never be reached
            raise AssertionError("mint must not embed when it reuses an existing type on a name clash")
        return _FakeResponse({"response": '{"type_name": "boxing_session", "definition": "a fresh definition", "parent": "none"}'})

    monkeypatch.setattr(llm.httpx, "post", dispatch)
    with db.get_pool().connection() as conn:
        existing = _add_type(conn, "boxing_session", "a bout of boxing", _vec(**{"0": 1.0}))
        before = conn.execute("SELECT count(*) FROM schema_ontology").fetchone()[0]

        got = ontology.mint(conn, "hit the heavy bag", [])

        assert got == existing
        # No row was added, and the existing definition was left untouched.
        assert conn.execute("SELECT count(*) FROM schema_ontology").fetchone()[0] == before
        assert conn.execute(
            "SELECT definition FROM schema_ontology WHERE id = %s", (existing,)
        ).fetchone()[0] == "a bout of boxing"


def test_mint_parent_grammar_is_locked_to_the_context_plus_none(monkeypatch):
    # Structured output: the reply's `parent` enum is exactly the neighbour names and "none",
    # so Ollama's decoder can't hang the new type under a parent that was never offered.
    captured = {}

    def fake_post(url, json, timeout):
        if url.endswith("/api/embed"):
            return _FakeResponse({"embeddings": [[0.1] * _DIM]})
        captured["json"] = json
        return _FakeResponse({"response": '{"type_name": "boxing_session", "definition": "a bout", "parent": "none"}'})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    context = [_ranked("workout_action", 1), _ranked("errand", 2)]
    with db.get_pool().connection() as conn:
        ontology.mint(conn, "hit the heavy bag", context)

    assert captured["json"]["format"]["properties"]["parent"]["enum"] == ["workout_action", "errand", "none"]


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


# --- concept extraction (the naming step), with the LLM faked ------------------------------


def test_extract_concepts_names_the_distinct_concepts(monkeypatch):
    # One call reads the fact and returns the kinds of things it is about, as a plain list.
    monkeypatch.setattr(llm.httpx, "post", lambda url, json, timeout: _FakeResponse(
        {"response": '{"concepts": ["a boxing session", "time with a friend", "a heat wave"]}'}))

    got = ontology.extract_concepts("boxing with my friend Jeremy during the heat wave")

    assert got == ["a boxing session", "time with a friend", "a heat wave"]


def test_extract_concepts_prompts_with_the_fact_and_demands_at_least_one(monkeypatch):
    # The prompt must carry the fact, and the schema must forbid an empty list — a fact is always
    # about something, so "no concepts" is a mis-read the decoder grammar refuses up front.
    captured = {}

    def fake_post(url, json, timeout):
        captured["json"] = json
        return _FakeResponse({"response": '{"concepts": ["a nap"]}'})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    ontology.extract_concepts("dozed off on the couch")

    assert "dozed off on the couch" in captured["json"]["prompt"]
    assert captured["json"]["format"]["properties"]["concepts"]["minItems"] == 1


def test_extract_concepts_rejects_an_empty_list(monkeypatch):
    # A reply naming no concept breaks the min-length and raises at the boundary rather than
    # letting a fact through with nothing to file it under.
    monkeypatch.setattr(llm.httpx, "post", lambda url, json, timeout: _FakeResponse(
        {"response": '{"concepts": []}'}))

    with pytest.raises(ValidationError):
        ontology.extract_concepts("something happened")


# --- routing one concept (recall → re-rank → decide → grey → mint), faked ------------------


def test_route_concept_reuses_a_clear_match(client, monkeypatch):
    # A concept whose nearest type the re-ranker scores well is reused — no new type is coined.
    def dispatch(url, json, timeout):
        if url.endswith("/api/embed"):  # the recall query embedding: point it straight at the type
            return _FakeResponse({"embeddings": [_vec(**{"0": 1.0})]})
        return _FakeResponse({"response": '{"scores": [{"type": "boxing_session", "score": 0.95}]}'})

    monkeypatch.setattr(llm.httpx, "post", dispatch)
    with db.get_pool().connection() as conn:
        existing = _add_type(conn, "boxing_session", "a bout of boxing", _vec(**{"0": 1.0}))
        before = conn.execute("SELECT count(*) FROM schema_ontology").fetchone()[0]

        got = ontology.route_concept(conn, "hit the heavy bag")

        assert got == existing
        assert conn.execute("SELECT count(*) FROM schema_ontology").fetchone()[0] == before


def test_route_concept_mints_on_an_empty_store(client, monkeypatch):
    # Nothing to recall, so nothing to reuse: the concept coins its first type and route returns it.
    _fake_ollama(monkeypatch, generate='{"type_name": "boxing_session", "definition": "a bout of boxing", "parent": "none"}')

    with db.get_pool().connection() as conn:
        got = ontology.route_concept(conn, "hit the heavy bag")

        row = conn.execute(
            "SELECT type_name FROM schema_ontology WHERE id = %s", (got,)
        ).fetchone()
        assert row == ("boxing_session",)


def test_route_concept_coins_then_reuses_the_same_type(client, monkeypatch):
    # The acceptance criterion end to end for one concept: the first fact of a novel kind coins a
    # type; a second fact of the same kind reuses it rather than minting a duplicate.
    with db.get_pool().connection() as conn:
        # First concept: empty store → mint. The mint reply names the new type; the embed lands its vector.
        _fake_ollama(monkeypatch, generate='{"type_name": "boxing_session", "definition": "a bout of boxing", "parent": "none"}')
        first = ontology.route_concept(conn, "hit the heavy bag")

        # Second concept of the same kind: recall now finds the coined type, and the re-ranker
        # scores it a clear reuse — so no second type is coined.
        def dispatch(url, json, timeout):
            if url.endswith("/api/embed"):
                return _FakeResponse({"embeddings": [_vec(**{"0": 1.0})]})
            return _FakeResponse({"response": '{"scores": [{"type": "boxing_session", "score": 0.95}]}'})

        monkeypatch.setattr(llm.httpx, "post", dispatch)
        second = ontology.route_concept(conn, "three rounds on the bag")

        assert second == first
        assert conn.execute(
            "SELECT count(*) FROM schema_ontology WHERE type_name = 'boxing_session'"
        ).fetchone()[0] == 1


def test_route_concept_grey_gate_reuses_on_a_fence_sitting_score(client, monkeypatch):
    # A top score in the grey band spends one yes/no call; a "fits" reuses rather than mints.
    monkeypatch.setattr(config, "REUSE_THRESHOLD", 0.7)
    monkeypatch.setattr(config, "MINT_THRESHOLD", 0.3)

    def dispatch(url, json, timeout):
        if url.endswith("/api/embed"):
            return _FakeResponse({"embeddings": [_vec(**{"0": 1.0})]})
        # Two generative calls share this path: the re-rank asks to score the pool, the grey gate
        # asks a single yes/no. The re-rank prompt is the one that says "Score each candidate".
        if "Score each candidate" in json["prompt"]:
            return _FakeResponse({"response": '{"scores": [{"type": "boxing_session", "score": 0.5}]}'})
        return _FakeResponse({"response": '{"fits": true}'})

    monkeypatch.setattr(llm.httpx, "post", dispatch)
    with db.get_pool().connection() as conn:
        existing = _add_type(conn, "boxing_session", "a bout of boxing", _vec(**{"0": 1.0}))

        got = ontology.route_concept(conn, "hit the heavy bag")

        assert got == existing


# --- the thin synthesis (Phase 2), a pure deterministic assembly ---------------------------


def test_synthesize_builds_the_thin_payload_and_nothing_more():
    # The payload carries exactly two things: the @type links and the raw text verbatim.
    payload = ontology.synthesize(
        ["boxing_session", "friends", "heat_wave"],
        "boxing with my friend Jeremy during the heat wave",
    )

    assert payload == {
        "@type": ["boxing_session", "friends", "heat_wave"],
        "text": "boxing with my friend Jeremy during the heat wave",
    }
    # Thin means thin: no particulars are pulled out into structured keys.
    assert set(payload.keys()) == {"@type", "text"}


def test_synthesize_keeps_the_text_verbatim_and_sorts_the_types():
    # The raw text is not touched; the @type links are sorted alphabetically for a stable payload,
    # whatever order the caller hands them in.
    raw = "  weird\tspacing and CASING kept As-Is  "
    payload = ontology.synthesize(["b_type", "a_type"], raw)

    assert payload["text"] == raw
    assert payload["@type"] == ["a_type", "b_type"]


# --- persistence, against the database with the fact embedding faked -----------------------


def test_persist_writes_the_fact_its_payload_embedding_and_links(client, monkeypatch):
    # One fact, one embedding row, and one link per concept — the whole write, atomic.
    monkeypatch.setattr(embedding.httpx, "post", lambda url, json, timeout: _FakeResponse(
        {"embeddings": [_vec(**{"0": 1.0})]}))

    with db.get_pool().connection() as conn:
        a = _add_type(conn, "boxing_session", "a bout of boxing", _vec(**{"0": 1.0}))
        b = _add_type(conn, "friends", "time spent with a friend", _vec(**{"1": 1.0}))
        payload = ontology.synthesize(["boxing_session", "friends"], "boxing with a friend")

        fact_id = ontology.persist(conn, "boxing with a friend", payload, [a, b])

        row = conn.execute(
            "SELECT raw_text, payload FROM diary_facts WHERE id = %s", (fact_id,)
        ).fetchone()
        assert row[0] == "boxing with a friend"
        assert row[1] == {"@type": ["boxing_session", "friends"], "text": "boxing with a friend"}
        # One embedding row in the active set, keyed back to the fact.
        assert conn.execute(
            "SELECT count(*) FROM active_diary_fact_embedding WHERE diary_fact_id = %s", (fact_id,)
        ).fetchone()[0] == 1
        # One link per concept.
        assert {r[0] for r in conn.execute(
            "SELECT ontology_id FROM diary_fact_ontology WHERE diary_fact_id = %s", (fact_id,)
        ).fetchall()} == {a, b}


def test_persist_payload_is_queryable_by_jsonb_operators(client, monkeypatch):
    # The point of storing JSON-LD in JSONB: reach into it with Postgres operators.
    monkeypatch.setattr(embedding.httpx, "post", lambda url, json, timeout: _FakeResponse(
        {"embeddings": [_vec(**{"0": 1.0})]}))

    with db.get_pool().connection() as conn:
        a = _add_type(conn, "boxing_session", "a bout of boxing", _vec(**{"0": 1.0}))
        payload = ontology.synthesize(["boxing_session"], "hit the heavy bag")
        fact_id = ontology.persist(conn, "hit the heavy bag", payload, [a])

        # The @type array and the raw text are both reachable through -> / ->>.
        got = conn.execute(
            "SELECT id FROM diary_facts WHERE payload -> '@type' ? 'boxing_session'"
        ).fetchall()
        assert [r[0] for r in got] == [fact_id]
        assert conn.execute(
            "SELECT payload ->> 'text' FROM diary_facts WHERE id = %s", (fact_id,)
        ).fetchone()[0] == "hit the heavy bag"


# --- the full write path, end to end ------------------------------------------------------


def test_ingest_routes_every_concept_links_all_and_files_the_thin_payload(client, monkeypatch):
    # A fact expressing several concepts is filed against all of them, with a thin payload naming
    # each type. Routing is stubbed to fixed types (its own tests cover the recall/mint plumbing),
    # so this proves the orchestration: name → route each → synthesize → persist, once.
    monkeypatch.setattr(embedding.httpx, "post", lambda url, json, timeout: _FakeResponse(
        {"embeddings": [_vec(**{"0": 1.0})]}))

    with db.get_pool().connection() as conn:
        a = _add_type(conn, "boxing_session", "a bout of boxing", _vec(**{"0": 1.0}))
        b = _add_type(conn, "friends", "time spent with a friend", _vec(**{"1": 1.0}))
        c = _add_type(conn, "heat_wave", "a spell of extreme heat", _vec(**{"2": 1.0}))

        # Name and route the concepts out of alphabetical order, to prove the payload sorts them.
        monkeypatch.setattr(ontology, "extract_concepts",
                            lambda text: ["a heat wave", "a boxing session", "time with a friend"])
        routed = iter([c, a, b])
        monkeypatch.setattr(ontology, "route_concept", lambda conn, concept: next(routed))

        raw = "boxing with my friend Jeremy during the heat wave"
        fact_id = ontology.ingest(conn, raw)

        assert conn.execute(
            "SELECT payload FROM diary_facts WHERE id = %s", (fact_id,)
        ).fetchone()[0] == {"@type": ["boxing_session", "friends", "heat_wave"], "text": raw}
        assert {r[0] for r in conn.execute(
            "SELECT ontology_id FROM diary_fact_ontology WHERE diary_fact_id = %s", (fact_id,)
        ).fetchall()} == {a, b, c}


def test_ingest_dedups_concepts_that_route_to_the_same_type(client, monkeypatch):
    # Two named concepts can resolve to one type; the fact links to it once, and its @type names it
    # once — the dedup collapses it before the join table's composite key would reject the second link.
    monkeypatch.setattr(embedding.httpx, "post", lambda url, json, timeout: _FakeResponse(
        {"embeddings": [_vec(**{"0": 1.0})]}))

    with db.get_pool().connection() as conn:
        a = _add_type(conn, "friends", "time spent with a friend", _vec(**{"0": 1.0}))

        monkeypatch.setattr(ontology, "extract_concepts",
                            lambda text: ["time with a friend", "the friendship itself"])
        monkeypatch.setattr(ontology, "route_concept", lambda conn, concept: a)

        fact_id = ontology.ingest(conn, "a long lunch with my friend")

        assert conn.execute(
            "SELECT payload -> '@type' FROM diary_facts WHERE id = %s", (fact_id,)
        ).fetchone()[0] == ["friends"]
        assert conn.execute(
            "SELECT count(*) FROM diary_fact_ontology WHERE diary_fact_id = %s", (fact_id,)
        ).fetchone()[0] == 1
