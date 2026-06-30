"""The kernel — the privileged core behind kernel.os-joy.com.

It exposes a small HTTP surface, every response in one envelope:
GET /health is the round trip the shell's connectivity dot probes;
POST /intake takes a line off the shell's prompt and answers "copy",
the channel by which content crosses the wire;
and the /login, /login/verify, /status, /logout routes are identity —
a one-time code emailed to a registered symbiot, spent for a session.
The privileged work — the buffer, the Dead Man's Switch —
layers on top of these round trips, never beside them.
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware

import config
import db
import identity
import logs
from dtos import IntakeRequest, LoginRequest, VerifyRequest
from email_client import EmailClient, GmailEmailClient
from rate_limit import RateLimitMiddleware

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
    yield
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

# The one reply /login ever gives —
# identical for a known address, an unknown one, or a recipient-smuggling string —
# so it's no oracle for who's registered.
LOGIN_REPLY = "if that address is registered, a login code is on its way"


@app.get("/")
def root() -> dict:
    # A name on the door:
    # anyone landing at the bare host gets a legible word back rather than a 404,
    # still inside the one envelope.
    return envelope("the ghost in the shell", {"version": VERSION})


@app.get("/health")
def health() -> dict:
    # The simplest possible round trip:
    # a reachable network with a dead kernel must read offline;
    # only a real 200 from here flips it green.
    return envelope("ok", {"version": VERSION})


@app.post("/intake")
def intake(body: IntakeRequest) -> dict:
    # Receive a line and acknowledge it.
    # FastAPI validates the body against the DTO.
    # We read it only to log a timestamped, content-free receipt — the line
    # count, never the text — then drop it: "copy" means *The Joy received it*,
    # not that it kept it. A reconnect arrives as one batch (the outbox joins its
    # queued lines with newlines), so the count tells a live tail whether one
    # POST carried one line or a whole drained queue.
    # Holding the line in the buffer is a separate concern that layers on top of this round trip,
    # never ahead of it.
    logs.get("intake").info("intake — %d line(s)", body.line.count("\n") + 1)
    return envelope("copy")


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
    return envelope(LOGIN_REPLY)


@app.post("/login/verify")
def login_verify(body: VerifyRequest, conn=Depends(db.get_conn)) -> dict:
    token = identity.verify_login_code(conn, body.address, body.code)
    if token is None:
        return envelope("that code didn't work — try again", None)
    status = identity.session_status(conn, token)
    return envelope("logged in", {"token": token, "email": status["email"]})


@app.post("/logout")
def logout(conn=Depends(db.get_conn), token: str | None = Depends(bearer_token)) -> dict:
    # Idempotent: no token, or a token already revoked, is a clean no-op.
    identity.logout(conn, token)
    return envelope("logged out", {"authed": False})


@app.get("/status")
def status(conn=Depends(db.get_conn), token: str | None = Depends(bearer_token)) -> dict:
    st = identity.session_status(conn, token)
    return envelope("authed" if st["authed"] else "not logged in", st)
