-- Tier 2 enrichment provenance: the record that the deep second pass has considered a message,
-- and the guarantee it considers each answered message exactly once.
--
-- Tier 1 answers fast, from the lexical diary reach and the running conversation.
-- Tier 2 is the deeper, slower pass that runs off the critical path once that fast answer is settled:
-- it reaches into the diary by *meaning* — vector recall plus a walk of the ontology the facts are filed under —
-- and, only when that reach genuinely adds something the fast answer didn't, sends an enriched follow-up as a missive.
-- That sweep needs one thing the schema can give it and timing cannot —
-- a way to know a message has already been through the pass, one that survives a crash mid-pass or a retry of the sweep.
--
-- intake_id is that link: the message this enrichment considered, and its exactly-once pin.
-- It is UNIQUE, so one message can back at most one enrichment row —
-- a sweep that crashed after sending the missive but before recording the pass as done simply re-runs,
-- hits this constraint, and the second write is refused,
-- so an interrupted or double-run sweep can never send two follow-ups for one message.
-- The sweep's eligibility is the mirror of this index — an answered, authed message with no enrichment row yet —
-- so a message never reached leaves itself eligible and is simply picked up on the next pass. No drop, no double.
-- ON DELETE CASCADE: the row is a provenance record, meaningless without the message it is about.
-- (Intake rows are never deleted — the diary of record is immutable — so this is a principled stance, not a live path.)
--
-- surfaced records the pass's verdict: true when the enrichment was worth sending,
-- false when the deep reach turned up nothing the fast answer hadn't already covered.
-- A suppressed pass is still recorded,
-- so a message that was considered and found not worth enriching is never reconsidered —
-- the gate is spent exactly once, like the pass itself.
-- missive_id points at the follow-up that was sent, when one was;
-- the CHECK binds the two so the row can never claim it surfaced without a missive to show for it,
-- nor carry a missive it says it never sent.
-- ON DELETE SET NULL on the missive side: the enrichment record outlives the missive if one were ever removed,
-- losing only the back-reference, never the fact that the pass ran.
CREATE TABLE enrichment (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    intake_id   BIGINT      NOT NULL UNIQUE REFERENCES intake (id) ON DELETE CASCADE,
    symbiot_id  BIGINT      NOT NULL REFERENCES symbiot (id) ON DELETE CASCADE,
    surfaced    BOOLEAN     NOT NULL,
    missive_id  BIGINT      REFERENCES missive (id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- surfaced exactly when there is a missive to show for it: the verdict and the artefact can never disagree.
    CONSTRAINT enrichment_surfaced_has_missive
        CHECK (surfaced = (missive_id IS NOT NULL))
);
