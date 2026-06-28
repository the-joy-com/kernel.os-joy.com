"""Identity: issuing one-time codes, spending them for sessions,
and reading or revoking those sessions.

Everything that touches a secret hashes it before it reaches the database —
HMAC with the server secret —
so a leaked table never yields a usable code or token.
The one rule that makes login safe against address-enumeration and recipient-smuggling lives here too:
a code is issued *only* on an exact match to a registered symbiot,
and the caller's reply is identical whether that match happened or not.
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import config
from email_client import EmailClient

# Codes are short and human-typed; sessions are long and machine-held.
_CODE_DIGITS = 6


def _hash(value: str) -> str:
    """HMAC a secret value with the server secret before it's stored."""
    return hmac.new(
        config.KERNEL_SECRET.encode(), value.encode(), hashlib.sha256
    ).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def issue_login_code(conn, address: str, email_client: EmailClient) -> None:
    """Issue a fresh code to a symbiot — but only if `address` matches a registered one.

    Normalises the address and looks for an exact symbiot match.
    No match (an unknown address, a blank one, a recipient-smuggling string) means no code and no email —
    and the caller returns the same reply regardless,
    so nothing here is observable from outside.
    On a match, the symbiot's single live code is overwritten in place,
    so only the newest one can ever be spent.
    """
    normalized = address.strip().lower()
    row = conn.execute(
        "SELECT id, email FROM symbiot WHERE email = %s", (normalized,)
    ).fetchone()
    if row is None:
        return
    symbiot_id, email = row

    code = f"{secrets.randbelow(10**_CODE_DIGITS):0{_CODE_DIGITS}d}"
    now = _now()
    expires_at = now + timedelta(seconds=config.LOGIN_CODE_TTL_SECONDS)
    reissue_cutoff = now - timedelta(seconds=config.LOGIN_REISSUE_INTERVAL_SECONDS)
    # Two guarantees, both in the row layer rather than in the order these statements run:
    # (1) one spendable code per symbiot —
    #     the partial unique index login_code_one_live_per_symbiot makes a second live code impossible,
    #     so the upsert overwrites the single live row in place instead of expire-then-insert;
    # (2) at most one fresh code per re-issue interval —
    #     the DO UPDATE fires only when the live code is already older than the cutoff,
    #     so a burst of /login taps can't become a burst of mail,
    #     and a double-tap never invalidates the code already in the inbox.
    #     The database decides whether a code was issued (RETURNING tells us);
    #     we email only when it says one was.
    issued = conn.execute(
        "INSERT INTO login_code (symbiot_id, code_hash, expires_at) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (symbiot_id) WHERE consumed_at IS NULL "
        "DO UPDATE SET code_hash = EXCLUDED.code_hash, "
        "expires_at = EXCLUDED.expires_at, created_at = now(), failed_attempts = 0 "
        "WHERE login_code.created_at <= %s "
        "RETURNING id",
        (symbiot_id, _hash(code), expires_at, reissue_cutoff),
    ).fetchone()
    if issued is None:
        # A fresh code already exists within the re-issue interval: keep it, send nothing.
        return

    email_client.send(
        to=email,
        subject="Your Joy login code",
        body=f"Your one-time login code is {code}\n\n"
        f"It expires in {config.LOGIN_CODE_TTL_SECONDS // 60} minutes. "
        f"If you didn't ask to log in, ignore this.",
    )


def logout(conn, token: str | None) -> None:
    """Revoke the session a token names.
    Idempotent — an absent or already-revoked token is a clean no-op."""
    if not token:
        return
    conn.execute(
        "UPDATE session SET revoked_at = now() "
        "WHERE token_hash = %s AND revoked_at IS NULL",
        (_hash(token),),
    )


def session_status(conn, token: str | None) -> dict:
    """Who, if anyone, this token authenticates. Always returns a status dict."""
    if not token:
        return {"authed": False, "email": None}
    row = conn.execute(
        "SELECT s.email FROM session se JOIN symbiot s ON s.id = se.symbiot_id "
        "WHERE se.token_hash = %s AND se.revoked_at IS NULL AND se.expires_at > now()",
        (_hash(token),),
    ).fetchone()
    if row is None:
        return {"authed": False, "email": None}
    return {"authed": True, "email": row[0]}


def verify_login_code(conn, address: str, code: str) -> str | None:
    """Spend a code for a session token, or return None if it can't be spent.

    The address names whose code this is,
    so a wrong guess can be charged against that symbiot's live code:
    after config.MAX_VERIFY_ATTEMPTS wrong tries the database burns it,
    making brute force a bounded budget the row enforces rather than a race the search space happens to win.
    A correct guess on an unconsumed, unexpired code marks it consumed (single-use) and mints a session;
    the plaintext token is returned once and only its hash is stored.
    Every failure — unknown address, no live code, wrong code, spent budget —
    returns None identically, so nothing here is an oracle.
    """
    normalized = address.strip().lower()
    row = conn.execute(
        "SELECT lc.id, lc.symbiot_id, lc.code_hash FROM login_code lc "
        "JOIN symbiot s ON s.id = lc.symbiot_id "
        "WHERE s.email = %s AND lc.consumed_at IS NULL AND lc.expires_at > now() "
        "ORDER BY lc.created_at DESC LIMIT 1",
        (normalized,),
    ).fetchone()
    if row is None:
        return None
    code_id, symbiot_id, code_hash = row

    if not hmac.compare_digest(_hash(code), code_hash):
        # Wrong guess: charge it against the code, and burn the code if the budget's spent.
        conn.execute(
            "UPDATE login_code SET failed_attempts = failed_attempts + 1, "
            "consumed_at = CASE WHEN failed_attempts + 1 >= %s THEN now() ELSE consumed_at END "
            "WHERE id = %s",
            (config.MAX_VERIFY_ATTEMPTS, code_id),
        )
        return None

    conn.execute(
        "UPDATE login_code SET consumed_at = now() WHERE id = %s", (code_id,)
    )

    token = secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(seconds=config.SESSION_TTL_SECONDS)
    conn.execute(
        "INSERT INTO session (symbiot_id, token_hash, expires_at) VALUES (%s, %s, %s)",
        (symbiot_id, _hash(token), expires_at),
    )
    return token
