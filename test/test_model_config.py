"""Model configuration: the durable catalog and role assignments, the resolver over them, and the /models command.

The whole point of this rung is the fully-local box: an operator with no cloud provider points the generative
roles at a model their own Ollama serves, through a command rather than a code change. So what these pin is
that contract — the builtins are seeded and reconciled from code, an operator can register / edit / delete
their own models and reassign a role, the store enforces its rules (a builtin is not editable or deletable, a
model in use is not deletable, a role and its model must be real), and the resolver the generative path reads
reflects a change once it's made.

The route is authed-gated like /timezone and /notifications, and box-level (one config per kernel), so these
drive it through a real session the way the shell does.
"""

from services.adapters import models
from services.memory import model_config
from conftest import SYMBIOT_EMAIL, extract_code


def _token(client, fake_email, address=SYMBIOT_EMAIL) -> str:
    # Walk the real login flow to a session token, the way the shell does.
    client.post("/login", json={"address": address})
    code = extract_code(fake_email)
    return client.post(
        "/login/verify", json={"address": address, "code": code}
    ).json()["data"]["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- the store: reconcile, seed, and the resolver over them ------------------------------------

def test_reconcile_seeds_the_builtins_and_the_default_roles(client):
    # A fresh boot leaves the catalog carrying every builtin and each role assigned its config default,
    # so the box behaves exactly as it did before these tables existed.
    with db_conn() as conn:
        catalog = {m["name"]: m for m in model_config.catalog(conn)}
        roles = model_config.roles(conn)
    for name in models.BUILTIN_MODELS:
        assert name in catalog and catalog[name]["is_builtin"] is True
    # Every assignable role is seeded, and to a real catalog model.
    for role in model_config.ASSIGNABLE_ROLES:
        assert roles[role] in catalog


def test_the_resolver_reads_the_seeded_assignment(client):
    # role_name resolves a role to its assigned model, and for_role hands back that model's characteristics —
    # the reply role lands on the seeded default, and its spec carries the builtin's window.
    assert models.role_name("reply") == models.BUILTIN_ROLES["reply"]
    spec = models.for_role("reply")
    assert spec.optimal_context_tokens == models.BUILTIN_MODELS[models.BUILTIN_ROLES["reply"]].optimal_context_tokens


def test_register_fills_sensible_defaults_for_a_bare_name(client):
    # The local-box case: register a model with only a name, and the missing characteristics fall back to
    # sensible local defaults — a local provider, a generous window, the builtin output cap.
    with db_conn() as conn:
        model_config.upsert_model(conn, "llama3.2:3b")
        row = next(m for m in model_config.catalog(conn) if m["name"] == "llama3.2:3b")
    assert row["provider"] == "ollama"
    assert row["optimal_context_tokens"] > 0
    assert row["max_output_tokens"] > 0
    assert row["is_builtin"] is False


def test_register_then_edit_updates_the_operator_model(client):
    with db_conn() as conn:
        model_config.upsert_model(conn, "llama3.2:3b", optimal_context_tokens=8192)
        model_config.upsert_model(conn, "llama3.2:3b", optimal_context_tokens=16384)
        row = next(m for m in model_config.catalog(conn) if m["name"] == "llama3.2:3b")
    assert row["optimal_context_tokens"] == 16384  # the newest registration stands


def test_a_builtin_cannot_be_edited(client):
    with db_conn() as conn:
        try:
            model_config.upsert_model(conn, "glm-5.2", optimal_context_tokens=1)
            assert False, "editing a builtin should have been refused"
        except model_config.ModelConfigError:
            pass


def test_a_builtin_cannot_be_deleted(client):
    with db_conn() as conn:
        try:
            model_config.delete_model(conn, "glm-5.2")
            assert False, "deleting a builtin should have been refused"
        except model_config.ModelConfigError:
            pass


def test_a_model_in_use_cannot_be_deleted(client):
    with db_conn() as conn:
        model_config.upsert_model(conn, "llama3.2:3b")
        model_config.set_role(conn, "reply", "llama3.2:3b")
        try:
            model_config.delete_model(conn, "llama3.2:3b")
            assert False, "deleting a model a role still points at should have been refused"
        except model_config.ModelConfigError as exc:
            assert "reply" in str(exc)  # the refusal names the role holding it


def test_delete_frees_a_model_once_no_role_points_at_it(client):
    with db_conn() as conn:
        model_config.upsert_model(conn, "llama3.2:3b")
        model_config.set_role(conn, "reply", "llama3.2:3b")
        # point reply back at a builtin, then the operator model is free to delete
        model_config.set_role(conn, "reply", "glm-5.2")
        model_config.delete_model(conn, "llama3.2:3b")
        assert all(m["name"] != "llama3.2:3b" for m in model_config.catalog(conn))


def test_assign_refuses_an_unknown_role_or_model(client):
    with db_conn() as conn:
        try:
            model_config.set_role(conn, "not_a_role", "glm-5.2")
            assert False, "an unknown role should have been refused"
        except model_config.ModelConfigError:
            pass
        try:
            model_config.set_role(conn, "reply", "no-such-model")
            assert False, "assigning a model not in the catalog should have been refused"
        except model_config.ModelConfigError:
            pass


# --- the route: /models read, write, and refusal ------------------------------------------------

def test_read_models_is_authed_only(client):
    unauthed = client.get("/models").json()
    assert unauthed["data"]["authed"] is False


def test_read_models_returns_the_full_state(client, fake_email):
    token = _token(client, fake_email)
    body = client.get("/models", headers=_auth(token)).json()
    data = body["data"]
    assert {m["name"] for m in data["catalog"]} >= set(models.BUILTIN_MODELS)
    assert set(data["roles"]) == set(models.BUILTIN_ROLES)
    assert "reply" in data["assignable_roles"]


def test_register_and_assign_through_the_route_takes_effect_in_the_resolver(client, fake_email):
    # The end the whole feature exists for: register a local model and point the reply role at it through the
    # command, and the resolver the reply path reads reflects it — no code change, no restart.
    token = _token(client, fake_email)
    client.post("/models", json={"action": "register", "name": "qwen2.5:7b", "provider": "ollama"}, headers=_auth(token))
    body = client.post(
        "/models", json={"action": "assign", "role": "reply", "model": "qwen2.5:7b"}, headers=_auth(token)
    ).json()
    assert body["data"]["roles"]["reply"] == "qwen2.5:7b"
    # The route refreshed the resolver cache, so the generative path now resolves the reply role to the local model.
    assert models.role_name("reply") == "qwen2.5:7b"


def test_the_resolver_lazy_loads_from_the_store_when_its_cache_is_cold(client, fake_email):
    # The crux of the spawned-child path: a reply is composed in a fresh process that inherits none of the
    # parent's memory, so its first resolution can't read a warm cache — it reads the store itself, through a
    # direct connection (config.DATABASE_URL). Simulate that cold start by clearing the cache, then resolve:
    # it must load the assignment back from the store rather than fall to a stale default.
    token = _token(client, fake_email)
    client.post("/models", json={"action": "register", "name": "qwen2.5:7b"}, headers=_auth(token))
    client.post("/models", json={"action": "assign", "role": "reply", "model": "qwen2.5:7b"}, headers=_auth(token))
    # Cold start: drop the warm cache the way a spawned child begins with none.
    models._catalog = None
    models._roles = None
    assert models.role_name("reply") == "qwen2.5:7b"  # lazy-loaded from the store, not a warm cache
    assert models.for_role("reply").provider == "ollama"  # and its characteristics came with it


def test_the_route_surfaces_a_refusal_with_its_reason(client, fake_email):
    token = _token(client, fake_email)
    body = client.post(
        "/models", json={"action": "register", "name": "glm-5.2", "optimal_context_tokens": 1}, headers=_auth(token)
    ).json()
    from core import protocol

    assert body["msg"] == protocol.MODEL_REFUSED
    assert "builtin" in body["data"]["reason"]
    # And the refused change didn't land: glm-5.2 still carries its real builtin window, not the 1 we tried.
    glm = next(m for m in body["data"]["catalog"] if m["name"] == "glm-5.2")
    assert glm["optimal_context_tokens"] == models.BUILTIN_MODELS["glm-5.2"].optimal_context_tokens


# --- a small local helper so the store tests read against a real connection ---------------------

def db_conn():
    from core import db

    return db.get_pool().connection()
