"""The leg that earns the name: the look step, and the three tools that read the standing set before they act.

What is under test is the four-step arc — parse, query, look, decide —
and the property that makes it worth the name:
the outcome turns on what the machine *observed*, not on what was said.
The same sentence, twice, comes out differently depending on what the store already holds.

The judging call is the one part that reaches a model, so its LLM boundary is faked here;
the hooks, the executors and the writes run end to end against the test database.
What a live judge actually decides is the by-hand smoke's to prove (test/qa/0013).
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core import db
from services.tools import reminder
from services.tools import tools
from services.loop import execution
from services.loop import worker

SEEDED_SYMBIOT_ID = 1  # conftest re-seeds exactly one symbiot with RESTART IDENTITY, so it's always id 1
ZONE = "Europe/Paris"
NOW = datetime(2026, 7, 14, 9, 0, tzinfo=ZoneInfo(ZONE))


def _fake_judge(monkeypatch, match):
    # The judging call, stubbed at the LLM boundary: whatever ref the test wants named, or None for "none of these".
    # The ref is text on the wire (see tools._observation_verdict_model), so the stub answers the way a provider would.
    monkeypatch.setattr(
        tools.llm,
        "generate_json",
        lambda prompt, schema, *, model=None: schema(match=None if match is None else str(match)),
    )


def _held(body: str, at: datetime) -> int:
    with db.get_pool().connection() as conn:
        return reminder.create(conn, SEEDED_SYMBIOT_ID, body, at)


def _intake(message: str) -> int:
    with db.get_pool().connection() as conn:
        return conn.execute(
            "INSERT INTO intake (message, symbiot_id, status) VALUES (%s, %s, 'answered') RETURNING id",
            (message, SEEDED_SYMBIOT_ID),
        ).fetchone()[0]


def _run(decision: tools.Decision, intake_id: int, *, verdict=None) -> tools.ToolResult:
    with db.get_pool().connection() as conn:
        with conn.transaction():
            return tools.execute(conn, decision, SEEDED_SYMBIOT_ID, intake_id, NOW, ZONE, verdict)


def _observe(decision: tools.Decision):
    with db.get_pool().connection() as conn:
        return tools.observe(conn, decision, SEEDED_SYMBIOT_ID, NOW, ZONE)


# --- the hook mechanism ----------------------------------------------------------------


def test_a_tool_without_a_hook_is_single_pass(client):
    # The look step is opt-in: a tool that declares no hook behaves exactly as every tool did before it existed.
    assert tools.REGISTRY[reminder.READ_NAME].observe is None
    decision = tools.Decision(reminder.READ_NAME, {"from_time": NOW, "until_time": NOW + timedelta(days=1)})
    assert _observe(decision) is None


def test_the_judge_can_only_name_a_candidate_it_was_shown(client, monkeypatch):
    # The refs come from code, so "the model decides and describes; code does" holds one level deeper:
    # the reply schema is a Literal over exactly the refs offered, so an invented row is not a possible answer.
    observation = tools.Observation(
        question="which?",
        candidates=[tools.ObservedCandidate(7, "a"), tools.ObservedCandidate(9, "b")],
    )
    _fake_judge(monkeypatch, 9)
    assert tools.judge_observation("…", observation, NOW, ZONE).match == 9
    model = tools._observation_verdict_model(observation)
    assert model(match=None).match is None
    try:
        model(match="3")
    except Exception:
        pass
    else:
        raise AssertionError("a ref nobody offered should not validate")


# --- the deduplication: the same sentence, two different worlds ------------------------


def test_the_dedup_hook_looks_only_at_live_reminders_near_the_proposed_time(client):
    fire_at = NOW + timedelta(days=1)
    near_it = _held("call the dentist", fire_at + timedelta(hours=1))
    _held("call the dentist", fire_at + timedelta(days=40))  # far outside the window
    fired = _held("call the dentist", fire_at + timedelta(hours=2))
    with db.get_pool().connection() as conn:
        reminder.mark_fired(conn, fired)

    decision = tools.Decision(
        reminder.NAME, {"reminder_message": "call the dentist", "fire_at": fire_at.replace(tzinfo=None)}
    )
    observation = _observe(decision)

    # Narrow on purpose: a reminder for next month says nothing about doubling up on tomorrow,
    # and one that already fired is no reason to refuse a new one.
    assert observation is not None
    assert [c.ref for c in observation.candidates] == [near_it]


def test_the_dedup_hook_stays_quiet_when_there_is_nothing_to_look_against(client):
    # An unclear time gives no window to look inside and an unclear line nothing to rank by,
    # and in both cases the executor is about to ask for what is missing anyway.
    assert _observe(tools.Decision(reminder.NAME, {"reminder_message": "x", "fire_at": None})) is None
    assert _observe(tools.Decision(reminder.NAME, {"reminder_message": None, "fire_at": NOW})) is None
    # Nothing held at all is the ordinary case, and it spends no judging call either.
    assert _observe(
        tools.Decision(reminder.NAME, {"reminder_message": "x", "fire_at": (NOW + timedelta(days=1)).replace(tzinfo=None)})
    ) is None


def test_a_matched_duplicate_writes_no_reminder_and_records_the_decline(client):
    fire_at = NOW + timedelta(days=1)
    held = _held("call the dentist", fire_at)
    intake_id = _intake("remind me to call the dentist tomorrow")
    decision = tools.Decision(
        reminder.NAME, {"reminder_message": "call the dentist", "fire_at": fire_at.replace(tzinfo=None)}
    )

    result = _run(decision, intake_id, verdict=tools.ObservationVerdict(match=held))

    # Nobody had to change anything, because it was already so — which is what SATISFIED is for,
    # and is precisely the ending the old did-it-or-not flag could not express.
    assert result.outcome == "SATISFIED"
    with db.get_pool().connection() as conn:
        count = conn.execute("SELECT count(*) FROM reminder").fetchone()[0]
        declines = conn.execute(
            "SELECT intake_id, reminder_id FROM reminder_decline"
        ).fetchall()
    assert count == 1, "nothing new was written"
    # The decision that would otherwise have left no trace leaves one.
    assert declines == [(intake_id, held)]


def test_the_same_sentence_acts_when_nothing_like_it_is_held(client):
    # The property that earns the name: identical words, different world, different outcome.
    fire_at = NOW + timedelta(days=1)
    intake_id = _intake("remind me to call the dentist tomorrow")
    decision = tools.Decision(
        reminder.NAME, {"reminder_message": "call the dentist", "fire_at": fire_at.replace(tzinfo=None)}
    )

    result = _run(decision, intake_id, verdict=tools.ObservationVerdict(match=None))

    assert result.outcome == "ACTED"
    with db.get_pool().connection() as conn:
        assert conn.execute("SELECT count(*) FROM reminder").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM reminder_decline").fetchone()[0] == 0


def test_a_check_that_could_not_run_lets_the_reminder_through(client):
    # A check must never be able to cause the harm it was added to prevent.
    # None is what a judging call that timed out comes back as,
    # and it has to be indistinguishable from "no duplicate" —
    # otherwise a safeguard against missing reminders has invented a new way for a reminder to go missing.
    fire_at = NOW + timedelta(days=1)
    _held("call the dentist", fire_at)
    intake_id = _intake("remind me to call the dentist tomorrow")
    decision = tools.Decision(
        reminder.NAME, {"reminder_message": "call the dentist", "fire_at": fire_at.replace(tzinfo=None)}
    )

    result = _run(decision, intake_id, verdict=None)

    assert result.outcome == "ACTED"
    with db.get_pool().connection() as conn:
        assert conn.execute("SELECT count(*) FROM reminder").fetchone()[0] == 2


def test_a_retried_message_records_one_decline_not_two(client):
    fire_at = NOW + timedelta(days=1)
    held = _held("call the dentist", fire_at)
    intake_id = _intake("remind me to call the dentist tomorrow")
    decision = tools.Decision(
        reminder.NAME, {"reminder_message": "call the dentist", "fire_at": fire_at.replace(tzinfo=None)}
    )

    _run(decision, intake_id, verdict=tools.ObservationVerdict(match=held))
    _run(decision, intake_id, verdict=tools.ObservationVerdict(match=held))

    with db.get_pool().connection() as conn:
        assert conn.execute("SELECT count(*) FROM reminder_decline").fetchone()[0] == 1


def test_a_duplicate_that_fired_in_the_gap_is_no_longer_a_duplicate(client):
    # The hook read the store, the judge took a beat, and the reminder went off meanwhile.
    # It is no reason to refuse a new one any more, so the act runs.
    fire_at = NOW + timedelta(days=1)
    held = _held("call the dentist", fire_at)
    with db.get_pool().connection() as conn:
        reminder.mark_fired(conn, held)
    intake_id = _intake("remind me to call the dentist tomorrow")
    decision = tools.Decision(
        reminder.NAME, {"reminder_message": "call the dentist", "fire_at": fire_at.replace(tzinfo=None)}
    )

    result = _run(decision, intake_id, verdict=tools.ObservationVerdict(match=held))

    assert result.outcome == "ACTED"
    with db.get_pool().connection() as conn:
        assert conn.execute("SELECT count(*) FROM reminder_decline").fetchone()[0] == 0


# --- cancelling and editing by language ------------------------------------------------


def test_the_target_hook_ranks_the_live_set_by_wording(client):
    dentist = _held("call the dentist about the referral", NOW + timedelta(days=1))
    _held("email Sam the invoice", NOW + timedelta(days=2))
    decision = tools.Decision(reminder.CANCEL_NAME, {"reference": "the dentist one"})

    observation = _observe(decision)

    assert observation is not None
    # Nearest wording first, so the judge reads the plausible one at the top of a short list.
    assert observation.candidates[0].ref == dentist


def test_cancelling_by_language_asks_rather_than_guessing_when_no_target_resolved(client):
    _held("call the dentist", NOW + timedelta(days=1))
    intake_id = _intake("cancel that reminder")

    result = _run(tools.Decision(reminder.CANCEL_NAME, {"reference": "that one"}), intake_id, verdict=None)

    # A cancel is the one write the symbiot cannot take back by saying so again,
    # so an unresolved target is asked about, never guessed at — and nothing is stamped.
    assert result.outcome == "UNCLEAR"
    with db.get_pool().connection() as conn:
        assert conn.execute("SELECT count(*) FROM reminder WHERE cancelled_at IS NOT NULL").fetchone()[0] == 0


def test_cancelling_by_language_stamps_the_reminder_the_judge_named(client):
    dentist = _held("call the dentist", NOW + timedelta(days=1))
    intake_id = _intake("drop the dentist reminder")

    result = _run(
        tools.Decision(reminder.CANCEL_NAME, {"reference": "the dentist one"}),
        intake_id,
        verdict=tools.ObservationVerdict(match=dentist),
    )

    assert result.outcome == "ACTED"
    with db.get_pool().connection() as conn:
        cancelled_at = conn.execute("SELECT cancelled_at FROM reminder WHERE id = %s", (dentist,)).fetchone()[0]
    assert cancelled_at is not None


def test_cancelling_a_reminder_that_already_fired_is_unable_not_unclear(client):
    dentist = _held("call the dentist", NOW + timedelta(days=1))
    with db.get_pool().connection() as conn:
        reminder.mark_fired(conn, dentist)
    intake_id = _intake("drop the dentist reminder")

    result = _run(
        tools.Decision(reminder.CANCEL_NAME, {"reference": "the dentist one"}),
        intake_id,
        verdict=tools.ObservationVerdict(match=dentist),
    )

    # Received, and cannot be carried out — asking again changes nothing, so there is nothing to ask for.
    assert result.outcome == "UNABLE"


def test_editing_by_language_takes_an_absolute_moment_and_lands_twice_the_same(client):
    dentist = _held("call the dentist", NOW + timedelta(days=1))
    thursday = (NOW + timedelta(days=3)).replace(tzinfo=None)
    intake_id = _intake("move the dentist one to Thursday at nine")
    decision = tools.Decision(
        reminder.UPDATE_NAME, {"reference": "the dentist one", "new_fire_at": thursday, "new_message": None}
    )

    first = _run(decision, intake_id, verdict=tools.ObservationVerdict(match=dentist))
    with db.get_pool().connection() as conn:
        once = conn.execute("SELECT fire_at FROM reminder WHERE id = %s", (dentist,)).fetchone()[0]
    _run(decision, intake_id, verdict=tools.ObservationVerdict(match=dentist))
    with db.get_pool().connection() as conn:
        twice, body = conn.execute(
            "SELECT fire_at, body FROM reminder WHERE id = %s", (dentist,)
        ).fetchone()

    assert first.outcome == "ACTED"
    # Absolute, never a delta — which is what makes a retried edit harmless with no second exactly-once pin.
    assert once == twice
    assert body == "call the dentist", "a new time alone leaves the line where it was"


def test_editing_by_language_asks_when_it_cannot_tell_what_to_change(client):
    dentist = _held("call the dentist", NOW + timedelta(days=1))
    intake_id = _intake("change the dentist reminder")

    result = _run(
        tools.Decision(reminder.UPDATE_NAME, {"reference": "the dentist one", "new_fire_at": None, "new_message": None}),
        intake_id,
        verdict=tools.ObservationVerdict(match=dentist),
    )

    assert result.outcome == "UNCLEAR"


def test_editing_refuses_a_new_time_that_is_already_past(client):
    dentist = _held("call the dentist", NOW + timedelta(days=1))
    intake_id = _intake("move the dentist one to yesterday morning")

    result = _run(
        tools.Decision(
            reminder.UPDATE_NAME,
            {"reference": "the dentist one", "new_fire_at": (NOW - timedelta(days=1)).replace(tzinfo=None), "new_message": None},
        ),
        intake_id,
        verdict=tools.ObservationVerdict(match=dentist),
    )

    # Terminal, not a question: asked when they want it instead, a human who named a past moment names it again,
    # and the same guard turns the same reading away forever. UNABLE ends it and names the time that was read.
    assert result.outcome == "UNABLE"
    with db.get_pool().connection() as conn:
        unchanged = conn.execute("SELECT fire_at FROM reminder WHERE id = %s", (dentist,)).fetchone()[0]
    assert unchanged > NOW


# --- reading it back in plain talk -----------------------------------------------------


def test_the_read_windows_the_set_and_groups_it_by_day(client):
    _held("call the dentist", NOW + timedelta(days=1, hours=1))
    _held("email Sam", NOW + timedelta(days=1, hours=6))
    _held("ring mum", NOW + timedelta(days=2))
    _held("far away", NOW + timedelta(days=40))
    intake_id = _intake("what am I holding for the next few days?")

    result = _run(
        tools.Decision(
            reminder.READ_NAME,
            {
                "from_time": NOW.replace(tzinfo=None),
                "until_time": (NOW + timedelta(days=3)).replace(tzinfo=None),
            },
        ),
        intake_id,
    )

    # A question asked, not an act performed — nothing changed, so nothing "happened".
    assert result.outcome == "REPORTED"
    # The window bounds the prompt rather than a ledger, so a narrow question is nearly free.
    assert "far away" not in result.summary
    # Grouped by the symbiot's own calendar day, since that is the unit a person hears a schedule in.
    assert result.summary.count("call the dentist") == 1 and "email Sam" in result.summary
    day_of_the_pair = (NOW + timedelta(days=1)).strftime("%A %d %B %Y")
    assert result.summary.count(day_of_the_pair) == 1, "the two on one day share one heading"


def test_the_read_says_plainly_when_the_window_is_empty(client):
    _held("far away", NOW + timedelta(days=40))
    intake_id = _intake("anything on tomorrow?")

    result = _run(
        tools.Decision(
            reminder.READ_NAME,
            {
                "from_time": NOW.replace(tzinfo=None),
                "until_time": (NOW + timedelta(days=1)).replace(tzinfo=None),
            },
        ),
        intake_id,
    )

    # "Nothing next week" is a real answer and silence isn't.
    assert result.outcome == "REPORTED"
    assert "nothing" in result.summary


def test_the_read_says_there_are_more_when_the_cap_bites(client, monkeypatch):
    monkeypatch.setattr(reminder.config, "REMINDER_DIGEST_LIMIT", 3)
    for n in range(5):
        _held(f"thing {n}", NOW + timedelta(days=1, hours=n))
    intake_id = _intake("what am I holding tomorrow?")

    result = _run(
        tools.Decision(
            reminder.READ_NAME,
            {
                "from_time": NOW.replace(tzinfo=None),
                "until_time": (NOW + timedelta(days=2)).replace(tzinfo=None),
            },
        ),
        intake_id,
    )

    # Incomplete and saying so is a limit; incomplete and silent would be a bug.
    assert "There are more beyond these" in result.summary
    assert "thing 4" not in result.summary


def test_the_read_asks_when_the_stretch_of_time_will_not_resolve(client):
    intake_id = _intake("what am I holding whenever?")

    result = _run(
        tools.Decision(reminder.READ_NAME, {"from_time": None, "until_time": None}), intake_id
    )

    assert result.outcome == "UNCLEAR"


# --- the arc, end to end through the worker -------------------------------------------


def test_the_worker_sequences_parse_query_look_decide(client, monkeypatch):
    # The four-step arc as _answer runs it, with only the model calls faked:
    # the decision names the tool, the hook queries the store, the judge rules, the executor decides.
    fire_at = NOW + timedelta(days=1)
    held = _held("call the dentist", fire_at)
    intake_id = _intake("remind me to call the dentist tomorrow")
    decision = tools.Decision(
        reminder.NAME, {"reminder_message": "call the dentist", "fire_at": fire_at.replace(tzinfo=None), "channels": None}
    )
    # Every model step is faked and run in-process (run_with_deadline runs fn(arg) rather than forking),
    # so the arc is exercised without a child spawn or a live model — a child is a fresh interpreter
    # and would not see these patches at all. The real thing is the by-hand smoke's to prove.
    monkeypatch.setattr(
        worker.execution, "run_with_deadline",
        lambda fn, arg, deadline: execution.Result(execution.COMPLETED, fn(arg)),
    )
    monkeypatch.setattr(worker.tools, "decide", lambda *a, **k: decision)
    monkeypatch.setattr(worker.tools, "judge_observation", lambda *a, **k: tools.ObservationVerdict(match=held))
    monkeypatch.setattr(
        worker.tools, "compose_confirmation", lambda message, result, now, zone: f"[{result.outcome}]"
    )
    shortlist = [tools.ToolCandidate(reminder.NAME, reminder.DESCRIPTION, 0.1)]

    result = worker._answer(
        db.get_pool(),
        intake_id,
        "remind me to call the dentist tomorrow",
        SEEDED_SYMBIOT_ID,
        [],
        worker.conversation.Conversation(gist=None, tail=[]),
        NOW,
        ZONE,
        shortlist,
    )

    # The whole point in one line: the machine looked before it leapt, and the answer says so.
    assert result.value == "[SATISFIED]"
    with db.get_pool().connection() as conn:
        assert conn.execute("SELECT count(*) FROM reminder").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM reminder_decline").fetchone()[0] == 1
