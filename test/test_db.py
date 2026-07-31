"""The request connection: the guarantee that nothing a route writes is still pending when it answers.

One narrow contract, and the sort that is easy to lose by accident.
A FastAPI dependency that yields runs its cleanup after the response has gone out,
so a request connection whose transaction closed there would commit *after* the shell had its reply —
leaving a window where the shell holds an acknowledgement for a write nothing else can see yet.
The reminders listing is what made that corrosive: it hands out display positions off the reply,
so a set one change stale points at the wrong row.

db.get_conn closes the window by borrowing the connection in autocommit.
What these pin is that it does, and that it hands the connection back exactly as it found it —
because the pool is shared with the background workers,
and one of them (worker._process_one) leans on the implicit transaction
to commit a reply and its conversation row together.
The live read-after-write behaviour over real HTTP is the by-hand smoke's to prove (test/qa/0012).
"""

from core import db


def test_the_request_connection_is_autocommit_so_a_write_lands_before_the_reply(client):
    # Each statement commits as it runs, so by the time a handler returns there is nothing pending at all.
    gen = db.get_conn()
    conn = next(gen)
    try:
        assert conn.autocommit is True
    finally:
        for _ in gen:
            pass


def test_the_connection_goes_back_to_the_pool_as_it_was_found(client):
    # A leaked autocommit would quietly take the workers' implicit transaction away from them,
    # and it would do it silently — nothing would fail, things would just stop landing together.
    pool = db.get_pool()
    with pool.connection() as conn:
        before = conn.autocommit
    gen = db.get_conn()
    borrowed = next(gen)
    assert borrowed.autocommit is True
    for _ in gen:
        pass
    assert borrowed.autocommit is before
    with pool.connection() as conn:
        assert conn.autocommit is before


def test_a_route_write_is_visible_on_another_connection_at_once(client, monkeypatch):
    # The end-to-end shape of the same guarantee, as far as a TestClient can see it:
    # a write made through the request connection is readable from a second, independent one
    # without anything having to close first.
    gen = db.get_conn()
    conn = next(gen)
    try:
        conn.execute("INSERT INTO symbiot (email) VALUES ('autocommit-probe@example.test')")
        with db.get_pool().connection() as other:
            found = other.execute(
                "SELECT count(*) FROM symbiot WHERE email = 'autocommit-probe@example.test'"
            ).fetchone()[0]
        assert found == 1, "the write should be committed the moment the statement ran"
    finally:
        for _ in gen:
            pass


def test_an_explicit_transaction_still_lands_all_or_nothing(client):
    # The escape hatch autocommit leaves for the writes that must go together
    # (identity.verify_login_code spending a code and minting a session is the one that most has to hold).
    gen = db.get_conn()
    conn = next(gen)
    try:
        try:
            with conn.transaction():
                conn.execute("INSERT INTO symbiot (email) VALUES ('txn-probe-a@example.test')")
                conn.execute("INSERT INTO symbiot (email) VALUES ('txn-probe-b@example.test')")
                raise RuntimeError("something went wrong halfway")
        except RuntimeError:
            pass
        with db.get_pool().connection() as other:
            left = other.execute(
                "SELECT count(*) FROM symbiot WHERE email LIKE 'txn-probe-%%@example.test'"
            ).fetchone()[0]
        assert left == 0, "a failed explicit transaction leaves neither statement behind"
    finally:
        for _ in gen:
            pass
