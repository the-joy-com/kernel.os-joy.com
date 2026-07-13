"""Tool calling: the catalog reconcile, the search that is the gate, and the decision call.

The embedding and the model are the two parts that reach off the box, so both are faked here;
what the tests pin is the machinery around them —
that the catalog is derived from the code registry and kept in sync,
that the search surfaces a candidate by meaning or by wording and is empty (and cheap) when nothing fits,
and that the decision returns a named tool with its arguments or an honest "none".
The live inference and the end-to-end fork are the by-hand smoke's to prove (test/qa/0007).
"""

import types
from datetime import datetime, timezone

from core import db
from services.tools import tools

# A fake embedding vector, 768-dimensional to match the active model's storage;
# its exact values don't matter because every text embeds to the same vector here,
# so a seeded tool sits at distance 0 from any query.
_VEC = [0.01] * 768


def _fake_embed(monkeypatch, calls=None):
    def embed(text, *, task):
        if calls is not None:
            calls.append(task)
        return list(_VEC)
    monkeypatch.setattr(tools.embedding, "embed", embed)


def test_reconcile_inserts_a_descriptor_and_embedding_per_registered_tool(client, monkeypatch):
    _fake_embed(monkeypatch)
    with db.get_pool().connection() as conn:
        tools.reconcile_catalog(conn)
        rows = conn.execute("SELECT count(*) FROM tool_catalog").fetchone()[0]
        embeds = conn.execute("SELECT count(*) FROM active_tool_embedding").fetchone()[0]
    # One descriptor row and one embedding per tool the code registry carries (the reminder, today).
    assert rows == len(tools.REGISTRY)
    assert embeds == len(tools.REGISTRY)
    assert "schedule_reminder" in tools.REGISTRY


def test_reconcile_is_idempotent_and_re_embeds_only_on_change(client, monkeypatch):
    calls = []
    _fake_embed(monkeypatch, calls)
    with db.get_pool().connection() as conn:
        tools.reconcile_catalog(conn)  # first run embeds each tool once
        first = len(calls)
        tools.reconcile_catalog(conn)  # unchanged — embeds nothing new
        assert len(calls) == first
        # A changed description re-embeds exactly that tool.
        conn.execute("UPDATE tool_catalog SET description = 'stale' WHERE name = 'schedule_reminder'")
        tools.reconcile_catalog(conn)
        assert len(calls) == first + 1


def test_reconcile_refills_a_missing_vector_so_a_model_swap_needs_no_backfill(client, monkeypatch):
    # A model swap repoints active_tool_embedding at a fresh, empty table; the descriptions don't change.
    # Reconcile must still refill the vector — otherwise the catalog would sit unsearchable until a
    # description happened to change. Simulate the swap by dropping the active vector, then reconcile.
    _fake_embed(monkeypatch)
    with db.get_pool().connection() as conn:
        tools.reconcile_catalog(conn)
        conn.execute("DELETE FROM active_tool_embedding")
        assert conn.execute("SELECT count(*) FROM active_tool_embedding").fetchone()[0] == 0
        tools.reconcile_catalog(conn)
        # The vector is back for every registered tool, with descriptions unchanged throughout.
        assert conn.execute("SELECT count(*) FROM active_tool_embedding").fetchone()[0] == len(tools.REGISTRY)


def test_reconcile_drops_a_catalog_row_for_an_unregistered_tool(client, monkeypatch):
    _fake_embed(monkeypatch)
    temp = tools.Tool(name="temp_tool", description="a throwaway", args_model=tools.BaseModel, executor=lambda *a: None)
    tools.register(temp)
    try:
        with db.get_pool().connection() as conn:
            tools.reconcile_catalog(conn)
            assert conn.execute("SELECT count(*) FROM tool_catalog WHERE name = 'temp_tool'").fetchone()[0] == 1
        del tools.REGISTRY["temp_tool"]
        with db.get_pool().connection() as conn:
            tools.reconcile_catalog(conn)
            assert conn.execute("SELECT count(*) FROM tool_catalog WHERE name = 'temp_tool'").fetchone()[0] == 0
    finally:
        tools.REGISTRY.pop("temp_tool", None)


def test_search_is_empty_and_cheap_on_an_empty_catalog(client, monkeypatch):
    # Nothing reconciled in: the gate returns nothing and never spends an embedding call to find that out.
    calls = []
    _fake_embed(monkeypatch, calls)
    with db.get_pool().connection() as conn:
        assert tools.search_catalog(conn, "remind me to call mum") == []
    assert calls == []


def test_search_surfaces_the_reminder_for_a_reminding_message(client, monkeypatch):
    _fake_embed(monkeypatch)
    with db.get_pool().connection() as conn:
        tools.reconcile_catalog(conn)
        candidates = tools.search_catalog(conn, "remind me to call the dentist tomorrow")
    names = [c.name for c in candidates]
    assert "schedule_reminder" in names


def test_decision_model_is_flat_with_tool_and_nullable_args():
    candidates = [tools.ToolCandidate("schedule_reminder", tools.REGISTRY["schedule_reminder"].description, 0.1)]
    model = tools._decision_model(candidates)
    fields = model.model_fields
    # A flat schema: the `tool` field plus every shortlisted tool's own argument fields, folded in.
    assert "tool" in fields
    assert "reminder_message" in fields and "fire_at" in fields
    # The arguments are nullable, so the model can name the tool yet leave one it couldn't read null.
    assert model(tool="schedule_reminder").reminder_message is None


def test_decide_names_the_tool_and_extracts_its_arguments(monkeypatch):
    fire_at = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        tools.llm, "generate_json",
        lambda prompt, schema, *, model=None: types.SimpleNamespace(
            tool="schedule_reminder", reminder_message="call the dentist", fire_at=fire_at
        ),
    )
    candidates = [tools.ToolCandidate("schedule_reminder", "…", 0.1)]
    decision = tools.decide("remind me to call the dentist at 9", candidates, [], fire_at, "UTC")
    assert decision.tool == "schedule_reminder"
    assert decision.args == {"reminder_message": "call the dentist", "fire_at": fire_at}


def test_decide_returns_no_tool_when_the_model_declines(monkeypatch):
    monkeypatch.setattr(
        tools.llm, "generate_json",
        lambda prompt, schema, *, model=None: types.SimpleNamespace(tool=tools.NO_TOOL),
    )
    candidates = [tools.ToolCandidate("schedule_reminder", "…", 0.1)]
    now = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
    decision = tools.decide("what's the weather like?", candidates, [], now, "UTC")
    assert decision.tool == tools.NO_TOOL
    assert decision.args == {}
