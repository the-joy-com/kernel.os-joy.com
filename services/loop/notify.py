"""The notification layer: the kernel's reach to the human symbiot, fanned across channels.

Most of what the kernel says is a reply to a line just asked.
A notification is the other kind:
a reminder comes due, a missive it raised on its own is waiting,
a follow-up it thought of unprompted —
words it pushes toward the symbiot rather than holding until they next look.

The whole point of the layer is that the reach doesn't hang on any single way of getting through.
A phone can be asleep, a browser can have refused permission,
a push service can drop a nudge on the floor.
So a notification is channel-agnostic:
one payload — a title, a body, and a pointer back to the durable inbox record it copies —
delivered across every channel the symbiot has,
so no one provider's delivery odds decide whether the reach lands.
It is the "double up first delivery" principle made structural.

The rule is one rule, default-plus-opt-in (see doc/notifications.md):
a notification fans out to every channel a caller names,
and a caller that has no reason to narrow names every channel that exists.
The reminder tool narrows to the channels the symbiot asked for when they set it;
a missive the kernel raised on its own has no such request behind it,
so it fans out everywhere.
Two filters sit under the fan-out, both silent by design
(the record already stands, so a dropped channel costs immediacy, never the message):
a channel that does not exist is dropped,
and a channel the symbiot has globally disabled
(see /notifications, services/memory/notification_prefs.py) is never fired —
so a request for "email only" against a disabled email simply reaches no one,
rather than erroring.

The dispatcher lives here, in the loop,
because it composes the two edges —
the web push transport (services/adapters/push.py)
and the email transport (services/adapters/email_client.py) —
with the symbiot's own stored preferences (services/memory/notification_prefs.py).
The channels themselves stay at the edge;
this is only the composition.
Adding a channel later is a new transport and one more entry in _SENDERS —
no tool and no caller changes,
because the notification shape is already uniform.
"""

from dataclasses import dataclass
from typing import Literal

from core import config
from core import logs
from core import protocol
from services.adapters import email_client
from services.adapters import push
from services.memory import notification_prefs

log = logs.get("notify")

# The channels that exist, as stable slugs — the identity of a channel on both sides of the layer.
# `email` is the transport, not `gmail`:
# the provider hides behind email_client, so the internals never name it
# (the same provider-independence the rest of the kernel keeps).
# The tuple is the single source for "every channel there is" —
# the default fan-out sweeps it, the /notifications route lists it,
# and a request naming anything outside it is dropped.
# Sorted, so the set reads the same wherever it is spelled out.
Channel = Literal["email", "web_push"]
ALL_CHANNELS: tuple[Channel, ...] = ("email", "web_push")


@dataclass(frozen=True)
class Notification:
    """One channel-agnostic notification: the same small thing every channel renders in its own medium.

    title and body are the real content — the words the symbiot is meant to read, not a placeholder;
    a channel-free knock was the old web-push doorbell,
    and the layer deliberately left it behind
    (both channels carry content now — see doc/notifications.md on why the encrypted push is the more private).
    pointer is the path back to the durable inbox record this copies (e.g. "/inbox"):
    the web push carries it so the shell can deep-link,
    and the email turns it into a link to open The Joy.
    The record is the source of truth;
    the notification is only ever the faster way to it."""

    title: str
    body: str
    pointer: str


def dispatch(pool, symbiot_id: int, notification: Notification, channels: list[Channel]) -> None:
    """Fan a notification to a symbiot across the given channels — the one entry point every caller uses.

    The caller decides the channels:
    the reminder passes what the symbiot asked for
    (or its whole supported set when they asked for nothing);
    a missive raised on its own passes ALL_CHANNELS.
    Whatever comes in, two silent filters narrow it before anything is sent —
    a channel that isn't real is dropped,
    and one the symbiot has globally disabled is skipped —
    so "email only" against a disabled email reaches no one rather than erroring,
    and an unknown slug can never reach a sender.
    Each channel send is best-effort and isolated:
    a channel that fails (or has nothing to reach) is logged and stepped over,
    never raised into the sweep or worker that called this,
    and never blocking the channels beside it.
    A no-op when the narrowed set is empty —
    the durable record already stands to be read on next open.
    """
    requested = [c for c in channels if c in ALL_CHANNELS]  # a slug that names no real channel steers nothing
    if not requested:
        return
    with pool.connection() as conn:
        disabled = notification_prefs.disabled_channels(conn, symbiot_id)
    for channel in requested:
        if channel in disabled:
            continue  # globally turned off by the symbiot — never fired, silently
        try:
            _SENDERS[channel](pool, symbiot_id, notification)
        except Exception:
            # A failed channel is a dropped courtesy, not a failed reach:
            # the record stands regardless,
            # so this is logged and swallowed,
            # never allowed to disturb the caller or the other channels.
            log.exception("notification channel %r failed", channel)


def _send_web_push(pool, symbiot_id: int, notification: Notification) -> None:
    """The web push channel: the notification, carried to every browser subscription the symbiot registered.

    Builds the content payload and hands it to the transport (push.fan_out),
    which resolves the symbiot's subscriptions, encrypts and sends,
    and prunes any the push service reports dead —
    all the web-push detail the dispatcher stays clear of.
    kind keeps the shell's routing vocabulary
    (protocol.TRAFFIC_WAITING, the inbox-traffic family),
    now with the real title and body riding under it
    and the pointer as the url to open.
    """
    payload = {
        "kind": protocol.TRAFFIC_WAITING,
        "title": notification.title,
        "body": notification.body,
        "url": notification.pointer,
    }
    push.fan_out(pool, symbiot_id, payload)


def _send_email(pool, symbiot_id: int, notification: Notification) -> None:
    """The email channel: the notification, sent to the address on the symbiot's identity row.

    Off when Gmail is unconfigured (like push is off with no VAPID key),
    so an unconfigured box simply doesn't fan to email rather than erroring.
    Resolves the one address every symbiot carries,
    and sends the title as the subject
    and the body followed by a link to open The Joy —
    since an email has nowhere to poll back to,
    the mail is the delivery, and the link is how the symbiot crosses from it to the shell to act.
    The client is built per send from config,
    the same stance push takes with its signer:
    notifications are infrequent,
    so a fresh, stateless client is cheaper to reason about
    than one held across the process.
    """
    if not config.GMAIL_CREDENTIALS_FILE or not config.GMAIL_SENDER:
        return
    with pool.connection() as conn:
        row = conn.execute("SELECT email FROM symbiot WHERE id = %s", (symbiot_id,)).fetchone()
    if row is None:
        return
    address = row[0]
    body = f"{notification.body}\n\nOpen The Joy to reply: {config.SHELL_URL}"
    client = email_client.GmailEmailClient(config.GMAIL_CREDENTIALS_FILE, config.GMAIL_SENDER)
    client.send(to=address, subject=notification.title, body=body)


# The dispatch table: a channel slug resolves to the code that sends over it —
# the same shape as the tool registry, and the same reason.
# A caller can only ever produce a slug;
# a slug resolves to a sender we wrote.
_SENDERS = {
    "email": _send_email,
    "web_push": _send_web_push,
}
