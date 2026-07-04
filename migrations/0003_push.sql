-- The reply channel: where the kernel reaches the symbiot when a message is settled.
--
-- A row is one durable way to reach the symbiot out-of-band — the address the kernel
-- sends a "your message has an answer" nudge to, even with the app closed. Today the
-- only kind is 'web_push' (a browser's push subscription: a service endpoint plus the
-- keys to encrypt a payload only that browser can read), so the web_push-specific columns
-- sit here directly under that kind. When a second kind arrives (a Telegram chat, an
-- email), it joins the CHECK and brings its own address columns — the rows above it, and
-- everything that references a channel by id, stay as they are. The name is the role;
-- the technology is one value of `kind`.
--
-- This migration owns the whole reply-channel schema: the channel table, and the one
-- column it adds to intake to tie a message to the channel that should hear about its
-- outcome. (The base intake table is 0002's; this extends it.)

CREATE TABLE reply_channel (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Which kind of channel this row is — the transport the nudge rides. One value today;
    -- the set grows by a one-line CHECK change, the way intake.status does.
    kind       TEXT        NOT NULL DEFAULT 'web_push' CHECK (kind IN ('web_push')),
    -- web_push: the push service URL the browser handed us. Unique so a browser
    -- re-subscribing (keys rotate, the service migrates it) updates its row in place
    -- rather than piling up stale duplicates the kernel would push to in vain.
    endpoint   TEXT        NOT NULL UNIQUE,
    -- web_push: the client's public key and auth secret, both needed to encrypt a push
    -- payload so only that browser can read it. Stored as the browser sent them (base64url).
    p256dh     TEXT        NOT NULL,
    auth       TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Which channel to notify when this message reaches a terminal outcome. Nullable: a
-- message captured before any channel was registered simply has no one to notify — its
-- answer still stands, waiting to be read on next open. ON DELETE SET NULL so pruning a
-- dead channel (a 404/410 from a push service) never drags its messages down with it:
-- the answer outlives the address it would have been announced to.
ALTER TABLE intake
    ADD COLUMN reply_channel_id BIGINT REFERENCES reply_channel (id) ON DELETE SET NULL;
