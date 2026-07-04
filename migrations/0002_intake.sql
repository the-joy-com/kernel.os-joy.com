-- Intake: the messages the symbiot hands the kernel, and the path each walks to an answer.
-- One row per /intake request — the message, not the lines inside it. A reconnect
-- drains the shell's outbox as a single request that joins its queued lines with
-- newlines, and that whole blob is one message: the kernel can't honestly recover
-- line boundaries from the wire (a newline isn't one), so it doesn't try.
-- It vouches only for what it can stand behind — the symbiot was offline, handed over
-- this content, and none of it was lost.
--
-- status walks one path to a terminal state. The happy path is
-- received → working → answered. A failing attempt lands in 'failed', which is not
-- terminal: a message is retried up to a bounded number of attempts, cycling
-- failed → received → working → failed, and only once its attempts are spent is it
-- parked in 'abandoned' — the terminal give-up. So the two ends are 'answered' and
-- 'abandoned'; 'failed' is a way-station between tries.
-- A plain TEXT word under a CHECK, not a native enum, so the set of states stays a
-- one-line change. The transitions are guarded in intake.py (each UPDATE names the
-- state it's allowed to move *from*), so "one row, one outcome" is enforced by the
-- row layer, not by the order calls happen to run in.
--
-- attempts counts how many times the message has been claimed for work — bumped on each
-- claim. It's the budget the retry logic reads: a 'failed' row with attempts left is
-- re-queued, one with none is abandoned, so a message that always fails can't be retried
-- forever.
--
-- created_at is stamped once, when the message lands; updated_at moves with every
-- status change, so "how long has this sat in its current state" is just
-- now() - updated_at — the clock the worker's deadline reads.
--
-- answer holds the reply the worker produces; null until the message reaches
-- 'answered' — received and working rows have no reply yet, and a failed one never will.
--
-- failed_reason is the mirror image: why the latest attempt failed. A crash records the
-- child's full traceback, a deadline records that it was swept — so no failure is ever
-- silent, and the trace is there for a later self-healing pass to read and act on. It's
-- non-null exactly while the row is 'failed' or 'abandoned'; re-queuing for another try
-- clears it, so received/working/answered rows never carry a stale reason.
CREATE TABLE intake (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    message       TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'received'
                      CHECK (status IN ('received', 'working', 'answered', 'failed', 'abandoned')),
    answer        TEXT,
    failed_reason TEXT,
    attempts      INT         NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
