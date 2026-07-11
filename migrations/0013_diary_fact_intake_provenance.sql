-- The provenance link from a filed fact back to the message it was made from,
-- and the guarantee that live ingestion files each message exactly once.
--
-- The read path (migration 0012) can answer off the diary,
-- but until now the diary filled only by a by-hand script.
-- Live ingestion closes that: a background sweep distils each of the symbiot's messages into a diary fact as it is answered.
-- That sweep needs one thing the schema can give it and timing cannot —
-- a way to know a message has already been filed, one that survives a crash mid-file or a retry of the sweep.
--
-- intake_id is that link: the message this fact was distilled from.
-- It is nullable, because not every fact comes from an intake message —
-- the by-hand ingestion smokes, and any future fact filed from elsewhere, carry no message id and store NULL.
-- ON DELETE SET NULL rather than CASCADE: a diary fact is the durable, precious thing (the whole store rests on that),
-- so if a message row were ever removed the fact it produced outlives it, losing only the back-reference.
-- (Intake rows are in fact never deleted — the diary of record is immutable — so this is a principled stance, not a live path.)
ALTER TABLE diary_facts
    ADD COLUMN intake_id BIGINT REFERENCES intake (id) ON DELETE SET NULL;

-- The exactly-once guarantee, enforced by the database rather than by the sweep being careful.
-- A UNIQUE index means one message can back at most one fact:
-- a sweep that crashed after filing, but before it could record the fact as done, simply re-files,
-- hits this constraint, and the second write is refused —
-- so an interrupted or double-run sweep can never leave a duplicate.
-- Postgres treats NULLs as distinct in a unique index,
-- so the many NULL intake_id rows (the by-hand facts) never collide with each other.
-- The sweep's eligibility is the mirror of this index — a message with no fact yet bearing its id —
-- so a drop (a message never filed) leaves the message eligible and simply picked up on the next pass.
-- No drop, no double.
CREATE UNIQUE INDEX diary_facts_intake_id ON diary_facts (intake_id);
