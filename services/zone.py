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

Why IANA names and not a fixed offset:
an offset ('+02:00') can't know about the summer-time shift, so it would drift half the year.
A zone name resolves to the correct offset for the instant it is read,
which is exactly what now_for does — it is the one place a stored name becomes a concrete local moment.
"""

from datetime import datetime
from zoneinfo import available_timezones, ZoneInfo

from pydantic import BaseModel

from services import llm

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
