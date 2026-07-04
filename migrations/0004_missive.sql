-- Missives: messages the kernel raises for a symbiot on its own — a nudge, or a line
-- relayed from the World — owing nothing to a prior /intake.
-- They live in a table of their own, apart from intake, because they don't share intake's
-- shape. An intake row is a question walking toward an answer: the symbiot's text in
-- `message`, the reply in `answer`, a status stepping received → working → answered. A
-- missive has neither a question nor a walk — the kernel authored it, it *is* the content,
-- and there's nothing to compute. Folding it into intake meant a row with an empty
-- `message`, its body smuggled into `answer`, and a status born at the finish line: the
-- model stretched to fit. A table of its own lets each mean exactly one thing.
--
-- symbiot_id is who the missive is for — required, since a message with no addressee is
-- meaningless (unlike an intake row, which is unauthed and has no addressee at all).
-- body is what the kernel wants to say. seen_at is the server-side "the shell has shown
-- this" flag, null until surfaced: /inbox lists a symbiot's unseen missives, and marking
-- them seen is what stops them returning on the next open or a second device. A symbiot's
-- own answers need none of this — the shell discovers those from the id it kept at COPY,
-- not from an inbox read.
CREATE TABLE missive (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbiot_id BIGINT      NOT NULL REFERENCES symbiot (id) ON DELETE CASCADE,
    body       TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    seen_at    TIMESTAMPTZ
);

-- The inbox read: a symbiot's unseen missives, oldest first. Partial on the exact
-- predicate the query uses, so listing an inbox never scans a symbiot's whole history —
-- only the handful still waiting to be shown.
CREATE INDEX missive_unseen_idx
    ON missive (symbiot_id, id)
    WHERE seen_at IS NULL;
