-- The reminder's lifecycle: a reminder can be typed in directly, and it can be taken back.
--
-- Migration 0017 made a reminder something the machine schedules from a message and then fires.
-- Nothing could read the standing set, nothing could edit it, and nothing could call one off,
-- so the lifecycle was one-way: born from a sentence, delivered, done.
-- Two things in the schema said no outright, and both are lifted here.
--
-- Nothing is dropped or rewritten: a fired reminder stays the ledger of what fired,
-- and a cancelled one becomes the ledger of what was called off —
-- preserve-don't-destroy, the same stance fired_at already takes.

-- 1. A reminder no longer has to have been born from something the symbiot said.
--
-- intake_id is the exactly-once pin against a retried message,
-- which is why it was NOT NULL: every reminder came from a message, so every reminder had one.
-- A reminder typed straight into the shell (the /reminders command) has no message behind it,
-- and under NOT NULL it was structurally impossible to store.
-- Dropping the constraint keeps the pin intact for the reminders that have one:
-- Postgres treats nulls as distinct under a UNIQUE index,
-- so many directly-made reminders can each carry no intake_id
-- while a tool-made one still conflicts with its own retry and writes nothing.
-- So: a tool-made reminder keeps its pin, a directly-made one simply carries none.
ALTER TABLE reminder ALTER COLUMN intake_id DROP NOT NULL;

-- 2. A reminder can be called off.
--
-- Cancelling stamps a time rather than deleting the row —
-- the shape 0017's own comment already settled for firing, applied to the other ending:
-- a reminder called off is recorded, not dropped,
-- so the audit surface can show it and the symbiot can see they called it off rather than finding a gap.
-- Null is the ordinary state; stamped means it will never fire.
ALTER TABLE reminder ADD COLUMN cancelled_at TIMESTAMPTZ;

-- The firing sweep's read has to learn about the new state, or a reminder called off still goes off.
-- The partial index is the mirror of the WHERE the sweep claims under (reminder.claim_due),
-- so both move together: unfired *and* uncancelled is what "due" now means.
DROP INDEX reminder_due;
CREATE INDEX reminder_due ON reminder (fire_at) WHERE fired_at IS NULL AND cancelled_at IS NULL;

-- The standing set's read: one symbiot's pending reminders, soonest first.
-- The shell's listing walks it, the plain-language read windows it,
-- and the deduplication recall looks inside a window of it —
-- three readers over the same narrow slice, which is why it earns an index of its own
-- rather than riding the sweep's global one (that one is keyed on fire_at alone, across every symbiot).
CREATE INDEX reminder_standing ON reminder (symbiot_id, fire_at)
    WHERE fired_at IS NULL AND cancelled_at IS NULL;
