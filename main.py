"""The kernel — the privileged core behind kernel.os-joy.com.

It exposes a small HTTP surface, every response in one envelope:
GET /health is the round trip the shell's connectivity dot probes;
POST /intake takes a line off the shell's prompt and answers "roger",
the channel by which content crosses the wire;
and the /login, /login/verify, /status, /logout routes are identity —
a one-time code emailed to a registered symbiot, spent for a session.
The privileged work — the buffer, the Dead Man's Switch —
layers on top of these round trips, never beside them.
"""

import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware

from core import config
from core import db
from services import identity
from core import logs
from core import protocol
from services import push
from services import worker
from core.dtos import (
    DeliveredRequest,
    IntakeRequest,
    LoginRequest,
    PushSubscriptionRequest,
    SeenRequest,
    VerifyRequest,
)

# The /intake handler is named intake(),
# so the module is reached by its function rather than imported as `intake` (which the handler would shadow).
from services.intake import mark_delivered, read_outcome, record_message, recover_orphaned
from services.missive import mark_seen, unseen_for_symbiot
from services.email_client import EmailClient, GmailEmailClient
from core.rate_limit import RateLimitMiddleware

# The single source the envelope reports.
# Bump in lockstep with real changes to what the kernel answers.
VERSION = "0.0.1"

# Wire the kernel's timestamped log stream before anything logs (db on startup,
# the routes below). Each call site names its own area via logs.get("<area>").
logs.configure()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Stand up the database before serving:
    # open the pool, bring the schema in line, and ensure the seeded symbiot exists.
    # Migrations are idempotent, so this is safe on every boot.
    db.open_pool(config.DATABASE_URL)
    db.migrate_and_seed(db.get_pool(), config.SYMBIOT_EMAIL)
    # Start the intake workers — the background loop that answers received messages.
    # A small pool, not one: a single slow or wedged message must not block every message behind it,
    # so several workers drain the queue in parallel (claim_next is race-safe, so they never grab the same message).
    # Alongside them, one reconcile sweep settles the rows a worker can't:
    # it fails a message stuck in 'working' past the ceiling,
    # re-queues a 'failed' one that still has attempts left,
    # and parks a spent one in 'abandoned' —
    # so every message reaches a terminal outcome, retried a bounded number of times along the way.
    # Threads, not asyncio tasks: the database work is synchronous and would block the event loop the API runs on.
    # Disabled under test, where the suite drives the state machine by hand.
    worker_stop = threading.Event()
    worker_threads = []
    if config.WORKER_ENABLED:
        # Before any worker of this process starts, reconcile rows the previous one left mid-work.
        # A row still 'working' at boot is an orphan — the worker that held it died with that process —
        # so recover_orphaned re-queues it for a fresh claim, or abandons it if its retry budget is spent.
        # Re-queued, not failed: the kernel fell over, the work didn't, so there's nothing to record as a failure.
        # Runs before the workers below so none can claim a row mid-recovery,
        # and only when workers are enabled — under test the suite drives the state machine, and this reconcile, by hand.
        log = logs.get("kernel")
        with db.get_pool().connection() as conn:
            requeued, abandoned = recover_orphaned(conn, config.MAX_INTAKE_ATTEMPTS)
        if requeued or abandoned:
            log.warning(
                "restart recovery: re-queued %d orphaned message(s), abandoned %d out of budget",
                requeued,
                len(abandoned),
            )
        # 'abandoned' is terminal: nudge each one's subscription (if any) that the kernel gave up,
        # the same as the reconcile sweep does — outside any transaction, a slow push must not hold a connection,
        # and a failed nudge must not break startup.
        for message_id in abandoned:
            try:
                push.notify(db.get_pool(), message_id)
            except Exception:
                log.exception("restart recovery: nudge failed for abandoned message %d", message_id)
        for n in range(config.WORKER_CONCURRENCY):
            thread = threading.Thread(
                target=worker.run,
                args=(worker_stop,),
                name=f"intake-worker-{n}",
                daemon=True,
            )
            thread.start()
            worker_threads.append(thread)
        # One reconcile sweep beside the pool — not another worker.
        # On a fixed cadence it fails messages stuck in 'working' past the deadline,
        # re-queues 'failed' ones that still have attempts, and parks the rest in 'abandoned',
        # so every row reaches a terminal outcome without a worker waiting between retries.
        sweep = threading.Thread(
            target=worker.run_reconcile_sweep,
            args=(worker_stop,),
            name="intake-reconcile-sweep",
            daemon=True,
        )
        sweep.start()
        worker_threads.append(sweep)
    yield
    worker_stop.set()
    for thread in worker_threads:
        thread.join(timeout=5)
    db.close_pool()


app = FastAPI(title="kernel.os-joy.com", version=VERSION, lifespan=lifespan)

# The edge rate limiter, added before CORS so CORS stays the outermost layer:
# that way even a 429 the limiter returns is dressed in the CORS headers the browser needs to read it,
# instead of surfacing to the shell as an opaque network failure.
app.add_middleware(RateLimitMiddleware)

# The shell runs on a different origin (shell.os-joy.com, or localhost in dev),
# so the browser needs explicit permission to *read* the kernel's responses —
# without it the connectivity dot's fetch is blocked and the kernel reads dead even when it's up.
# Only the shell's own origins, only the method it uses;
# nothing wildcarded.
ALLOWED_ORIGINS = [
    "https://shell.os-joy.com",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    # `vite preview` (the built-shell server) — the service worker, and so the
    # offline outbox, only exist in a real build, so they can only be tested there.
    "http://localhost:4173",
    "http://127.0.0.1:4173",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    # GET for the health probe and /status, POST for /intake and the login routes.
    # The browser preflights the POSTs (they carry a JSON body or an auth header);
    # the middleware answers that OPTIONS itself, so it isn't listed here.
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def envelope(msg: str, data=None) -> dict:
    """Every kernel response wears the same shape.

    `msg` is a human-legible line about what happened;
    `data` is the payload to act on — a JSON array, a JSON object, or null when there's nothing to carry.
    The shell always knows where to look and never has to guess the shape per route.
    """
    return {"msg": msg, "data": data}


# --- dependencies ---------------------------------------------------------

# Built once; tests override this dependency to inject a fake that records mail.
_email_client = GmailEmailClient(config.GMAIL_CREDENTIALS_FILE, config.GMAIL_SENDER)


def get_email_client() -> EmailClient:
    return _email_client


def bearer_token(authorization: str | None = Header(default=None)) -> str | None:
    """The token from an `Authorization: Bearer <token>` header, or None."""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


# --- routes ---------------------------------------------------------------

# Every `msg` a route returns is a token from protocol.py — the one catalog of what the
# kernel says to the shell — so the wire's vocabulary lives in one place, not scattered
# one literal per handler.


@app.get("/")
def root() -> dict:
    # A name on the door:
    # anyone landing at the bare host gets a legible word back rather than a 404,
    # still inside the one envelope.
    return envelope(protocol.GREETING, {"version": VERSION})


@app.get("/answers")
def answers(id: int, conn=Depends(db.get_conn)) -> dict:
    """Report what became of a message — the read half of the /intake round trip.

    When the shell sends a line, /intake writes it down and hands back an id.
    That's all the shell gets at first: an acknowledgement, not a reply,
    because the answer is computed afterward by a worker and isn't ready yet.
    So the shell holds onto the id and comes back here to ask "is it done, and how did it end?" —
    on next open, and while a message is in flight, until it settles.

    The reply is one of four words the shell knows how to act on (see protocol.py):
      answer    — it's done; the reply text rides along in data.answer.
      abandoned — the kernel tried its budget of times and gave up; said plainly, never left silent.
      wait out  — still in flight (received, working, or between retries); ask again later.
      unknown   — no message carries that id.
    The kernel's own state machine has more states than these,
    but the shell only needs to know settled-or-not and, if settled, which way —
    so the in-flight states collapse to the single "wait out".

    The id is a bare correlation handle: the kernel never echoes back the line the symbiot sent,
    so an answer crosses the wire as itself, for the shell to show on its own terms.
    And a failed message's stored traceback never crosses either —
    read_outcome can see it, but it stays the kernel's own diagnostic, not the symbiot's to read.
    """
    outcome = read_outcome(conn, id)
    if outcome is None:
        return envelope(protocol.ANSWER_UNKNOWN, {"id": id})
    status, answer = outcome
    if status == "answered":
        return envelope(protocol.ANSWER_READY, {"id": id, "answer": answer})
    if status == "abandoned":
        return envelope(protocol.ANSWER_ABANDONED, {"id": id})
    return envelope(protocol.ANSWER_PENDING, {"id": id})


@app.post("/answers/delivered")
def answers_delivered(body: DeliveredRequest, conn=Depends(db.get_conn)) -> dict:
    """Confirm the shell has shown a message's outcome — the reply's 'truly out' receipt.

    The outbox's COPY proves a line reached the kernel;
    this is the mirror on the way back:
    after the shell renders an answer (or an abandonment) it read off /answers,
    it POSTs the id here and the kernel stamps delivered_at —
    so 'answered' means the reply was produced, and delivered_at means it actually reached the symbiot,
    never a hopeful guess.
    Unauthed like /answers itself — the id is the capability —
    and the stamp only touches a terminal, still-undelivered row,
    so a stray, in-flight, or already-delivered id is a clean no-op rather than an error.
    """
    delivered = mark_delivered(conn, body.ids)
    return envelope(protocol.COPY, {"delivered": delivered})


@app.get("/health")
def health() -> dict:
    # The simplest possible round trip:
    # a reachable network with a dead kernel must read offline;
    # only a real 200 from here flips it green.
    return envelope(protocol.OK, {"version": VERSION})


@app.get("/inbox")
def inbox(conn=Depends(db.get_conn), token: str | None = Depends(bearer_token)) -> dict:
    """A symbiot's unseen inbound messages — the ones it couldn't have discovered on its own.

    The shell learns of an answer to its *own* line from the id it kept at intake, and asks
    /answers about it. But a message the kernel raises unprompted — a nudge, a line relayed
    from the World — was never sent from here, so there's no id to have kept; this is where
    the shell discovers those. Identity-gated on purpose: these messages are addressed to a
    symbiot, so a caller with no live session is owed nothing and gets an empty list rather
    than an error (nothing here is an oracle about who's registered or what's waiting).
    Each message carries its id and body; the shell shows it, then POSTs the ids to
    /inbox/seen so it isn't offered again.
    """
    symbiot_id = identity.authenticated_symbiot_id(conn, token)
    messages = (
        [{"id": mid, "body": body} for mid, body in unseen_for_symbiot(conn, symbiot_id)]
        if symbiot_id is not None
        else []
    )
    return envelope(protocol.TRAFFIC_WAITING, {"messages": messages})


@app.post("/inbox/seen")
def inbox_seen(
    body: SeenRequest,
    conn=Depends(db.get_conn),
    token: str | None = Depends(bearer_token),
) -> dict:
    """Acknowledge inbox messages the shell has shown, so /inbox stops offering them.

    Scoped to the caller's own missives, so an id that isn't theirs changes nothing.
    A no-op — no session, no ids, or ids already seen — is a clean no-op, never an error.
    """
    symbiot_id = identity.authenticated_symbiot_id(conn, token)
    seen = mark_seen(conn, symbiot_id, body.ids) if symbiot_id is not None else 0
    return envelope(protocol.COPY, {"seen": seen})


@app.post("/intake")
def intake(
    body: IntakeRequest,
    conn=Depends(db.get_conn),
    token: str | None = Depends(bearer_token),
) -> dict:
    # Receive a message, write it down, then acknowledge it.
    # FastAPI validates the body against the DTO;
    # we persist it as one 'received' row *before* answering, so "roger" now means
    # *The Joy has it, durably* — not "saw it and dropped it", as it did before.
    # The write commits in lockstep with this response (db.get_conn commits the request's transaction on success),
    # so the acknowledgement can never outrun the durable record behind it.
    # One row per request, never the lines within: a reconnect arrives as one batch
    # (the outbox joins its queued lines with newlines),
    # and that whole blob is one message — the kernel doesn't split on a newline it can't trust as a boundary.
    # The count stays a content-free transport diagnostic,
    # telling a live tail whether one POST carried one line or a whole drained queue.
    # Read who sent it from the session, now, while the request is in hand:
    # a live session names the symbiot, its absence is an anonymous line — both welcome, the input layer never gates on auth.
    # We stamp that on the row so the worker can answer by it later; the shell can't assert identity, only the token proves it.
    symbiot_id = identity.authenticated_symbiot_id(conn, token)
    message_id = record_message(conn, body.line, body.reply_channel_id, symbiot_id)
    logs.get("intake").info("intake — %d line(s)", body.line.count("\n") + 1)
    # Hand back the row's id: the batch crossed the wire with no identity of its own,
    # so this is the handle the shell keeps to ask /answers, later, what became of it.
    return envelope(protocol.COPY, {"id": message_id})


@app.post("/login")
def login(
    body: LoginRequest,
    conn=Depends(db.get_conn),
    email_client: EmailClient = Depends(get_email_client),
) -> dict:
    # Issue a code only on an exact symbiot match;
    # the reply never reveals whether that happened,
    # so an attacker learns nothing about who's registered.
    identity.issue_login_code(conn, body.address, email_client)
    return envelope(protocol.LOGIN_SENT)


@app.post("/login/verify")
def login_verify(body: VerifyRequest, conn=Depends(db.get_conn)) -> dict:
    token = identity.verify_login_code(conn, body.address, body.code)
    if token is None:
        return envelope(protocol.LOGIN_FAILED, None)
    status = identity.session_status(conn, token)
    return envelope(protocol.LOGGED_IN, {"token": token, "email": status["email"]})


@app.post("/logout")
def logout(
    conn=Depends(db.get_conn), token: str | None = Depends(bearer_token)
) -> dict:
    # Idempotent: no token, or a token already revoked, is a clean no-op.
    identity.logout(conn, token)
    return envelope(protocol.LOGGED_OUT, {"authed": False})


@app.get("/push/key")
def push_key() -> dict:
    # The public application server key the shell subscribes with.
    # Null when push is unconfigured — the shell reads that as "no push here" and falls back to poll-on-open,
    # so a kernel without a VAPID key still serves answers, just without the nudge.
    return envelope(protocol.PUSH_KEY, {"key": push.application_server_key()})


@app.post("/push/subscribe")
def push_subscribe(
    body: PushSubscriptionRequest,
    conn=Depends(db.get_conn),
    token: str | None = Depends(bearer_token),
) -> dict:
    # Register (or refresh) a browser's push address as a reply channel, and hand back its id —
    # the token the shell then threads through /intake so the kernel knows which channel to nudge when that message settles.
    # Ungated, like /intake: the right to be reachable is never fenced behind identity,
    # and a push address is nothing an attacker gains by planting.
    # A session, when one is present, ties the channel to that symbiot so the kernel can also
    # reach them for a missive (a message it raises on its own); without one it stays anonymous
    # and still serves per-message reply nudges.
    symbiot_id = identity.authenticated_symbiot_id(conn, token)
    channel_id = push.save_subscription(
        conn, body.endpoint, body.keys.p256dh, body.keys.auth, symbiot_id
    )
    return envelope(protocol.SUBSCRIBED, {"id": channel_id})


@app.get("/status")
def status(
    conn=Depends(db.get_conn), token: str | None = Depends(bearer_token)
) -> dict:
    st = identity.session_status(conn, token)
    return envelope(protocol.AUTHED if st["authed"] else protocol.NOT_AUTHED, st)
