-- Intake as the diary of record: the symbiot's words, kept verbatim and kept forever.
--
-- The base table (0002) already stores each message the moment it lands, before /intake
-- answers 'roger', and no code path anywhere edits the words after — so "verbatim,
-- persist-first, immutable" has been true in practice from the first row. This migration
-- turns that practice into a guarantee the database itself enforces, one level below any
-- caller, so the governing rule of the diary — keep the raw words verbatim, whole, forever,
-- from entry one — no longer rests on every future writer happening to respect it.
--
-- Two guards, matching the two ways the words could be lost:
--
--   1. message is immutable. Any UPDATE that would change a stored row's message text is
--      refused. The state machine that walks a message to its answer only ever touches
--      status, answer, attempts, the timestamps, symbiot_id and delivered_at — never
--      message — so every legitimate transition passes untouched; only a rewrite of what
--      the symbiot actually said is rejected. The trigger fires solely when the text would
--      in fact change (the WHEN clause), so an UPDATE that leaves message alone costs nothing.
--
--   2. a row is never deleted. "Forever" means a message is walked to a terminal state, never
--      erased. A row-level BEFORE DELETE guard refuses outright. (This does not touch the
--      test suite's TRUNCATE ... CASCADE reset: TRUNCATE fires TRUNCATE triggers, not the
--      row-level DELETE trigger, so a clean-slate wipe between tests still works.)
--
-- Note this guards the words, not the derived work-state around them. status/answer/attempts
-- are the message's walk toward a reply, legitimately mutable; the immutable thing is the
-- message itself. And nothing derived from the words (tags, slices, classifications) is
-- stored here at all — that stays recomputed on read, the diary's third rule, pinned by a
-- test rather than the schema.

CREATE FUNCTION intake_reject_delete() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'intake rows are never deleted: the diary keeps every message forever (id=%)', OLD.id;
END;
$$;

CREATE FUNCTION intake_reject_message_edit() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'intake.message is immutable: the symbiot''s words are kept verbatim, never rewritten (id=%)', OLD.id;
END;
$$;

CREATE TRIGGER intake_no_delete
    BEFORE DELETE ON intake
    FOR EACH ROW
    EXECUTE FUNCTION intake_reject_delete();

CREATE TRIGGER intake_message_immutable
    BEFORE UPDATE ON intake
    FOR EACH ROW
    WHEN (NEW.message IS DISTINCT FROM OLD.message)
    EXECUTE FUNCTION intake_reject_message_edit();
