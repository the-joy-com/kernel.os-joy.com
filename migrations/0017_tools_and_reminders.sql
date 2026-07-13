-- Tool calling: the catalog of tools the symbiot can reach for, and the effect the first tool records.
--
-- Everything the loop did until now was speech. This is the seam where it acts.
-- A tool is four things joined by its name (see doc/tool-calling.md):
-- a name, a description, an argument schema, and a code executor.
-- The last two live in code (services/tools.py — the executor is a Python callable,
-- the source of truth for which tools exist);
-- the first three — the descriptor — come to rest here, as a searchable row,
-- so a message can be matched against the tools by meaning the same way it is matched against the diary.
-- The split is the point:
-- the model can only ever produce a *name*, and a name resolves to a callable we wrote,
-- so "code executes, never the model" is structural, not a promise.
--
-- The store follows the same governing split the ontology store rests on (migration 0010):
-- the text is durable and precious, the vectors are derived and disposable.
-- The descriptor's text lives in tool_catalog with no vector column near it;
-- the embedding lives decoupled, in a per-model table keyed back to it, reached through an active_* view —
-- so a model swap rebuilds the vectors without touching the text, exactly as it does for the ontology and diary sets.
-- Unlike those, the catalog is reconciled from code on every startup (tools.reconcile_catalog),
-- so its vectors are re-derived cheaply whenever a description changes,
-- or the active set lacks a tool's vector (a model swap) —
-- which is what lets a swap refill itself rather than needing an offline re-embed.

-- The searchable descriptor of each registered tool.
-- name is what the model emits when it chooses the tool, and the join to the code registry that runs it;
-- UNIQUE, because the name is the identity of the tool on both sides of the split.
-- description is the prose the recall matches a message against, and the decision call reads to judge fit —
-- embedded below, never carrying a vector column of its own.
-- Nothing references this row by id across restarts (the reminder below ties to the intake, not the tool),
-- so the catalog can be reconciled — rows inserted, updated, or dropped to match the code — with no fallout.
CREATE TABLE tool_catalog (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT        NOT NULL UNIQUE,
    description TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The decoupled per-model vector store for the catalog, named after the model that produced the vectors —
-- the same shape and the same reasons as ontology_embedding_nomic_embed_text (migration 0010):
-- one clean HNSW index over one comparable vector space, model_id stamping every row's provenance,
-- cascading if the descriptor it belongs to is ever removed.
CREATE TABLE tool_embedding_nomic_embed_text (
    tool_id   BIGINT      NOT NULL PRIMARY KEY REFERENCES tool_catalog (id) ON DELETE CASCADE,
    model_id  BIGINT      NOT NULL REFERENCES embedding_model (id),
    embedding vector(768) NOT NULL
);

CREATE INDEX tool_embedding_nomic_embed_text_hnsw
    ON tool_embedding_nomic_embed_text USING hnsw (embedding vector_cosine_ops);

-- The stable name the recall reads and the reconcile writes, always resolving to the active model's set —
-- so "which set is live" is one CREATE OR REPLACE VIEW away and no query names a versioned table.
CREATE VIEW active_tool_embedding AS
    SELECT tool_id, model_id, embedding FROM tool_embedding_nomic_embed_text;

-- ---------------------------------------------------------------------------------------
-- The first tool's effect: a one-shot reminder.
--
-- The registry's first and only inhabitant is schedule_reminder: at a future moment, say a stored line back.
-- It is the cleanest possible first action — no external driver, no third-party credential,
-- only a durable row here and the reply path already built —
-- so what is under test is the machinery of acting, not the plumbing of an integration.
--
-- intake_id is the message this reminder was scheduled from,
-- and its exactly-once pin against a retried message:
-- UNIQUE, so a triggering message re-run (a deadline bite, a crash) that fires the executor again
-- conflicts and writes nothing — the reminder already stands, and only the spoken confirmation is re-derived.
-- The same shape the enrichment pass and diary ingestion use to make an effect exactly-once in the database,
-- rather than by the loop being careful.
--
-- body is the line to say back when the time comes.
-- fire_at is the resolved moment, an absolute instant (TIMESTAMPTZ) computed in the symbiot's timezone at
-- schedule time (services/zone.py), so the due check compares two absolute instants and the summer-time shift
-- is already baked in.
-- fired_at is null until the reminder has been delivered, and stamped when it fires —
-- the exactly-once pin on the *firing* side
-- (the due sweep claims an unfired, due row and stamps it in the same transaction as the missive it raises,
-- so a crash re-fires nothing and a delivered reminder is never sent twice),
-- and the ledger of what fired and when
-- (preserve-don't-destroy: a fired reminder is recorded, not dropped).
CREATE TABLE reminder (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    intake_id  BIGINT      NOT NULL UNIQUE REFERENCES intake (id) ON DELETE CASCADE,
    symbiot_id BIGINT      NOT NULL REFERENCES symbiot (id) ON DELETE CASCADE,
    body       TEXT        NOT NULL,
    fire_at    TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    fired_at   TIMESTAMPTZ
);

-- The due sweep's read: the oldest unfired reminder whose moment has come.
-- A partial index on fire_at over only the unfired rows,
-- so the check is a cheap index scan that never walks the reminders already delivered —
-- the mirror of the WHERE fired_at IS NULL the sweep claims under.
CREATE INDEX reminder_due ON reminder (fire_at) WHERE fired_at IS NULL;
