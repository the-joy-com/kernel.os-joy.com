"""Offline duplicate GC: the merge pass that collapses semantic duplicates, with the model faked.

Everything here runs against the test database with hand-built vectors and a faked generative client,
so a passing suite proves the detect → confirm → cluster → pick → collapse plumbing, its SQL, and the schema constraints —
the idempotent link re-point, the parent guards, the tombstone, the dropped vector —
without a single call to a real model.
The generative calls go through the cloud client (llm.OpenAI, Scaleway's), faked at that boundary.
The live round trip is proven separately, by hand (test/qa/0002_ontology_gc_smoke.py).
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core import config
from core import db
from services.adapters import llm
from services.memory import ontology_gc

# The active model's output width; every hand-built vector must match it or the ::vector cast rejects it.
_DIM = 768


class _FakeChat:
    """Callable stand-in for llm.OpenAI (the Scaleway generative client): records each
    chat.completions.create payload in captured["json"] and answers from canned data. The prompt sits
    at messages[-1]["content"] and a schema's grammar at response_format (see _schema). `generate` may
    be a value or a callable(kwargs), so one fake can dispatch the two calls run_once makes (the pair
    confirmation and the survivor pick) on the prompt.
    """

    def __init__(self, *, generate=None):
        self._generate = generate
        self.captured = {}

    def __call__(self, *, base_url=None, api_key=None, timeout=None, max_retries=None):
        self.captured["base_url"] = base_url
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create, parse=self._parse))
        return self

    def _create(self, **kwargs):
        # The free-text path (no schema).
        self.captured["json"] = kwargs
        text = self._generate(kwargs) if callable(self._generate) else self._generate
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    def _parse(self, **kwargs):
        # The structured path: response_format is the Pydantic model class the caller passed.
        self.captured["json"] = kwargs
        text = self._generate(kwargs) if callable(self._generate) else self._generate
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _never(msg):
    # A generate handler that fails the test if the boundary is ever reached.
    def _raise(kwargs):  # pragma: no cover - must never be reached
        raise AssertionError(msg)

    return _raise


def _prompt(fake):
    # The single user message's content — where the prompt lands in an OpenAI chat request.
    return fake.captured["json"]["messages"][-1]["content"]


def _prompt_of(kwargs):
    # The same, off a create() call's kwargs — for a fake that dispatches on the prompt.
    return kwargs["messages"][-1]["content"]


def _schema(fake):
    # The JSON schema a generate_json call bound the decoder to — the Pydantic model handed to `parse`.
    return fake.captured["json"]["response_format"].model_json_schema()


def _vec(**index_value: float) -> list[float]:
    # A _DIM-long vector, zero everywhere except the named indices —
    # enough to place a handful of points so cosine distance orders them the way a test needs.
    v = [0.0] * _DIM
    for i, x in index_value.items():
        v[int(i)] = x
    return v


def _add_type(conn, type_name, definition, vec, parent_id=None, merged_into=None) -> int:
    # Land one ontology type and its embedding the way the minter does:
    # durable text in schema_ontology, the vector in the active model's table stamped with that model.
    oid = conn.execute(
        "INSERT INTO schema_ontology (type_name, definition, parent_id, merged_into) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (type_name, definition, parent_id, merged_into),
    ).fetchone()[0]
    model_id = conn.execute("SELECT id FROM embedding_model WHERE is_active").fetchone()[0]
    literal = "[" + ",".join(repr(x) for x in vec) + "]"
    conn.execute(
        "INSERT INTO ontology_embedding_nomic_embed_text (ontology_id, model_id, embedding) "
        "VALUES (%s, %s, %s::vector)",
        (oid, model_id, literal),
    )
    return oid


def _add_fact(conn, raw_text, ontology_ids) -> int:
    # A diary fact linked to the given concepts; payload is empty JSON, times default in the database.
    fact_id = conn.execute(
        "INSERT INTO diary_facts (raw_text, payload) VALUES (%s, %s::jsonb) RETURNING id",
        (raw_text, "{}"),
    ).fetchone()[0]
    for oid in ontology_ids:
        conn.execute(
            "INSERT INTO diary_fact_ontology (diary_fact_id, ontology_id) VALUES (%s, %s)",
            (fact_id, oid),
        )
    return fact_id


def _links(conn, fact_id) -> set[int]:
    return {r[0] for r in conn.execute(
        "SELECT ontology_id FROM diary_fact_ontology WHERE diary_fact_id = %s", (fact_id,)
    ).fetchall()}


# --- detection (candidate pairs) and clustering (union-find), no model ---------------------


def test_candidate_pairs_catches_near_skips_far_and_excludes_merged(client, monkeypatch):
    # The vector pre-filter offers only live types nearer than GC_DISTANCE, as unordered a<b pairs.
    monkeypatch.setattr(config, "GC_DISTANCE", 0.2)
    with db.get_pool().connection() as conn:
        a = _add_type(conn, "workout_action", "physical training", _vec(**{"0": 1.0}))
        b = _add_type(conn, "training_session", "a training bout", _vec(**{"0": 1.0, "1": 0.02}))  # ~on top of a
        c = _add_type(conn, "phone_call", "a call", _vec(**{"5": 1.0}))                            # orthogonal, far
        m = _add_type(conn, "old_twin", "already folded", _vec(**{"0": 1.0, "1": 0.01}), merged_into=a)  # near but merged

        pairs = ontology_gc.candidate_pairs(conn)

    ids_in_pairs = {oid for pair in pairs for oid in pair}
    assert tuple(sorted((a, b))) in {tuple(sorted(p)) for p in pairs}  # the true near pair is caught
    assert c not in ids_in_pairs   # the far type is never offered
    assert m not in ids_in_pairs   # a merged type is excluded even though its vector is near


def test_cluster_unions_transitively_linked_pairs():
    # A≡B and B≡C is one family of three; a disjoint pair is its own group.
    groups = sorted(sorted(g) for g in ontology_gc.cluster([(1, 2), (2, 3), (10, 11)]))
    assert groups == [[1, 2, 3], [10, 11]]


# --- pair confirmation and survivor pick, with the LLM faked -------------------------------


def test_confirm_same_kind_reads_both_definitions(monkeypatch):
    # The model sees both types by name and definition.
    # It is a fast, thinking-off call like every other through this boundary —
    # thinking disabled, decode grammar sent — and the boolean is parsed and checked on the way back.
    fake = _FakeChat(generate='{"same": true}')
    monkeypatch.setattr(llm, "OpenAI", fake)
    a = ontology_gc.Type(1, "workout_action", "physical training")
    b = ontology_gc.Type(2, "training_session", "a training bout")

    assert ontology_gc.confirm_same_kind(a, b) is True
    prompt = _prompt(fake)
    assert "workout_action" in prompt and "physical training" in prompt
    assert "training_session" in prompt and "a training bout" in prompt
    assert fake.captured["json"]["reasoning_effort"] == "none"  # thinking off
    assert _schema(fake) == ontology_gc._SameKindReply.model_json_schema()


def test_confirm_same_kind_false_leaves_them_apart(monkeypatch):
    monkeypatch.setattr(llm, "OpenAI", _FakeChat(generate='{"same": false}'))

    assert ontology_gc.confirm_same_kind(
        ontology_gc.Type(1, "sprint", "a short fast run"),
        ontology_gc.Type(2, "marathon", "a long endurance run"),
    ) is False


def test_pick_survivor_offers_the_cluster_names_and_returns_the_id(monkeypatch):
    # The chosen name maps back to its id.
    # A fast, thinking-off call like the confirmation: the cluster's names are offered in the prompt,
    # and the Literal reply model becomes the decode grammar so the answer can only be one of them.
    fake = _FakeChat(generate='{"survivor": "workout_action"}')
    monkeypatch.setattr(llm, "OpenAI", fake)
    types = [ontology_gc.Type(7, "workout_action", "training"), ontology_gc.Type(9, "training_session", "a bout")]

    assert ontology_gc.pick_survivor(types) == 7
    prompt = _prompt(fake)
    assert "workout_action" in prompt and "training_session" in prompt
    assert fake.captured["json"]["reasoning_effort"] == "none"  # thinking off


def test_pick_survivor_rejects_a_name_outside_the_cluster(monkeypatch):
    # A survivor the cluster never held violates the reply model and raises at the boundary:
    # the decode grammar would forbid it upstream, and the same model validates it here regardless.
    monkeypatch.setattr(llm, "OpenAI", _FakeChat(generate='{"survivor": "ghost"}'))

    with pytest.raises(ValidationError):
        ontology_gc.pick_survivor([ontology_gc.Type(1, "a", "x"), ontology_gc.Type(2, "b", "y")])


# --- the collapse, against the database ----------------------------------------------------


def test_collapse_repoints_links_idempotently_children_tombstones_and_drops_vector(client):
    with db.get_pool().connection() as conn:
        survivor = _add_type(conn, "workout_action", "training", _vec(**{"0": 1.0}))
        loser = _add_type(conn, "training_session", "a bout", _vec(**{"0": 1.0, "1": 0.02}))
        child = _add_type(conn, "boxing_session", "boxing", _vec(**{"2": 1.0}), parent_id=loser)
        only_loser = _add_fact(conn, "a training session", [loser])
        both = _add_fact(conn, "a workout, a session", [survivor, loser])  # the idempotent-collision case

        ontology_gc.collapse(conn, survivor, loser)

        # The lone-loser link is re-pointed;
        # the fact linked to both collapses to a single survivor link
        # (the colliding loser link was dropped, never tripping the composite primary key).
        assert _links(conn, only_loser) == {survivor}
        assert _links(conn, both) == {survivor}
        # The loser's child is re-parented onto the survivor — the tree edge follows the merge.
        assert conn.execute(
            "SELECT parent_id FROM schema_ontology WHERE id = %s", (child,)
        ).fetchone()[0] == survivor
        # The loser survives as a redirect, not a delete, so any lingering reference still resolves.
        assert conn.execute(
            "SELECT merged_into FROM schema_ontology WHERE id = %s", (loser,)
        ).fetchone()[0] == survivor
        # Its vector is gone, so recall stops offering it; the survivor's vector is untouched.
        assert conn.execute(
            "SELECT count(*) FROM active_ontology_embedding WHERE ontology_id = %s", (loser,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM active_ontology_embedding WHERE ontology_id = %s", (survivor,)
        ).fetchone()[0] == 1


def test_collapse_nulls_a_survivor_parent_that_pointed_at_the_loser(client):
    # The survivor's own parent was the loser:
    # rather than leave it pointing at a tombstone (or become its own parent), the edge is nulled —
    # no type ends up parented to itself or to a redirect.
    with db.get_pool().connection() as conn:
        loser = _add_type(conn, "parentish", "x", _vec(**{"0": 1.0, "1": 0.02}))
        survivor = _add_type(conn, "kept", "y", _vec(**{"0": 1.0}), parent_id=loser)

        ontology_gc.collapse(conn, survivor, loser)

        assert conn.execute(
            "SELECT parent_id FROM schema_ontology WHERE id = %s", (survivor,)
        ).fetchone()[0] is None


# --- the whole pass, end to end, with the model faked -------------------------------------


def _fake_confirm_and_pick(monkeypatch, *, same: bool, survivor: str):
    # run_once makes two kinds of generative call: the pair confirmation and the survivor pick.
    # Dispatch on the prompt — only the survivor prompt mentions "survivor".
    def dispatch(kwargs):
        if "survivor" in _prompt_of(kwargs):
            return '{"survivor": "%s"}' % survivor
        return '{"same": %s}' % ("true" if same else "false")

    monkeypatch.setattr(llm, "OpenAI", _FakeChat(generate=dispatch))


def test_run_once_merges_a_confirmed_pair_and_repoints_its_facts(client, monkeypatch):
    _fake_confirm_and_pick(monkeypatch, same=True, survivor="workout_action")
    with db.get_pool().connection() as conn:
        a = _add_type(conn, "workout_action", "training", _vec(**{"0": 1.0}))
        b = _add_type(conn, "training_session", "a bout", _vec(**{"0": 1.0, "1": 0.02}))
        fact = _add_fact(conn, "a workout", [b])

        report = ontology_gc.run_once(conn)

        assert report == [{"survivor": a, "merged": [b]}]
        assert conn.execute(
            "SELECT merged_into FROM schema_ontology WHERE id = %s", (b,)
        ).fetchone()[0] == a
        assert _links(conn, fact) == {a}
        assert conn.execute(
            "SELECT count(*) FROM active_ontology_embedding WHERE ontology_id = %s", (b,)
        ).fetchone()[0] == 0


def test_run_once_leaves_a_near_but_rejected_pair_intact(client, monkeypatch):
    # Distance-near but the model says different kinds (sprint vs marathon): nothing is merged.
    _fake_confirm_and_pick(monkeypatch, same=False, survivor="sprint")
    with db.get_pool().connection() as conn:
        a = _add_type(conn, "sprint", "a short fast run", _vec(**{"0": 1.0}))
        b = _add_type(conn, "marathon", "a long endurance run", _vec(**{"0": 1.0, "1": 0.02}))

        report = ontology_gc.run_once(conn)

        assert report == []
        assert conn.execute(
            "SELECT merged_into FROM schema_ontology WHERE id = %s", (b,)
        ).fetchone()[0] is None


def test_run_once_on_empty_store_does_nothing_and_calls_no_model(client, monkeypatch):
    # No pairs to weigh, so the pass returns before it would ever reach the model.
    monkeypatch.setattr(llm, "OpenAI",
                        _FakeChat(generate=_never("run_once must not call the model when there are no candidate pairs")))
    with db.get_pool().connection() as conn:
        assert ontology_gc.run_once(conn) == []
