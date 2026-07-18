-- Enrichment gains a reason for its silence: a follow-up composed but held back as an echo,
-- told apart from a pass that simply had nothing to add.
--
-- A suppressed enrichment pass (surfaced = false) now happens for two distinct reasons,
-- and until this migration they were indistinguishable in the row:
--   1. the gate chose silence — the deep reach turned up nothing the fast answers hadn't already covered;
--   2. the echo guard caught it — the gate *did* compose a follow-up,
--      but it was near-identical to a deep reply already sent, so it was held back (enrichment.is_echo_of_prior).
-- The second is a decision the machine made on the symbiot's behalf — it wanted to speak and was muzzled —
-- and the system must stay auditable about that rather than letting a swallowed follow-up vanish without a trace.
-- So this column records which silences were the guard's doing, and /observe surfaces their count.
--
-- A boolean, defaulting false,
-- so every existing row (and every gate-chose-silence pass) reads honestly as "not an echo suppression".
-- Only the anchor of a burst whose follow-up the guard held ever carries true.
--
-- The CHECK binds it to the verdict it qualifies: a row can never be echo_suppressed and surfaced at once —
-- an echo held back is by definition a follow-up that was not sent, so nothing surfaced.
ALTER TABLE enrichment
    ADD COLUMN echo_suppressed BOOLEAN NOT NULL DEFAULT false,
    ADD CONSTRAINT enrichment_echo_suppressed_not_surfaced
        CHECK (NOT (echo_suppressed AND surfaced));
