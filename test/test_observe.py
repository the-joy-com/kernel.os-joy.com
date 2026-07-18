"""Observe: the reads behind the /observe hub's cards.

recent_utterances is a pure read over the machine side of the conversation stream, so the tests pin the two
things it must get right and nothing it doesn't: that each utterance is resolved to its words and labelled by
the mechanism that raised it (a fast reply, a deep follow-up, a note), and that the stream's own order is
handed back oldest-first with the symbiot's own lines left out. It writes nothing, so there is no write to check.

recent_reminders is the read behind the second card: a plain join of the reminder ledger to its triggering
intake, so the tests pin what that card is for — each reminder paired with the human line that triggered it,
newest first — and that the route gates on a session and renders its times on the symbiot's own clock.
"""

from core import db
from conftest import SYMBIOT_EMAIL, extract_code
from services import observe

SEEDED_SYMBIOT_ID = 1  # conftest re-seeds exactly one symbiot with RESTART IDENTITY, so it's always id 1


def _token(client, fake_email, address=SYMBIOT_EMAIL) -> str:
    # Walk the real login flow to a session token, the way the shell does.
    client.post("/login", json={"address": address})
    code = extract_code(fake_email)
    return client.post("/login/verify", json={"address": address, "code": code}).json()["data"]["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _with_conn(fn):
    with db.get_pool().connection() as conn:
        return fn(conn)


def _intake(message, answer=None, symbiot_id=SEEDED_SYMBIOT_ID, status="answered") -> int:
    return _with_conn(lambda c: c.execute(
        "INSERT INTO intake (message, answer, symbiot_id, status) VALUES (%s, %s, %s, %s) RETURNING id",
        (message, answer, symbiot_id, status),
    ).fetchone()[0])


def _missive(body, symbiot_id=SEEDED_SYMBIOT_ID) -> int:
    return _with_conn(lambda c: c.execute(
        "INSERT INTO missive (symbiot_id, body) VALUES (%s, %s) RETURNING id",
        (symbiot_id, body),
    ).fetchone()[0])


def _enrichment(intake_id, missive_id, symbiot_id=SEEDED_SYMBIOT_ID) -> None:
    _with_conn(lambda c: c.execute(
        "INSERT INTO enrichment (intake_id, symbiot_id, surfaced, missive_id) VALUES (%s, %s, true, %s)",
        (intake_id, symbiot_id, missive_id),
    ))


def _enrichment_pass(intake_id, *, echo_suppressed, symbiot_id=SEEDED_SYMBIOT_ID) -> None:
    # A suppressed enrichment pass — no missive — told apart by why it was silent:
    # echo_suppressed true for a follow-up the guard held back, false for a gate that had nothing to add.
    _with_conn(lambda c: c.execute(
        "INSERT INTO enrichment (intake_id, symbiot_id, surfaced, missive_id, echo_suppressed) "
        "VALUES (%s, %s, false, NULL, %s)",
        (intake_id, symbiot_id, echo_suppressed),
    ))


def _item(role, *, intake_id=None, missive_id=None, symbiot_id=SEEDED_SYMBIOT_ID) -> int:
    return _with_conn(lambda c: c.execute(
        "INSERT INTO conversation_item (symbiot_id, role, token_count, intake_id, missive_id) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (symbiot_id, role, 1, intake_id, missive_id),
    ).fetchone()[0])


def _reminder(trigger, body, *, fire_at_sql="now() + interval '1 day'", fired=False,
              symbiot_id=SEEDED_SYMBIOT_ID, channels=None) -> int:
    # A reminder is only ever raised from a message, so it carries a triggering intake — seed one for it.
    intake_id = _intake(message=trigger, answer="ok", symbiot_id=symbiot_id)
    fired_sql = "now()" if fired else "NULL"
    return _with_conn(lambda c: c.execute(
        f"INSERT INTO reminder (intake_id, symbiot_id, body, fire_at, fired_at, channels) "
        f"VALUES (%s, %s, %s, {fire_at_sql}, {fired_sql}, %s) RETURNING id",
        (intake_id, symbiot_id, body, channels),
    ).fetchone()[0])


def _recent(**kwargs):
    return _with_conn(lambda c: observe.recent_machine_utterances(c, SEEDED_SYMBIOT_ID, **kwargs))


def _recent_reminders(**kwargs):
    return _with_conn(lambda c: observe.recent_reminders(c, SEEDED_SYMBIOT_ID, **kwargs))


def test_labels_each_mechanism_and_resolves_its_words(client):
    # A fast reply: the words are the intake row's answer, the trigger is its message.
    quick_intake = _intake(message="how's the migration going", answer="the reindex is the slow part")
    _item("machine", intake_id=quick_intake)
    # A deep follow-up: a missive an enrichment row claims — its words are the missive body, no trigger.
    deep_missive = _missive("the reindex is still the bottleneck")
    _enrichment(_intake(message="any update", answer="soon"), deep_missive)
    _item("machine", missive_id=deep_missive)
    # A note: a missive no enrichment claims (a reminder, a relay) — its words are the body, no trigger.
    note_missive = _missive("café closes in ten minutes")
    _item("machine", missive_id=note_missive)

    got = _recent()

    assert [u.mechanism for u in got] == ["quick", "deep", "note"]
    assert [u.text for u in got] == [
        "the reindex is the slow part",
        "the reindex is still the bottleneck",
        "café closes in ten minutes",
    ]
    assert [u.trigger for u in got] == ["how's the migration going", None, None]


def test_returns_oldest_first_and_omits_the_symbiot_side(client):
    # A full exchange: the symbiot's line and the machine's reply, two stream rows on one intake row.
    intake_id = _intake(message="a question", answer="an answer")
    _item("symbiot", intake_id=intake_id)  # the human's own line — must not appear
    _item("machine", intake_id=intake_id)  # the reply — must appear
    later = _missive("a later note")
    _item("machine", missive_id=later)

    got = _recent()

    # Only the machine's lines, in the order they were said (oldest first), never the symbiot's own.
    assert [u.text for u in got] == ["an answer", "a later note"]


def test_limit_keeps_the_newest(client):
    for n in range(5):
        _item("machine", intake_id=_intake(message=f"q{n}", answer=f"a{n}"))

    got = _recent(limit=2)

    # The two newest, still handed back oldest-first within that window.
    assert [u.text for u in got] == ["a3", "a4"]


def test_route_requires_a_session(client):
    # A symbiot's own output is not an anonymous thing to show, so no session is turned away.
    body = client.get("/observe/echoes").json()
    assert body["msg"] == "not authenticated"


def test_route_returns_a_scored_shape_with_a_local_time_label(client, fake_email):
    token = _token(client, fake_email)
    # One line, so echoes short-circuits before embedding — the route test needs no live model.
    _item("machine", intake_id=_intake(message="how's it going", answer="the reindex is slow"))

    body = client.get("/observe/echoes", headers=_auth(token)).json()

    assert body["msg"] == "observe echoes"
    data = body["data"]
    assert data["scored"] is True
    assert data["clusters"] == []
    assert len(data["singles"]) == 1
    u = data["singles"][0]
    assert u["mechanism"] == "quick"
    assert u["text"] == "the reindex is slow"
    assert u["trigger"] == "how's it going"
    assert u["when"], "expected a rendered local-time label"
    assert data["held_back"] == 0  # nothing has been muzzled yet, and the field is always present


def test_held_back_count_counts_only_echo_suppressed_passes(client):
    # The audit count is the guard's tally alone: a sent follow-up and a gate-chose-silence pass don't count,
    # only the follow-ups the echo guard composed and then held back.
    _enrichment(_intake("q1", "a1"), _missive("a follow-up that was sent"))  # surfaced — not held back
    _enrichment_pass(_intake("q2", "a2"), echo_suppressed=False)             # gate chose silence — not an echo
    _enrichment_pass(_intake("q3", "a3"), echo_suppressed=True)              # held back as an echo
    _enrichment_pass(_intake("q4", "a4"), echo_suppressed=True)              # held back as an echo

    assert _with_conn(lambda c: observe.held_back_count(c, SEEDED_SYMBIOT_ID)) == 2


def test_route_carries_the_held_back_count(client, fake_email):
    token = _token(client, fake_email)
    _item("machine", intake_id=_intake(message="q", answer="only one"))  # one line — echoes short-circuits, no model
    _enrichment_pass(_intake("q2", "a2"), echo_suppressed=True)

    body = client.get("/observe/echoes", headers=_auth(token)).json()

    assert body["data"]["held_back"] == 1


def test_echoes_clusters_near_duplicates_and_leaves_the_rest_single(client, monkeypatch):
    _item("machine", intake_id=_intake(message="q1", answer="the reindex is the slow part"))
    _item("machine", intake_id=_intake(message="q2", answer="the reindex is still the bottleneck"))
    _item("machine", intake_id=_intake(message="q3", answer="the weather is lovely today"))

    # Stubbed vectors: the two reindex lines point almost the same way (cosine ≈ 1),
    # the weather line is orthogonal to both — so exactly one cluster of the two forms.
    vecs = {
        "the reindex is the slow part": [1.0, 0.0, 0.0],
        "the reindex is still the bottleneck": [0.99, 0.01, 0.0],
        "the weather is lovely today": [0.0, 1.0, 0.0],
    }
    monkeypatch.setattr(observe.embedding, "embed_many", lambda texts, *, task: [vecs[t] for t in texts])

    result = _with_conn(lambda c: observe.machine_echoes(c, SEEDED_SYMBIOT_ID))

    assert result.scored is True
    assert len(result.clusters) == 1
    assert [u.text for u in result.clusters[0].members] == [
        "the reindex is the slow part",
        "the reindex is still the bottleneck",
    ]
    assert result.clusters[0].similarity > 0.9
    assert [u.text for u in result.singles] == ["the weather is lovely today"]


def test_echoes_chains_a_run_of_near_duplicates_into_one_cluster(client, monkeypatch):
    _item("machine", intake_id=_intake(message="q1", answer="a"))
    _item("machine", intake_id=_intake(message="q2", answer="b"))
    _item("machine", intake_id=_intake(message="q3", answer="c"))

    # a~b and b~c are close, a~c is not: transitivity must still land all three in one cluster.
    vecs = {"a": [1.0, 0.0], "b": [0.92, 0.39], "c": [0.7, 0.71]}
    monkeypatch.setattr(observe.embedding, "embed_many", lambda texts, *, task: [vecs[t] for t in texts])

    result = _with_conn(lambda c: observe.machine_echoes(c, SEEDED_SYMBIOT_ID, threshold=0.9))

    assert len(result.clusters) == 1
    assert [u.text for u in result.clusters[0].members] == ["a", "b", "c"]
    assert result.singles == []


def test_echoes_degrades_to_the_plain_mirror_when_the_embedder_is_down(client, monkeypatch):
    _item("machine", intake_id=_intake(message="q1", answer="one"))
    _item("machine", intake_id=_intake(message="q2", answer="two"))

    def boom(texts, *, task):
        raise RuntimeError("ollama unreachable")

    monkeypatch.setattr(observe.embedding, "embed_many", boom)

    result = _with_conn(lambda c: observe.machine_echoes(c, SEEDED_SYMBIOT_ID))

    assert result.scored is False
    assert result.clusters == []
    assert [u.text for u in result.singles] == ["one", "two"]


def test_echoes_skips_scoring_for_a_lone_line(client, monkeypatch):
    _item("machine", intake_id=_intake(message="q", answer="only one"))

    def fail(*args, **kwargs):
        raise AssertionError("a single line cannot echo — the embedder must not be called")

    monkeypatch.setattr(observe.embedding, "embed_many", fail)

    result = _with_conn(lambda c: observe.machine_echoes(c, SEEDED_SYMBIOT_ID))

    assert result.scored is True
    assert result.clusters == []
    assert [u.text for u in result.singles] == ["only one"]


def test_reminders_pair_each_with_its_trigger_newest_first(client):
    # The pairing is the whole point: each reminder carries the human line that triggered it,
    # so an over-eager schedule is legible. Newest first, the order the audit is read in.
    _reminder("remind me to call the dentist tomorrow", "call the dentist")
    _reminder("don't let me forget to email Sam", "email Sam", fired=True, channels=["email"])

    got = _recent_reminders()

    assert [r.trigger for r in got] == [
        "don't let me forget to email Sam",
        "remind me to call the dentist tomorrow",
    ]
    assert [r.body for r in got] == ["email Sam", "call the dentist"]
    assert [r.fired for r in got] == [True, False]
    assert [r.channels for r in got] == [["email"], None]


def test_reminders_limit_keeps_the_newest(client):
    for n in range(5):
        _reminder(f"remind me n{n}", f"body{n}")

    got = _recent_reminders(limit=2)

    assert [r.trigger for r in got] == ["remind me n4", "remind me n3"]


def test_reminders_route_requires_a_session(client):
    # A symbiot's own reminders are not an anonymous thing to show, so no session is turned away.
    body = client.get("/observe/reminders").json()
    assert body["msg"] == "not authenticated"


def test_reminders_route_returns_the_pairing_with_local_time_labels(client, fake_email):
    token = _token(client, fake_email)
    _reminder("remind me to stretch at 3", "stretch")

    body = client.get("/observe/reminders", headers=_auth(token)).json()

    assert body["msg"] == "observe reminders"
    reminders = body["data"]["reminders"]
    assert len(reminders) == 1
    r = reminders[0]
    assert r["trigger"] == "remind me to stretch at 3"
    assert r["body"] == "stretch"
    assert r["fired"] is False
    assert r["channels"] is None
    assert r["fire_at"], "expected a rendered local-time label"
    assert r["created_at"], "expected a rendered local-time label"
