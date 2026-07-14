-- Model configuration: the models the kernel can talk to, and which one does which job.
--
-- Until now both of these were compile-time constants:
-- the catalog of models lived as a hardcoded dict in services/adapters/models.py,
-- and the assignment of a model to a job lived as a scatter of config constants (REPLY_MODEL, RERANK_MODEL, and the rest).
-- That was fine for a box wired to the cloud providers it shipped knowing,
-- and wrong for a box that has none of them — a friend's home server with nothing but a local Ollama.
-- So both become durable, operator-editable state (see the /models command in main.py),
-- the way the timezone and the notification switches already are —
-- but global, box-level, not per-symbiot: a model is a property of the machine and the Ollama it can reach,
-- not of a person's perception of their own day.
--
-- Two tables, because "the model config" is two separate things.
-- `model` is the catalog: the set of models the kernel knows how to talk to, each with the characteristics it must be driven by.
-- `model_role` is the assignment: which model, out of that catalog, plays each generative role.
-- The split lets a role point at a catalog entry by name,
-- so assigning one model to two roles stores its specs once, in one place.
--
-- Neither is seeded here.
-- Both are reconciled from code at boot (services/memory/model_config.py, reconcile_and_seed),
-- the same idiom the tool catalog uses:
-- the builtin models are upserted from the seed in adapters/models.py (the source of truth for their verified specs),
-- and each role is seeded from its config default (which honours an existing .env override) only when it has no row yet.
-- So a fresh box comes up behaving exactly as it did before these tables existed,
-- and an operator's own edits are never trampled by a later boot.

-- The catalog: one row per model the kernel can talk to.
-- name is the exact id the provider answers to, and the join a role points at —
-- PRIMARY KEY, since the name is the model's identity on both sides (the provider call and the role assignment).
-- provider is what the generative boundary dispatches on ('scaleway', 'mistral', 'ollama') —
-- kept as free TEXT rather than a CHECK, because the set of providers lives in code (llm._call),
-- so adding one is a code change, not a migration to widen a constraint.
-- optimal_context_tokens is the window the model reads *well* (deliberately below its advertised maximum),
-- the budget the context guard holds a prompt to;
-- max_output_tokens is the ceiling on a single reply.
-- Both are the characteristics the kernel needs to drive the model,
-- filled with sensible defaults when an operator adds a bare model name (see model_config.upsert_model).
-- is_builtin marks the rows reconciled from code,
-- so the boot reconcile updates their specs to match the code while leaving an operator's own added models (is_builtin FALSE) untouched,
-- and the /models command refuses to edit a builtin's specs (a reconcile would only overwrite them).
CREATE TABLE model (
    name                   TEXT        NOT NULL PRIMARY KEY,
    provider               TEXT        NOT NULL,
    optimal_context_tokens INTEGER     NOT NULL,
    max_output_tokens      INTEGER     NOT NULL,
    is_builtin             BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The assignment: one row per generative role, naming the catalog model that plays it.
-- role is the stable slug the kernel resolves against (the live set is BUILTIN_ROLES in adapters/models.py — 'reply', 'rerank', 'mint', 'enrich', and the rest) —
-- PRIMARY KEY, so a role holds exactly one standing assignment and the write is a plain upsert.
-- model_name references the catalog:
-- ON DELETE RESTRICT so a model a role still points at cannot be deleted out from under it
-- (the /models command surfaces that as a refusal rather than orphaning a role),
-- and ON UPDATE CASCADE so a catalog rename carries its assignments with it.
CREATE TABLE model_role (
    role       TEXT        NOT NULL PRIMARY KEY,
    model_name TEXT        NOT NULL REFERENCES model (name) ON UPDATE CASCADE ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
