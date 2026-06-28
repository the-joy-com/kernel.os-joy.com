"""The kernel — the privileged core behind kernel.os-joy.com.

It exposes a small HTTP surface, every response in one envelope:
GET /health is the round trip the shell's connectivity dot probes;
POST /intake takes a line off the shell's prompt and answers "copy",
the channel by which content crosses the wire.
The privileged work — identity, the buffer, the Dead Man's Switch —
layers on top of these round trips, never beside them.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from dtos import IntakeRequest

# The single source the envelope reports.
# Bump in lockstep with real changes to what the kernel answers.
VERSION = "0.0.1"

app = FastAPI(title="kernel.os-joy.com", version=VERSION)

# The shell runs on a different origin (shell.os-joy.com, or localhost in dev),
# so the browser needs explicit permission to *read* the kernel's responses —
# without it the connectivity dot's fetch is blocked and the kernel reads dead even when it's up.
# Only the shell's own origins, only the method it uses;
# nothing wildcarded.
ALLOWED_ORIGINS = [
    "https://shell.os-joy.com",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    # GET for the health probe, POST for the shell sending a line to /intake.
    # The browser preflights the POST (it carries a JSON body);
    # the middleware answers that OPTIONS itself, so it isn't listed here.
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def envelope(msg: str, data=None) -> dict:
    """Every kernel response wears the same shape.

    `msg` is a human-legible line about what happened; `data` is the
    payload to act on — a JSON array, a JSON object, or null when there's
    nothing to carry. The shell always knows where to look and never has
    to guess the shape per route.
    """
    return {"msg": msg, "data": data}


@app.get("/")
def root() -> dict:
    # A name on the door: anyone landing at the bare host gets a legible
    # word back rather than a 404, still inside the one envelope.
    return envelope("the ghost in the shell", {"version": VERSION})


@app.get("/health")
def health() -> dict:
    # The simplest possible round trip: a reachable network with a dead
    # kernel must read offline; only a real 200 from here flips it green.
    return envelope("ok", {"version": VERSION})


@app.post("/intake")
def intake(_body: IntakeRequest) -> dict:
    # Receive a line and acknowledge it. FastAPI validates the body against the DTO.
    # We then drop it on purpose (hence the leading underscore): 
    # "copy" means *The Joy received it*, not that it kept it.
    # Holding the line in the buffer is a separate concern that layers on top of this round trip, 
    # never ahead of it.
    return envelope("copy")
