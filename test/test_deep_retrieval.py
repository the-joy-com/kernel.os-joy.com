"""Deep retrieval: the ontology-walk half of the Tier 2 reach, and how deep_search joins its two movements.

The vector-recall movement (recall_facts) embeds the message and searches the HNSW, so it needs live Ollama and real
vectors — that is the by-hand smoke's job (test/qa/0005). What is provable here without a model is the rest:
the ontology walk (expand_by_concept) is pure SQL over the diary/ontology store, and deep_search's joining of recall
and walk is exercised by stubbing recall to a fixed list and letting the real walk run out from it.
"""

from core import db
from services.memory import deep_retrieval


def _concept(type_name, definition="a kind of thing") -> int:
    with db.get_pool().connection() as conn:
        return conn.execute(
            "INSERT INTO schema_ontology (type_name, definition) VALUES (%s, %s) RETURNING id",
            (type_name, definition),
        ).fetchone()[0]


def _fact(raw_text, concept_ids, *, intake_id=None) -> int:
    with db.get_pool().connection() as conn:
        fact_id = conn.execute(
            "INSERT INTO diary_facts (raw_text, payload, intake_id) VALUES (%s, %s::jsonb, %s) RETURNING id",
            (raw_text, "{}", intake_id),
        ).fetchone()[0]
        for concept_id in concept_ids:
            conn.execute(
                "INSERT INTO diary_fact_ontology (diary_fact_id, ontology_id) VALUES (%s, %s)",
                (fact_id, concept_id),
            )
        return fact_id


def _texts(related):
    return [r.raw_text for r in related]


def test_expand_pulls_siblings_sharing_the_seeds_concepts_most_shared_first(client):
    # A seed fact filed under two concepts; siblings sharing more of them rank ahead of siblings sharing fewer,
    # and a fact sharing none of the seed's concepts is never pulled.
    c1 = _concept("workout")
    c2 = _concept("friends")
    c3 = _concept("cooking")
    seed = _fact("boxing with Jeremy", [c1, c2])
    both = _fact("a run with Jeremy", [c1, c2])   # shares 2
    one = _fact("a solo swim", [c1])              # shares 1
    _fact("baked bread", [c3])                    # shares 0 — never pulled

    with db.get_pool().connection() as conn:
        related = deep_retrieval.expand_by_concept(conn, [seed])

    assert _texts(related) == ["a run with Jeremy", "a solo swim"]  # most-shared first, seed excluded
    assert all(r.distance is None for r in related)  # reached through the tree, not measured
    _ = (both, one)  # named for readability


def test_expand_excludes_the_messages_own_fact(client):
    # A sibling that is itself the fact this very message became must not be walked back in — no self-enrichment.
    c1 = _concept("workout")
    seed = _fact("boxing", [c1])
    with db.get_pool().connection() as conn:
        own_message = conn.execute(
            "INSERT INTO intake (message, symbiot_id, status) VALUES ('own', 1, 'answered') RETURNING id"
        ).fetchone()[0]
    _fact("the message's own fact", [c1], intake_id=own_message)

    with db.get_pool().connection() as conn:
        related = deep_retrieval.expand_by_concept(conn, [seed], exclude_intake_ids=[own_message])

    assert related == []  # the only sibling was the message's own fact, excluded


def test_expand_is_empty_for_no_seeds(client):
    # Nothing was recalled to walk out from, so the caller need not special-case an empty seed set.
    with db.get_pool().connection() as conn:
        assert deep_retrieval.expand_by_concept(conn, []) == []


def test_deep_search_returns_the_recalled_facts_then_the_walked_siblings(client, monkeypatch):
    # deep_search joins its two movements: the vector-recalled facts first (in their distance order),
    # then the siblings the ontology walk pulls out from them, with the recalled seeds themselves not repeated.
    c1 = _concept("workout")
    recalled = _fact("boxing", [c1])
    sibling = _fact("a run", [c1])

    # Stub the vector-recall movement (its embedding call needs live Ollama); the walk out from it runs for real.
    monkeypatch.setattr(
        deep_retrieval, "recall_facts",
        lambda conn, query_text, **kw: [
            deep_retrieval.Related(id=recalled, raw_text="boxing", effective_at=None, distance=0.1)
        ],
    )
    with db.get_pool().connection() as conn:
        related = deep_retrieval.deep_search(conn, "did some exercise")

    assert _texts(related) == ["boxing", "a run"]  # recall first, then the walked-in sibling
    _ = sibling
