-- Intake, step 7 of the answer guarantee: the reply is only "truly out" once the shell confirms it showed it.
-- 'answered' means the worker produced the reply and it is durably readable through /answers;
-- delivered_at means the terminal that reply was emitted to has confirmed it displayed the outcome to the symbiot.
-- The honest counterpart, on the way back, of the outbox's COPY:
-- the kernel marks a reply delivered when it actually reached the human, never on a hopeful guess.
--
-- Nullable, null until the ack lands (POST /answers/delivered).
-- Only ever set on a terminal row (answered or abandoned — both are outcomes the shell renders).
-- The same shape as a missive's seen_at, so "the symbiot has seen this" reads one way across the whole schema.
-- A lost ack leaves it null: the signal errs only toward not-yet-delivered, never toward a delivery that didn't happen —
-- so delivered_at IS NOT NULL is always trustworthy.
ALTER TABLE intake
    ADD COLUMN delivered_at TIMESTAMPTZ;
