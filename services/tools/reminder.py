"""The reminder: the tool registry's first and only inhabitant.

"Remind me of X at Y" — said in the ordinary flow of conversation —
and at that moment the agent reaches back out and says it.
One message, one future time, one fire.
It is the cleanest possible first action, and that is the whole point of choosing it:
it needs no external driver and no third-party credential,
only a durable row in our own store (migration 0017) and the reply path already built,
so what is proven through it is the machinery of *acting* (services/tools.py),
not the plumbing of an integration.

Two halves live here.
The executor is the *act*:
it reads the arguments the decision extracted,
and — when the time and the line are both clear —
stores the reminder, exactly once against the message that triggered it.
When the time can't be read, it stores nothing and returns a result that asks the human for it,
rather than guessing (the reactive-ambiguity law).
Time resolution is the symbiot's, not the server's:
a fire_at with no zone is read as the symbiot's local wall clock,
and stored as the absolute instant that names.
The due side is the *fire*:
claim_due finds the oldest unfired reminder whose moment has come,
and mark_fired stamps it delivered —
the two the firing sweep (worker._fire_one) sequences into a single transaction with the missive it raises,
so a reminder fires exactly once and a crash mid-fire re-fires nothing.

The tool is registered into services/tools.py at import (the register call at the foot of this module),
so importing the tools package assembles the registry with this tool in it.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from services.tools import tools

# The reminder's name on both sides of the split — what the model emits, and the join to this executor.
NAME = "schedule_reminder"

# The prose the catalog recall matches a message against, and the decision reads to judge fit.
# Written to surface on the obvious phrasings ("remind me", "don't let me forget") by wording and by meaning,
# and to name plainly what the tool does, since the decision judges the tool by this, not by its label.
DESCRIPTION = (
    "Schedule a one-shot reminder: remember something for the human symbiot and say it back to them at a "
    "future time they name. Use this when they ask to be reminded of something later — \"remind me to call "
    "the dentist tomorrow at 9\", \"don't let me forget to email Sam this evening\". One message, one time, "
    "one reminder."
)


class ReminderArgs(BaseModel):
    """The reminder's arguments — both nullable, so the decision can name the tool yet leave one it couldn't read.

    reminder_message is the line to say back when the time comes, phrased the way it should be heard then.
    fire_at is the resolved moment, extracted by the decision against the symbiot's local now —
    a concrete instant, or null when the time couldn't be read with confidence,
    which the executor reads as "ask".
    """

    reminder_message: str | None = None
    fire_at: datetime | None = None


def claim_due(conn):
    """The oldest unfired reminder whose moment has come, claimed for firing, or None when none is due.

    The firing sweep's read:
    an unfired reminder (fired_at null) whose fire_at has passed, oldest first,
    taken under FOR UPDATE SKIP LOCKED so two sweeps never claim the same one —
    a second steps over the locked row to the next.
    The row lock holds for the caller's transaction,
    in which the missive is raised and mark_fired stamped,
    so the claim and the delivery commit together or not at all.
    Returns (id, symbiot_id, body), or None.
    """
    return conn.execute(
        "SELECT id, symbiot_id, body FROM reminder "
        "WHERE fired_at IS NULL AND fire_at <= now() "
        "ORDER BY fire_at LIMIT 1 FOR UPDATE SKIP LOCKED"
    ).fetchone()


def mark_fired(conn, reminder_id: int) -> None:
    """Stamp a reminder delivered — the exactly-once pin on the firing side.

    Set in the same transaction as the missive it raised (worker._fire_one),
    so a fired reminder is recorded the instant it is sent:
    a crash before the commit leaves fired_at null and the reminder simply due again,
    a commit stamps it and it is never sent twice.
    The row is kept, not cleared — the ledger of what fired.
    """
    conn.execute("UPDATE reminder SET fired_at = now() WHERE id = %s", (reminder_id,))


def _execute(conn, symbiot_id: int, intake_id: int, args: ReminderArgs, now_local, zone_name: str) -> tools.ToolResult:
    """Store the reminder, exactly once — or, when the time isn't clear, ask for it rather than guess.

    Both the line and the time must be there to act.
    When either is missing the executor stores nothing and returns an un-effected result,
    so the confirmation asks the human instead of pretending a reminder was set
    (the reactive-ambiguity law — ask rather than guess).
    A fire_at with no timezone is read as the symbiot's local wall clock (the decision resolved it in their zone),
    and stored as the absolute instant that names, so the due check later compares two absolute instants.
    The write is exactly-once against the triggering message: ON CONFLICT (intake_id) DO NOTHING,
    so a retried message re-runs this harmlessly —
    the reminder already stands, and only the spoken confirmation is re-derived.
    """
    body = (args.reminder_message or "").strip()
    if not body or args.fire_at is None:
        return tools.ToolResult(
            effected=False,
            summary=(
                "the human asked to be reminded, but the time (or what the reminder should say) wasn't clear; "
                "ask them when they want it, and what it should say if that is missing too"
            ),
        )
    fire_at = args.fire_at
    if fire_at.tzinfo is None:
        fire_at = fire_at.replace(tzinfo=ZoneInfo(zone_name))
    conn.execute(
        "INSERT INTO reminder (intake_id, symbiot_id, body, fire_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (intake_id) DO NOTHING",
        (intake_id, symbiot_id, body, fire_at),
    )
    local = fire_at.astimezone(ZoneInfo(zone_name))
    return tools.ToolResult(
        effected=True,
        summary=f'a reminder was scheduled for {local.strftime("%A %d %B %Y at %H:%M")} ({zone_name}), to say: "{body}"',
    )


tools.register(tools.Tool(name=NAME, description=DESCRIPTION, args_model=ReminderArgs, executor=_execute))
