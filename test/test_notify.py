"""The notification dispatcher: fan-out across channels, and the two silent filters under it.

The transports themselves are proven elsewhere — web push in test_push.py, email in test_identity.py's
login flow — so here both are faked at their edge (push._send is recorded, the Gmail client is a recorder),
and what these pin is the dispatcher's own job: that a notification reaches every channel asked for, that a
channel that doesn't exist or one the symbiot has globally disabled is dropped silently, and that one channel
failing never robs the others or the caller.
"""

from core import config
from core import db
from services.adapters import email_client
from services.adapters import push
from services.loop import notify
from services.memory import notification_prefs
from services.memory import presence

SEEDED_SYMBIOT_ID = 1  # conftest re-seeds exactly one symbiot with RESTART IDENTITY, so it's always id 1

# The throwaway VAPID key that switches web push on for a test; the suite never sends with it.
TEST_VAPID_KEY = "ASNSWLwop5XkSvQC4zmGN2wEpHTZbs56UQOrByFyZqk"

_N = notify.Notification(title="Reminder", body="call the dentist", pointer="/inbox")


def _enable_web_push(monkeypatch, sent):
    # Switch push on and record every send instead of putting it on the wire.
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", TEST_VAPID_KEY)
    monkeypatch.setattr(
        push, "_send", lambda endpoint, p256dh, auth, payload: sent.append(payload) or False
    )
    with db.get_pool().connection() as conn:
        push.save_subscription(conn, "https://push.example/d1", "k", "a", SEEDED_SYMBIOT_ID)


def _enable_email(monkeypatch, sent):
    # Configure Gmail (so the email channel isn't a no-op) and swap the real client for a recorder.
    monkeypatch.setattr(config, "GMAIL_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setattr(config, "GMAIL_SENDER", "joy@example.com")

    class _Recorder:
        def __init__(self, *args):
            pass

        def send(self, to, subject, body):
            sent.append((to, subject, body))

    monkeypatch.setattr(email_client, "GmailEmailClient", _Recorder)


def test_dispatch_fans_to_every_channel_asked_for(client, monkeypatch):
    pushes, emails = [], []
    _enable_web_push(monkeypatch, pushes)
    _enable_email(monkeypatch, emails)
    notify.dispatch(db.get_pool(), SEEDED_SYMBIOT_ID, _N, list(notify.ALL_CHANNELS))
    # Web push carries the content under the shell-routing kind; email carries subject + body + a link home.
    assert pushes == [{"kind": "traffic waiting", "title": "Reminder", "body": "call the dentist", "url": "/inbox"}]
    assert len(emails) == 1
    to, subject, body = emails[0]
    assert to == "symbiot@example.com" and subject == "Reminder"
    assert "call the dentist" in body and config.SHELL_URL in body


def test_dispatch_narrows_to_a_single_named_channel(client, monkeypatch):
    pushes, emails = [], []
    _enable_web_push(monkeypatch, pushes)
    _enable_email(monkeypatch, emails)
    notify.dispatch(db.get_pool(), SEEDED_SYMBIOT_ID, _N, ["email"])
    assert emails and pushes == []  # only the one the caller named fired


def test_dispatch_drops_a_channel_that_does_not_exist(client, monkeypatch):
    pushes = []
    _enable_web_push(monkeypatch, pushes)
    # A slug that names no real channel steers nothing; the valid one beside it still fires.
    notify.dispatch(db.get_pool(), SEEDED_SYMBIOT_ID, _N, ["carrier_pigeon", "web_push"])
    assert len(pushes) == 1


def test_dispatch_skips_a_globally_disabled_channel(client, monkeypatch):
    pushes, emails = [], []
    _enable_web_push(monkeypatch, pushes)
    _enable_email(monkeypatch, emails)
    with db.get_pool().connection() as conn:
        notification_prefs.set_channel(conn, SEEDED_SYMBIOT_ID, "email", False)
    # Both asked for, but email is switched off for this symbiot — it is never fired, silently.
    notify.dispatch(db.get_pool(), SEEDED_SYMBIOT_ID, _N, list(notify.ALL_CHANNELS))
    assert len(pushes) == 1 and emails == []


def test_dispatch_disabled_channel_is_dropped_even_when_named_alone(client, monkeypatch):
    emails = []
    _enable_email(monkeypatch, emails)
    with db.get_pool().connection() as conn:
        notification_prefs.set_channel(conn, SEEDED_SYMBIOT_ID, "email", False)
    # "email only" against a disabled email reaches no one — the record still stands to be read on next open.
    notify.dispatch(db.get_pool(), SEEDED_SYMBIOT_ID, _N, ["email"])
    assert emails == []


def test_dispatch_email_is_off_when_gmail_is_unconfigured(client, monkeypatch):
    # Gmail unset (the suite default) — the email channel is a no-op rather than an error, and the client
    # is never even built. Cleared explicitly here too, so the test holds whatever a dev .env happens to carry.
    monkeypatch.setattr(config, "GMAIL_CREDENTIALS_FILE", "")
    monkeypatch.setattr(config, "GMAIL_SENDER", "")
    built = []

    class _Recorder:
        def __init__(self, *args):
            built.append(1)

        def send(self, to, subject, body):
            pass

    monkeypatch.setattr(email_client, "GmailEmailClient", _Recorder)
    notify.dispatch(db.get_pool(), SEEDED_SYMBIOT_ID, _N, ["email"])
    assert built == []  # unconfigured — the channel returns before the client is ever constructed


def test_dispatch_is_best_effort_one_channel_failing_spares_the_others(client, monkeypatch):
    pushes = []
    _enable_web_push(monkeypatch, pushes)
    monkeypatch.setattr(config, "GMAIL_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setattr(config, "GMAIL_SENDER", "joy@example.com")

    class _Boom:
        def __init__(self, *args):
            pass

        def send(self, to, subject, body):
            raise RuntimeError("gmail is down")

    monkeypatch.setattr(email_client, "GmailEmailClient", _Boom)
    # Email raises; the dispatcher swallows it and web push still lands — a failed channel is a dropped
    # courtesy, never a failed reach, and it must not disturb the caller or the channels beside it.
    notify.dispatch(db.get_pool(), SEEDED_SYMBIOT_ID, _N, list(notify.ALL_CHANNELS))
    assert len(pushes) == 1


def test_dispatch_is_a_noop_when_the_set_is_empty(client, monkeypatch):
    pushes = []
    _enable_web_push(monkeypatch, pushes)
    notify.dispatch(db.get_pool(), SEEDED_SYMBIOT_ID, _N, [])
    assert pushes == []


def _mark_present():
    # Stamp the seeded symbiot seen now, so presence.is_active reads them as watching the shell.
    with db.get_pool().connection() as conn:
        presence.mark_seen(conn, SEEDED_SYMBIOT_ID)


def _mark_seen_ago(seconds: float):
    # Stamp last_seen_at a fixed span in the past, to sit either side of the presence window.
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE symbiot SET last_seen_at = now() - make_interval(secs => %s) WHERE id = %s",
            (seconds, SEEDED_SYMBIOT_ID),
        )


def test_dispatch_holds_out_of_app_channels_when_present_and_courtesy(client, monkeypatch):
    pushes, emails = [], []
    _enable_web_push(monkeypatch, pushes)
    _enable_email(monkeypatch, emails)
    _mark_present()
    # A courtesy fan-out (suppress_when_present) to a symbiot who's watching the shell: both out-of-app channels
    # are held, because the live /inbox poll is already surfacing the record in front of them. The nudge collapses
    # to the durable record alone — exactly what a missive raised while the symbiot is present should do.
    notify.dispatch(db.get_pool(), SEEDED_SYMBIOT_ID, _N, list(notify.ALL_CHANNELS), suppress_when_present=True)
    assert pushes == [] and emails == []


def test_dispatch_ignores_presence_without_the_courtesy_switch(client, monkeypatch):
    pushes, emails = [], []
    _enable_web_push(monkeypatch, pushes)
    _enable_email(monkeypatch, emails)
    _mark_present()
    # The reminder path: an explicit request, so suppress_when_present is off (the default). Presence is never
    # even read, and both named channels fire though the symbiot is right here — an intent is not a courtesy.
    notify.dispatch(db.get_pool(), SEEDED_SYMBIOT_ID, _N, list(notify.ALL_CHANNELS))
    assert len(pushes) == 1 and len(emails) == 1


def test_dispatch_fires_when_absent_even_with_courtesy_on(client, monkeypatch):
    pushes, emails = [], []
    _enable_web_push(monkeypatch, pushes)
    _enable_email(monkeypatch, emails)
    # No stamp at all — a freshly seeded symbiot has never polled (last_seen_at null), so they read as absent
    # and the courtesy fan-out reaches out over every channel, exactly as it must when no one is watching.
    notify.dispatch(db.get_pool(), SEEDED_SYMBIOT_ID, _N, list(notify.ALL_CHANNELS), suppress_when_present=True)
    assert len(pushes) == 1 and len(emails) == 1


def test_dispatch_fires_when_presence_is_stale(client, monkeypatch):
    pushes, emails = [], []
    _enable_web_push(monkeypatch, pushes)
    _enable_email(monkeypatch, emails)
    _mark_seen_ago(config.PRESENCE_ACTIVE_WINDOW_SECONDS + 5)
    # Seen, but longer ago than the window — the tab has gone quiet (backgrounded, closed, or asleep). The
    # symbiot is owed the nudge again, so the courtesy fan-out fires over every channel despite the old stamp.
    notify.dispatch(db.get_pool(), SEEDED_SYMBIOT_ID, _N, list(notify.ALL_CHANNELS), suppress_when_present=True)
    assert len(pushes) == 1 and len(emails) == 1


def test_dispatch_present_still_honors_globally_disabled(client, monkeypatch):
    pushes, emails = [], []
    _enable_web_push(monkeypatch, pushes)
    _enable_email(monkeypatch, emails)
    _mark_present()
    with db.get_pool().connection() as conn:
        notification_prefs.set_channel(conn, SEEDED_SYMBIOT_ID, "web_push", False)
    # Presence and the global switch are independent filters that stack: web push is disabled outright, email is
    # held only because they're present. Both land on nothing here — proof the courtesy hold doesn't override or
    # replace the standing preference, it sits beside it.
    notify.dispatch(db.get_pool(), SEEDED_SYMBIOT_ID, _N, list(notify.ALL_CHANNELS), suppress_when_present=True)
    assert pushes == [] and emails == []
