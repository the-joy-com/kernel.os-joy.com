"""The kernel — the privileged core behind kernel.os-joy.com.

First slice: prove the server is even there. One endpoint, GET /health,
answering 200 so the shell's connectivity dot has something real to probe.
Everything else (send/ack, identity, the buffer, the Dead Man's Switch)
lands on top of this round trip, never beside it.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Pinned here for now, the single source the envelope reports.
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
    allow_methods=["GET"],
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
