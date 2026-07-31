"""The symbiot's timezone: inferring it from a place, storing it, reading it back, and the authed route.

The inference is the one part that reaches the model, so its LLM boundary is faked here;
what the tests pin is the guarantee around it —
that only a name the system's timezone database actually carries is ever returned or stored,
so a null answer or an invented-but-well-formed name both come back as the honest "say again".
The store reads and the /timezone route (authed only) are exercised end to end against the test database,
with the inference faked so no test makes a network call.
"""

from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from core import db
from core import protocol
from services import identity
from services.loop import zone


def _symbiot_id() -> int:
    with db.get_pool().connection() as conn:
        return conn.execute("SELECT id FROM symbiot LIMIT 1").fetchone()[0]


def _session_token() -> str:
    # Mint a live session directly, the way verify_login_code would, so the authed route has a real token.
    token = "test-timezone-token"
    with db.get_pool().connection() as conn:
        symbiot_id = conn.execute("SELECT id FROM symbiot LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO session (symbiot_id, token_hash, expires_at) "
            "VALUES (%s, %s, now() + interval '1 hour')",
            (symbiot_id, identity._hash(token)),
        )
    return token


def test_infer_returns_a_validated_iana_name(monkeypatch):
    # The model names a real zone; it passes the timezone-database check and comes back as-is.
    monkeypatch.setattr(zone.llm, "generate_json", lambda prompt, schema: zone._ZoneReply(timezone="Europe/Paris"))
    assert zone.infer("Strasbourg") == "Europe/Paris"


def test_infer_rejects_a_name_the_timezone_database_does_not_carry(monkeypatch):
    # A well-formed but non-existent zone must not be trusted: the validation, not the model, is the guarantee.
    monkeypatch.setattr(zone.llm, "generate_json", lambda prompt, schema: zone._ZoneReply(timezone="Nowhere/Bogus"))
    assert zone.infer("the moon") is None


def test_infer_returns_none_when_the_model_places_nothing(monkeypatch):
    # A null answer — the model couldn't read a place — is the honest "say again".
    monkeypatch.setattr(zone.llm, "generate_json", lambda prompt, schema: zone._ZoneReply(timezone=None))
    assert zone.infer("asdfgh") is None


def test_now_for_is_aware_and_falls_back_to_utc_on_a_bad_zone():
    # A real zone resolves to an aware local time; a name that doesn't resolve falls back to UTC, never raises.
    assert zone.now_for("Asia/Tokyo").tzinfo is not None
    assert zone.now_for("Nowhere/Bogus").utcoffset() == datetime.now(dt_timezone.utc).utcoffset()


def test_local_date_reads_the_instant_in_the_symbiots_day_not_utc():
    # An instant that is already the next day east of Greenwich must read as that local day, not the UTC one —
    # the exact skew the fix closes: 13:00 UTC is past midnight in Auckland, so the fact belongs to the 14th there.
    instant = datetime(2026, 7, 13, 13, 0, tzinfo=dt_timezone.utc)
    assert instant.date() == date(2026, 7, 13)                      # the UTC date, the old wrong-calendar behaviour
    assert zone.local_date(instant, "Pacific/Auckland") == date(2026, 7, 14)  # the symbiot's actual day
    assert zone.local_date(instant, "UTC") == date(2026, 7, 13)     # UTC symbiot sees the plain column date


def test_local_date_falls_back_to_utc_on_a_bad_zone():
    # A name that no longer resolves reads as the UTC day rather than raising, mirroring now_for's fallback.
    instant = datetime(2026, 7, 13, 13, 0, tzinfo=dt_timezone.utc)
    assert zone.local_date(instant, "Nowhere/Bogus") == date(2026, 7, 13)


def test_parse_future_wall_clock_names_which_way_a_time_was_wrong():
    now = datetime(2026, 7, 14, 20, 5, tzinfo=ZoneInfo("Europe/Paris"))
    # A moment still ahead comes back with nothing to say about it.
    ahead, refusal = zone.parse_future_wall_clock("+45m", now)
    assert ahead == now + timedelta(minutes=45) and refusal is None
    # The two failures are told apart, because they are different mistakes:
    # a phrase outside the grammar was typed wrong, a moment already gone was aimed wrong.
    unreadable, reason = zone.parse_future_wall_clock("tomorrow at nine", now)
    assert unreadable is None and reason.startswith("couldn't read 'tomorrow at nine' as a time")
    gone, gone_reason = zone.parse_future_wall_clock("19:00", now)
    assert gone is None and gone_reason == "that time has already passed"


def test_parse_future_wall_clock_only_suggests_forms_the_grammar_accepts():
    # The hint lives next to the grammar so it can't go on advising a form the parser has stopped taking.
    # That is the claim; this is it checked — every example the refusal offers must itself parse.
    now = datetime(2026, 7, 14, 20, 5, tzinfo=ZoneInfo("Europe/Paris"))
    _, reason = zone.parse_future_wall_clock("next week", now)
    suggested = [said.removeprefix("or ") for said in reason.split("try ")[1].split(", ")]
    assert len(suggested) == 3
    for said in suggested:
        assert zone.parse_wall_clock(said, now) is not None, said


def test_parse_wall_clock_reads_a_span_from_now():
    now = datetime(2026, 7, 14, 20, 5, tzinfo=ZoneInfo("Europe/Paris"))
    assert zone.parse_wall_clock("+45m", now) == now + timedelta(minutes=45)
    assert zone.parse_wall_clock("+2h", now) == now + timedelta(hours=2)
    assert zone.parse_wall_clock("+3d", now) == now + timedelta(days=3)


def test_parse_wall_clock_reads_the_face_of_the_clock_in_the_symbiots_zone():
    now = datetime(2026, 7, 14, 20, 5, tzinfo=ZoneInfo("Europe/Paris"))
    # A full date and time, and a bare time meaning today — both stamped with the symbiot's own zone,
    # never an offset read off the text or off the box.
    assert zone.parse_wall_clock("2026-08-01 09:30", now) == datetime(
        2026, 8, 1, 9, 30, tzinfo=ZoneInfo("Europe/Paris")
    )
    assert zone.parse_wall_clock("21:15", now) == datetime(
        2026, 7, 14, 21, 15, tzinfo=ZoneInfo("Europe/Paris")
    )


def test_parse_wall_clock_refuses_anything_outside_the_grammar():
    now = datetime(2026, 7, 14, 20, 5, tzinfo=ZoneInfo("Europe/Paris"))
    # The terse path spends no model call, so a phrase it can't read deterministically is refused,
    # never guessed at — the caller surfaces that as a legible "try +45m, 20:05, …".
    for said in ("tomorrow at nine", "next week", "+2 hours", "9pm", "2026-08-01", "", "+9999999h"):
        assert zone.parse_wall_clock(said, now) is None, said


def test_of_defaults_to_utc_until_a_zone_is_set(client):
    # A fresh symbiot has the schema default: a defined "now", just not yet localised.
    with db.get_pool().connection() as conn:
        assert zone.of(conn, _symbiot_id()) == "UTC"


def test_set_for_persists_the_inferred_zone_and_of_reads_it_back(client, monkeypatch):
    monkeypatch.setattr(zone, "infer", lambda location: "Asia/Tokyo")
    symbiot_id = _symbiot_id()
    with db.get_pool().connection() as conn:
        assert zone.set_for(conn, symbiot_id, "tokyo") == "Asia/Tokyo"
    with db.get_pool().connection() as conn:
        assert zone.of(conn, symbiot_id) == "Asia/Tokyo"


def test_set_for_stores_nothing_when_the_place_cannot_be_placed(client, monkeypatch):
    # A place that can't be placed leaves the existing zone untouched — a fumble never overwrites a good zone.
    monkeypatch.setattr(zone, "infer", lambda location: None)
    symbiot_id = _symbiot_id()
    with db.get_pool().connection() as conn:
        assert zone.set_for(conn, symbiot_id, "gibberish") is None
        assert zone.of(conn, symbiot_id) == "UTC"


def test_timezone_route_turns_away_a_visitor(client):
    # No session: the route is authed only, so an unauthed caller is told NOT_AUTHED and nothing is stored.
    res = client.post("/timezone", json={"location": "Tokyo"})
    assert res.json()["msg"] == protocol.NOT_AUTHED


def test_timezone_route_sets_the_zone_for_an_authed_symbiot(client, monkeypatch):
    monkeypatch.setattr(zone, "infer", lambda location: "America/New_York")
    token = _session_token()
    res = client.post(
        "/timezone", json={"location": "just landed in NYC"}, headers={"Authorization": f"Bearer {token}"}
    )
    body = res.json()
    assert body["msg"] == protocol.TIMEZONE_SET
    assert body["data"]["timezone"] == "America/New_York"
    with db.get_pool().connection() as conn:
        assert zone.of(conn, _symbiot_id()) == "America/New_York"


def test_timezone_route_reports_an_unplaceable_location(client, monkeypatch):
    monkeypatch.setattr(zone, "infer", lambda location: None)
    token = _session_token()
    res = client.post(
        "/timezone", json={"location": "somewhere over the rainbow"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert res.json()["msg"] == protocol.TIMEZONE_UNCLEAR
