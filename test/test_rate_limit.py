"""The edge rate limiter: the kernel sheds abusive volume before it reaches a route.

This is the soft outer layer — the cheap gate, not the vault. The durable guarantees
(one live code, a bounded re-issue interval, a per-code attempt budget) live in the
database and are proven in test_identity.py. Here we only prove the gate itself:
the sensitive routes refuse a burst, the health probe never does, and one noisy client
can't spend another's budget. The address is deliberately a non-match — the limiter sits
in front of the route, so it counts every call whether or not a code would be issued.
"""

JUNK_ADDRESS = "nobody@example.com"


def _post_login(client, ip=None):
    headers = {"X-Real-IP": ip} if ip else {}
    return client.post("/login", json={"address": JUNK_ADDRESS}, headers=headers)


def test_health_is_never_throttled(client):
    # The connectivity probe is exempt: a healthy kernel must never read offline,
    # however hard the shell's dot polls it.
    for _ in range(200):
        assert client.get("/health").status_code == 200


def test_limit_is_per_client_ip(client):
    # Keyed by X-Real-IP (what nginx stamps), so exhausting one caller's budget
    # leaves a different caller untouched.
    for _ in range(6):
        _post_login(client, ip="10.0.0.1")
    assert _post_login(client, ip="10.0.0.1").status_code == 429
    assert _post_login(client, ip="10.0.0.2").status_code == 200


def test_login_throttled_after_burst(client):
    # /login is capped at 5/min; the sixth call in a burst is refused.
    last = None
    for _ in range(6):
        last = _post_login(client)
    assert last.status_code == 429
    assert last.json()["data"] is None
    assert "retry-after" in {k.lower() for k in last.headers}
