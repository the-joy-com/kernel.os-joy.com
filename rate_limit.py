"""Rate limiting: a small in-memory sliding-window limiter at the kernel's edge.

Hand-rolled, no dependency — the same spirit as the hand-rolled migrations and HMAC.
The moment a request can send an email (/login) or guess a code (/login/verify),
an unthrottled endpoint is an email cannon and a brute-force oracle,
so the limit lives here, in front of the routes, not inside them.

Keyed by client IP.
The kernel sits on 127.0.0.1 behind nginx,
so the socket peer is always localhost —
the real caller's address arrives in the `X-Real-IP` header nginx stamps (`proxy_set_header X-Real-IP $remote_addr;`).
Without that header every caller collapses into one bucket and the limiter throttles the whole world as if it were a single client,
so setting it is a deploy step, not an option.

State is a per-(ip, route) window of recent request timestamps, pruned on touch.
One uvicorn process holds it in memory;
a restart forgets it, which is fine —
this is abuse-dampening, not accounting.
"""

import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

import config

# Per-route ceilings as (max_requests, window_seconds), keyed by exact path.
# The two sensitive routes are tight;
# everything else gets a generous default that a human — even one double-tapping or retrying on a flaky line — will never reach,
# but a script hammering the kernel will.
_LIMITS = {
    "/login": (5, 60),  # caps email bombing; a person needs one or two
    "/login/verify": (10, 60),  # a 6-digit code in a 10-min TTL stays infeasible
}
_DEFAULT_LIMIT = (120, 60)  # generous headroom for the shell's polling and retries

# /health is the connectivity probe;
# throttling it would make a healthy kernel read offline to the shell's dot.
# It is the one route the limiter never touches.
_EXEMPT = {"/health"}


class RateLimiter:
    """The counting itself, kept apart from the wiring so a test can reset it."""

    def __init__(self, *, enabled: bool = True):
        self.enabled = enabled
        self._hits: dict[tuple[str, str], deque] = defaultdict(deque)

    def reset(self) -> None:
        """Forget all counters — used between tests so state can't leak across them."""
        self._hits.clear()

    def retry_after(self, path: str, client_key: str, now: float) -> int | None:
        """Seconds to wait if this hit is over the limit, else None.

        When the hit is allowed it's recorded;
        when it's refused nothing is recorded,
        so a blocked caller can't push their own window forward by hammering.
        """
        if not self.enabled or path in _EXEMPT:
            return None
        limit, window = _LIMITS.get(path, _DEFAULT_LIMIT)
        hits = self._hits[(client_key, path)]
        # Drop the timestamps that have aged out of the window.
        while hits and hits[0] <= now - window:
            hits.popleft()
        if len(hits) >= limit:
            return int(window - (now - hits[0])) + 1
        hits.append(now)
        return None


# One process, one limiter; a restart forgets the counters, which is fine.
limiter = RateLimiter(enabled=config.RATE_LIMIT_ENABLED)


def _client_key(request: Request) -> str:
    # Trust only the address our own nginx stamps;
    # fall back to the socket peer (which, behind nginx, is localhost — see the module docstring).
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        wait = limiter.retry_after(
            request.url.path, _client_key(request), time.monotonic()
        )
        if wait is not None:
            # Mirror main.envelope({...}); importing it here would be circular.
            return JSONResponse(
                status_code=429,
                content={
                    "msg": "slow down — too many requests, try again shortly",
                    "data": None,
                },
                headers={"Retry-After": str(wait)},
            )
        return await call_next(request)
