"""The durable half of the model configuration:
seeding the two tables from code, and the /models command's writes.

Two tables sit behind this (migration 0019):
`model` is the catalog of models the kernel can talk to,
and `model_role` is which model plays each generative role.
This module owns every write to them —
the boot reconcile that seeds the builtins and the default assignments,
and the create / edit / delete / reassign the /models command drives.
The *reading* the generative path does at call time lives in adapters/models.py
(the resolver and its cache);
this module is the store that resolver reads, and the route that changes it.

The governing rules, all enforced here rather than left to the caller:
  - the builtin models are reconciled from code (adapters/models.BUILTIN_MODELS) on every boot,
    so their verified specs track the code
    and a newly-shipped builtin appears without a migration;
  - a role is seeded from its config default (BUILTIN_ROLES) only when it has no row yet,
    so an operator's reassignment is never overwritten by a later boot;
  - an operator may add / edit / delete their *own* models freely,
    but not touch a builtin's specs (a reconcile would only overwrite them)
    and not delete a model a role still points at (it would orphan the role);
  - a bare model name registered by the operator gets sensible defaults filled in,
    so "point reply at my local qwen" needs a name and nothing else.
"""

from services.adapters.models import BUILTIN_MODELS, BUILTIN_ROLES

# The roles the /models command lets the operator reassign —
# the keys of the default map, so the two never drift.
ASSIGNABLE_ROLES = tuple(BUILTIN_ROLES.keys())

# The sensible defaults filled in when an operator registers a model
# without spelling out its characteristics.
# A local model is the case this whole feature exists for,
# so an unnamed provider defaults to ollama;
# the window and output ceiling default to the same figures the builtin generative models carry
# (see BUILTIN_MODELS),
# generous enough to drive a capable local model well
# and safely clamped by the budget guard regardless.
_DEFAULT_PROVIDER = "ollama"
_DEFAULT_OPTIMAL_CONTEXT_TOKENS = 131072
_DEFAULT_MAX_OUTPUT_TOKENS = 16384


class ModelConfigError(Exception):
    """A /models write the store refuses — editing a builtin, deleting a model in use, an unknown role.

    Carries a human-legible reason the route surfaces to the shell as-is,
    so the operator learns *why* the change didn't take
    rather than that it silently didn't.
    """


def reconcile_and_seed(conn) -> None:
    """Bring the store in line with code at boot: upsert the builtin models, seed any unset role.

    Idempotent, so every startup can call it.
    The builtins are upserted by name and their specs updated to match the code —
    the code is the source of truth for a builtin's verified characteristics,
    and an operator cannot edit them,
    so overwriting here only ever corrects drift.
    Operator-added models (is_builtin FALSE) are never touched.
    Each role is then seeded from its config default,
    but only when it has no row yet
    and only when that default names a model the catalog actually carries,
    so the seed can never violate the foreign key
    and an operator's own assignment always stands.
    """
    for model in BUILTIN_MODELS.values():
        conn.execute(
            "INSERT INTO model (name, provider, optimal_context_tokens, max_output_tokens, is_builtin) "
            "VALUES (%s, %s, %s, %s, TRUE) "
            "ON CONFLICT (name) DO UPDATE SET "
            "provider = EXCLUDED.provider, "
            "optimal_context_tokens = EXCLUDED.optimal_context_tokens, "
            "max_output_tokens = EXCLUDED.max_output_tokens, "
            "is_builtin = TRUE",
            (model.name, model.provider, model.optimal_context_tokens, model.max_output_tokens),
        )
    for role, model_name in BUILTIN_ROLES.items():
        # Seed only when absent (DO NOTHING keeps an operator's choice)
        # and only when the default model exists,
        # so a role never points at a name the catalog doesn't carry.
        conn.execute(
            "INSERT INTO model_role (role, model_name) "
            "SELECT %s, %s WHERE EXISTS (SELECT 1 FROM model WHERE name = %s) "
            "ON CONFLICT (role) DO NOTHING",
            (role, model_name, model_name),
        )


def catalog(conn) -> list[dict]:
    """Every model in the catalog, builtin and operator-added, for the /models command's read.

    Ordered builtins-first then by name,
    so the list reads as "what ships, then what you added".
    Each row carries whether it's a builtin,
    so the shell can show which ones it may edit and which it may only assign.
    """
    rows = conn.execute(
        "SELECT name, provider, optimal_context_tokens, max_output_tokens, is_builtin "
        "FROM model ORDER BY is_builtin DESC, name"
    ).fetchall()
    return [
        {
            "name": name,
            "provider": provider,
            "optimal_context_tokens": optimal_context_tokens,
            "max_output_tokens": max_output_tokens,
            "is_builtin": is_builtin,
        }
        for (name, provider, optimal_context_tokens, max_output_tokens, is_builtin) in rows
    ]


def roles(conn) -> dict[str, str]:
    """Which model plays each role right now — role → model name, for the /models command's read."""
    return {role: model_name for (role, model_name) in conn.execute(
        "SELECT role, model_name FROM model_role ORDER BY role"
    ).fetchall()}


def upsert_model(
    conn,
    name: str,
    *,
    provider: str | None = None,
    optimal_context_tokens: int | None = None,
    max_output_tokens: int | None = None,
) -> None:
    """Register a new operator model, or edit an existing operator one — the /models add/edit write.

    A name and nothing else is enough:
    the provider, window, and output ceiling fall back to sensible defaults
    (a local model, a generous window, the builtin output cap)
    so registering a bare local model just works.
    Refuses to touch a builtin —
    its specs are code-owned and a reconcile would overwrite an edit anyway —
    so a builtin can be assigned to a role but never redefined.
    An operator row is upserted by name,
    so editing is just registering the same name again with new characteristics.
    """
    existing = conn.execute("SELECT is_builtin FROM model WHERE name = %s", (name,)).fetchone()
    if existing is not None and existing[0]:
        raise ModelConfigError(
            f"{name!r} is a builtin model — its characteristics are fixed in code; you can assign it to a role, but not edit it"
        )
    conn.execute(
        "INSERT INTO model (name, provider, optimal_context_tokens, max_output_tokens, is_builtin) "
        "VALUES (%s, %s, %s, %s, FALSE) "
        "ON CONFLICT (name) DO UPDATE SET "
        "provider = EXCLUDED.provider, "
        "optimal_context_tokens = EXCLUDED.optimal_context_tokens, "
        "max_output_tokens = EXCLUDED.max_output_tokens",
        (
            name,
            provider or _DEFAULT_PROVIDER,
            optimal_context_tokens or _DEFAULT_OPTIMAL_CONTEXT_TOKENS,
            max_output_tokens or _DEFAULT_MAX_OUTPUT_TOKENS,
        ),
    )


def delete_model(conn, name: str) -> None:
    """Remove an operator model — the /models delete write.

    Refuses a builtin (it belongs to the code, not the operator)
    and a model a role still points at (deleting it would orphan the role).
    The in-use check is explicit rather than left to the foreign key alone,
    so the refusal carries which roles are holding the model
    rather than a raw constraint error.
    """
    existing = conn.execute("SELECT is_builtin FROM model WHERE name = %s", (name,)).fetchone()
    if existing is None:
        raise ModelConfigError(f"no model named {name!r} to delete")
    if existing[0]:
        raise ModelConfigError(f"{name!r} is a builtin model — it ships with the kernel and can't be deleted")
    holders = [
        role for (role,) in conn.execute(
            "SELECT role FROM model_role WHERE model_name = %s ORDER BY role", (name,)
        ).fetchall()
    ]
    if holders:
        raise ModelConfigError(
            f"{name!r} is still assigned to {', '.join(holders)} — point those role(s) at another model first"
        )
    conn.execute("DELETE FROM model WHERE name = %s", (name,))


def set_role(conn, role: str, model_name: str) -> None:
    """Point a generative role at a model in the catalog — the /models reassign write.

    Refuses an unknown role (only the assignable roles are real)
    and a model not in the catalog
    (a role must resolve to a model the kernel can actually talk to).
    The write is a plain upsert on the role,
    so a role only ever holds one standing assignment.
    """
    if role not in ASSIGNABLE_ROLES:
        raise ModelConfigError(
            f"{role!r} isn't a role you can set — the roles are: {', '.join(ASSIGNABLE_ROLES)}"
        )
    if conn.execute("SELECT 1 FROM model WHERE name = %s", (model_name,)).fetchone() is None:
        raise ModelConfigError(
            f"no model named {model_name!r} in the catalog — register it first, then assign it"
        )
    conn.execute(
        "INSERT INTO model_role (role, model_name) VALUES (%s, %s) "
        "ON CONFLICT (role) DO UPDATE SET model_name = EXCLUDED.model_name",
        (role, model_name),
    )
