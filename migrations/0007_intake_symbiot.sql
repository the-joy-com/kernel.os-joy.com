-- Intake learns who sent each line: the symbiot behind it, when there is a live session, or no one when there isn't.
-- The kernel reads the request's session at /intake and stamps the id here — the shell can't assert it,
-- so who a line is from is the server's finding, not a claim the browser gets to make.
-- This is what lets the worker answer a recognized symbiot differently from an anonymous caller,
-- and it's the seam the intelligence layer will condition on: "who am I answering" is its first question.
--
-- Nullable on purpose: a line from a caller with no live session has no symbiot, and that's a valid message,
-- never a rejected one — the input layer accepts unauthed input, identity only colours the reply.
-- ON DELETE SET NULL, matching how intake already treats its reply_channel_id (0003):
-- the message record stands on its own, so losing the symbiot drops the authorship, not the line.
ALTER TABLE intake
    ADD COLUMN symbiot_id BIGINT REFERENCES symbiot (id) ON DELETE SET NULL;
