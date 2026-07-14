"""Web Push: the kernel's browser-facing transport, under two roles.

This is the sending half of the reply channel, and the web-push leg of the notification layer —
one transport, two entry points, told apart by who is waiting and what they need.

The first is notify(): the per-message reply nudge.
The shell registers a browser's push address (save_subscription);
the kernel keeps it,
and when a message reaches a terminal outcome — answered or abandoned —
it sends that browser a small, content-free knock,
so the symbiot learns there's a reply waiting even with the app closed.
That knock carries only the message's id and how it settled;
the shell wakes and reads the real answer from /answers.
Nothing private rides it, because it doesn't need to —
the symbiot is at the shell, which has somewhere to poll back to.
It nudges the one channel a message named,
and serves an anonymous caller as readily as a known one,
since the channel is tied to the message, not to an identity.

The second is fan_out(): the web-push leg of the notification dispatcher
(services/loop/notify.py).
Where notify() nudges one channel about one message,
fan_out() carries a whole payload — real title and body —
to every subscription a symbiot has,
because a notification is addressed to the symbiot, not tied to a single request.
It carries content on purpose:
a web push payload is end-to-end encrypted
(the push service relays ciphertext it can't read),
so the encrypted push is a fit place for the words,
and the durable inbox behind every notification answers the rest
(see doc/notifications.md).
The dispatcher builds the payload and the narrowing;
this only resolves the symbiot's subscriptions and sends.

VAPID is how a push service knows the push is really from us:
every send is signed with a private key whose public half the browser subscribed with.
The key lives in config;
we build the signer from it on demand rather than caching it,
so it's cheap to reason about and a config change (or a test) takes effect at once.
When no key is configured push is simply off —
answers still store and /answers still serves them,
notifications still land in the authed inbox,
only the out-of-band nudge is skipped —
so the reach degrades to poll-on-open, never breaks.

A push send is network I/O to an external service:
it can be slow, fail, or find the subscription gone.
So it is done outside any database transaction,
never raises into its caller
(a failed nudge must not disturb a worker or a sweep),
and prunes a subscription the service reports dead (404/410)
so the kernel stops pushing into the void.
"""

import base64
import json

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush

from core import config
from core import logs
from core import protocol

# The internal terminal states, mapped to the shell-facing word the reply nudge carries.
# The words come from protocol.py — the same source /answers reads —
# so the push and the fetch that follows it can't drift
# into speaking different vocabularies to the same shell.
_PAYLOAD_STATUS = {
    "abandoned": protocol.ANSWER_ABANDONED,
    "answered": protocol.ANSWER_READY,
}

log = logs.get("push")

# A push that hangs mustn't hold anything up; a send past this counts as failed.
PUSH_TIMEOUT_SECONDS = 6.0

# How long the push service should hold a nudge for a device it can't reach right now.
# The library's default is 0 — "deliver this instant or discard" —
# which drops every nudge to a sleeping phone (Android Doze) or a closed laptop,
# the exact case an out-of-band nudge exists to cover.
# A day gives the push service room to store-and-forward until the device next wakes;
# a stale nudge that arrives late is harmless,
# because the shell just wakes and reads whatever /answers or /inbox holds by then.
PUSH_TTL_SECONDS = 86400

# These nudges are time-sensitive, so they go out at high urgency.
# On Android this maps to FCM high priority,
# the one tier allowed to wake a dozing device immediately;
# the spec default ("normal") is the batchable tier
# Doze may hold back to a later maintenance window.
PUSH_URGENCY = "high"


# The members below are ordered alphabetically, as far as the code allows:
# each is defined above the ones that call it,
# so reading top to bottom still follows define-before-use —
# _vapid first, then the rest in alphabetical order within that constraint.
def _vapid() -> Vapid02 | None:
    """The signer built from the configured key, or None when push is unconfigured."""
    if not config.VAPID_PRIVATE_KEY:
        return None
    return Vapid02.from_string(config.VAPID_PRIVATE_KEY)


def _read_target(conn, message_id: int):
    """The reply channel owed a nudge for this message, plus how the message settled.

    Returns (channel_id, endpoint, p256dh, auth, status),
    or None when the message has no channel linked (nobody asked to be told)
    or doesn't exist — either way, nothing to send.
    """
    return conn.execute(
        "SELECT rc.id, rc.endpoint, rc.p256dh, rc.auth, i.status "
        "FROM intake i JOIN reply_channel rc ON rc.id = i.reply_channel_id "
        "WHERE i.id = %s",
        (message_id,),
    ).fetchone()


def application_server_key() -> str | None:
    """The public key the shell subscribes with (base64url), or None when push is off.

    Derived from the private key each call so it stays a single source of truth —
    the private half is the only thing configured,
    the public half falls out of it.
    """
    signer = _vapid()
    if signer is None:
        return None
    point = signer.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(point).decode().rstrip("=")


def is_enabled() -> bool:
    """Whether the kernel can send pushes at all — i.e. a signing key is configured."""
    return bool(config.VAPID_PRIVATE_KEY)


def prune_subscription(conn, channel_id: int) -> None:
    """Forget a reply channel the push service reported gone.

    ON DELETE SET NULL on intake means the messages that pointed at it keep their answers;
    only the dead address goes."""
    conn.execute("DELETE FROM reply_channel WHERE id = %s", (channel_id,))


def save_subscription(
    conn, endpoint: str, p256dh: str, auth: str, symbiot_id: int | None = None
) -> int:
    """Store a browser's push address as a reply channel, and return its channel id.
    Idempotent on the endpoint.

    A browser re-subscribing (its keys rotate, the push service migrates it)
    sends the same endpoint with fresh keys,
    so this upserts on the endpoint —
    the row is updated in place rather than duplicated,
    and its id is stable,
    which matters because that id is what the shell threads through /intake
    to say "notify this channel".
    The row is a reply_channel of kind 'web_push' — the only kind there is today.

    symbiot_id ties the channel to whoever registered it, when that's known —
    the shell sends its session token with /push/subscribe,
    so a channel registered while logged in belongs to that symbiot
    and can be reached for a notification (a missive, a reminder).
    It's optional: a subscribe with no session leaves the channel anonymous,
    which still serves per-message reply nudges.
    On a re-subscribe the identity is adopted but never cleared —
    COALESCE keeps an existing symbiot if this call happens to be anonymous,
    so an already-linked channel can't be un-linked by a later logged-out refresh.
    """
    row = conn.execute(
        "INSERT INTO reply_channel (kind, endpoint, p256dh, auth, symbiot_id) "
        "VALUES ('web_push', %s, %s, %s, %s) "
        "ON CONFLICT (endpoint) DO UPDATE SET "
        "p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth, "
        "symbiot_id = COALESCE(EXCLUDED.symbiot_id, reply_channel.symbiot_id) "
        "RETURNING id",
        (endpoint, p256dh, auth, symbiot_id),
    ).fetchone()
    return row[0]


def _send(endpoint: str, p256dh: str, auth: str, payload: dict) -> bool:
    """Send one push. Returns True if the subscription is gone and should be pruned.

    Never raises: a push is a courtesy,
    and a failed courtesy must not take down the worker or sweep that asked for it.
    A 404/410 from the push service means the subscription is dead
    (the browser unsubscribed, the address expired) —
    the one failure worth acting on, by pruning;
    anything else is logged and swallowed.
    """
    signer = _vapid()
    if signer is None:
        return False
    subscription = {"endpoint": endpoint, "keys": {"auth": auth, "p256dh": p256dh}}
    try:
        webpush(
            data=json.dumps(payload),
            headers={"Urgency": PUSH_URGENCY},
            subscription_info=subscription,
            timeout=PUSH_TIMEOUT_SECONDS,
            ttl=PUSH_TTL_SECONDS,
            vapid_claims={"sub": config.VAPID_SUBJECT},
            vapid_private_key=signer,
        )
        return False
    except WebPushException as error:
        status = getattr(error.response, "status_code", None)
        if status in (404, 410):
            return True  # the subscription is dead — signal the caller to prune it
        log.warning("push send failed (%s)", status)
        return False
    except Exception:
        log.exception("push send errored")
        return False


def fan_out(pool, symbiot_id: int, payload: dict) -> None:
    """Push a payload to every subscription a symbiot has registered —
    the notification layer's web-push leg.

    Called by the dispatcher (services/loop/notify.py)
    once it has built the content payload and decided this symbiot should hear over web push.
    Where notify() follows the one channel a message named,
    this fans out to all of a symbiot's channels,
    because a notification is addressed to the symbiot, not tied to any single request.
    The payload is the dispatcher's to shape — it carries the real title and body —
    and this stays the transport:
    it resolves the subscriptions, sends,
    and prunes any the push service reports dead.
    Best-effort like every push:
    a no-op when push is off or the symbiot has no channel,
    it runs its sends outside any transaction,
    never raises into its caller,
    and prunes any channel found dead.
    """
    if not is_enabled():
        return
    with pool.connection() as conn:
        subscriptions = conn.execute(
            "SELECT id, endpoint, p256dh, auth FROM reply_channel WHERE symbiot_id = %s",
            (symbiot_id,),
        ).fetchall()
    dead = [
        channel_id
        for channel_id, endpoint, p256dh, auth in subscriptions
        if _send(endpoint, p256dh, auth, payload)
    ]
    if dead:
        with pool.connection() as conn:
            for channel_id in dead:
                prune_subscription(conn, channel_id)


def notify(pool, message_id: int) -> None:
    """Nudge the subscription owed one for a message that just reached a terminal outcome.

    Called by the worker (on an answer) and the reconcile sweep (on an abandonment),
    after the outcome is committed.
    The database reads are quick and transactional;
    the send itself runs outside any transaction,
    so a slow or dead push service never holds a connection or blocks the caller.
    A content-free knock:
    only the message's id and the shell-facing word for how it settled ride the push,
    never the answer text —
    the symbiot is at the shell, which wakes and reads the real reply from /answers.
    This is the reply nudge, not a notification:
    it follows the one channel a message named,
    so it serves an anonymous caller exactly as it serves a known one,
    where the notification layer (fan_out) reaches only a known symbiot's registered channels.
    A no-op when push is off or the message has no channel linked.
    """
    if not is_enabled():
        return
    with pool.connection() as conn:
        target = _read_target(conn, message_id)
    if target is None:
        return
    channel_id, endpoint, p256dh, auth, status = target
    payload = {
        "id": message_id,
        "kind": protocol.REPLY,
        "status": _PAYLOAD_STATUS.get(status, status),
    }
    if _send(endpoint, p256dh, auth, payload):
        with pool.connection() as conn:
            prune_subscription(conn, channel_id)
