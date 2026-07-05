"""The identity flow, one test per build-log box (session_14.md, 198–206).

Everything runs against the test database with a FakeEmailClient,
so a passing suite proves the state machine and its security invariants — not the wire.
The wire (real Gmail, the live kernel) is proven separately, by hand,
before any box is ticked.
"""

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from core import config
from core import db
from conftest import SYMBIOT_EMAIL, count_codes, extract_code


def _login(client, address):
    return client.post("/login", json={"address": address})


def _verify(client, code, address=SYMBIOT_EMAIL):
    return client.post("/login/verify", json={"address": address, "code": code})


def test_login_issues_code_and_authes(client, fake_email):  # B198
    r = _login(client, SYMBIOT_EMAIL)
    assert r.status_code == 200
    code = extract_code(fake_email)
    assert fake_email.sent[-1].to == SYMBIOT_EMAIL

    v = _verify(client, code)
    assert v.status_code == 200
    token = v.json()["data"]["token"]
    assert token

    s = client.get("/status", headers={"Authorization": f"Bearer {token}"})
    body = s.json()["data"]
    assert body["authed"] is True
    assert body["email"] == SYMBIOT_EMAIL


def test_wrong_code_rejected_then_retry(client, fake_email):  # B199
    _login(client, SYMBIOT_EMAIL)
    code = extract_code(fake_email)

    wrong = "111111" if code != "111111" else "222222"
    bad = _verify(client, wrong)
    assert bad.status_code == 200
    assert bad.json()["data"] is None  # stays unauthed

    # the correct code still works afterwards — a bad guess never derails the flow
    good = _verify(client, code)
    assert good.json()["data"]["token"]


def test_empty_address_sends_nothing(client, fake_email):  # B200
    for blank in ["", "   "]:
        r = _login(client, blank)
        assert r.status_code == 200
    assert fake_email.sent == []
    assert count_codes() == 0


def test_unknown_address_identical_reply_no_email(client, fake_email):  # B201
    known = _login(client, SYMBIOT_EMAIL).json()
    fake_email.sent.clear()

    unknown = _login(client, "stranger@example.com").json()
    assert unknown == known  # byte-identical: no enumeration oracle
    assert fake_email.sent == []


@pytest.mark.parametrize(
    "address",
    [
        "symbiot@example.com, attacker@evil.com",
        "symbiot@example.com;attacker@evil.com",
        "symbiot@example.com.evil.com",
        "attacker+symbiot@example.com",
        "symbiot@example.com\nattacker@evil.com",
    ],
)
def test_recipient_smuggling_sends_nothing(client, fake_email, address):  # B202
    canonical = _login(client, SYMBIOT_EMAIL).json()
    fake_email.sent.clear()

    r = _login(client, address)
    assert r.json() == canonical  # same reply as success
    assert fake_email.sent == []  # nothing to anyone, smuggled or otherwise


def test_only_latest_code_works(client, fake_email):  # B203
    _login(client, SYMBIOT_EMAIL)
    first = extract_code(fake_email)
    _login(client, SYMBIOT_EMAIL)
    second = extract_code(fake_email)
    if first == second:
        pytest.skip("two random codes collided; nothing to distinguish")

    rejected = _verify(client, first)
    assert rejected.json()["data"] is None  # the earlier code is dead

    accepted = _verify(client, second)
    assert accepted.json()["data"]["token"]  # only the latest authes


def test_reissue_never_accumulates_codes(client, fake_email):  # security invariant
    # Three issuances overwrite one row in place — never pile up live codes.
    for _ in range(3):
        _login(client, SYMBIOT_EMAIL)
    assert count_codes() == 1


def test_db_forbids_two_live_codes(client):  # security invariant — timing-independent
    # The partial unique index makes two spendable codes impossible at the row level,
    # so no interleaving of concurrent /login calls can ever leave two codes alive.
    # Proven against the constraint directly, not by racing requests:
    # this is the guarantee the application code leans on, asserted at its source.
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    with db.get_pool().connection() as conn:
        symbiot_id = conn.execute("SELECT id FROM symbiot LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO login_code (symbiot_id, code_hash, expires_at) VALUES (%s, %s, %s)",
            (symbiot_id, "live-code-one", future),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO login_code (symbiot_id, code_hash, expires_at) VALUES (%s, %s, %s)",
                (symbiot_id, "live-code-two", future),
            )


def test_code_dies_after_too_many_wrong_attempts(client, fake_email):  # security invariant
    # A live code absorbs a fixed budget of wrong guesses, then the database burns it —
    # so brute force is bounded by the row, not by how fast an attacker can guess.
    _login(client, SYMBIOT_EMAIL)
    code = extract_code(fake_email)
    wrong = "000000" if code != "000000" else "999999"

    for _ in range(config.MAX_VERIFY_ATTEMPTS):
        assert _verify(client, wrong).json()["data"] is None

    # The budget is spent: even the correct code can no longer spend the dead row.
    assert _verify(client, code).json()["data"] is None


def test_wrong_address_never_burns_anothers_code(client, fake_email):  # security invariant
    # A guess against an address with no live code is charged to no one,
    # so an attacker can't spend the symbiot's attempt budget without knowing it's theirs.
    _login(client, SYMBIOT_EMAIL)
    code = extract_code(fake_email)

    for _ in range(config.MAX_VERIFY_ATTEMPTS + 3):
        assert _verify(client, "000000", address="stranger@example.com").json()["data"] is None

    # The symbiot's own code is untouched and still spends.
    assert _verify(client, code).json()["data"]["token"]


def test_reissue_within_interval_sends_one_email(client, fake_email, monkeypatch):  # security invariant
    # With a real cool-off, a second /login inside the window is a no-op:
    # no second email, no new row — and the code already delivered still works.
    monkeypatch.setattr(config, "LOGIN_REISSUE_INTERVAL_SECONDS", 60)
    _login(client, SYMBIOT_EMAIL)
    _login(client, SYMBIOT_EMAIL)

    assert len(fake_email.sent) == 1
    assert count_codes() == 1
    code = extract_code(fake_email)
    assert _verify(client, code).json()["data"]["token"]


@pytest.mark.parametrize("junk", ["banana", "12345", "   not an email   "])
def test_non_email_address_treated_as_unknown(client, fake_email, junk):  # documents: no format gate
    # We never validate the address's shape — we only match it. Garbage that isn't even
    # email-shaped takes the same no-match path as any unknown address: no code, no email,
    # the one canonical reply. (Typo feedback is the shell's job, not the kernel's.)
    canonical = _login(client, SYMBIOT_EMAIL).json()
    fake_email.sent.clear()

    assert _login(client, junk).json() == canonical
    assert fake_email.sent == []
    assert count_codes() == 1  # only the symbiot's own code, none for the junk


def test_status_unauthed_without_token(client):  # B204 (kernel half)
    s = client.get("/status")
    assert s.status_code == 200
    assert s.json()["data"]["authed"] is False


def test_logout_drops_session(client, fake_email):  # B205
    _login(client, SYMBIOT_EMAIL)
    token = _verify(client, extract_code(fake_email)).json()["data"]["token"]

    out = client.post("/logout", headers={"Authorization": f"Bearer {token}"})
    assert out.status_code == 200

    s = client.get("/status", headers={"Authorization": f"Bearer {token}"})
    assert s.json()["data"]["authed"] is False


def test_logout_idempotent_when_unauthed(client):  # B206
    out = client.post("/logout")  # no token at all
    assert out.status_code == 200
    assert out.json()["data"]["authed"] is False  # clean no-op, never errors
