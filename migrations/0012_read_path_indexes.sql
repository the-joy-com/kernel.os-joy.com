-- The read path's indexes over the diary facts: the fast lexical reach, and the effective-time order.
--
-- The write path (migration 0010) laid down the durable text and the vectors derived from it,
-- under one governing idea: the text is the precious, durable thing; everything computed from it is disposable.
-- These indexes extend that idea rather than break it.
-- Nothing here adds a column of derived data beside the raw text —
-- no stored tsvector, no materialised effective time —
-- because a stored column is a second source of truth that can drift from the words it was computed from.
-- They are all *expression* indexes:
-- the search form and the effective time are computed on the fly from the columns that already exist,
-- and the index is only a fast path to that computation, droppable and rebuildable at will without ever touching a fact.
-- So the same split holds one level out:
-- raw_text and the two clocks are the truth; these indexes are merely how the read path reaches them quickly.

-- ---------------------------------------------------------------------------------------
-- pg_trgm: trigram matching, for the fuzzy half of the lexical reach.
--
-- Full-text search matches whole lexemes: it finds "boxing" for "box", but not "Strasborg" for "Strasbourg" —
-- a typo or a half-remembered spelling produces a different lexeme and simply misses.
-- pg_trgm measures similarity by the three-letter fragments two strings share, so a near-miss still scores high,
-- and its `%` operator (similarity above a threshold) is what lets the retrieval catch the words FTS lets slip.
-- IF NOT EXISTS so a database that already has the extension (a shared cluster, a re-run) is left alone.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------------------
-- The full-text indexes: raw_text turned into a searchable, ranked form — in both languages the diary is lived in.
--
-- Full-text search doesn't match raw strings; it matches *words*.
-- to_tsvector(<language>, raw_text) is what does the turning:
-- it breaks a fact's text into its meaningful words, folds each to a root ("running" and "ran" both become "run"),
-- and drops noise words like "the" and "a" —
-- so a search matches on the words a fact is about, not on its exact spelling.
-- A GIN index is an *inverted* index: for every word, it keeps the list of facts that contain it,
-- so a query jumps straight to the handful of facts that share a word with it, instead of reading every row to check.
-- That is what keeps the search fast as the diary grows.
--
-- These are *expression* indexes, built on the computed to_tsvector(...) rather than on a stored column —
-- keeping the schema's rule that the text is the one durable thing and everything derived from it stays droppable.
-- The catch of an expression index: Postgres only reaches for it when a query computes the *identical* expression,
-- so the retrieval query must spell to_tsvector('english', raw_text) — and to_tsvector('french', raw_text) — exactly as here.
--
-- Why two, one per language: the word-folding and the noise-word list are language-specific.
-- The symbiot lives mostly in English but slips into French for the emotive entries — the ones a reply can least afford to miss —
-- and an English analyser cannot fold French ("énervé" and "énerver" would stay strangers) or strip French noise words.
-- So raw_text is indexed under both analysers, and the retrieval query matches either,
-- so each language's entries fold and rank on their own proper rules rather than one language being read through the other's.
-- (Trigram, below, is language-agnostic and catches what both analysers miss — a typo, a word neither stems alike.)
CREATE INDEX diary_facts_raw_text_fts_en
    ON diary_facts USING gin (to_tsvector('english', raw_text));

CREATE INDEX diary_facts_raw_text_fts_fr
    ON diary_facts USING gin (to_tsvector('french', raw_text));

-- The trigram index: the fuzzy companion to the FTS index above.
-- gin_trgm_ops indexes raw_text by its trigrams,
-- so the `%` similarity operator (and similarity() ranking) runs against the index
-- instead of comparing the query to every fact's trigrams from scratch.
CREATE INDEX diary_facts_raw_text_trgm
    ON diary_facts USING gin (raw_text gin_trgm_ops);

-- ---------------------------------------------------------------------------------------
-- The effective-time index: the order the read path reasons on.
--
-- A fact's effective time is when it happened if it said so, otherwise when it was told:
-- COALESCE(happened_at, created_at), the collapse the write path deliberately left for the reader (migration 0011).
-- The write path filled happened_at honestly (null when the fact named no moment) and stopped there;
-- standing created_at in for that null, and the index that keeps sorting on the result cheap, are the read path's,
-- and this is where that index lands. A btree, because effective time is ordered and range-queried —
-- "the most recent," "since last week," "in order" — not matched for equality.
-- An expression index again: nothing stores the collapsed time, it is computed from the two clocks per row,
-- and this index is only the fast path to that computation.
CREATE INDEX diary_facts_effective_at
    ON diary_facts (COALESCE(happened_at, created_at));
