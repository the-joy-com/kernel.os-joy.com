-- Short-term conversational memory: the recent back-and-forth a reply sits inside,
-- held as a gradient — the near turns word-for-word, the far turns folded into one running summary,
-- and nothing ever thrown away. Two tables carry it.
--
-- This is a *second* kind of memory beside the diary (diary_facts).
-- The diary is recall by relevance — the facts whose words bear on the message, from anywhere in its history.
-- This is recall by recency — the thread that lets "and the second one?" find what it points back to,
-- which no relevance search could recover, because a pronoun carries none of the words that would surface the thing it stands for.
-- The read path (retrieval.py, reply.py) is unchanged; this is added beside it, sharing only the prompt's token window.
--
-- conversation_item is the stream: one row per utterance, both directions —
-- the symbiot's messages, the machine's replies, and the machine's proactive missives.
-- The row does not copy the words: it *points* to where they already live durably
-- (the intake row for a symbiot message or its reply, the missive row for a machine-initiated line),
-- and carries the three things the read needs that the source table doesn't hold —
-- the utterance's role, its timestamp, and its token count.
--
-- The pointer is two nullable foreign keys under a CHECK that exactly one is set,
-- so real database-enforced integrity reaches *both* source tables (a pointer can never dangle)
-- and "every utterance comes from exactly one place" is a constraint the database holds, not a rule the writing code has to remember.
-- A symbiot's message and the reply to it are two rows both pointing at the same intake row, told apart by role:
-- the text each resolves to is intake.message for the symbiot side, intake.answer for the machine side, and a missive's body for a machine-initiated line.
--
-- token_count is computed once, at write, with tiktoken (the same local counter the budget guard uses, services/models.py).
-- Counting it at write turns the read-time "how much fits?" question into arithmetic Postgres can do over an integer column,
-- with no tokeniser call on the path the symbiot waits on.
-- It is stored here rather than on intake on purpose:
-- intake is the diary of record and forbids storing anything derivable from the words beside them,
-- and a token count is exactly that —
-- but this table is a projection built alongside the record, so caching a derived figure here is legitimate where on the record it isn't.
--
-- ON DELETE CASCADE (unlike diary_facts' SET NULL):
-- a conversation item is meaningless without the words it points at,
-- so if a source row ever went, the item goes with it rather than dangling textless.
-- Source rows are in fact never deleted (intake is immutable; a missive persists),
-- so this is a principled stance, not a live path.
CREATE TABLE conversation_item (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbiot_id  BIGINT      NOT NULL REFERENCES symbiot (id) ON DELETE CASCADE,
    role        TEXT        NOT NULL CHECK (role IN ('symbiot', 'machine')),
    token_count INT         NOT NULL,
    intake_id   BIGINT      REFERENCES intake (id) ON DELETE CASCADE,
    missive_id  BIGINT      REFERENCES missive (id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Exactly one source: XOR of the two "is set" tests is true only when one is non-null,
    -- false when both are null and false when both are set —
    -- so the row always resolves to one place, never zero and never two.
    CONSTRAINT conversation_item_one_source
        CHECK ((intake_id IS NOT NULL) <> (missive_id IS NOT NULL))
);

-- The Bucket 1 read walks a symbiot's items newer than the Gist's cutoff, in id order and with no token cap —
-- the whole tail back to where the Gist ends (services/conversation.py).
-- This index serves that "items newer than the cutoff" scan for both the reader and the compression sweep
-- (which sums the same range to decide when to fold), without ever touching another symbiot's stream.
CREATE INDEX conversation_item_symbiot_id_idx
    ON conversation_item (symbiot_id, id);

-- conversation_gist is the Bucket 2 store: everything older than the verbatim tail, folded into one running summary paragraph.
-- The table is APPEND-ONLY — each compression fold inserts a *new* row carrying the updated paragraph and the cutoff_item_id it reached
-- (the id of the last conversation_item absorbed).
-- The current Gist is simply the newest row for the symbiot;
-- nothing is ever overwritten, so the table is also a durable, inspectable history of how the summary grew and where its boundary stood at every step.
--
-- cutoff_item_id is a hard foreign-key integer the code reads directly,
-- never a value parsed back out of the summary prose (which would be a probabilistic guess at a fact the schema can state exactly).
-- Because the table is append-only, a fold is a single INSERT — no flag to flip, no row to overwrite —
-- and exactly-once falls out of the cutoff only ever moving forward:
-- a crash before the commit leaves the same turns eligible next pass,
-- a crash after has already advanced the cutoff so those turns fall outside the next pass's gather.
CREATE TABLE conversation_gist (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbiot_id     BIGINT      NOT NULL REFERENCES symbiot (id) ON DELETE CASCADE,
    gist_text      TEXT        NOT NULL,
    cutoff_item_id BIGINT      NOT NULL REFERENCES conversation_item (id) ON DELETE CASCADE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The current Gist is the newest row for a symbiot (DISTINCT ON (symbiot_id) ORDER BY id DESC),
-- and the compression sweep reads it every pass.
-- This index makes that a lookup, not a scan of a symbiot's whole fold history.
CREATE INDEX conversation_gist_symbiot_id_idx
    ON conversation_gist (symbiot_id, id DESC);
