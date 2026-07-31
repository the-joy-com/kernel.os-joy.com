"""The reminder: the tool registry's first inhabitant, and the whole lifecycle of the row it writes.

"Remind me of X at Y" — said in the ordinary flow of conversation —
and at that moment the agent reaches back out and says it.
One message, one future time, one fire.
It is the cleanest possible first action, and that is the whole point of choosing it:
it needs no external driver and no third-party credential,
only a durable row in our own store (migration 0017) and the reply path already built,
so what is proven through it is the machinery of *acting* (services/tools/tools.py),
not the plumbing of an integration.

Four tools live over that one row, and they are all here because they are all the reminder:
schedule_reminder sets one, cancel_reminder calls one off,
update_reminder moves or rewords one, and read_reminders says what is held over a stretch of time.
Three of them exist because of one absence —
for a while nothing could *read* the reminders the machine was currently holding,
so the lifecycle was one-way: born from a sentence, fired, done.

Three parts live here, and the module is the reminder table's data layer as much as it is the tool.
The executor is the *act*:
it reads the arguments the decision extracted,
and — when the time and the line are both clear —
stores the reminder, exactly once against the message that triggered it.
When the time can't be read, it stores nothing
and returns a result that asks the human for it,
rather than guessing (the reactive-ambiguity law).
Time resolution is the symbiot's, not the server's:
a fire_at is read as the symbiot's local wall clock — the clock face the decision named, in their zone —
and stored as the absolute instant that names.
The due side is the *fire*:
claim_due finds the oldest live reminder whose moment has come,
and mark_fired stamps it delivered —
the two the firing sweep (worker._fire_one) sequences into a single transaction
with the missive it raises,
so a reminder fires exactly once and a crash mid-fire re-fires nothing.
The third is the *lifecycle*, the store layer over the standing set
(create, standing, find, near, within, update, cancel, record_decline, refusal_reason):
the reads that let anything at all know what the machine is currently holding —
including the two the agentic readers share, `near` for judging and `within` for summarising —
and the two writes that let a reminder be moved or called off.
It lives here rather than in a home of its own
because this module is the reminder table's data layer and has been since it was written —
reading the set belongs beside the claim and the fired stamp, not beside them in a second file.

Every write over the standing set carries the same three conditions in its WHERE clause —
the row is this symbiot's, it hasn't fired, it hasn't been cancelled —
and reads the rowcount back rather than reading the row first and deciding:
the firing sweep waits for no one, so the guarantee lives in the statement, not in the timing (see update).
Which of the three was false is named afterwards, in plain words, by refusal_reason —
so a surface that has to explain a refusal asks this module rather than reading the stamps itself.

Two hooks sit alongside the executors, and they are what make this more than a store with a mouth:
_observe is the deduplication recall (is one of these the same intent as what was just asked for?),
and _target_observe, shared by the cancel and the edit, resolves which held reminder a phrase points at.
Both are pure reads whose findings one small judging call rules on before any executor runs —
the *look* step, in services/tools/tools.py.

Every tool is registered into services/tools/tools.py at import
(the register calls at the foot of this module),
so importing the tools package assembles the registry with all four in it.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from core import config
from services.tools import tools
from services.loop import notify
from services.loop import zone

# The reminder's name on both sides of the split — what the model emits, and the join to this executor.
NAME = "schedule_reminder"

# The channels this tool can notify over —
# the reminder is a plain say-back, so it supports every channel there is,
# and this is the single source that does double duty:
# it types the schema field the request lands in
# (so the model can only ever pick a channel the tool supports),
# and it is the set the firing sweep fans across by default
# when the symbiot named none (worker._fire_one).
# A tool that supported only a subset would narrow this to its own tuple;
# the reminder, having no reason to, rides the whole set.
SUPPORTED_CHANNELS = notify.ALL_CHANNELS

# The prose the catalog recall matches a message against, and the decision reads to judge fit.
# Written to surface on the obvious phrasings ("remind me", "don't let me forget")
# by wording and by meaning,
# and to name plainly what the tool does,
# since the decision judges the tool by this, not by its label.
DESCRIPTION = (
    "Schedule a one-shot reminder: remember something for the human symbiot "
    "and say it back to them at a future time they name. "
    "Use this when they ask to be reminded of something later — "
    '"remind me to call the dentist tomorrow at 9", '
    '"don\'t let me forget to email Sam this evening". '
    "It fits only an explicit request to be reminded, not a message that merely mentions a future task or event. "
    "One message, one time, one reminder."
)


class ReminderArgs(BaseModel):
    """The reminder's arguments — both nullable, so the decision can name the tool yet leave one it couldn't read.

    reminder_message is the line to say back when the time comes,
    phrased the way it should be heard then.
    fire_at is the resolved moment, extracted by the decision against the symbiot's local now —
    a concrete instant, or null when the time couldn't be read with confidence,
    which the executor reads as "ask".
    channels is the list of notification channels the human explicitly requested (e.g. "by email"),
    constrained to the ones this tool supports,
    or null/empty if they didn't specify.
    """

    reminder_message: str | None = None
    fire_at: datetime | None = None
    channels: list[notify.Channel] | None = Field(
        default=None,
        description=(
            "Which channels to deliver this reminder over, read from how the human asks to be reached. "
            "Any phrasing that says to use a channel counts, wherever it sits in the sentence — "
            "a trailing 'by email' and a leading 'email me a reminder to…' name the channel just the same. "
            'So "by email", "email me", "email me a reminder to…", "send it to my email" all mean ["email"]; '
            '"push me", "on web push", "notify my browser", "push me a reminder to…" all mean ["web_push"]; '
            'asking for more than one names them all, e.g. ["email", "web_push"]. '
            "When the human names no channel — only what to be reminded of and when — leave this null; "
            "null is the default and means deliver over every channel. "
            "Do not mistake the reminder's own content for a channel: "
            '"remind me to email Sam" is a task that happens to mention email, '
            "not a request to be reminded by email, so it stays null."
        ),
    )


@dataclass
class StandingReminder:
    """One reminder as the standing set reports it — what a listing prints, and the handle it prints against.

    reminder_id is the row's own id, the handle every write names:
    the shell prints positions, holds the ids, and sends the ids back.
    body is the line to be said back when it fires.
    fire_at is when it is (or was) due, an absolute instant to be rendered in the symbiot's own zone.
    channels is where it is to be delivered, or None when the symbiot named none and it rides every channel.
    state is one word for where the reminder stands — pending, fired, or cancelled —
    which is what a listing wants: one line per reminder, so a spent one and a called-off one
    have to read apart from each other and from a live one without a reader comparing two stamps.
    Deriving it here rather than at the surface keeps the reading of the two stamps
    in the module that owns them, the same shape the reminders lens takes (observe.RecentReminder).
    """

    reminder_id: int
    body: str
    fire_at: datetime
    channels: list[str] | None
    state: str


def cancel(conn, symbiot_id: int, reminder_id: int) -> bool:
    """Call a reminder off — stamp cancelled_at, never delete. True when the write landed.

    One of the two writes over the standing set, and it carries the three conditions both of them do:
    the row is this symbiot's, it hasn't fired, it hasn't already been cancelled.
    Zero rows touched means one of the three was false, and that is the refusal —
    which the route reports rather than pretending anything changed.
    The row is kept, stamped: a reminder called off is recorded, not dropped,
    so the symbiot sees they called it off rather than finding a gap where it was.
    """
    cursor = conn.execute(
        "UPDATE reminder SET cancelled_at = now() "
        "WHERE id = %s AND symbiot_id = %s AND fired_at IS NULL AND cancelled_at IS NULL",
        (reminder_id, symbiot_id),
    )
    return cursor.rowcount == 1


def claim_due(conn):
    """The oldest live reminder whose moment has come, claimed for firing, or None when none is due.

    The firing sweep's read:
    an unfired, uncancelled reminder whose fire_at has passed, oldest first,
    taken under FOR UPDATE SKIP LOCKED so two sweeps never claim the same one —
    a second steps over the locked row to the next.
    The cancelled clause is what makes cancelling real:
    without it a reminder the symbiot called off would still go off,
    and it mirrors the partial index the claim rests on (migration 0023), which carries the same two conditions.
    The row lock holds for the caller's transaction,
    in which the missive is raised and mark_fired stamped,
    so the claim and the delivery commit together or not at all.
    Returns (id, symbiot_id, body, channels), or None.
    """
    return conn.execute(
        "SELECT id, symbiot_id, body, channels FROM reminder "
        "WHERE fired_at IS NULL AND cancelled_at IS NULL AND fire_at <= now() "
        "ORDER BY fire_at LIMIT 1 FOR UPDATE SKIP LOCKED"
    ).fetchone()


def create(
    conn,
    symbiot_id: int,
    body: str,
    fire_at: datetime,
    channels: list[notify.Channel] | None = None,
    intake_id: int | None = None,
) -> int | None:
    """Store one reminder and return its id, or None when a retried message's reminder already stands.

    The one write both ways in share — the executor's act and the terse command's `add` —
    so the row's shape is settled in a single place.
    intake_id is the exactly-once pin when there is a message behind this
    (ON CONFLICT (intake_id) DO NOTHING, so a retried message re-runs harmlessly and this returns None);
    a reminder typed straight in passes none, and a null never conflicts,
    since Postgres treats nulls as distinct under the UNIQUE index (migration 0023).
    fire_at must already be the absolute instant — resolving a wall clock into one is the caller's job,
    because the two callers read time from different places
    (the decision's reading of a sentence, or the command's own deterministic parse).
    """
    row = conn.execute(
        "INSERT INTO reminder (intake_id, symbiot_id, body, fire_at, channels) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (intake_id) DO NOTHING RETURNING id",
        (intake_id, symbiot_id, body, fire_at, channels),
    ).fetchone()
    return row[0] if row is not None else None


def find(conn, symbiot_id: int, reminder_id: int):
    """One of the symbiot's reminders by id, whatever state it is in, or None when no such row is theirs.

    Scoped on symbiot_id like every other read here, so an id is only ever resolved within the caller's own set.
    Every state, not just the live ones: this is what lets a refusal be legible —
    a write that touched nothing can be reported as fired, or already called off,
    rather than as a bare "no".
    Returns (id, body, fire_at, channels, fired_at, cancelled_at), or None.
    """
    return conn.execute(
        "SELECT id, body, fire_at, channels, fired_at, cancelled_at FROM reminder "
        "WHERE id = %s AND symbiot_id = %s",
        (reminder_id, symbiot_id),
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


def near(conn, symbiot_id: int, wording: str, limit: int, window: tuple[datetime, datetime] | None = None):
    """The live reminders whose wording sits nearest to `wording`, optionally only inside a window of time.

    The recall the two agentic readers share, and the only read here that ranks rather than orders.
    Live only, since a reminder that already fired or was called off is no reason to refuse a new one
    and is not something a phrase like "the dentist one" can be pointing at.
    Ranked by trigram similarity — pg_trgm, the same fuzzy lexical measure the diary recall leans on,
    so a half-remembered wording still surfaces the reminder it meant.
    Capped, because what this feeds is a judging call, and a judge reads a handful of plausible lines well
    and a whole day's worth badly.
    The window is the deduplication's narrowing: a proposed moment tells you where to look,
    and a reminder for Friday says nothing about whether next month is being doubled up on.
    Resolving which reminder a phrase points at passes none, since there is no proposed moment to window by —
    the whole live set is in play and the wording is the only signal there is.
    Returns rows of (id, body, fire_at), nearest wording first.
    """
    clause = "" if window is None else "AND fire_at BETWEEN %(from_time)s AND %(until_time)s "
    return conn.execute(
        "SELECT id, body, fire_at FROM reminder "
        "WHERE symbiot_id = %(symbiot_id)s AND fired_at IS NULL AND cancelled_at IS NULL "
        + clause
        + "ORDER BY similarity(body, %(wording)s) DESC, fire_at LIMIT %(limit)s",
        {
            "symbiot_id": symbiot_id,
            "wording": wording,
            "limit": limit,
            "from_time": None if window is None else window[0],
            "until_time": None if window is None else window[1],
        },
    ).fetchall()


def record_decline(conn, intake_id: int, reminder_id: int) -> None:
    """Write down that the machine refused to set a reminder it judged the store already held.

    The trace the deduplication would otherwise not leave.
    Both sides of the judgment go in the row — the message that asked and the reminder it matched —
    because the failure worth catching is two differently worded intents collapsing into one,
    and the wording is the evidence (migration 0024).
    Exactly-once against the triggering message, the same pin the reminder itself carries,
    so a retried message that re-runs the check records one decline rather than a second.
    """
    conn.execute(
        "INSERT INTO reminder_decline (intake_id, reminder_id) VALUES (%s, %s) "
        "ON CONFLICT (intake_id) DO NOTHING",
        (intake_id, reminder_id),
    )


def refusal_reason(conn, symbiot_id: int, reminder_id: int) -> str:
    """Why a scoped write touched nothing, said in plain words the surface can print as-is.

    The write scopes on three conditions at once,
    so a rowcount of zero means one of them was false without saying which.
    This reads the row afterwards, purely to name it —
    a read *after* the refusal, never before the write, so it can't turn the guarantee back into a race.
    A reminder that has since fired is the case worth naming precisely:
    printing a list, being pulled away, and coming back minutes later to act on it
    is the ordinary way a terminal gets used, so the answer has to be legible rather than a bare no.
    It lives beside the writes it explains, and reads the row through find,
    so the meaning of each stamp is settled in one module rather than restated wherever a refusal is surfaced.
    """
    found = find(conn, symbiot_id, reminder_id)
    if found is None:
        return f"no reminder {reminder_id}"
    if found[4] is not None:
        return f"reminder {reminder_id} has already fired"
    if found[5] is not None:
        return f"reminder {reminder_id} was already called off"
    return f"reminder {reminder_id} couldn't be changed"


def standing(conn, symbiot_id: int, limit: int, settled: int) -> list[StandingReminder]:
    """The symbiot's standing reminder set: the live ones soonest first, then the last few that settled.

    The read the whole lifecycle stands on, and the one three readers share —
    the shell's listing walks it, the plain-language read windows it,
    and the deduplication recall looks inside a window of it.
    The live ones lead, ordered by when they come due, because that is what the symbiot is holding.
    A short tail of settled ones follows, most recent first,
    so a reminder that has fired reads as fired rather than as a gap where it used to be,
    and a cancel just made is visibly a cancel.
    Each is capped separately: the live cap is what a person can take in as plain lines,
    the settled tail is small on purpose, since the full history is the audit surface's to show.
    Each row carries its state as one word rather than the two stamps it is read from (StandingReminder),
    so what a listing prints is decided here, where the stamps' meaning is already settled.
    Returns StandingReminders, live before settled.
    """
    state = (
        "CASE WHEN fired_at IS NOT NULL THEN 'fired' "
        "WHEN cancelled_at IS NOT NULL THEN 'cancelled' "
        "ELSE 'pending' END AS state"
    )
    live = conn.execute(
        f"SELECT id, body, fire_at, channels, {state} FROM reminder "
        "WHERE symbiot_id = %s AND fired_at IS NULL AND cancelled_at IS NULL "
        "ORDER BY fire_at LIMIT %s",
        (symbiot_id, limit),
    ).fetchall()
    # Ordered by the moment they settled, not by fire_at:
    # a reminder cancelled long before its time is recent news, and its fire_at would bury it.
    # Exactly one of the two stamps is ever set — firing skips a cancelled row and cancelling skips a fired one —
    # so coalesce over the pair is the settling moment, not a guess between two.
    spent = conn.execute(
        f"SELECT id, body, fire_at, channels, {state} FROM reminder "
        "WHERE symbiot_id = %s AND (fired_at IS NOT NULL OR cancelled_at IS NOT NULL) "
        "ORDER BY coalesce(fired_at, cancelled_at) DESC LIMIT %s",
        (symbiot_id, settled),
    ).fetchall()
    return [
        StandingReminder(reminder_id=r[0], body=r[1], fire_at=r[2], channels=r[3], state=r[4])
        for r in live + spent
    ]


def update(
    conn,
    symbiot_id: int,
    reminder_id: int,
    body: str | None = None,
    fire_at: datetime | None = None,
) -> bool:
    """Change a live reminder's line, its moment, or both. True when the write landed.

    The other write over the standing set, under the same three conditions as cancel:
    the row is this symbiot's, it hasn't fired, it hasn't been cancelled.
    A null argument leaves that column alone, so an edit can move the time without restating the line.
    Both null touches nothing and is reported as a refusal, since there was no change to make.

    What this deliberately does *not* do is read the row, decide it looks editable, and then write.
    The firing sweep runs every REMINDER_SWEEP_INTERVAL_SECONDS and it waits for no one:
    a reminder can go off in the gap between that read and that write,
    and an edit that had already made up its mind would then quietly rewrite one that has already fired.
    All three conditions in the statement itself closes the gap,
    because the database checks and writes in the same breath —
    a slow hand and a flaky connection are the ordinary case here, not the exception,
    so the guarantee can't rest on the two happening close together in time.
    Values are absolute, never deltas ("set it to Thursday at nine", not "push it back an hour"),
    which is what makes a retried edit harmless without inventing a second exactly-once pin:
    applied twice, an absolute value lands in the same place.
    """
    if body is None and fire_at is None:
        return False
    cursor = conn.execute(
        "UPDATE reminder SET body = coalesce(%s, body), fire_at = coalesce(%s, fire_at) "
        "WHERE id = %s AND symbiot_id = %s AND fired_at IS NULL AND cancelled_at IS NULL",
        (body, fire_at, reminder_id, symbiot_id),
    )
    return cursor.rowcount == 1


def within(conn, symbiot_id: int, from_time: datetime, until_time: datetime, limit: int):
    """The live reminders due inside a window, soonest first, capped — the plain-language read's own read.

    "What am I holding for next week" resolves to two instants, and this is what falls between them.
    The window is what keeps the read small, and it is the part that matters for cost:
    the prompt that summarises this carries what was asked about rather than a ledger,
    so a wide question is a bigger read and a narrow one is nearly free —
    and the day this runs on a small local model, asking about tomorrow costs what tomorrow costs.
    One over the cap is fetched deliberately,
    so the caller can tell a full answer from one with more beyond what it can see,
    and say so rather than passing off the first handful as all of it.
    Returns rows of (id, body, fire_at), up to limit + 1 of them.
    """
    return conn.execute(
        "SELECT id, body, fire_at FROM reminder "
        "WHERE symbiot_id = %s AND fired_at IS NULL AND cancelled_at IS NULL "
        "AND fire_at BETWEEN %s AND %s "
        "ORDER BY fire_at LIMIT %s",
        (symbiot_id, from_time, until_time, limit + 1),
    ).fetchall()


def _execute(
    conn,
    symbiot_id: int,
    intake_id: int,
    args: ReminderArgs,
    now_local,
    zone_name: str,
    verdict: tools.ObservationVerdict | None = None,
) -> tools.ToolResult:
    """Store the reminder, exactly once — unless the store already holds it, or the time isn't clear.

    verdict is what the judge made of what _observe found, and it is read first:
    a named match means the machine is already holding this one,
    so nothing is written, the decline is recorded, and the result is SATISFIED —
    nobody has to change anything, because it was already so.
    A verdict of None — no candidates, no match, or a judging call that could not run —
    all mean the same thing here, and the act runs as it always did.

    Both the line and the time must be there to act.
    When either is missing the executor stores nothing and returns UNCLEAR,
    so the confirmation asks the human instead of pretending a reminder was set
    (the reactive-ambiguity law — ask rather than guess).
    The fire_at is read as the symbiot's local wall clock —
    the clock face the decision named, stamped with their zone —
    whatever offset the decision may have attached to it:
    the zone is ground truth and a model's offset is only a guess,
    so taking the wall-clock reading and stamping the zone is right
    whether the decision left it bare or labelled it (even mislabelled),
    and it is stored as the absolute instant that names,
    so the due check later compares two absolute instants.
    A resolved instant that lands at or before now is refused outright, as UNABLE:
    a reminder points forward, and firing one the moment it is written
    would say it back at once, uselessly.
    Refused rather than asked about, because asking cannot end well here.
    A human who names a moment already gone will name it again when asked —
    the reading was faithful, so the same question gets the same answer and the exchange never terminates.
    UNABLE says the one thing that is true and lets them decide what to do about it,
    and the summary names the time that was read
    so the other case — a fire_at the decision handed back as a bare UTC reading,
    which the local-wall-clock rule above then mis-stamps hours early — is visible as the misreading it is.
    The write is exactly-once against the triggering message:
    ON CONFLICT (intake_id) DO NOTHING,
    so a retried message re-runs this harmlessly —
    the reminder already stands, and only the spoken confirmation is re-derived.
    """
    if verdict is not None and verdict.match is not None:
        held = find(conn, symbiot_id, verdict.match)
        # The judge only ever names a row _observe offered, and _observe only reads this symbiot's live set,
        # so this resolves; the guard is against a row that fired in the gap, which is no longer a duplicate.
        if held is not None and held[4] is None and held[5] is None:
            record_decline(conn, intake_id, verdict.match)
            already = zone.local(held[2], zone_name).strftime("%A %d %B %Y at %H:%M")
            return tools.ToolResult(
                outcome="SATISFIED",
                summary=(
                    f'a reminder for that is already standing — "{held[1]}", '
                    f"set for {already} ({zone_name}); nothing was scheduled, because nothing needed to be"
                ),
            )
    body = (args.reminder_message or "").strip()
    if not body or args.fire_at is None:
        return tools.ToolResult(
            outcome="UNCLEAR",
            summary=(
                "the human asked to be reminded, but the time (or what the reminder should say) wasn't clear; "
                "ask them when they want it, and what it should say if that is missing too"
            ),
        )
    fire_at = args.fire_at.replace(tzinfo=ZoneInfo(zone_name))
    if fire_at <= now_local:
        gone = zone.local(fire_at, zone_name).strftime("%A %d %B %Y at %H:%M")
        return tools.ToolResult(
            outcome="UNABLE",
            summary=(
                f"the time that came out of what they said, {gone} ({zone_name}), has already gone by, "
                "and a reminder only points forward, so nothing was set; "
                "name that time back to them, so a misread one is theirs to correct"
            ),
        )
    create(conn, symbiot_id, body, fire_at, args.channels, intake_id=intake_id)
    local = fire_at.astimezone(ZoneInfo(zone_name))
    ch_str = f" over {', '.join(args.channels)}" if args.channels else ""
    return tools.ToolResult(
        outcome="ACTED",
        summary=f'a reminder was scheduled for {local.strftime("%A %d %B %Y at %H:%M")} ({zone_name}){ch_str}, to say: "{body}"',
    )


def _observe(conn, symbiot_id: int, args: ReminderArgs, now_local, zone_name: str) -> tools.Observation | None:
    """The deduplication recall: is there already a reminder that looks like this one?

    The step that makes this the first decision the machine takes on what it *observed*
    rather than on what was *said*.
    "Remind me to email Sam" is a perfectly good sentence in isolation —
    whether it is a duplicate is a fact about the world, not about the words —
    so the words are not enough and the store has to be looked at.

    It looks in a narrow place on purpose.
    Live reminders only, since one that already fired is no reason to refuse a new one.
    Inside a window around the moment the decision just read,
    since a reminder for Friday says nothing about whether next month is being doubled up on.
    Ranked by how near the wording sits to what was asked for, and capped,
    so a busy Friday hands the judge a handful of plausible candidates rather than the whole day.
    The window, the ranking and the cap are the three knobs, and they live beside the tool recall's own.

    Returns None when there is nothing to read against —
    an unclear time gives no window to look inside, and an unclear line gives nothing to rank by,
    and in both cases the executor is about to ask for what is missing anyway.
    """
    body = (args.reminder_message or "").strip()
    if not body or args.fire_at is None:
        return None
    fire_at = args.fire_at.replace(tzinfo=ZoneInfo(zone_name))
    window = timedelta(hours=config.REMINDER_DEDUP_WINDOW_HOURS)
    found = near(
        conn,
        symbiot_id,
        body,
        config.REMINDER_DEDUP_LIMIT,
        window=(fire_at - window, fire_at + window),
    )
    if not found:
        return None
    return tools.Observation(
        question=(
            "Is one of these records the same thing the human is asking to be reminded of — "
            "the same errand or intent, not merely the same topic? "
            'A record saying "call the dentist" is the same intent as "remind me to ring the dentist"; '
            'it is not the same intent as "call the dentist about the referral letter", '
            "which is a different, more specific errand and deserves its own reminder."
        ),
        candidates=[
            tools.ObservedCandidate(
                ref=row[0],
                description=f'"{row[1]}", set for {zone.local(row[2], zone_name).strftime("%A %d %B at %H:%M")}',
            )
            for row in found
        ],
    )


tools.register(
    tools.Tool(
        name=NAME,
        description=DESCRIPTION,
        args_model=ReminderArgs,
        executor=_execute,
        observe=_observe,
    )
)


# ---------------------------------------------------------------------------------------
# The three tools that read the standing set before they act.
#
# The registry's own claim was always that a second tool is a new entry and not a rewrite;
# these are where that gets tested, and it held —
# each is a name, a description, an argument schema, an executor and a hook, and nothing else moved.
#
# All three lean on the same recall (near), for the same reason:
# "cancel the dentist one" and "move the dentist one to Thursday" can't be resolved in a single forward pass either,
# because which row is meant is a fact about what is held, not about the sentence.
# So the hook narrows and ranks, the judge names one, and the executor writes.
# A verdict of None — no candidates, no match, or a judging call that could not run —
# means the executor asks which one rather than guessing at a write it can't undo.
#
# The read is the odd one out and has no hook: it does its own reading in the executor,
# because there is nothing to judge — a window is a window, and what falls inside it is the answer.


CANCEL_NAME = "cancel_reminder"

CANCEL_DESCRIPTION = (
    "Call off a reminder the human symbiot is already holding, so it never fires. "
    "Use this when they ask to drop, cancel, delete or forget a reminder they set earlier — "
    '"cancel the dentist reminder", "drop the one about emailing Sam", '
    '"forget the reminder for Friday". '
    "It fits only a request to call an existing reminder off, "
    "never a request to set a new one or to change when an existing one fires."
)

READ_NAME = "read_reminders"

READ_DESCRIPTION = (
    "Look at the reminders the human symbiot is currently holding over a stretch of time, and say what they are. "
    "Use this when they ask what is coming up or what is being held for them — "
    '"what am I holding for next week", "anything on tomorrow", '
    '"what have you got for me before I fly on the 14th". '
    "It fits a question about what already stands, never a request to set, change or cancel anything."
)

UPDATE_NAME = "update_reminder"

UPDATE_DESCRIPTION = (
    "Change a reminder the human symbiot is already holding — when it fires, what it says, or both. "
    "Use this when they ask to move, reschedule, push, bring forward or reword an existing reminder — "
    '"move the dentist one to Thursday at nine", "make the Sam reminder say to call rather than email". '
    "It fits only a change to a reminder that already stands, never a request to set a new one or to cancel one."
)


class CancelArgs(BaseModel):
    """The cancel's one argument: the words the human used to point at a reminder they are holding.

    reference is their own phrasing of which one they mean — "the dentist one", "the Friday reminder" —
    read straight from the message rather than resolved, because resolving it is the hook's job
    and the hook has the store to resolve it against.
    Null when they named no particular one, which the executor reads as "ask which".
    """

    reference: str | None = Field(
        default=None,
        description=(
            "The human's own words for which reminder they mean, copied from their message — "
            'the subject of it ("the dentist"), or how they referred to it ("the Friday one"). '
            "Do not resolve it, invent an id, or guess at a reminder they didn't point at; "
            "leave it null if they named no particular reminder."
        ),
    )


class ReadArgs(BaseModel):
    """The read's two arguments: the stretch of time the question is about, as two wall-clock instants.

    The model's whole job here is turning a phrase — "next week", "tomorrow", "the rest of today",
    "before I fly on the 14th" — into a start and an end, and nothing else.
    Both are read as the symbiot's own wall clock: the zone comes from the kernel,
    the same law the reminder's fire time follows.
    A phrase that won't resolve leaves them null,
    which the executor reads as "ask" rather than guessing at a window
    and reporting confidently on the wrong stretch of time.
    """

    from_time: datetime | None = Field(
        default=None,
        description=(
            "The start of the stretch of time the human is asking about, as their own wall-clock reading "
            "(for example 2026-07-14 00:00). Do not convert it to UTC and do not attach a timezone offset. "
            'For "the rest of today" or "anything coming up" this is now.'
        ),
    )
    until_time: datetime | None = Field(
        default=None,
        description=(
            "The end of that stretch, as their own wall-clock reading (for example 2026-07-20 23:59). "
            "Do not convert it to UTC and do not attach a timezone offset. "
            "Leave both this and the start null if you cannot read a stretch of time out of the message "
            "with confidence."
        ),
    )


class UpdateArgs(BaseModel):
    """The edit's arguments: which reminder, and the new value for its time, its line, or both.

    reference points at the reminder in the human's own words, exactly as the cancel's does.
    new_fire_at and new_message are the values to put in — absolute, never deltas.
    "Set it to Thursday at nine" is a moment;
    "push it back an hour" is an instruction the model must resolve into a moment
    against the reminder it can see,
    which is why the argument is described as a concrete reading and never as a shift.
    That is also what makes a retried edit harmless without a second exactly-once pin:
    applied twice, an absolute value lands in the same place.
    """

    reference: str | None = Field(
        default=None,
        description=(
            "The human's own words for which reminder they mean, copied from their message. "
            "Do not resolve it or invent an id; leave it null if they named no particular reminder."
        ),
    )
    new_fire_at: datetime | None = Field(
        default=None,
        description=(
            "The moment the reminder should fire from now on, as the human's own wall-clock reading "
            "(for example 2026-07-14 09:00) — never a shift like 'an hour later'. "
            "Do not convert it to UTC and do not attach a timezone offset. "
            "Leave it null when they are only changing what the reminder says."
        ),
    )
    new_message: str | None = Field(
        default=None,
        description=(
            "The line the reminder should say from now on, phrased the way it should be heard then. "
            "Leave it null when they are only changing when it fires."
        ),
    )


def _cancel_execute(
    conn,
    symbiot_id: int,
    intake_id: int,
    args: CancelArgs,
    now_local,
    zone_name: str,
    verdict: tools.ObservationVerdict | None = None,
) -> tools.ToolResult:
    """Call off the reminder the judge picked out — or ask which one, when it picked none.

    No verdict, or a verdict naming nothing, both mean the same thing:
    the machine does not know which reminder is meant, so it asks rather than cancelling one on a guess.
    That is the reactive-ambiguity law at its sharpest,
    because a cancel is the one write here the symbiot cannot take back by saying so again.
    The write itself is the same scoped one the terse path uses,
    so a reminder that fired in the gap is refused rather than re-stamped, and the refusal says so.
    """
    if verdict is None or verdict.match is None:
        return tools.ToolResult(
            outcome="UNCLEAR",
            summary=(
                "the human asked to call off a reminder, but it isn't clear which one they mean; "
                "ask them which reminder to drop, naming what it says or when it is due"
            ),
        )
    held = find(conn, symbiot_id, verdict.match)
    if held is None:
        return tools.ToolResult(
            outcome="UNABLE",
            summary="the reminder they meant is no longer on record, so there is nothing to call off",
        )
    if not cancel(conn, symbiot_id, verdict.match):
        settled = "has already fired" if held[4] is not None else "had already been called off"
        return tools.ToolResult(
            outcome="UNABLE",
            summary=f'the reminder "{held[1]}" {settled}, so it could not be called off',
        )
    due = zone.local(held[2], zone_name).strftime("%A %d %B %Y at %H:%M")
    return tools.ToolResult(
        outcome="ACTED",
        summary=f'the reminder "{held[1]}", which was set for {due} ({zone_name}), was called off and will not fire',
    )


def _read_execute(
    conn,
    symbiot_id: int,
    intake_id: int,
    args: ReadArgs,
    now_local,
    zone_name: str,
    verdict: tools.ObservationVerdict | None = None,
) -> tools.ToolResult:
    """Read the standing set over the stretch of time asked about, and hand back the facts for the voice to say.

    The one tool here that is asked a question rather than asked to act, which is why it answers REPORTED:
    nothing was asked to change, so nothing happened, and ACTED's "it happened" would be untrue of a read.
    The facts, grouped by day and in order, near ones before far ones —
    the confirmation call turns them into prose in the symbiot's own voice,
    which is what makes this the other thing entirely from the terse listing:
    that one is for operating on the rows, this one is for being told.
    An empty window says so plainly, because "nothing next week" is a real answer and silence isn't.
    A window that won't resolve is asked about rather than guessed at.
    The read is capped, and when the cap bites it says there are more beyond what it can see
    rather than summarising the first handful as though that were all of it —
    incomplete and saying so is a limit; incomplete and silent would be a bug.
    """
    if args.from_time is None or args.until_time is None:
        return tools.ToolResult(
            outcome="UNCLEAR",
            summary=(
                "the human asked what is being held for them, but over what stretch of time isn't clear; "
                "ask them which stretch they mean — today, this week, before a particular date"
            ),
        )
    from_time = args.from_time.replace(tzinfo=ZoneInfo(zone_name))
    until_time = args.until_time.replace(tzinfo=ZoneInfo(zone_name))
    if until_time < from_time:
        from_time, until_time = until_time, from_time
    span = (
        f'from {from_time.strftime("%A %d %B %Y at %H:%M")} to {until_time.strftime("%A %d %B %Y at %H:%M")}'
    )
    found = within(conn, symbiot_id, from_time, until_time, config.REMINDER_DIGEST_LIMIT)
    if not found:
        return tools.ToolResult(
            outcome="REPORTED",
            summary=f"nothing at all is being held for them {span} ({zone_name})",
        )
    capped = len(found) > config.REMINDER_DIGEST_LIMIT
    shown = found[: config.REMINDER_DIGEST_LIMIT]
    # Grouped by the symbiot's own calendar day, since that is the unit a person hears a schedule in.
    by_day: dict[str, list[str]] = {}
    for _reminder_id, body, fire_at in shown:
        local = zone.local(fire_at, zone_name)
        by_day.setdefault(local.strftime("%A %d %B %Y"), []).append(f'{local.strftime("%H:%M")} — "{body}"')
    lines = [f"{day}: " + "; ".join(items) for day, items in by_day.items()]
    tail = (
        f" There are more beyond these {config.REMINDER_DIGEST_LIMIT} in that stretch — "
        "say so, rather than presenting these as all of them."
        if capped
        else ""
    )
    return tools.ToolResult(
        outcome="REPORTED",
        summary=(
            f"what is being held for them {span} ({zone_name}):\n" + "\n".join(lines) + tail
        ),
    )


def _target_observe(conn, symbiot_id: int, args, now_local, zone_name: str) -> tools.Observation | None:
    """The recall behind cancelling and editing by language: which held reminder is this phrase pointing at?

    The same shape as the deduplication's hook and the same reason for existing:
    "the dentist one" resolves against what is held, not against the sentence.
    Shared by both tools, since the question is identical and only what happens to the answer differs —
    which is the whole point of the hook being a property of a tool rather than a step in the reminder.
    No window here: there is no proposed moment to look around,
    so the live set is what is in play and the wording is the only signal.
    Returns None when they pointed at nothing, or when there is nothing held to point at,
    and the executor then asks which one rather than acting on a guess.
    """
    reference = (args.reference or "").strip()
    if not reference:
        return None
    found = near(conn, symbiot_id, reference, config.REMINDER_TARGET_LIMIT)
    if not found:
        return None
    return tools.Observation(
        question=(
            "Which of these reminders is the human pointing at? "
            "Match on what the reminder is about and when it is due, against how they referred to it. "
            "If two of them fit their words equally well, or none clearly does, answer null — "
            "being asked which one is far better for them than the wrong one being changed."
        ),
        candidates=[
            tools.ObservedCandidate(
                ref=row[0],
                description=f'"{row[1]}", set for {zone.local(row[2], zone_name).strftime("%A %d %B at %H:%M")}',
            )
            for row in found
        ],
    )


def _update_execute(
    conn,
    symbiot_id: int,
    intake_id: int,
    args: UpdateArgs,
    now_local,
    zone_name: str,
    verdict: tools.ObservationVerdict | None = None,
) -> tools.ToolResult:
    """Move a held reminder's moment, change its line, or both — or ask, when either half isn't clear.

    Two things must be clear to act: which reminder, and what to change it to.
    Either missing is asked about rather than guessed at, the same law the schedule follows.
    A new moment is read as the symbiot's wall clock and refused outright if it isn't in the future —
    UNABLE, not a question, for exactly the reason the schedule refuses one that way:
    a reminder points forward, and a human asked when they want it instead
    will answer with the moment they already named, which fails the same guard again.
    """
    if verdict is None or verdict.match is None:
        return tools.ToolResult(
            outcome="UNCLEAR",
            summary=(
                "the human asked to change a reminder, but it isn't clear which one they mean; "
                "ask them which reminder to change, naming what it says or when it is due"
            ),
        )
    said = (args.new_message or "").strip() or None
    if said is None and args.new_fire_at is None:
        return tools.ToolResult(
            outcome="UNCLEAR",
            summary=(
                "the human asked to change a reminder, but what to change about it isn't clear; "
                "ask them whether they mean a new time, a new wording, or both"
            ),
        )
    fire_at = None
    if args.new_fire_at is not None:
        fire_at = args.new_fire_at.replace(tzinfo=ZoneInfo(zone_name))
        if fire_at <= now_local:
            gone = zone.local(fire_at, zone_name).strftime("%A %d %B %Y at %H:%M")
            return tools.ToolResult(
                outcome="UNABLE",
                summary=(
                    f"the new time that came out of what they said, {gone} ({zone_name}), has already gone by, "
                    "and a reminder only points forward, so it was left where it was; "
                    "name that time back to them, so a misread one is theirs to correct"
                ),
            )
    held = find(conn, symbiot_id, verdict.match)
    if held is None:
        return tools.ToolResult(
            outcome="UNABLE",
            summary="the reminder they meant is no longer on record, so there was nothing to change",
        )
    if not update(conn, symbiot_id, verdict.match, body=said, fire_at=fire_at):
        settled = "has already fired" if held[4] is not None else "had already been called off"
        return tools.ToolResult(
            outcome="UNABLE",
            summary=f'the reminder "{held[1]}" {settled}, so it could not be changed',
        )
    now_says = said or held[1]
    now_due = zone.local(fire_at or held[2], zone_name).strftime("%A %d %B %Y at %H:%M")
    return tools.ToolResult(
        outcome="ACTED",
        summary=f'that reminder now says "{now_says}" and is set for {now_due} ({zone_name})',
    )


tools.register(
    tools.Tool(
        name=CANCEL_NAME,
        description=CANCEL_DESCRIPTION,
        args_model=CancelArgs,
        executor=_cancel_execute,
        observe=_target_observe,
    )
)
tools.register(
    tools.Tool(
        name=READ_NAME,
        description=READ_DESCRIPTION,
        args_model=ReadArgs,
        executor=_read_execute,
    )
)
tools.register(
    tools.Tool(
        name=UPDATE_NAME,
        description=UPDATE_DESCRIPTION,
        args_model=UpdateArgs,
        executor=_update_execute,
        observe=_target_observe,
    )
)
