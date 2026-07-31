"""Observe: the read side of the observability corner, the /observe surface's data layer.

The shell's /observe command is a hub of cards, each a lens onto something worth watching in the running machine,
and this module is where a card's data is read. It is read-only by construction:
it looks at what the machine has already said and reports it, and touches no state the loop writes,
so a lens can never change how the machine behaves — which is the whole safety of an observe-first surface.

The first card is echoes: the symbiot's recent machine utterances, scored so redundancy can be seen —
the same thought said twice in slightly different clothes, which a tired human reading their own scrollback misses.
recent_utterances gathers them off the conversation stream — the one place every line the machine says lands,
whatever produced it — following each row's pointer to the words that live durably elsewhere
(the intake row's answer for a fast reply, the missive's body for a follow-up or a reminder),
and labelling each by the mechanism that raised it, which falls out for free from which pointer the row carries:
an intake pointer is a fast reply, a missive an enrichment marks is a deep follow-up, any other missive a note.
echoes then embeds those lines and groups the near-duplicates into clusters — the *more or less* the same,
measured as cosine closeness rather than guessed at — leaving the lines that echoed nothing to stand alone.

The second card is reminders: the symbiot's most recent reminders,
each paired with the human line that triggered it (recent_reminders).
Its point is that pairing — the message said, and the reminder it produced —
so a reminder set against a line that only *mentioned* a task, rather than asked for one, is visible at a glance,
and the real examples can be gathered to harden the tool's judgment. A plain read of the reminder ledger, like the rest here.

The same card carries the mirror image of that mistake:
the declines (recent_declines) — the times the machine refused to set a reminder it judged the store already held.
Deduplication writes nothing, that being its entire job,
so without a row of its own the first decision the machine takes
on its own judgment rather than on the symbiot's words would be the one decision that leaves no trace,
which is backwards.
The failure worth catching is it reading "call the dentist about the referral letter"
as the same thing as "call the dentist", filing nothing,
and the symbiot never learning it made that call —
so the decline carries both wordings, because the wording is the evidence.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from services import echo
from services.adapters import embedding

# How far back the echoes lens reaches: the most recent machine utterances to gather.
# A knob left deliberately generous and un-tuned: the observe-first ethic is to set it against real output later,
# not guess the right window blind — so it lives here as a plain default, not in config yet.
RECENT_UTTERANCE_LIMIT = 40

# How many reminders the reminders lens reaches back over — a shorter window than the utterances one,
# because a reminder is a rarer event and the card is read to spot a bad pairing, not to scroll a stream.
# The same un-tuned plain default, to be set against real use rather than guessed at blind.
RECENT_REMINDER_LIMIT = 20


@dataclass
class MachineUterrance:
    """One thing the machine said, as the echoes lens reports it.

    mechanism is which part of the loop raised it — 'quick' for a fast reply, 'deep' for an enrichment follow-up,
    'note' for a line the kernel raised on its own (a reminder, a relay).
    trigger is the human line it answered, when there is one (a fast reply answers a message);
    a machine-initiated line has none, so it is None.
    created_at is the absolute instant it went onto the stream, to be rendered in the symbiot's own zone.
    """

    mechanism: str
    text: str
    trigger: str | None
    created_at: datetime


@dataclass
class MachineUtteranceCluster:
    """A set of machine utterances that say more or less the same thing — an echo, as the lens reports it.

    members are the lines that clustered together, newest first;
    similarity is the strongest pairwise closeness within the set (0..1), the headline number the view shows.
    """

    similarity: float
    members: list[MachineUterrance]


@dataclass
class MachineEchoes:
    """The echoes lens's full answer: what clustered, what stood alone, and whether scoring ran at all.

    clusters are the echo groups, strongest first; singles are the lines that echoed nothing, newest first.
    scored is False only when the similarity pass could not run because the embedder was unreachable —
    the lens then still shows the plain mirror (every line a single) rather than erroring,
    honest that it could not measure closeness this time.
    """

    scored: bool
    clusters: list[MachineUtteranceCluster]
    singles: list[MachineUterrance]


@dataclass
class RecentReminder:
    """One scheduled reminder as the reminders lens reports it — the effect and the line that triggered it.

    trigger is the human message the reminder was scheduled from, the words that made the machine act;
    it is the pairing the card exists for, since a reminder set against a line that only mentioned a task —
    rather than asked to be reminded — is exactly the over-eagerness this lens is meant to surface.
    It is None for a reminder the symbiot typed in directly, which had no line behind it —
    and that absence is itself the distinction the card wants to draw.
    reminder_id is the row's own id, so a line on the card can be spoken about and acted on.
    body is the line to be said back when it fires.
    fire_at is when it is due and created_at when it was scheduled, both absolute instants
    to be rendered in the symbiot's own zone.
    state is one word for where the reminder stands — pending, fired, or cancelled —
    so a spent one and a called-off one read apart from each other and from a live one.
    channels is where it is to be delivered, or None when the symbiot named none and it rides every channel.
    """

    reminder_id: int
    trigger: str | None
    body: str
    fire_at: datetime
    created_at: datetime
    state: str
    channels: list[str] | None


@dataclass
class ReminderDecline:
    """One time the machine refused to set a reminder it judged the store already held.

    asked is the human message that asked for it, held is the line of the standing reminder the judge matched it to;
    the pairing is the whole point,
    because the failure worth catching is two differently worded intents collapsing into one,
    and the wording is the evidence.
    reminder_id is the matched reminder's own id, so a decline can be read against the reminder above it on the card.
    fire_at is when that standing reminder is due, and created_at when the refusal was made —
    both absolute instants to be rendered in the symbiot's own zone.
    """

    reminder_id: int
    asked: str
    held: str
    fire_at: datetime
    created_at: datetime


def held_back_count(conn, symbiot_id: int) -> int:
    """How many deep follow-ups the echo guard has held back for this symbiot — the muzzle's audit count.

    A deep reply the gate composed but the guard refused as a near-duplicate is never sent;
    it is recorded echo_suppressed on its enrichment row (worker._enrich_one), and this counts those, all-time,
    so the echoes card can show the other side of the coin:
    the lens sees the redundancy that got through, and this many were stopped before delivery.
    A pure read: no lock, no write, off the loop's path.
    """
    return conn.execute(
        "SELECT count(*) FROM enrichment WHERE symbiot_id = %s AND echo_suppressed",
        (symbiot_id,),
    ).fetchone()[0]


def machine_echoes(conn, symbiot_id: int, threshold: float = echo.ECHO_THRESHOLD) -> MachineEchoes:
    """Score the symbiot's recent machine utterances for redundancy, grouping the near-duplicates into clusters.

    The gather is recent_utterances; the scoring embeds each line once — as a 'document',
    so two of my own lines are compared symmetrically — and reads the cosine closeness between every pair.
    Lines at or above the threshold are joined into a cluster, transitively,
    so a chain of near-duplicates lands in one group, and a cluster's headline similarity is the strongest pair inside it;
    a line that echoes nothing stands alone. Clusters come back strongest first, their members and the singles newest first.
    Read-only, and off the loop's path: the embedding cost is paid only here, when a symbiot opens the lens.
    Fewer than two lines cannot echo, so scoring is skipped and the embedder is never called.
    If the embedder is unreachable the pass degrades rather than fails — every line comes back a single, scored False —
    so the lens still shows the plain mirror instead of an error.
    """
    utterances = recent_machine_utterances(conn, symbiot_id)
    if len(utterances) < 2:
        return MachineEchoes(scored=True, clusters=[], singles=utterances)
    try:
        vectors = embedding.embed_many([u.text for u in utterances], task="document")
    except Exception:
        # The embedder is down or slow: don't error the read — fall back to the plain mirror, honestly unscored.
        return MachineEchoes(scored=False, clusters=[], singles=utterances)

    n = len(utterances)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    # Pairwise closeness, kept so a cluster's headline can be its strongest pair without recomputing.
    sims: dict[tuple[int, int], float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            s = echo.cosine(vectors[i], vectors[j])
            sims[(i, j)] = s
            if s >= threshold:
                union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    clusters: list[MachineUtteranceCluster] = []
    singles: list[MachineUterrance] = []
    for members in groups.values():
        if len(members) >= 2:
            rep = max(sims[(a, b)] for a in members for b in members if a < b)
            clusters.append(MachineUtteranceCluster(similarity=rep, members=[utterances[i] for i in sorted(members)]))
        else:
            singles.append(utterances[members[0]])
    clusters.sort(key=lambda c: c.similarity, reverse=True)
    singles.sort(key=lambda u: u.created_at, reverse=True)
    return MachineEchoes(scored=True, clusters=clusters, singles=singles)


def recent_declines(conn, symbiot_id: int, limit: int = RECENT_REMINDER_LIMIT) -> list[ReminderDecline]:
    """The times the machine refused to set a reminder it judged the store already held, newest first.

    The other half of the reminders card's pairing, and the reason the declines table exists at all:
    deduplication writes nothing by design,
    so without this the first decision the machine takes on its own judgment rather than on the symbiot's words
    would be the one that leaves no trace.
    Both sides of that judgment are here — the message that asked, and the standing reminder it matched —
    because the failure worth catching is two differently worded intents collapsing into one,
    and the wording is the evidence.
    A pure read like the rest of this module, reaching the symbiot through the reminder it matched.
    """
    rows = conn.execute(
        """
        SELECT d.reminder_id, i.message, r.body, r.fire_at, d.created_at
        FROM reminder_decline d
        JOIN reminder r ON r.id = d.reminder_id
        JOIN intake i ON i.id = d.intake_id
        WHERE r.symbiot_id = %(symbiot)s
        ORDER BY d.id DESC
        LIMIT %(limit)s
        """,
        {"symbiot": symbiot_id, "limit": limit},
    ).fetchall()
    return [
        ReminderDecline(reminder_id=r[0], asked=r[1], held=r[2], fire_at=r[3], created_at=r[4])
        for r in rows
    ]


def recent_machine_utterances(conn, symbiot_id: int, limit: int = RECENT_UTTERANCE_LIMIT) -> list[MachineUterrance]:
    """The symbiot's most recent machine utterances, newest first, for the echoes lens.

    Reads the machine side of the conversation stream and resolves each row to its words and its origin:
    a fast reply's words are its intake row's answer and its trigger is that row's message;
    a missive's words are its body, and an enrichment row claiming it marks a deep follow-up ('deep');
    any other missive is a note.
    Bounded by `limit` at the newest end (the stream's own id order) and handed back newest-first,
    so the lens opens on the latest line — the way a symbiot scanning their own scrollback reads it.
    """
    rows = conn.execute(
        """
        SELECT
            CASE
                WHEN ci.intake_id IS NOT NULL THEN 'quick'
                WHEN e.id IS NOT NULL          THEN 'deep'
                ELSE 'note'
            END AS mechanism,
            CASE
                WHEN ci.intake_id IS NOT NULL THEN i.answer
                ELSE m.body
            END AS text,
            i.message AS trigger,
            ci.created_at
        FROM conversation_item ci
        LEFT JOIN intake     i ON i.id = ci.intake_id
        LEFT JOIN missive    m ON m.id = ci.missive_id
        LEFT JOIN enrichment e ON e.missive_id = ci.missive_id
        WHERE ci.symbiot_id = %(symbiot)s
          AND ci.role = 'machine'
        ORDER BY ci.id DESC
        LIMIT %(limit)s
        """,
        {"symbiot": symbiot_id, "limit": limit},
    ).fetchall()
    # Newest-first straight off the index — the order the lens reads.
    return [
        MachineUterrance(mechanism=r[0], text=r[1], trigger=r[2], created_at=r[3])
        for r in rows
    ]


def recent_reminders(conn, symbiot_id: int, limit: int = RECENT_REMINDER_LIMIT) -> list[RecentReminder]:
    """The symbiot's most recent reminders, newest first, for the reminders lens.

    Reads the reminder ledger and resolves each row to the human message that triggered it (intake.message),
    so the card shows the pairing that matters for hardening the tool: the line said, and the reminder it produced.

    A LEFT join, and the distinction is the sort that hides.
    This used to be an inner join, resting on the fact that every reminder was born from a message —
    true while that was the only way one could exist, false the moment a reminder can be typed in directly.
    An inner join does not complain about that; it drops the row.
    Every directly-made reminder would have been absent from the audit surface,
    and the card would have gone on looking complete while reporting a subset,
    which is worse than no card at all.
    So: a left join, and a reminder with no line behind it reads as one the symbiot set itself —
    which is the distinction the card exists to draw anyway.

    Newest first, the order a "last few reminders" audit is scanned in, and bounded by `limit` at that newest end.
    A pure read: it touches the reminder and intake rows only, holds no lock, and writes nothing — off the loop's path.
    """
    rows = conn.execute(
        """
        SELECT r.id, i.message, r.body, r.fire_at, r.created_at, r.channels,
               CASE WHEN r.fired_at IS NOT NULL THEN 'fired'
                    WHEN r.cancelled_at IS NOT NULL THEN 'cancelled'
                    ELSE 'pending' END AS state
        FROM reminder r
        LEFT JOIN intake i ON i.id = r.intake_id
        WHERE r.symbiot_id = %(symbiot)s
        ORDER BY r.id DESC
        LIMIT %(limit)s
        """,
        {"symbiot": symbiot_id, "limit": limit},
    ).fetchall()
    return [
        RecentReminder(
            reminder_id=r[0], trigger=r[1], body=r[2], fire_at=r[3], created_at=r[4], channels=r[5], state=r[6]
        )
        for r in rows
    ]
