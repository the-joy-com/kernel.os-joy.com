-- A second clock on every diary fact: when the event actually happened.
--
-- diary_facts already carried created_at — when the row was written,
-- which for live ingestion is the moment the symbiot told us the fact.
-- That answers "when did we hear about this?", but not "when did it happen?":
-- a boxing session fought yesterday but told to us tonight was created tonight, yet happened yesterday,
-- and a life doesn't record itself in the order it's lived.
-- So a fact needs one more time beyond its row birth.
--
-- happened_at is when the thing actually occurred in the world, and it is nullable on purpose.
-- Plenty of facts carry no temporal cue at all —
-- a bare "I live in Strasbourg" points at no moment in particular —
-- and rather than guess a time and pretend to a precision we don't have, we leave the column empty.
-- A null happened_at means "no event time known", and is read as created_at — the time we recorded the fact.
-- So the ordering rule is simple: use the event time when the fact gave us one, otherwise the time we recorded it.
-- No fact is ever without a usable timestamp, and none is ever fitted with a fabricated one.
ALTER TABLE diary_facts
    ADD COLUMN happened_at TIMESTAMPTZ;
