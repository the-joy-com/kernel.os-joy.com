"""Zone: the symbiot's local timezone — inferred from a place it names, stored, and read as its "now".

The machine used to perceive time as the server does: UTC.
So a reply that mentioned the hour, or read a "this evening" off the diary, spoke in UTC —
wrong by however far the symbiot sits from Greenwich.
This module is where the symbiot's own timezone comes from, and where its local "now" is read,
so every human-facing clock is the human's, not the box's.

The zone is not typed in as a cryptic identifier.
The symbiot says where it is in plain words — "Tokyo", "I just landed in New York", "back home in Strasbourg" —
and one LLM call reads that place and names the IANA timezone for it
(the same structured-output boundary the rest of the kernel leans on).
The model's answer is never trusted on its face:
it is checked against the system's own timezone database,
and only a name that database actually carries is ever stored.
A place the model can't turn into a real zone comes back as None —
the honest "say again" the caller surfaces on the wire, rather than a plausible-looking zone that isn't real.
Switching zones is re-naming where you are;
the store holds one zone per symbiot and the newest naming wins,
so a symbiot that moves simply says so again.

Reading a *time* the symbiot names happens two ways, and both land here.
In the ordinary flow of talk the decision call reads it out of the sentence, against the local now this module hands it.
On the terse command path there is no model call to spend, so parse_wall_clock reads a small closed grammar itself
and refuses anything outside it — deterministic on purpose, because that path exists to skip the reasoning.
Either way the law is the same one: read the face of the clock, stamp the symbiot's zone, never trust an offset.

Why IANA names and not a fixed offset:
an offset ('+02:00') can't know about the summer-time shift, so it would drift half the year.
A zone name resolves to the correct offset for the instant it is read,
which is exactly what now_for does — it is the one place a stored name becomes a concrete local moment.
"""

import re
from datetime import date, datetime, timedelta
from zoneinfo import available_timezones, ZoneInfo

from pydantic import BaseModel

from services.adapters import llm

# The fallback zone for a symbiot that has never named a place,
# and for a stored name that somehow no longer resolves (a tzdata that dropped a zone between reads).
# UTC is the old server-clock behaviour made explicit:
# a defined "now" that is simply not yet localised, never a null or a crash on the read path.
DEFAULT_ZONE = "UTC"


class _ZoneReply(BaseModel):
    """The inference's answer: the IANA timezone name for the place, or null when it names no place.

    A plain module-level model — its shape never depends on anything per call, so nothing is built each time.
    timezone defaults null so the model has an explicit way to say "I can't place this" rather than guess a zone;
    the caller validates whatever comes back against the real timezone database regardless,
    so an invented-but-well-formed name is rejected exactly like a null one."""

    timezone: str | None = None


# The members below are ordered alphabetically, as far as the code allows:
# _infer_prompt first (infer calls it), then the rest in alphabetical order.
def _infer_prompt(location: str) -> str:
    return (
        "You are given a place a person says they are in, in their own words.\n"
        f'Place: "{location}"\n\n'
        "Return the IANA timezone identifier for that place — for example 'Europe/Paris', "
        "'America/New_York', 'Asia/Tokyo', 'Australia/Sydney'. Read the place out of the words even when "
        "they are casual ('just landed in NYC' is 'America/New_York', 'back home in Strasbourg' is "
        "'Europe/Paris').\n"
        "If the input names no place you can turn into a timezone, return null — do not guess.\n\n"
        'Return JSON only: {"timezone": "<IANA name>"} or {"timezone": null}.'
    )


def infer(location: str) -> str | None:
    """Read a place named in plain words and return its IANA timezone, or None when it can't be placed.

    One structured LLM call names the zone;
    the answer is then held to the system's own timezone database,
    so only a name that database actually carries is returned.
    A null answer (the model couldn't place it), or a well-formed name the database doesn't know,
    both come back None — the single honest "say again" the caller acts on.
    The validation, not the model's confidence, is the guarantee:
    a stored zone is always one now_for can resolve, so the read path never trips on a name that isn't real.
    """
    reply = llm.generate_json(_infer_prompt(location), _ZoneReply)
    name = (reply.timezone or "").strip()
    return name if name in available_timezones() else None


def local(moment: datetime, zone_name: str) -> datetime:
    """`moment` read on the human's clock — the same absolute instant, expressed in `zone_name`.

    The read-path companion to now_for:
    now_for says what the local moment is right now,
    this re-expresses a stored instant in the human's zone so its date and its time of day are the ones they would name.
    A stored timestamp is an absolute instant (a TIMESTAMPTZ column), and which wall-clock reading it shows
    depends entirely on the zone it is viewed from —
    so without this a fact or a turn from late evening slips to the next day, or an early morning to the day before,
    merely because the store keeps UTC while the reply speaks in the human's local time,
    and the model is handed a "now" and a remembered moment on two different clocks.
    Falls back to UTC on a name that no longer resolves, for the same reason now_for does:
    a wrong-but-defined reading on a render is recoverable, a crash composing a reply is not.
    """
    try:
        return moment.astimezone(ZoneInfo(zone_name))
    except Exception:
        return moment.astimezone(ZoneInfo(DEFAULT_ZONE))


def local_date(moment: datetime, zone_name: str) -> date:
    """The calendar day `moment` fell on in `zone_name` — the human's day, not the server's.

    The date half of local(): the local instant reduced to its calendar day,
    for the fact lines that carry a date but no time of day."""
    return local(moment, zone_name).date()


def now_for(zone_name: str) -> datetime:
    """The current local date and time in `zone_name`, as a timezone-aware datetime.

    The one place a stored zone name becomes a concrete moment:
    the name resolves to the offset in force for *now*,
    so the summer-time shift is handled without the store ever holding an offset that would drift.
    A name that no longer resolves (a tzdata gap, a hand-mangled row) falls back to UTC rather than raising —
    a wrong-but-defined clock on a background path is recoverable;
    a crash composing a reply is not.
    """
    try:
        return datetime.now(ZoneInfo(zone_name))
    except Exception:
        return datetime.now(ZoneInfo(DEFAULT_ZONE))


def of(conn, symbiot_id: int) -> str:
    """The symbiot's stored IANA timezone name, or the UTC default when it has none.

    Read straight off the symbiot row (migration 0016 makes the column NOT NULL DEFAULT 'UTC'),
    so this returns a usable zone for every symbiot from its first boot,
    localised only once the human has named where they are.
    A row that somehow carries a blank still reads as the default, never empty."""
    row = conn.execute("SELECT timezone FROM symbiot WHERE id = %s", (symbiot_id,)).fetchone()
    return row[0] if row and row[0] else DEFAULT_ZONE


def parse_future_wall_clock(text: str, now_local: datetime) -> tuple[datetime | None, str | None]:
    """Read a typed time that has to still be ahead of the symbiot — the moment, or None and why not.

    parse_wall_clock below says what the text names;
    this is the rule the terse command path adds on top, since a reminder is for the future,
    so a moment already gone means it was typed wrong.
    It lives here, next to the grammar, because a refusal has to describe that grammar to be any use —
    and a hint written anywhere else can go on advising a form the parser has stopped accepting,
    with nothing to say it has drifted.
    The two ways it fails are worth telling apart in the answer:
    a text outside the grammar was typed wrong, a moment in the past was aimed wrong.
    The reason comes back as plain words, ready to be printed as it is.
    """
    fire_at = parse_wall_clock(text, now_local)
    if fire_at is None:
        return None, f"couldn't read {text!r} as a time — try +45m, 20:05, or 2026-07-14 20:05"
    if fire_at <= now_local:
        return None, "that time has already passed"
    return fire_at, None


def parse_wall_clock(text: str, now_local: datetime) -> datetime | None:
    """Read a time the symbiot typed into an absolute instant on their own clock, or None when it isn't one of the forms.

    The deterministic half of reading time, beside infer's inferring half —
    and the one the terse command path uses (the shell's /reminders),
    where spending a model call is the thing that path exists to avoid:
    an instruction that had no ambiguity in it shouldn't be sent through reasoning,
    which is slower, costs tokens, and leaves room to misread it.
    So the grammar is small and closed, and anything outside it comes back None to be refused flatly
    rather than guessed at:
      +45m, +2h, +3d      a span from now, in minutes, hours or days
      2026-07-14 20:05    a plain wall-clock date and time
      20:05               a bare time, meaning today
    Kernel-side, not browser-side,
    because the browser's clock and the zone the symbiot told the kernel it lives in can disagree,
    and the zone is the ground truth.
    The zone comes off now_local (now_for stamps it), so a bare face of the clock is read as the symbiot's own —
    the same law the reminder's fire time follows: read the face, stamp the zone, never infer an offset.
    Whether the instant is far enough in the future is not this function's rule to keep;
    this only says what the text names, and parse_future_wall_clock above is where the terse path adds that rule.
    """
    said = text.strip()
    relative = re.fullmatch(r"\+(\d{1,4})([mhd])", said)
    if relative is not None:
        span, unit = int(relative.group(1)), relative.group(2)
        return now_local + {"m": timedelta(minutes=span), "h": timedelta(hours=span), "d": timedelta(days=span)}[unit]
    for pattern in ("%Y-%m-%d %H:%M", "%H:%M"):
        try:
            read = datetime.strptime(said, pattern)
        except ValueError:
            continue
        # A bare time names no date, so strptime dates it 1900-01-01; today is what the symbiot meant.
        if pattern == "%H:%M":
            read = read.replace(year=now_local.year, month=now_local.month, day=now_local.day)
        return read.replace(tzinfo=now_local.tzinfo)
    return None


def render_now(now_local: datetime, zone_name: str) -> str:
    """The one-line current-time reference a prompt reasons about time against — the human's clock, not the server's.

    States the symbiot's current local date and time and its zone,
    so a mention of the hour, a "this evening", or a "how long ago" is read in the human's day rather than UTC —
    which is what the machine spoke in when it had no clock at all.
    now_local is expected already expressed in the symbiot's zone (now_for returns it so);
    the line renders its wall-clock reading as given and names the zone alongside it.
    The one home for this line, so every path that hands the model a present states it identically:
    the fast reply on the critical path and the deep follow-up composed a beat later both read it from here.
    Kept to a single sentence, and its callers hold it outside the compressible memory block,
    so it is never squeezed away on an overrun."""
    stamp = now_local.strftime("%A %d %B %Y, %H:%M")
    return (
        f"For reference, the human symbiot's local date and time right now is {stamp} ({zone_name}). "
        "Reason about any mention of time — today, tonight, how long ago — in their local time, not UTC."
    )


def set_for(conn, symbiot_id: int, location: str) -> str | None:
    """Infer the timezone for a place the symbiot named and store it on the symbiot; return the zone set.

    Infers first and writes only on success:
    a place that can't be placed (infer returns None) stores nothing and returns None,
    so a fumbled location never overwrites a good zone with a guess.
    On success the newest naming wins — the column is overwritten in place —
    so a symbiot that moves just says where it is again.
    Returns the IANA name written, for the caller to confirm back to the human in their own words.
    """
    zone = infer(location)
    if zone is None:
        return None
    conn.execute("UPDATE symbiot SET timezone = %s WHERE id = %s", (zone, symbiot_id))
    return zone
