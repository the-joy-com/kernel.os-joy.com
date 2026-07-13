"""Offline duplicate garbage collection: collapsing the semantic duplicates lazy minting breeds.

Forward-only minting can only ever look at the store it has *now*,
so over time it coins the same idea twice under two names —
`workout_action` on Tuesday, `training_session` on Friday —
whenever recall or the re-rank narrowly misses the twin already there
(see the build log for why this is a property of routing by similarity, not a bug).
Nothing on the write path can close that gap:
a name that doesn't yet exist is always a legal thing to coin.
So it is closed here instead, backward —
a pass that reads the store as it stands, finds the near-twins, and merges them into one.

The pass runs off the read path, on a slow cadence, so a question is never slowed by it. Its shape:

  * find suspects cheaply — the type pairs sitting close in vector space are only *candidates*;
    nothing is ever merged on distance alone (a sprint and a marathon sit close and are not the same),
    so the distance is just a pre-filter that keeps the model from being asked about every pair;
  * confirm each pair with the model — one yes/no call: are these two genuinely the same kind of thing?
  * stitch confirmed pairs into clusters — if A≡B and B≡C are both confirmed,
    that is one family of three, resolved together, not two separate merges (union-find);
  * pick a survivor per cluster — the model names the one canonical type to keep;
  * collapse the rest into it — re-point every fact link and child type onto the survivor,
    leave each loser behind as a redirect (merged_into) rather than a delete so any lingering reference resolves,
    and drop the loser's vector so recall stops offering it.

Every model call crosses the boundary as a validated Pydantic model (llm.generate_json),
the same fast, thinking-off, grammar-constrained call the router's write path makes.
A small model with no reasoning trace confirms only the clear-cut twins and leaves the subtler synonyms standing;
that under-merging is the price of keeping every local call fast, and is the honest floor this pass works from.
"""

import threading
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, create_model

from core import config
from core import db
from core import logs
from services.adapters import llm


@dataclass(frozen=True)
class Type:
    """One live ontology type, as the merge pass reads it: id, name, and the definition it judges by."""

    ontology_id: int
    type_name: str
    definition: str


def candidate_pairs(conn, distance: float | None = None) -> list[tuple[int, int]]:
    """The near-twin suspects: pairs of live types whose vectors sit within `distance`, nearest first.

    A self-join over the active-model vectors,
    each side held to live types (merged_into IS NULL) so a type already folded away is never re-offered.
    The pair is ordered a<b once, never both ways,
    and the cosine distance is only a cheap pre-filter — the model makes the real same-or-not call,
    so this net is cast by proximity alone and deliberately kept simple:
    on a store the size of one life's concepts an exact all-pairs scan is cheap and exact,
    and needs no index or approximate search.
    """
    if distance is None:
        distance = config.GC_DISTANCE
    rows = conn.execute(
        """
        SELECT ea.ontology_id, eb.ontology_id
        FROM active_ontology_embedding ea
        JOIN active_ontology_embedding eb ON eb.ontology_id > ea.ontology_id
        JOIN schema_ontology a ON a.id = ea.ontology_id AND a.merged_into IS NULL
        JOIN schema_ontology b ON b.id = eb.ontology_id AND b.merged_into IS NULL
        WHERE (ea.embedding <=> eb.embedding) < %(distance)s
        ORDER BY (ea.embedding <=> eb.embedding)
        """,
        {"distance": distance},
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _load_types(conn, ontology_ids: set[int]) -> dict[int, Type]:
    # The name and definition behind each id the pairs mention, in one read — what the model judges by.
    rows = conn.execute(
        "SELECT id, type_name, definition FROM schema_ontology WHERE id = ANY(%s)",
        (list(ontology_ids),),
    ).fetchall()
    return {r[0]: Type(r[0], r[1], r[2]) for r in rows}


class _SameKindReply(BaseModel):
    """The pair-confirmation verdict: are these two types the same kind of thing, to be merged?

    A plain module-level model — a fixed single boolean,
    nothing about it depends on the pair in hand,
    so it is written once rather than built per call (like the grey gate's, unlike the re-rank's)."""

    same: bool


def _confirm_prompt(a: Type, b: Type) -> str:
    # Both types offered by name *and definition*, so the model judges meaning, not the labels.
    return (
        "You decide whether two concept types in a personal diary's vocabulary name the same kind of "
        "thing — genuine duplicates that should be merged into one.\n\n"
        f"Type A: {a.type_name} — {a.definition}\n"
        f"Type B: {b.type_name} — {b.definition}\n\n"
        "Judge the *kind* of thing, not mere topical closeness — a sprint and a marathon are related "
        "yet are different kinds of act, and must not be merged. Merge only when the two are one and "
        "the same kind wearing two names.\n\n"
        'Return JSON only: {"same": true} if they are the same kind and should be merged, '
        '{"same": false} if not.'
    )


def confirm_same_kind(a: Type, b: Type) -> bool:
    """Ask the model the one question distance can't answer: are these two types the same kind?

    The precise call recall's cosine nearness was never allowed to make,
    run here on the two full definitions.
    A True marks the pair a real duplicate to be merged; a False leaves them apart.
    It is a fast, thinking-off call like every other through this boundary:
    a small model with no reasoning trace confirms the clear-cut twins and passes over the subtler synonyms,
    which leaves this pass under-merging rather than over-merging — the safe direction, and the price of local speed.
    """
    return llm.generate_json(_confirm_prompt(a, b), _SameKindReply).same


def cluster(pairs: list[tuple[int, int]]) -> list[list[int]]:
    """Stitch confirmed same-kind pairs into clusters — the union-find that makes A≡B, B≡C one family.

    Two pairs that share a type describe one group, not two overlapping merges,
    so a fact filed under any member ends up under a single survivor rather than split across sibling merges.
    Each returned cluster holds every type transitively linked by the confirmed pairs,
    sorted for a stable order;
    a lone unconfirmed type never appears, since only ids drawn from real pairs enter the union.
    """
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path compression: point straight at the grandparent
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in pairs:
        union(a, b)

    groups: dict[int, list[int]] = {}
    for node in parent:
        groups.setdefault(find(node), []).append(node)
    return [sorted(group) for group in groups.values() if len(group) >= 2]


def _survivor_reply_model(names: tuple[str, ...]) -> type[BaseModel]:
    # Built per call: the legal survivor names aren't known until we have the cluster,
    # so `survivor` is a Literal over exactly this cluster's type names,
    # which becomes the decode grammar (see llm.generate_json) so the model can only emit a real member.
    return create_model("_SurvivorReply", survivor=(Literal[names], ...))


def _survivor_prompt(types: list[Type]) -> str:
    lines = "\n".join(f"- {t.type_name} — {t.definition}" for t in types)
    return (
        f"These {len(types)} concept types have been confirmed to name the same kind of thing, and "
        "will be merged into one.\n"
        "Pick which one should survive as the single canonical type — the clearest, most general name "
        "and definition, the one every merged fact is best filed under.\n\n"
        f"{lines}\n\n"
        'Return JSON only: {"survivor": "<one of the names above>"}.'
    )


def pick_survivor(types: list[Type]) -> int:
    """Let the model choose which type in a confirmed cluster survives, and return its id.

    The cluster's types are one idea under several names;
    the model, already holding every definition, names the clearest to keep as canonical.
    Its answer is constrained to a Literal of exactly the cluster's names —
    the decode grammar (see llm.generate_json) — so it can only resolve to a real member,
    and we map that name back to its id for the collapse.
    A fast, thinking-off call like the confirmation before it.
    """
    by_name = {t.type_name: t.ontology_id for t in types}
    names = tuple(by_name)
    reply = llm.generate_json(_survivor_prompt(types), _survivor_reply_model(names))
    return by_name[reply.survivor]


def collapse(conn, survivor_id: int, loser_id: int) -> None:
    """Fold one loser type into the survivor — atomically, and idempotently on the fact links.

    Everything that pointed at the loser is re-pointed at the survivor,
    the loser is left behind as a redirect rather than deleted,
    and its vector is dropped so recall stops offering it.
    The whole move is one transaction,
    so the store is never caught with a fact linked to a type that has half vanished.

    The fact-link re-point must be idempotent:
    a fact already linked to *both* loser and survivor would collide with the join table's composite key on a blind re-point,
    so the colliding loser links are dropped first and only the rest are re-pointed.
    The child re-point carries one guard:
    a child that *is* the survivor must not be made its own parent,
    and if the survivor's own parent was the loser that edge is nulled rather than left pointing at a redirect —
    either way no type ends up parented to itself or to a tombstone.
    """
    with conn.transaction():
        # Idempotent fact-link re-point:
        # drop the loser links that would collide with an existing survivor link for the same fact,
        # then re-point the rest onto the survivor.
        conn.execute(
            "DELETE FROM diary_fact_ontology loser "
            "WHERE loser.ontology_id = %(loser)s AND EXISTS ("
            "  SELECT 1 FROM diary_fact_ontology survivor "
            "  WHERE survivor.diary_fact_id = loser.diary_fact_id "
            "    AND survivor.ontology_id = %(survivor)s)",
            {"loser": loser_id, "survivor": survivor_id},
        )
        conn.execute(
            "UPDATE diary_fact_ontology SET ontology_id = %(survivor)s WHERE ontology_id = %(loser)s",
            {"loser": loser_id, "survivor": survivor_id},
        )
        # Re-point the loser's children onto the survivor, never making the survivor its own parent.
        conn.execute(
            "UPDATE schema_ontology SET parent_id = %(survivor)s "
            "WHERE parent_id = %(loser)s AND id <> %(survivor)s",
            {"loser": loser_id, "survivor": survivor_id},
        )
        # If the survivor's own parent was the loser, that edge would now point at a redirect: null it.
        conn.execute(
            "UPDATE schema_ontology SET parent_id = NULL WHERE id = %(survivor)s AND parent_id = %(loser)s",
            {"loser": loser_id, "survivor": survivor_id},
        )
        # Tombstone the loser as a redirect to the survivor — kept, not deleted,
        # so any lingering reference still resolves;
        # recall already skips a type whose merged_into is set.
        conn.execute(
            "UPDATE schema_ontology SET merged_into = %(survivor)s WHERE id = %(loser)s",
            {"loser": loser_id, "survivor": survivor_id},
        )
        # Drop the loser's vector through the active view, so recall stops offering it outright.
        conn.execute(
            "DELETE FROM active_ontology_embedding WHERE ontology_id = %s", (loser_id,)
        )


def run_once(conn) -> list[dict]:
    """One full merge pass over the store: find, confirm, cluster, pick, collapse. Returns a report.

    The reads (candidate pairs, the types behind them) and the model calls (confirm, pick) run with no transaction open,
    so a slow round trip never pins a transaction;
    only each collapse opens one, and only around its own writes.
    The report is one entry per cluster merged — the survivor id and the ids folded into it —
    so the sweep can log what it did and a by-hand run can be eyeballed.
    """
    pairs = candidate_pairs(conn)
    if not pairs:
        return []
    types = _load_types(conn, {oid for pair in pairs for oid in pair})
    confirmed = [(a, b) for a, b in pairs if confirm_same_kind(types[a], types[b])]
    report: list[dict] = []
    for group in cluster(confirmed):
        survivor_id = pick_survivor([types[oid] for oid in group])
        losers = [oid for oid in group if oid != survivor_id]
        for loser_id in losers:
            collapse(conn, survivor_id, loser_id)
        report.append({"survivor": survivor_id, "merged": losers})
    return report


def run_sweep(stop: threading.Event) -> None:
    """Run one merge pass every GC_SWEEP_INTERVAL_SECONDS until `stop` is set. Started from lifespan.

    The offline counterpart to the intake reconcile sweep:
    a single thread waking on a slow cadence — a day, not seconds,
    because duplicates accrue slowly and this never sits on the read path.
    A bad iteration is logged and swallowed so it can't take the loop down,
    the same discipline the intake sweep keeps.
    It holds one pooled connection per pass but opens no transaction across a model call.
    """
    log = logs.get("ontology-gc")
    log.info("ontology gc sweep started")
    while not stop.is_set():
        try:
            with db.get_pool().connection() as conn:
                merges = run_once(conn)
            if merges:
                folded = sum(len(m["merged"]) for m in merges)
                log.info(
                    "ontology gc: merged %d duplicate type(s) into %d survivor(s)", folded, len(merges)
                )
        except Exception:
            log.exception("ontology gc sweep iteration failed")
        stop.wait(config.GC_SWEEP_INTERVAL_SECONDS)
    log.info("ontology gc sweep stopped")
