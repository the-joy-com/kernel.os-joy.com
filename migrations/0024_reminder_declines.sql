-- The decision that would otherwise have left no trace: a reminder the machine refused to set twice.
--
-- Everything else the reminders do leaves a mark.
-- Scheduling writes a row, cancelling stamps it, firing stamps it, and all of it surfaces under /observe.
-- Deduplication writes nothing — that is its entire job —
-- so the first decision the machine takes on its own judgment rather than on the symbiot's words
-- would be the one decision that leaves no trace, which is backwards.
--
-- The failure worth catching is it reading "call the dentist about the referral letter"
-- as the same thing as "call the dentist", filing nothing,
-- and the symbiot never learning it made that call.
-- The reminders card already catches the mirror image of that mistake —
-- a reminder set against a line that only mentioned a task —
-- by showing the human line beside what the machine did.
-- This is the other half of the same pairing.
--
-- A counter on the matched reminder would have been cheaper
-- and would have dropped the only field worth having:
-- the whole failure mode is two differently worded intents collapsing into one,
-- and the wording is the evidence.
-- So a decline is a row with both sides of the judgment in it.
--
-- intake_id is the message that asked, UNIQUE —
-- the same exactly-once pin the reminder itself carries,
-- so a retried message that re-runs the check writes one decline, not a second.
-- reminder_id is the standing reminder the judge said was the same intent.
-- Both cascade: a decline is evidence about a pairing, and it has no meaning once either side is gone.
CREATE TABLE reminder_decline (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    intake_id   BIGINT      NOT NULL UNIQUE REFERENCES intake (id) ON DELETE CASCADE,
    reminder_id BIGINT      NOT NULL REFERENCES reminder (id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The audit read's order: a symbiot's most recent declines first (the reminders card).
-- Reached through the reminder it matched, which is where symbiot_id lives —
-- kept off this row on purpose, since a decline is about the pairing and the pairing already names whose it is.
CREATE INDEX reminder_decline_recent ON reminder_decline (reminder_id, created_at DESC);
