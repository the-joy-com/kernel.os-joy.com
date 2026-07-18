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
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from services.adapters import embedding

# Cosine closeness at or above which two of my lines count as an echo — a semantic near-duplicate,
# the "more or less the same" a hash would miss. Tuned against real nomic-embed-text output (test/qa/0009):
# unrelated lines land around 0.55–0.60 and clear paraphrases around 0.80–0.88, so the bar sits in that gap —
# low enough to catch a loose paraphrase, high enough to leave unrelated lines alone. Still a knob; move it if real use drifts.
ECHO_THRESHOLD = 0.75

# How far back the echoes lens reaches: the most recent machine utterances to gather.
# A knob left deliberately generous and un-tuned: the observe-first ethic is to set it against real output later,
# not guess the right window blind — so it lives here as a plain default, not in config yet.
RECENT_UTTERANCE_LIMIT = 40


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

    members are the lines that clustered together, oldest first;
    similarity is the strongest pairwise closeness within the set (0..1), the headline number the view shows.
    """

    similarity: float
    members: list[MachineUterrance]


@dataclass
class MachineEchoes:
    """The echoes lens's full answer: what clustered, what stood alone, and whether scoring ran at all.

    clusters are the echo groups, strongest first; singles are the lines that echoed nothing, oldest first.
    scored is False only when the similarity pass could not run because the embedder was unreachable —
    the lens then still shows the plain mirror (every line a single) rather than erroring,
    honest that it could not measure closeness this time.
    """

    scored: bool
    clusters: list[MachineUtteranceCluster]
    singles: list[MachineUterrance]


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors, 0 when either has no length — the closeness the echo test reads."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def machine_echoes(conn, symbiot_id: int, threshold: float = ECHO_THRESHOLD) -> MachineEchoes:
    """Score the symbiot's recent machine utterances for redundancy, grouping the near-duplicates into clusters.

    The gather is recent_utterances; the scoring embeds each line once — as a 'document',
    so two of my own lines are compared symmetrically — and reads the cosine closeness between every pair.
    Lines at or above the threshold are joined into a cluster, transitively,
    so a chain of near-duplicates lands in one group, and a cluster's headline similarity is the strongest pair inside it;
    a line that echoes nothing stands alone. Clusters come back strongest first, singles oldest first.
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
            s = _cosine(vectors[i], vectors[j])
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
    singles.sort(key=lambda u: u.created_at)
    return MachineEchoes(scored=True, clusters=clusters, singles=singles)


def recent_machine_utterances(conn, symbiot_id: int, limit: int = RECENT_UTTERANCE_LIMIT) -> list[MachineUterrance]:
    """The symbiot's most recent machine utterances, oldest first, for the echoes lens.

    Reads the machine side of the conversation stream and resolves each row to its words and its origin:
    a fast reply's words are its intake row's answer and its trigger is that row's message;
    a missive's words are its body, and an enrichment row claiming it marks a deep follow-up ('deep');
    any other missive is a note.
    Bounded by `limit` at the newest end (the stream's own id order), then handed back oldest-first
    so the reader scans it the way the conversation ran — the order redundancy is easiest to see in.
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
    # Newest-first off the index, reversed here so the lens reads oldest-first.
    return [
        MachineUterrance(mechanism=r[0], text=r[1], trigger=r[2], created_at=r[3])
        for r in reversed(rows)
    ]
