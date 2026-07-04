-- Tie a reply channel to the symbiot it belongs to, so the kernel can reach a symbiot
-- out-of-band on its own initiative — not only in answer to a line that named the channel.
--
-- A reply nudge for the symbiot's own message never needed this: the shell threads the
-- channel id through /intake per message (0003's reply_channel_id), so the kernel already
-- knows where to send that one. But a missive is unsolicited — there's no /intake call to
-- carry a channel — so to nudge a symbiot that a missive is waiting, the kernel has to find
-- their channels by identity. That's what this column is for.
--
-- Nullable on purpose. A browser can register a push address before it logs in — push is
-- ungated, like /intake, because the right to be reachable isn't fenced behind identity —
-- and such a channel still serves per-message reply nudges; it simply has no symbiot yet.
-- It gains one the next time /notify runs inside a session (save_subscription adopts the
-- caller's identity without overwriting an existing one). ON DELETE CASCADE: a channel is
-- only as meaningful as the symbiot it points at, so if the symbiot goes, its channels go.
ALTER TABLE reply_channel
    ADD COLUMN symbiot_id BIGINT REFERENCES symbiot (id) ON DELETE CASCADE;

-- The missive-nudge lookup: every channel a given symbiot can be reached on. Partial, since
-- the anonymous (null-symbiot) rows are never fetched this way.
CREATE INDEX reply_channel_symbiot_idx
    ON reply_channel (symbiot_id)
    WHERE symbiot_id IS NOT NULL;
