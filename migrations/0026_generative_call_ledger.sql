-- The ledger of which model actually answered: one row per generative call the kernel makes for a named role.
--
-- Every generative call resolves a *role* to a model name and then walks a fallback ladder (services/adapters/llm.py),
-- so the model that answers is often not the one the role names, and it never appears in the reply itself.
-- Until now the only trace of that was a log line (llm._served):
-- enough to read a live tail with, and gone the moment the log rotated.
-- So the fact the symbiot could reasonably want to inspect — whose words did I actually get? — was the one thing
-- the machine kept no durable record of, which is backwards for a surface built to be audited.
-- This table is that log line made durable, and the /observe hub's third card reads it.
--
-- The distinction the row exists to draw is requested_model against served_model.
-- Equal, the primary answered and the ladder never moved.
-- Different, a rung fell through and a humbler model composed the words the symbiot read —
-- which is exactly the case that cannot be attributed after the fact from anywhere else,
-- since a reply that came back thin, or wrong, or in the wrong voice looks identical either way.
-- served_provider rides alongside the name because the ladder falls across *clouds*, not only models:
-- knowing a call landed on Mistral rather than Google is the shape of the outage, not a detail of it.
--
-- reply_chars is the cheapest possible tell for the failure mode this attribution was built for:
-- a model that degenerates mid-reply runs the same characters until it hits its own output ceiling,
-- so a reply sitting at the cap is degenerate on its face, and one at a couple of hundred characters is ordinary.
-- It is the length of the model's *raw* reply body, structured-output wrapper and all,
-- because the output ceiling this is read against applies to that same raw body and not to the words unwrapped from it.
-- A length, never the words: the ledger records who answered and how much they said, never what was said.
-- The words already live durably where they belong (the intake row's answer, the missive's body),
-- and the echoes card is where they are read — this card is about the machine, not the conversation.
--
-- Box-level, not per-symbiot, and deliberately so:
-- which model served a call is a property of the machine and the providers it can reach,
-- exactly like the catalog and the role assignments it is read against (migration 0019),
-- which are box-level too and read through the box-level /models command.
-- So there is no symbiot_id here and no per-symbiot scoping on the read.
-- Nothing in the row is anyone's content — a role, two model names, a provider, a length, an instant.
--
-- No foreign key to `model`, though every name here is one the catalog knew at the time.
-- The ledger is history, and history must not be rewritten or refused by a later edit to the catalog:
-- an operator who deletes a model they have stopped using should not thereby erase the record of the calls it served,
-- nor be blocked from deleting it by rows that only describe the past.
-- The names are kept as plain text for that reason — a record of what was true when the call was made.
--
-- role is free TEXT rather than a CHECK, for the same reason `model.provider` is:
-- the set of generative roles lives in code (adapters/models.BUILTIN_ROLES),
-- so a new role joins the ledger by naming itself at its call site, not by a migration widening a constraint.
CREATE TABLE generative_call (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    role            TEXT        NOT NULL,
    requested_model TEXT        NOT NULL,
    served_provider TEXT        NOT NULL,
    served_model    TEXT        NOT NULL,
    reply_chars     INTEGER     NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The one read the lens makes: the last n calls for a named set of roles, newest first.
-- Leading on role because the card asks for the reply and the follow-up specifically, never the whole ledger,
-- and descending on id so the newest end — the only end a "last n" read ever wants — is the cheap one.
CREATE INDEX generative_call_role_recent ON generative_call (role, id DESC);
