-- The notification layer: fanning out the kernel's reach.
--
-- A notification is channel-agnostic — one payload (title, body, pointer) pushed to every channel the symbiot has,
-- or narrowed to the one they explicitly requested.
-- The durable inbox record stays the source of truth; channels are just transports.
--
-- For the reminder (the first notifying tool) to know which channels to fire over when its moment comes,
-- it needs to remember the channels the symbiot named when they set it (or null, meaning the tool's whole supported set).
-- We add an array of channels to the reminder row.

ALTER TABLE reminder ADD COLUMN channels TEXT[];

-- Notification preferences: the symbiot's standing choice of which channels the kernel may reach them on.
--
-- The notification layer above gave the reminder its per-fire channels; services/loop/notify.py fans a notification out across channels.
-- That path defaults every channel ON —
-- a symbiot who never touches this is reachable everywhere, which is what "double up first delivery" wants.
-- So this table holds only the exceptions:
-- a row per channel the symbiot has taken a position on, carrying whether it's enabled.
-- Absence of a row means the default (enabled), so the dispatcher's read asks only for the ones switched off.
--
-- symbiot_id is whose preference this is;
-- a channel a symbiot disables is never fired for them, no matter who asked for it (see /notifications, and notify.dispatch's filter).
-- channel is the stable slug the layer speaks ('web_push', 'email') —
-- kept as free TEXT rather than a CHECK, because the set of channels lives in code (notify.ALL_CHANNELS is the single source),
-- the way the tool catalog's names do, so a new channel is a code change with no migration to widen a constraint.
-- The primary key on (symbiot_id, channel) is what makes the write a plain upsert:
-- a symbiot only ever holds one standing position per channel.
-- ON DELETE CASCADE: a preference is only as meaningful as the symbiot it belongs to.

CREATE TABLE notification_preference (
    symbiot_id BIGINT      NOT NULL REFERENCES symbiot (id) ON DELETE CASCADE,
    channel    TEXT        NOT NULL,
    enabled    BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbiot_id, channel)
);
