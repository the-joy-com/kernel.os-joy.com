-- The ontology and diary store:
-- where unstructured facts get filed against a structured picture of the World,
-- and where the embeddings that route them live.
--
-- The governing idea of this schema is a split the whole design rests on:
-- the *text* is the durable, precious thing; the *vectors* are derived and disposable.
-- A type's definition, an entity's label and description, a diary fact's raw words —
-- that text is model-agnostic and never depends on any embedding.
-- The vectors are merely computed from it,
-- and any embedding model can be swapped for a better one tomorrow.
-- So the durable text lives in its own tables (schema_ontology, diary_facts),
-- with no vector column anywhere near it,
-- and every vector lives decoupled, in per-model storage that records which model produced it.
-- Switching models never touches the text and never alters an existing vector in place —
-- it builds a fresh set alongside the old and flips a pointer. See the embedding_model block below.

-- ---------------------------------------------------------------------------------------
-- Durable text 1: the ontology — the vocabulary a fact can be filed against.
--
-- The vocabulary is not poured in from the outside; it is grown from use.
-- When the router meets a fact it recalls the nearest existing types by vector distance,
-- then a generative re-ranker — not the raw distance — decides whether any of them truly fits;
-- only when none does does the model coin a new type, embed it, and file the fact against it.
-- The distance can't be the judge because it conflates topic with category:
-- a sprint and a marathon sit close in vector space and are still different kinds of thing,
-- so the match-or-mint call belongs to the re-ranker,
-- with a fast yes/no LLM check settling the grey zone.
-- So the store starts empty and fills with exactly the concepts this diary has actually needed,
-- in the order it needed them, never the whole World up front.
-- That logic lives in the app; this table is only where the result comes to rest.
--
-- type_name is the human-readable label;
-- definition is the text that gets embedded and that every later recall is measured against.
-- No embedding column here — the vector lives decoupled below, keyed back to this row.
-- type_name is UNIQUE only as a cheap backstop against an exact-label collision;
-- the real duplicate control is the re-ranker at mint time and the merge pass described below.
--
-- parent_id is the sub-type edge that keeps the ontology from going flat.
-- A type is never coined in a vacuum:
-- the model is handed the closest rejected candidates
-- and asked whether the new type is a specialisation of one of them,
-- so "boxing_session" is minted as a child of "workout_action" rather than a stranger beside it.
-- It is self-referential and nullable — a NULL parent is a root type — and ON DELETE SET NULL,
-- because losing a hierarchy edge is recoverable where losing the type itself is not.
--
-- merged_into is the tombstone that makes offline garbage collection safe.
-- Forward-only lazy minting will spawn semantic duplicates over time
-- (workout_action coined on Tuesday, training_session on Friday);
-- rather than paralyse the write path chasing a perfect threshold,
-- we accept the entropy and let an asynchronous job cluster the vectors,
-- pick a survivor, and collapse the rest into it.
-- A collapsed type is not deleted — it stays as a redirect (merged_into points at the survivor),
-- so any lingering reference still resolves,
-- while the merge pass drops its vector so recall stops offering it.
-- A live type has merged_into NULL.
CREATE TABLE schema_ontology (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    type_name   TEXT        NOT NULL UNIQUE,
    definition  TEXT        NOT NULL,
    parent_id   BIGINT      REFERENCES schema_ontology (id) ON DELETE SET NULL,
    merged_into BIGINT      REFERENCES schema_ontology (id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- parent_id is walked both ways:
-- down from a parent to its children,
-- and by the merge pass when it re-points a collapsed type's children onto the survivor,
-- so it earns its own index.
CREATE INDEX schema_ontology_parent_id ON schema_ontology (parent_id);

-- ---------------------------------------------------------------------------------------
-- Durable text 2: the diary facts — each unstructured entry, kept whole.
--
-- raw_text is the symbiot's words as they arrived, kept verbatim like the intake diary —
-- it is the durable truth this row is built on, and the thing every vector is re-derivable from.
-- payload is the LLM's JSON-LD rendering of the fact, stored as JSONB,
-- so a filed day can be read back with plain Postgres operators.
-- The concepts a fact is filed under are deliberately not a column here:
-- a fact is a bundle of concepts, not a single category —
-- "boxing session with my friend Jeremy during the heat wave"
-- is at once a boxing_session, a friends fact, and a heat_wave fact,
-- so the fact-to-concept link is many-to-many and lives in diary_fact_ontology below.
-- No embedding column here either — the fact's embedding lives in the decoupled store below,
-- keyed back to this row.
CREATE TABLE diary_facts (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    raw_text    TEXT        NOT NULL,
    payload     JSONB       NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A GIN index over the JSONB payload.
-- A generalised inverted index is what lets Postgres look *inside* a JSON document
-- rather than treating it as one opaque blob:
-- it indexes each key and value within the payload,
-- so a question that reaches into the JSON-LD properties —
-- a duration, a place, a participant that never earned a column of its own —
-- stays fast instead of scanning every fact.
CREATE INDEX diary_facts_payload_gin ON diary_facts USING gin (payload);

-- ---------------------------------------------------------------------------------------
-- The fact-to-concept links: which concepts each fact is filed under.
--
-- This is the many-to-many that lets one fact be several things at once.
-- Each row is one concept a fact was filed under,
-- and each link is a full routing result in its own right:
-- every concept the fact touches runs the same recall, re-rank, and mint-if-new path,
-- so "boxing session with my friend Jeremy during the heat wave"
-- ends up with three rows here — boxing_session, friends, heat_wave — sharing one diary_fact_id.
-- The links are flat tags: a fact is equally all of its concepts, with no primary-versus-context rank.
-- The specific individual (Jeremy) still lives in the fact's payload;
-- what is filed here is the concept (friends), which every other fact about friends reuses.
--
-- ON DELETE CASCADE on the fact side: drop a fact and its concept links go with it.
-- ON DELETE RESTRICT on the concept side is the promise diary_facts used to make itself:
-- a type still carrying links cannot be deleted out from under them; the merge pass re-points first.
-- The composite primary key is also the dedup — a fact cannot be tagged with the same concept twice.
--
-- The merge pass re-points here now instead of on diary_facts,
-- and it has to be idempotent: when a fact is already linked to the survivor and also to the loser,
-- a blind re-point would collide with the primary key,
-- so the pass re-points where the survivor link is absent and drops the loser link otherwise.
CREATE TABLE diary_fact_ontology (
    diary_fact_id BIGINT NOT NULL REFERENCES diary_facts (id)     ON DELETE CASCADE,
    ontology_id   BIGINT NOT NULL REFERENCES schema_ontology (id) ON DELETE RESTRICT,
    PRIMARY KEY (diary_fact_id, ontology_id)
);

-- An index on the concept side of the link.
-- The primary key already indexes (diary_fact_id, ontology_id) for "every concept of this fact",
-- but "every fact of this concept" — and the merge pass's sweep off a collapsing type —
-- reads by ontology_id, which the primary key's leading column cannot serve.
CREATE INDEX diary_fact_ontology_ontology_id ON diary_fact_ontology (ontology_id);

-- ---------------------------------------------------------------------------------------
-- The embedding-model registry: the pointer that makes the model swappable.
--
-- Embeddings from different models are not comparable —
-- they live in different vector spaces and often have different dimensions,
-- so a distance measured between two models' vectors is meaningless,
-- and a fixed-dimension `vector` column can't even hold the new model's output beside the old.
-- This table is how we treat that as a first-class, non-destructive operation,
-- instead of a migration to fear.
-- Each row is one embedding model we've used or are using:
-- its name (e.g. 'nomic-embed-text'),
-- its version (so a re-pulled model with changed weights is a new row, not a silent overwrite),
-- and the dimension it outputs (which fixes the vector(N) type of that model's storage tables).
-- is_active is the pointer retrieval reads:
-- exactly one model is active at a time,
-- and a swap is "populate the new model's tables, then flip this flag" — never an ALTER,
-- never an in-place change to existing vectors.
CREATE TABLE embedding_model (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       TEXT        NOT NULL,
    version    TEXT        NOT NULL,
    dimension  INT         NOT NULL,
    is_active  BOOLEAN     NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);

-- At most one active model, enforced by the database rather than by callers being careful.
-- Every row the index includes has the same key (is_active = true),
-- so uniqueness collapses it to a single row —
-- the same partial-unique idiom the login schema uses for "one live code".
CREATE UNIQUE INDEX embedding_model_one_active
    ON embedding_model (is_active)
    WHERE is_active;

-- The first model: nomic-embed-text, 768-dimensional, active from the first boot.
-- Its version tracks the model's own semantic version;
-- when the exact Ollama digest is pinned it can be recorded here as a new row and cut over to.
-- This seed is what makes the store usable out of the box —
-- the vector tables below are typed to its 768 dimensions.
INSERT INTO embedding_model (name, version, dimension, is_active)
VALUES ('nomic-embed-text', 'v1.5', 768, true);

-- ---------------------------------------------------------------------------------------
-- Decoupled vector storage, one set of tables per model, named after the model.
--
-- The suffix names the model that produced the vectors (_nomic_embed_text here) —
-- not a version number, and deliberately not the dimension.
-- Naming by dimension was the tempting shortcut:
-- a `vector` column is fixed-dimension and HNSW can only index a fixed dimension,
-- so at first glance the dimension is what forces the split.
-- But two different models can share a dimension (many are 768),
-- and if they shared one table they would share one HNSW index —
-- a single graph whose edges assume every vector is comparable.
-- Across two models that is false:
-- a distance between a nomic vector and another model's vector is meaningless,
-- so half the graph's links would be garbage.
-- Search would still return the right rows (the active_* views filter by model_id),
-- but it would be a filtered scan over a polluted graph — degraded recall, and slower —
-- precisely during a model swap, the one moment this whole design exists to keep clean.
-- So each model gets its own table and therefore its own clean HNSW index,
-- built over one vector space only.
-- model_id still stamps every row with the model that produced it:
-- it is the provenance that lets a set be proven homogeneous,
-- and that catches a half-finished re-embed (mixed model_id) before it can corrupt a search.
--
-- Adopting a second model is a later migration:
-- it creates its own tables (e.g. _bge at that model's dimension), a re-embed that fills them,
-- a repoint of the views below, and a flip of the is_active flag above.
-- The _nomic_embed_text tables stay queryable the whole time and are dropped only once the new set is trusted.
-- No downtime, no mutation of what already exists.
--
-- The true boundary the suffix stands for is one comparable vector space, which is model *and* version:
-- a re-pulled nomic with changed weights is a new vector space even at the same 768 dimensions,
-- so it earns its own suffixed set (e.g. _nomic_embed_text_v2) and is adopted exactly like a different model would be —
-- the family name here is just today's shorthand for the single version this store has ever held.
--
-- ontology vectors are what the router searches to recall a fact's nearest candidate types;
-- the re-ranker, not this distance, then decides whether one truly fits or a new type is coined,
-- so this search is the wide recall pass at the front of the vocabulary-growth path.
-- A merged type's vector is dropped by the garbage-collection pass,
-- so recall stops offering it even though its text row lingers as a redirect.
-- diary-fact vectors are stored for persistence and for later "facts like this one" search.
-- Both are keyed one-to-one back to their durable source row and cascade if it's ever removed.
CREATE TABLE ontology_embedding_nomic_embed_text (
    ontology_id BIGINT      NOT NULL PRIMARY KEY REFERENCES schema_ontology (id) ON DELETE CASCADE,
    model_id    BIGINT      NOT NULL REFERENCES embedding_model (id),
    embedding   vector(768) NOT NULL
);

CREATE TABLE diary_fact_embedding_nomic_embed_text (
    diary_fact_id BIGINT      NOT NULL PRIMARY KEY REFERENCES diary_facts (id) ON DELETE CASCADE,
    model_id      BIGINT      NOT NULL REFERENCES embedding_model (id),
    embedding     vector(768) NOT NULL
);

-- These indexes are what make recall fast: instead of comparing a query vector against every
-- row, HNSW walks a graph to find the closest ones in roughly constant time, trading a little
-- accuracy for a lot of speed. vector_cosine_ops tells the index to measure closeness the same
-- way recall does — by cosine distance — so the index and the query agree on what "nearest" means.
-- Built on the concrete per-model tables so the pass-through views below inherit them.
CREATE INDEX ontology_embedding_nomic_embed_text_hnsw
    ON ontology_embedding_nomic_embed_text USING hnsw (embedding vector_cosine_ops);

CREATE INDEX diary_fact_embedding_nomic_embed_text_hnsw
    ON diary_fact_embedding_nomic_embed_text USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------------------
-- The storage-side pointer: stable view names that always resolve to the active set.
--
-- Retrieval and persistence never name a versioned table directly —
-- they read and write these views,
-- so "which set is live" is one CREATE OR REPLACE VIEW away,
-- and no query has to change on a model swap.
-- A plain pass-through view is transparent to the planner,
-- so a distance search through active_ontology_embedding still uses the underlying HNSW index.
CREATE VIEW active_ontology_embedding AS
    SELECT ontology_id, model_id, embedding FROM ontology_embedding_nomic_embed_text;

CREATE VIEW active_diary_fact_embedding AS
    SELECT diary_fact_id, model_id, embedding FROM diary_fact_embedding_nomic_embed_text;
