"""Intake: durability at receipt, and the status path a message walks.

Everything runs against the test database, so a passing suite proves the durable
write and the state machine — not the wire.
The live round trip (a real POST landing a real row) is proven separately, by hand.
"""

import psycopg
import pytest

import db
import intake


def _rows():
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT id, message, status, created_at, updated_at, answer, failed_reason "
            "FROM intake ORDER BY id"
        ).fetchall()


def _insert(message="a message") -> int:
    # Land a message the way the route does, returning its id for the transition tests.
    with db.get_pool().connection() as conn:
        return intake.record_message(conn, message)


def test_intake_persists_one_received_row(client):
    # The route writes the message down before it answers, marked received.
    r = client.post("/intake", json={"line": "a captured thought"})
    assert r.status_code == 200
    assert r.json()["msg"] == "roger"

    rows = _rows()
    assert len(rows) == 1
    _id, message, status, created_at, updated_at, answer, failed_reason = rows[0]
    assert message == "a captured thought"
    assert status == "received"
    assert answer is None  # no reply until it's answered
    assert failed_reason is None  # nor a reason until (if) it fails
    assert created_at == updated_at  # nothing has touched it since it landed
    # The route hands back the row's id — the handle the shell keeps to ask /answers later.
    assert r.json()["data"]["id"] == _id


def test_batch_lands_as_one_message(client):
    # A reconnect drains the outbox as one request joining its queued lines with newlines;
    # the kernel stores that whole blob as one message,
    # never split on a newline it can't trust as a boundary.
    batch = "first thought\nsecond thought\nthird thought"
    client.post("/intake", json={"line": batch})

    rows = _rows()
    assert len(rows) == 1  # one row per request, not one per line
    assert rows[0][1] == batch  # every line intact, nothing dropped


def test_intake_tolerates_a_stale_reply_channel(client):
    # A browser can carry a reply_channel_id whose channel no longer exists —
    # its channel was pruned, or the database was reset under a browser that still remembers one.
    # That dangling id must not crash intake: the line is accepted, it just loses its nudge,
    # exactly as if the channel had been deleted (ON DELETE SET NULL).
    r = client.post(
        "/intake", json={"line": "line with a dead channel", "reply_channel_id": 999999}
    )
    assert r.status_code == 200  # not a 500 from a foreign-key violation
    assert r.json()["msg"] == "roger"

    with db.get_pool().connection() as conn:
        channel = conn.execute(
            "SELECT reply_channel_id FROM intake WHERE id = %s",
            (r.json()["data"]["id"],),
        ).fetchone()[0]
    assert channel is None  # the dangling id collapsed to NULL, never stored as a broken reference


def test_status_path_received_to_answered(client):
    _insert()
    with db.get_pool().connection() as conn:
        claimed = intake.claim_next(conn)
        assert claimed is not None
        message_id, _message, _symbiot = claimed
        assert intake.mark_answered(conn, message_id, "a reply") is True
    row = _rows()[0]
    assert row[2] == "answered"
    assert row[5] == "a reply"  # the reply is stored on the row


def test_status_path_received_to_failed(client):
    _insert()
    with db.get_pool().connection() as conn:
        message_id, *_ = intake.claim_next(conn)
        assert intake.mark_failed(conn, message_id, "a reason") is True
    row = _rows()[0]
    assert row[2] == "failed"
    assert row[6] == "a reason"  # why it failed is recorded, never silent


def test_claim_next_takes_oldest_first(client):
    older = _insert("older")
    _insert("newer")
    with db.get_pool().connection() as conn:
        claimed = intake.claim_next(conn)
    assert claimed[0] == older  # lowest id, oldest, taken first


def test_claim_next_none_when_nothing_waiting(client):
    with db.get_pool().connection() as conn:
        assert intake.claim_next(conn) is None


def test_claim_won_once(client):  # one row, one outcome — timing-independent
    # The only received row can be claimed once; a second claim finds nothing.
    _insert()
    with db.get_pool().connection() as conn:
        assert intake.claim_next(conn) is not None
        assert intake.claim_next(conn) is None  # already working, not received


def test_two_workers_claim_distinct_messages(client):  # what the worker pool leans on
    # Two workers claiming at the same time must never grab the same message.
    # Proven with overlapping transactions: while c1 holds its claimed row, c2 claims —
    # FOR UPDATE SKIP LOCKED makes c2 skip the locked row and take the other one.
    a = _insert("a")
    b = _insert("b")
    with db.get_pool().connection() as c1:
        first = intake.claim_next(c1)
        with db.get_pool().connection() as c2:
            second = intake.claim_next(c2)
    assert first is not None and second is not None
    assert {first[0], second[0]} == {a, b}  # two claimers, two different messages


def test_cannot_answer_before_claiming(client):  # the guard, from the other side
    # answered is reachable only from working, so a received-but-unclaimed message
    # can't jump straight to answered.
    message_id = _insert()
    with db.get_pool().connection() as conn:
        assert intake.mark_answered(conn, message_id, "x") is False
    assert _rows()[0][2] == "received"  # untouched


def test_one_message_one_terminal_state(client):  # received → working → answered XOR failed
    # A message leaves working in exactly one direction. Once answered, it can't also
    # be marked failed, and it can't be answered twice.
    _insert()
    with db.get_pool().connection() as conn:
        message_id, *_ = intake.claim_next(conn)
        assert intake.mark_answered(conn, message_id, "the reply") is True
        assert intake.mark_failed(conn, message_id, "x") is False  # can't flip an answered row
        assert intake.mark_answered(conn, message_id, "again") is False  # nor answer it twice
    assert _rows()[0][2] == "answered"


def _age(message_id: int, expression: str = "now() - interval '10 minutes'"):
    # Backdate a row's updated_at so the deadline sweep sees it as overdue,
    # without the test having to sleep out a real ceiling.
    with db.get_pool().connection() as conn:
        conn.execute(
            f"UPDATE intake SET updated_at = {expression} WHERE id = %s", (message_id,)
        )


def test_deadline_sweep_fails_overdue_working(client):  # the referee rules a hang
    # A message that has sat in working past the ceiling is failed, so a hang can't
    # leave it waiting on an answer forever.
    message_id = _insert()
    with db.get_pool().connection() as conn:
        intake.claim_next(conn)
    _age(message_id)
    with db.get_pool().connection() as conn:
        assert intake.fail_overdue(conn, 300) == 1
    row = _rows()[0]
    assert row[2] == "failed"
    assert row[6] == intake.SWEEP_REASON  # a swept failure says so, told apart from a worker's reason


def test_deadline_sweep_spares_fresh_working(client):  # work still in progress is left be
    # A message claimed a moment ago is well inside the ceiling; the sweep leaves it
    # working so live work isn't killed out from under itself.
    _insert()
    with db.get_pool().connection() as conn:
        intake.claim_next(conn)
        assert intake.fail_overdue(conn, 300) == 0
    assert _rows()[0][2] == "working"


def test_deadline_sweep_touches_only_working(client):  # received and answered are out of scope
    # The sweep is guarded on working, so an overdue row is failed only if it's working:
    # a never-claimed received row and an already-answered one are both left alone.
    answered = _insert("finished")  # oldest, claimed first
    working = _insert("hung")  # claimed but never finished
    still_received = _insert("never claimed")  # newest, stays received
    with db.get_pool().connection() as conn:
        answered_id, *_ = intake.claim_next(conn)
        assert answered_id == answered
        working_id, *_ = intake.claim_next(conn)
        assert working_id == working
        intake.mark_answered(conn, answered, "done")
    for message_id in (answered, working, still_received):
        _age(message_id)  # every row overdue, so only status decides what the sweep takes
    with db.get_pool().connection() as conn:
        assert intake.fail_overdue(conn, 300) == 1  # the working row, and only it
    by_id = {row[0]: row[2] for row in _rows()}
    assert by_id[still_received] == "received"  # never claimed, out of scope
    assert by_id[working] == "failed"
    assert by_id[answered] == "answered"  # a terminal row is never re-ruled


def test_transition_bumps_updated_at(client):  # the timeout step's clock
    # updated_at rides every transition, so "how long in the current state" is
    # answerable as now() - updated_at.
    _insert()
    created_at = _rows()[0][3]
    with db.get_pool().connection() as conn:
        intake.claim_next(conn)
    updated_at = _rows()[0][4]
    assert updated_at > created_at  # the move advanced the clock


def _attempts(message_id: int) -> int:
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT attempts FROM intake WHERE id = %s", (message_id,)
        ).fetchone()[0]


def test_claim_bumps_attempts(client):  # the retry budget's meter
    # Each claim counts as one attempt,
    # so the retry logic can bound how many tries a message gets.
    message_id = _insert()
    assert _attempts(message_id) == 0  # nothing tried yet
    with db.get_pool().connection() as conn:
        intake.claim_next(conn)
    assert _attempts(message_id) == 1  # one claim, one attempt


def test_requeue_failed_retries_within_budget(client):  # a failure gets another chance
    # A failed message with attempts to spare goes back to received for another try,
    # its stale reason cleared so the re-queued row is clean again.
    message_id = _insert()
    with db.get_pool().connection() as conn:
        intake.claim_next(conn)  # attempts -> 1
        intake.mark_failed(conn, message_id, "a transient blip")
    with db.get_pool().connection() as conn:
        assert intake.requeue_failed(conn, 3) == 1  # 1 attempt of 3, room to retry
    row = _rows()[0]
    assert row[2] == "received"  # back in the queue for a worker to claim
    assert row[6] is None  # failed_reason cleared on the way back
    assert _attempts(message_id) == 1  # re-queuing doesn't touch the meter; the next claim will


def test_abandon_exhausted_parks_out_of_budget(client):  # retries don't run forever
    # A message that has used its whole budget is parked in the terminal abandoned state
    # rather than retried again, keeping the reason it last failed for.
    message_id = _insert()
    with db.get_pool().connection() as conn:
        intake.claim_next(conn)  # attempts -> 1
        intake.mark_failed(conn, message_id, "keeps breaking")
    with db.get_pool().connection() as conn:
        assert intake.requeue_failed(conn, 1) == 0  # budget of 1 already spent — no retry
        assert intake.abandon_exhausted(conn, 1) == [message_id]  # so it's parked, by id
    row = _rows()[0]
    assert row[2] == "abandoned"  # terminal give-up
    assert row[6] == "keeps breaking"  # the reason it gave up on is kept


def test_retry_lifecycle_ends_in_abandoned(client):  # bounded retries, then a verdict
    # A message that fails every attempt is retried up to the budget and then abandoned —
    # the retrying is itself bounded, so it can't become a new way to loop forever.
    message_id = _insert()
    max_attempts = 3
    for _ in range(max_attempts):
        with db.get_pool().connection() as conn:
            claimed = intake.claim_next(conn)
            assert claimed is not None  # a received row is waiting each round
            intake.mark_failed(conn, message_id, "keeps breaking")
        with db.get_pool().connection() as conn:
            # the reconcile sweep's two moves: within budget re-queues, out of budget parks
            intake.requeue_failed(conn, max_attempts)
            intake.abandon_exhausted(conn, max_attempts)
    row = _rows()[0]
    assert row[2] == "abandoned"  # spent its tries, given a terminal verdict
    assert row[6] == "keeps breaking"  # still says why
    assert _attempts(message_id) == max_attempts  # tried exactly the budget, never more


# --- restart recovery: rows a dead process left mid-work ------------------------------


def test_recover_orphaned_requeues_within_budget(client):  # a restart orphan is retried, not failed
    # A row left 'working' by a dead process, with attempts to spare, goes back to received —
    # never failed: the kernel fell over, the work didn't, so no failure reason is invented.
    message_id = _insert()
    with db.get_pool().connection() as conn:
        intake.claim_next(conn)  # attempts -> 1, now 'working'
    with db.get_pool().connection() as conn:
        requeued, abandoned = intake.recover_orphaned(conn, 3)
    assert (requeued, abandoned) == (1, [])
    row = _rows()[0]
    assert row[2] == "received"  # back in the queue for a fresh claim
    assert row[6] is None  # no failure reason invented for a process that merely died
    assert _attempts(message_id) == 1  # recovery doesn't touch the meter; the next claim will


def test_recover_orphaned_abandons_when_budget_spent(client):  # a poison message can't loop across restarts
    # A row caught 'working' whose attempts are already spent is parked in abandoned,
    # not re-queued — so a message that crashes the kernel every time can't loop forever on restart.
    message_id = _insert()
    with db.get_pool().connection() as conn:
        intake.claim_next(conn)  # attempts -> 1
    with db.get_pool().connection() as conn:
        requeued, abandoned = intake.recover_orphaned(conn, 1)  # budget of 1 already spent
    assert requeued == 0
    assert abandoned == [message_id]
    row = _rows()[0]
    assert row[2] == "abandoned"  # terminal give-up
    assert row[6] == intake.ORPHAN_ABANDON_REASON  # says why, though it has no traceback to keep


def test_recover_orphaned_touches_only_working(client):  # received, answered, failed are out of scope
    # Recovery is guarded on 'working', so a row in any other state is left exactly as it is —
    # a failed row keeps its own reason rather than being overwritten by recovery's.
    answered = _insert("done")  # oldest, claimed first
    failed = _insert("broke")
    still_received = _insert("never claimed")  # newest, never claimed
    with db.get_pool().connection() as conn:
        a_id, *_ = intake.claim_next(conn)
        assert a_id == answered
        intake.mark_answered(conn, answered, "x")
        f_id, *_ = intake.claim_next(conn)
        assert f_id == failed
        intake.mark_failed(conn, failed, "its own reason")
    with db.get_pool().connection() as conn:
        assert intake.recover_orphaned(conn, 3) == (0, [])  # nothing was 'working'
    by_id = {row[0]: (row[2], row[6]) for row in _rows()}
    assert by_id[still_received] == ("received", None)
    assert by_id[answered] == ("answered", None)
    assert by_id[failed] == ("failed", "its own reason")  # untouched, not overwritten


def test_recover_orphaned_splits_by_budget(client):  # one call, both moves, disjoint
    # In a single recovery, an orphaned working row with budget is re-queued,
    # while one whose budget is spent is abandoned — the two moves partition the working rows, never overlap.
    requeued_row = _insert("has budget")  # oldest, claimed first
    abandoned_row = _insert("out of budget")
    with db.get_pool().connection() as conn:
        intake.claim_next(conn)  # requeued_row -> working, attempts 1
        intake.claim_next(conn)  # abandoned_row -> working, attempts 1
        # push one past the budget by hand, as re-runs across restarts would
        conn.execute("UPDATE intake SET attempts = 2 WHERE id = %s", (abandoned_row,))
    with db.get_pool().connection() as conn:
        requeued, abandoned = intake.recover_orphaned(conn, 2)  # budget of 2
    assert requeued == 1
    assert abandoned == [abandoned_row]
    by_id = {row[0]: row[2] for row in _rows()}
    assert by_id[requeued_row] == "received"  # attempts 1 < 2, retried
    assert by_id[abandoned_row] == "abandoned"  # attempts 2 >= 2, parked


# --- delivery confirmation: the reply's "truly out" receipt ---------------------------


def _delivered_at(message_id: int):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT delivered_at FROM intake WHERE id = %s", (message_id,)
        ).fetchone()[0]


def test_mark_delivered_stamps_a_shown_answer(client):  # answered → confirmed out
    # Once the shell has shown an answer, the kernel stamps delivered_at,
    # so a produced reply is told apart from one that actually reached the symbiot.
    message_id = _insert()
    with db.get_pool().connection() as conn:
        intake.claim_next(conn)
        intake.mark_answered(conn, message_id, "the reply")
    assert _delivered_at(message_id) is None  # produced, but not yet confirmed out
    with db.get_pool().connection() as conn:
        assert intake.mark_delivered(conn, [message_id]) == 1
    assert _delivered_at(message_id) is not None  # now truly out


def test_mark_delivered_covers_abandoned(client):  # an abandonment is an outcome too
    # The shell renders a give-up notice as much as an answer,
    # so a delivered abandonment is confirmed the same way.
    message_id = _insert()
    with db.get_pool().connection() as conn:
        intake.claim_next(conn)
        intake.mark_failed(conn, message_id, "kept breaking")
        intake.abandon_exhausted(conn, 1)  # budget of 1 already spent
    with db.get_pool().connection() as conn:
        assert intake.mark_delivered(conn, [message_id]) == 1
    assert _delivered_at(message_id) is not None


def test_mark_delivered_ignores_in_flight(client):  # nothing to confirm until it's settled
    # A message still in flight has no outcome to have been shown,
    # so a stray ack for it stamps nothing.
    message_id = _insert()  # received, never claimed
    with db.get_pool().connection() as conn:
        assert intake.mark_delivered(conn, [message_id]) == 0
    assert _delivered_at(message_id) is None


def test_mark_delivered_is_idempotent(client):  # a re-ack changes nothing
    # A second confirmation of an already-delivered message is a clean no-op,
    # so the stamp records the first delivery and never drifts.
    message_id = _insert()
    with db.get_pool().connection() as conn:
        intake.claim_next(conn)
        intake.mark_answered(conn, message_id, "the reply")
        assert intake.mark_delivered(conn, [message_id]) == 1
        assert intake.mark_delivered(conn, [message_id]) == 0  # already out, nothing to do


def test_answers_delivered_route_confirms_an_answer(client):  # the ack over the wire
    # The shell POSTs the id it has shown; the kernel rogers back and marks it delivered.
    message_id = _insert()
    with db.get_pool().connection() as conn:
        intake.claim_next(conn)
        intake.mark_answered(conn, message_id, "the reply")
    r = client.post("/answers/delivered", json={"ids": [message_id]})
    assert r.status_code == 200
    assert r.json()["msg"] == "roger"
    assert r.json()["data"]["delivered"] == 1
    assert _delivered_at(message_id) is not None


def test_answers_delivered_route_empty_is_a_clean_noop(client):  # nothing to confirm
    # Nothing shown this pass is not an error — the shell just has nothing to acknowledge.
    r = client.post("/answers/delivered", json={"ids": []})
    assert r.status_code == 200
    assert r.json()["data"]["delivered"] == 0


# --- the /answers route: the shell asking what became of a message it captured --------


def test_answers_pending_while_in_flight(client):
    # A message the worker hasn't finished reads "wait out" — the shell keeps waiting.
    message_id = _insert()
    r = client.get(f"/answers?id={message_id}")
    assert r.status_code == 200
    assert r.json()["msg"] == "wait out"
    assert r.json()["data"]["id"] == message_id


def test_answers_returns_the_reply_once_answered(client):
    # Answered → the reply comes back, tied to the id so it's never an orphan.
    message_id = _insert()
    with db.get_pool().connection() as conn:
        intake.claim_next(conn)
        intake.mark_answered(conn, message_id, "the reply")
    body = client.get(f"/answers?id={message_id}").json()
    assert body["msg"] == "answer"
    assert body["data"] == {"id": message_id, "answer": "the reply"}


def test_answers_says_abandoned_when_given_up(client):
    # Abandoned → the kernel gave up, said plainly. The traceback is NOT leaked to the wire.
    message_id = _insert()
    with db.get_pool().connection() as conn:
        intake.claim_next(conn)
        intake.mark_failed(conn, message_id, "a private traceback")
        intake.abandon_exhausted(conn, 1)  # budget of 1 already spent
    body = client.get(f"/answers?id={message_id}").json()
    assert body["msg"] == "abandoned"
    assert "traceback" not in str(body["data"]).lower()  # the reason stays the kernel's own


def test_answers_unknown_for_a_missing_id(client):
    # No such message — the shell learns the id buys nothing rather than hanging on it.
    body = client.get("/answers?id=999999").json()
    assert body["msg"] == "unknown"
    assert body["data"]["id"] == 999999


# --- the diary bedrock: the words are verbatim, immutable, and never deleted -----------


def test_message_cannot_be_edited(client):  # verbatim, now enforced by the database
    # A stored message's text is immutable: the words the symbiot handed over are kept
    # exactly, and the database refuses any update that would rewrite them — so "keep the
    # words verbatim" no longer rests on no caller happening to touch the column.
    message_id = _insert("the exact words")
    with pytest.raises(psycopg.errors.RaiseException):
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE intake SET message = %s WHERE id = %s", ("rewritten", message_id)
            )
    assert _rows()[0][1] == "the exact words"  # untouched, the edit refused


def test_intake_row_cannot_be_deleted(client):  # forever, now enforced by the database
    # A message is walked to a terminal state, never erased: the diary keeps every entry
    # forever, so the database refuses to delete an intake row outright.
    message_id = _insert("kept forever")
    with pytest.raises(psycopg.errors.RaiseException):
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM intake WHERE id = %s", (message_id,))
    assert len(_rows()) == 1  # still there, the delete refused


def test_transitions_leave_the_words_untouched(client):  # the guard binds words, not the walk
    # The immutability guard binds only the words. The message's walk to an answer —
    # claim, then answer — moves status and stores the reply as ever, and the line it
    # started with is unchanged: the guard fires on a rewrite, never on a legitimate move.
    message_id = _insert("the original line")
    with db.get_pool().connection() as conn:
        intake.claim_next(conn)
        assert intake.mark_answered(conn, message_id, "a reply") is True
    row = _rows()[0]
    assert row[1] == "the original line"  # words verbatim through the whole walk
    assert row[2] == "answered"  # yet the transition still landed
    assert row[5] == "a reply"


def test_intake_stores_only_raw_words_and_work_state(client):  # derived ≠ stored, tripwired
    # The diary keeps the raw words and the work-state that walks them to an answer, and
    # nothing else. Anything reconstructable from the words (tags, slices, classifications,
    # a normalized copy) is recomputed on read, never kept as a second source of truth.
    # This pins the allowed columns, so the day someone reaches to store a derived value
    # beside the words it fails here and forces the deliberate "recompute on read" choice.
    with db.get_pool().connection() as conn:
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'intake'"
            ).fetchall()
        }
    assert columns == {
        "id",
        "message",  # the raw words — verbatim, immutable, the one source of truth
        "status",
        "answer",
        "failed_reason",
        "attempts",
        "created_at",
        "updated_at",
        "reply_channel_id",
        "delivered_at",
        "symbiot_id",
    }
