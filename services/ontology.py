"""Ontology routing: filing a diary fact into the vocabulary of concepts it touches.

A fact is rarely just one thing, so it is filed under every concept it genuinely touches, not one,
and each concept takes the same trip — recall, re-rank, mint-if-new — on its own.
This module owns that path over the ontology store (migration 0010).

The path opens with recall: a wide, approximate nomination of the concept types
nearest an embedded fact or concept, ordered by cosine distance.
Recall never decides.
Vector distance measures how *related* two things are, not whether they are the *same kind* of thing —
a sprint and a marathon land almost on top of each other and are still different kinds of act —
so the distance is only allowed to pull the plausible candidates into the room.
The re-rank that judges which of them truly fits — a generative model reads the fact and each
candidate's definition together and scores the fit — and the minting that coins a new type when
none does are the rest of this path, and read the pool recall hands back.

A raw fact enters this path first through naming: an LLM reads the fact and names the distinct
concepts it expresses, and each named concept then takes that recall / re-rank / mint-if-new trip
on its own. Once every concept has resolved to a type — reused or freshly coined — the fact is
rendered into a deliberately thin JSON-LD payload — its `@type` links and its own raw text, with no
particulars extracted — and persisted once: the fact row, its embedding, and one link per concept
it earned. `ingest` is the entry point that runs the whole path; the steps below build up to it.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, create_model

from core import config
from services import embedding
from services import llm

# The three bands the top re-rank score falls into, deciding what happens to the concept.
REUSE = "reuse"  # a clear enough fit: link the fact to that existing type
GREY = "grey"  # ambiguous: escalate to the one-shot LLM gate
MINT = "mint"  # nothing fits: coin a new type

# How many of the nearest existing types the minter is shown when it coins a new one.
# A type is never coined in a vacuum:
# the model sees these neighbours and either places the new type under one of them or declares it a root,
# so the vocabulary grows as a tree, not a scatter.
# Kept small on purpose — the parent is a single choice among a short list the model can hold in focus,
# the same short-list discipline that sizes the recall pool.
MINT_CONTEXT = 3

# The reply value that means "this type has no parent" — it is a root, parent_id NULL.
# Offered alongside the neighbour names as the one always-legal choice,
# so the model is never forced to hang a new root under an ill-fitting parent
# just because the grammar left it no way to say none.
_MINT_NO_PARENT = "none"


@dataclass(frozen=True)
class Candidate:
    """One nominated ontology type and how near it fell to the query.

    distance is cosine distance through the active-model vectors — smaller is nearer.
    It orders the pool and nothing more, never the match-or-mint call, which is the re-ranker's."""

    ontology_id: int
    type_name: str
    definition: str
    distance: float


def recall_candidates(conn, embedding: list[float], limit: int | None = None) -> list[Candidate]:
    """The nominate pass: the `limit` nearest ontology types to `embedding`, nearest first.

    Reads the store through active_ontology_embedding — the view that always resolves to the live
    model's vectors — so a model swap costs this query nothing and it never names a versioned table.
    A merged type is excluded even if its vector lingers before the garbage pass drops it,
    so recall never nominates a concept already folded into another.
    An empty store returns an empty list: the first concept a diary ever sees has nothing to match,
    which is exactly the signal to mint it.

    ef_search — the HNSW working-set width — is opened per query, comfortably above the pool.
    The index answers approximately from a set of candidates it walks the graph to fill,
    and a set no wider than the result would cap the recall this pass exists to protect,
    from the very first fact rather than only once the store grows.
    It and the search run in one transaction so SET LOCAL holds across both regardless of the
    connection's autocommit mode, and reverts at transaction end rather than leaking onto the pool.
    """
    if limit is None:
        limit = config.RECALL_POOL
    # pgvector has no psycopg adapter installed, so the vector crosses as its text literal and casts ::vector.
    vector_literal = "[" + ",".join(repr(x) for x in embedding) + "]"
    with conn.transaction():
        # SET itself can't take a bind parameter; set_config(..., is_local => true) is the
        # parameter-safe equivalent of SET LOCAL, so the width stays confined to this transaction.
        conn.execute(
            "SELECT set_config('hnsw.ef_search', %s, true)",
            (str(config.RECALL_EF_SEARCH),),
        )
        rows = conn.execute(
            """
            SELECT o.id, o.type_name, o.definition,
                   e.embedding <=> %(q)s::vector AS distance
            FROM active_ontology_embedding e
            JOIN schema_ontology o ON o.id = e.ontology_id
            WHERE o.merged_into IS NULL
            ORDER BY e.embedding <=> %(q)s::vector
            LIMIT %(limit)s
            """,
            {"q": vector_literal, "limit": limit},
        ).fetchall()
    return [Candidate(r[0], r[1], r[2], r[3]) for r in rows]


@dataclass(frozen=True)
class Ranked:
    """One candidate and how well the re-rank judged it categorises the fact.

    score is 0.0–1.0: the generative model's read of fit, not a vector distance —
    it is what decides reuse vs mint, the call recall's distance was never allowed to make."""

    candidate: Candidate
    score: float


def _rerank_prompt(fact_text: str, candidates: list[Candidate]) -> str:
    # Each candidate is offered by name *and definition*, so the model judges meaning, not the label.
    lines = "\n".join(f"- {c.type_name} — {c.definition}" for c in candidates)
    return (
        "You classify a personal diary fact against candidate concept types.\n"
        f'Fact: "{fact_text}"\n\n'
        "Score each candidate type from 0.0 to 1.0 by how well it *categorises* this fact: "
        "1.0 means the fact is clearly an instance of that kind of thing, 0.0 means unrelated. "
        "Judge the kind of thing, not mere topical closeness — a sprint and a marathon are related "
        "yet are different kinds of act.\n\n"
        f"Candidates (name — definition):\n{lines}\n\n"
        'Return JSON only, one entry per candidate: '
        '{"scores": [{"type": "<name>", "score": <0.0-1.0>}]}'
    )


def _rerank_reply_model(candidates: list[Candidate]) -> type[BaseModel]:
    """Build — at runtime — the Pydantic model the re-rank reply must match for *this* candidate pool.

    Why built on the fly rather than written as a normal `class ...(BaseModel)`:
    the set of legal type names isn't known until we see the pool recall handed back,
    and we want the schema to name exactly those — so each call constructs a fresh model
    whose `type` field can only be one of the candidate names in front of us.

    The model describes a reply shaped like:

        {"scores": [{"type": "boxing_session", "score": 0.9},
                    {"type": "friends",        "score": 0.2}]}

    Two things it locks down by construction, not by cleanup afterwards:
      - `type` is a Literal over the exact candidate names,
        so the model can't score a type we never offered or invent a new one —
        that value simply isn't in the grammar.
      - `score` is a float pinned to 0.0–1.0, so an out-of-range number can't come back.

    The model does double duty: its JSON schema is handed to Ollama as the decoder's grammar,
    so the constraints hold *while* the tokens are generated, and the same model validates the
    reply on the way back — a violation raises at the boundary instead of being silently coerced.
    The one thing a schema can't force is *coverage* — that every candidate gets a score —
    so an omitted candidate is defaulted to 0.0 by the caller (rerank_candidates), not here.
    """
    # The closed set of names the reply's `type` field may take:
    # the names recall nominated, and nothing else. Fed to Literal below to become that constraint.
    names = tuple(c.type_name for c in candidates)
    # create_model defines a Pydantic model *dynamically* — the runtime equivalent of writing:
    #     class _RerankScore(BaseModel):
    #         type: Literal[<names>]
    #         score: float = Field(ge=0.0, le=1.0)
    # Each keyword is one field, valued as a (type, default) tuple;
    # `...` (Ellipsis) marks the field required, with no default.
    # `Literal[names]` turns the tuple of names into an enum-like type — `type` must equal one of them;
    # `Field(ge=0.0, le=1.0)` bounds the score to the inclusive range 0.0–1.0.
    score_entry = create_model(
        "_RerankScore",
        type=(Literal[names], ...),
        score=(float, Field(ge=0.0, le=1.0)),
    )
    # Wrap those entries in the top-level reply object: {"scores": [ <_RerankScore>, ... ]}.
    # One _RerankScore per candidate the model scored; the list itself is the required field.
    return create_model("_RerankReply", scores=(list[score_entry], ...))


def rerank_candidates(fact_text: str, candidates: list[Candidate]) -> list[Ranked]:
    """Score every recalled candidate for how well it fits the fact, and return them best first.

    A single LLM call scores the whole pool at once:
    the fact plus each candidate's definition go in,
    and a score from 0.0 to 1.0 per candidate comes back
    (see llm.generate_json and _rerank_reply_model for how the reply is shaped and checked).

    Two edge cases:
      - an empty pool returns an empty list — recall found nothing, so there is nothing to score;
      - a candidate the model forgot to score defaults to 0.0, so it just falls to the bottom.
    """
    if not candidates:
        return []
    reply = llm.generate_json(
        _rerank_prompt(fact_text, candidates), _rerank_reply_model(candidates)
    )
    by_name = {s.type: s.score for s in reply.scores}
    ranked = [Ranked(c, by_name.get(c.type_name, 0.0)) for c in candidates]
    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked


def decide(ranked: list[Ranked]) -> str:
    """Which band the top score falls in — REUSE, GREY, or MINT — the match-or-mint gate.

    An empty ranking is MINT: recall offered nothing (an empty or wholly-unrelated store),
    so there is nothing to reuse and the concept is coined.
    Otherwise the best score is read against the two configured thresholds; the band between
    them is the grey zone the one-shot LLM gate resolves.
    """
    if not ranked:
        return MINT
    top = ranked[0].score
    if top >= config.REUSE_THRESHOLD:
        return REUSE
    if top <= config.MINT_THRESHOLD:
        return MINT
    return GREY


def _grey_gate_prompt(fact_text: str, candidate: Candidate) -> str:
    # The one candidate on the fence, offered by name *and definition* so the model judges meaning.
    return (
        "You decide whether one existing concept type correctly categorises a personal diary fact.\n"
        f'Fact: "{fact_text}"\n\n'
        f"Candidate type: {candidate.type_name} — {candidate.definition}\n\n"
        "Is the fact clearly an instance of that kind of thing? "
        "Judge the kind of thing, not mere topical closeness — a sprint and a marathon are related "
        "yet are different kinds of act.\n\n"
        'Return JSON only: {"fits": true} if this type categorises the fact, {"fits": false} if not.'
    )


class _GreyGateReply(BaseModel):
    """The grey-zone gate's one-bit verdict: does the candidate type categorise the fact?

    A True reuses that type, a False coins a new one — the two outcomes the grey band defers to.
    This is a plain, module-level model, not a per-call build like the re-rank's:
    its shape is fixed at a single boolean, with no candidate names to fold into the grammar,
    so nothing about it depends on the pool in front of us."""

    fits: bool


def resolve_grey(fact_text: str, top: Candidate) -> str:
    """The grey-zone binary gate: reuse-or-mint the top candidate when the re-rank score was ambiguous.

    decide() lands the top score in the grey band when it is neither a clear reuse nor a clear mint.
    Rather than force one or the other on a shaky score, spend one fast yes/no LLM call on the single
    best candidate — does this existing type accurately categorise the fact?
    A yes reuses it (REUSE), a no coins a new type (MINT); this collapses the grey band to one of the
    two live outcomes, so the caller never has to act on GREY itself.
    """
    reply = llm.generate_json(_grey_gate_prompt(fact_text, top), _GreyGateReply)
    return REUSE if reply.fits else MINT


def _mint_prompt(fact_text: str, context: list[Ranked]) -> str:
    # The neighbours are the nearest existing types the mint is placed among,
    # each offered by name *and definition* so the model judges the parent by meaning, not the label.
    placement = ""
    if context:
        lines = "\n".join(f"- {r.candidate.type_name} — {r.candidate.definition}" for r in context)
        placement += (
            "Here are the existing types nearest this fact — coin the new one in relation to them:\n"
            f"{lines}\n\n"
            "If the new type is a more specific kind of one of these, name that type as its parent; "
            f'if it stands on its own as a root concept, use "{_MINT_NO_PARENT}".\n\n'
        )
    else:
        # A cold or wholly-unrelated store: there is nothing to place the type under,
        # so the only honest parent is none and the new type is the first of its line.
        placement += (
            "There are no existing types to place this under yet, "
            f'so its parent is "{_MINT_NO_PARENT}".\n\n'
        )
    return (
        "You are growing a personal diary's vocabulary of concept types.\n"
        "No existing type fits this fact, so coin a new one for it.\n\n"
        f'Fact: "{fact_text}"\n\n'
        f"{placement}"
        "Name the *kind* of thing the fact is, not this one instance of it — "
        "the type is what every later fact of the same kind will reuse.\n\n"
        "Return JSON only: "
        '{"type_name": "<short snake_case label>", '
        '"definition": "<one sentence naming that kind of thing>", '
        f'"parent": "<one of the names above, or {_MINT_NO_PARENT}>"}}'
    )


def _mint_reply_model(context: list[Ranked]) -> type[BaseModel]:
    """Build — at runtime — the Pydantic model the mint reply must match for *this* neighbour set.

    Like the re-rank's reply model, the legal parent names aren't known until we see the context,
    so each call constructs a fresh model whose `parent` field is a Literal over exactly this
    context's names plus the always-present "none" — the same strict-schema-as-decoder-grammar trick.
    The model can't name a parent we never offered, and it can't fail to answer the parent question:
    the grammar admits only a real neighbour or an explicit none, so a floating parent is impossible.
    type_name and definition are free text — what the new type is called and what it means —
    checked only for being present, since their content is exactly what we are asking the model to coin.
    """
    # The closed set the reply's `parent` may take:
    # the neighbour names, and "none" for a root — nothing else is in the grammar.
    parents = tuple(r.candidate.type_name for r in context) + (_MINT_NO_PARENT,)
    return create_model(
        "_MintReply",
        type_name=(str, ...),
        definition=(str, ...),
        parent=(Literal[parents], ...),
    )


def mint(conn, fact_text: str, ranked: list[Ranked]) -> int:
    """Coin a new ontology type for a fact nothing existing fits, and return its id.

    Called on the MINT verdict. The new type is placed among its neighbours, never in a vacuum:
    the model is shown the top MINT_CONTEXT re-ranked types and must give the new type a parent
    that is one of them or none — a parent grows the tree, a none makes a root (parent_id NULL),
    which is the normal outcome for a cold store or a genuinely first-of-its-kind concept.
    The definition it coins is embedded (as a stored document) and its vector lands in the active
    model's table, so the very next recall can nominate this type like any other.

    The returned id is the type the fact should be filed under — either the freshly minted one, or,
    on an exact-name collision, the existing type of that name. The name is never duplicated:
    the UNIQUE type_name is the last-ditch dedup, and a clash resolves to reuse, not a suffixed twin.
    Two clashes are guarded, for two different reasons:
      - the model may deliberately name a type that already exists — caught by the pre-check below,
        which also spares a wasted embedding call for a row we would never insert;
      - two mints may race to coin the same new name at once — caught by ON CONFLICT in the insert,
        so the database, not our timing, lets exactly one win and the loser reuses the winner's row.
    """
    context = ranked[:MINT_CONTEXT]
    reply = llm.generate_json(_mint_prompt(fact_text, context), _mint_reply_model(context))

    # The model named a type that already exists: reuse it rather than mint a twin,
    # and return before embedding — the vector we would compute is for a row we will never write.
    existing = conn.execute(
        "SELECT id FROM schema_ontology WHERE type_name = %s", (reply.type_name,)
    ).fetchone()
    if existing is not None:
        return existing[0]

    # The parent is one of the neighbours the grammar allowed, or none for a root.
    by_name = {r.candidate.type_name: r.candidate.ontology_id for r in context}
    parent_id = None if reply.parent == _MINT_NO_PARENT else by_name[reply.parent]

    # Embed before opening the transaction — this is a network call to Ollama,
    # and a slow round trip must not be held across an open transaction pinning a pooled connection.
    vector = embedding.embed(reply.definition, task="document")
    # pgvector has no psycopg adapter installed, so the vector crosses as its text literal and casts ::vector.
    vector_literal = "[" + ",".join(repr(x) for x in vector) + "]"

    with conn.transaction():
        # ON CONFLICT collapses the concurrent-mint race to a single winner:
        # the loser's insert returns no row, and we reuse the name that won just below.
        row = conn.execute(
            "INSERT INTO schema_ontology (type_name, definition, parent_id) "
            "VALUES (%s, %s, %s) ON CONFLICT (type_name) DO NOTHING RETURNING id",
            (reply.type_name, reply.definition, parent_id),
        ).fetchone()
        if row is None:
            return conn.execute(
                "SELECT id FROM schema_ontology WHERE type_name = %s", (reply.type_name,)
            ).fetchone()[0]
        ontology_id = row[0]
        # Land the vector in the active model's table through the view, so a model swap never has to
        # touch this write and it never names a versioned table — the same stance recall reads under.
        model_id = conn.execute("SELECT id FROM embedding_model WHERE is_active").fetchone()[0]
        conn.execute(
            "INSERT INTO active_ontology_embedding (ontology_id, model_id, embedding) "
            "VALUES (%s, %s, %s::vector)",
            (ontology_id, model_id, vector_literal),
        )
    return ontology_id


class _ConceptsReply(BaseModel):
    """The naming step's answer: the distinct concepts a raw fact expresses.

    A plain, module-level model — its shape never depends on the pool, so nothing is built per call.
    `min_length=1` is folded into the decoder grammar and re-checked on the way back:
    a diary fact is always *about* something, so an empty list is a mis-read, not a valid answer,
    and it fails at the boundary rather than filing a fact under no concept at all."""

    concepts: list[str] = Field(min_length=1)


def _extract_concepts_prompt(fact_text: str) -> str:
    return (
        "You read a personal diary fact and name the distinct concepts it expresses.\n"
        f'Fact: "{fact_text}"\n\n'
        "A single fact is usually several things at once — "
        '"a boxing session with my friend Jeremy during the heat wave" is at once a boxing session, '
        "time spent with a friend, and a spell of extreme heat.\n"
        "Name each distinct *kind of thing* the fact is about, as a short self-contained phrase in "
        'plain words — the kind, not the particular: "time with a friend", not "Jeremy".\n'
        "Name only what the fact genuinely expresses; do not invent concepts it doesn't touch.\n\n"
        'Return JSON only: {"concepts": ["<concept>", ...]}, with at least one.'
    )


def extract_concepts(fact_text: str) -> list[str]:
    """Name the distinct concepts a raw fact expresses — the fan-out point of the whole path.

    One LLM call reads the fact and returns the *kinds of things* it is about, each a short
    self-contained phrase, so that everything downstream can route each concept on its own.
    It names, it does not file: a phrase here is the query the recall pass will embed, never a type.
    Naming the kind and not the particular ("time with a friend", not "Jeremy") is what keeps the
    particulars in the raw text, where the thin synthesis deliberately leaves them.
    """
    return llm.generate_json(_extract_concepts_prompt(fact_text), _ConceptsReply).concepts


class _TemporalReply(BaseModel):
    """When the event a fact describes actually happened, or null when the fact gives no cue.

    A plain module-level model — its shape never depends on the pool, so nothing is built per call.
    `happened_at` is an optional timestamp:
    the model returns a resolved ISO 8601 instant when the fact carries a temporal cue,
    and null when it names no moment at all, in which case the fact's time collapses to `created_at` at read time.
    Pydantic parses the string back into a datetime and raises on a malformed one,
    so a bad date fails loud at the boundary rather than being filed as a quietly wrong instant."""

    happened_at: datetime | None = None


def _temporal_prompt(fact_text: str, reference: datetime) -> str:
    # The reference instant — now, the moment the fact is being recorded —
    # is given ONLY so relative cues ("yesterday", "last Tuesday") can be resolved to concrete instants.
    # It is deliberately not a default:
    # a fact with no time expression must come back null, never fall back to this moment,
    # or the nullable column that lets happened_at collapse to created_at at read time is pointless.
    return (
        "You read a personal diary fact and decide when the event it describes actually happened.\n"
        f'Fact: "{fact_text}"\n\n'
        f"For reference, now is {reference.isoformat()} (UTC). "
        "Use this reference ONLY to resolve a relative time cue in the fact — "
        '"yesterday", "last Tuesday", "this morning" each become a concrete instant relative to it.\n'
        "The reference is NOT a default answer. If the fact contains no time expression at all — no "
        "date, no relative cue, no time of day, it simply states something with no *when* (e.g. "
        '"I live in Strasbourg") — return null. Do not substitute the reference moment for a missing '
        "time, and never invent one.\n"
        "When the fact does give a time, return it as a full ISO 8601 timestamp in UTC; "
        "if it names only a day with no time of day, use 00:00:00 of that day.\n\n"
        'Return JSON only: {"happened_at": "<ISO 8601 UTC timestamp>"} or {"happened_at": null}.'
    )


def extract_happened_at(fact_text: str, *, reference: datetime) -> datetime | None:
    """Read the one time a fact happened out of its raw text, or None when it names no moment.

    Time is the single particular the deliberately-thin write path promotes out of the raw text into structure
    (see the build log):
    one LLM call reads the fact against a reference instant — now, the moment it is being recorded —
    so a relative cue like "yesterday" resolves to a concrete instant, and returns that event time or None.
    None is the honest answer for a fact with no temporal cue:
    the column stays empty and the read path stands `created_at` in for it,
    rather than fabricating a precision the fact never carried.
    It reads the fact whole, once, like the naming step — not per concept —
    because a fact happens at one time whatever kinds it touches.
    """
    reply = llm.generate_json(_temporal_prompt(fact_text, reference), _TemporalReply)
    return reply.happened_at


def route_concept(conn, concept_text: str) -> int:
    """Route one named concept to a type id — reuse an existing one or coin a new one.

    The recall / re-rank / mint-if-new trip for a single concept, run in the fact's own words:
    embed the concept as a query, recall the nearest existing types, re-rank them for true fit,
    and read the top score's band. A clear enough fit reuses that type; nothing fitting mints a new
    one; the grey band in between spends one yes/no call to settle reuse-or-mint rather than guess.
    Returns the id of the type the concept resolved to, whichever way it got there.
    """
    vector = embedding.embed(concept_text, task="query")
    candidates = recall_candidates(conn, vector)
    ranked = rerank_candidates(concept_text, candidates)
    verdict = decide(ranked)
    if verdict == GREY:
        verdict = resolve_grey(concept_text, ranked[0].candidate)
    if verdict == REUSE:
        return ranked[0].candidate.ontology_id
    return mint(conn, concept_text, ranked)


def synthesize(type_names: list[str], raw_text: str) -> dict:
    """Render a routed fact into its thin JSON-LD payload — deliberately, not an LLM step.

    The payload carries exactly two things: the fact's `@type` links to the types it routed to,
    and its own raw text verbatim. Nothing is extracted from the text into structured fields —
    the particulars stay inside `text`, which remains the durable truth (see the build log for why
    the first synthesis is kept this thin, and why richer structure is an open question, not a plan).
    Both values are already in hand by the time we get here — the types from routing, the text from
    the fact itself — so there is nothing for a model to judge, and this is plain assembly, no call.
    The `@type` links are sorted alphabetically: the concepts a fact resolved to are a set, not a
    sequence, so a stable order makes the payload deterministic rather than beholden to the order the
    concepts happened to be named in — the distance- and score-ordered lists upstream keep their order,
    which there is load-bearing; here it is not.
    """
    return {"@type": sorted(type_names), "text": raw_text}


def persist(
    conn,
    raw_text: str,
    payload: dict,
    ontology_ids: list[int],
    *,
    happened_at: datetime | None = None,
    intake_id: int | None = None,
) -> int:
    """Save the routed fact once — the fact row, its embedding, and one link per concept — and return its id.

    The raw text is embedded (as a stored document) before the transaction opens, the same discipline
    the minter keeps: a network round trip to Ollama must not be held across an open transaction
    pinning a pooled connection. Inside one transaction then: the fact and its thin payload land in
    diary_facts, the vector lands in the active model's set through the view (so a model swap never
    touches this write), and one diary_fact_ontology row is written per concept the fact resolved to —
    the many-to-many that lets a single fact be all of its concepts at once. The whole write is atomic:
    a fact is never left half-filed, with an embedding but no links or a payload but no vector.

    happened_at is the fact's event clock (migration 0011):
    when the thing occurred, or None when the fact named no moment —
    stored as-is, null and all, with the read path standing created_at in for a null.
    The other clock, created_at, is filled automatically by the row default, so it is not passed here.

    intake_id is the message this fact was distilled from, or None for a fact filed outside the message flow (a by-hand smoke).
    It is stored under a UNIQUE index, so persisting the same message twice files it once —
    the second call reuses the first's fact rather than duplicating it, which is how live ingestion stays exactly-once.
    """
    vector = embedding.embed(raw_text, task="document")
    # pgvector has no psycopg adapter installed, so the vector crosses as its text literal and casts ::vector.
    vector_literal = "[" + ",".join(repr(x) for x in vector) + "]"
    with conn.transaction():
        # happened_at rides through exactly as given, None becoming a SQL NULL for a fact that named no moment;
        # created_at fills itself from the row default — the telling moment for a live write.
        # intake_id is the message this fact came from (NULL for a by-hand fact),
        # and its UNIQUE index makes filing exactly-once:
        # a re-file of an already-filed message conflicts, writes nothing, and reuses the fact the first run committed —
        # so an interrupted or repeated ingestion sweep never duplicates a fact.
        row = conn.execute(
            "INSERT INTO diary_facts (raw_text, payload, happened_at, intake_id) "
            "VALUES (%s, %s::jsonb, %s, %s) ON CONFLICT (intake_id) DO NOTHING RETURNING id",
            (raw_text, json.dumps(payload), happened_at, intake_id),
        ).fetchone()
        if row is None:
            # Already filed for this intake_id (only a non-NULL id can conflict — NULLs are always distinct):
            # reuse the existing fact and write no second embedding or links.
            return conn.execute(
                "SELECT id FROM diary_facts WHERE intake_id = %s", (intake_id,)
            ).fetchone()[0]
        fact_id = row[0]
        model_id = conn.execute("SELECT id FROM embedding_model WHERE is_active").fetchone()[0]
        conn.execute(
            "INSERT INTO active_diary_fact_embedding (diary_fact_id, model_id, embedding) "
            "VALUES (%s, %s, %s::vector)",
            (fact_id, model_id, vector_literal),
        )
        for ontology_id in ontology_ids:
            conn.execute(
                "INSERT INTO diary_fact_ontology (diary_fact_id, ontology_id) VALUES (%s, %s)",
                (fact_id, ontology_id),
            )
    return fact_id


def ingest(conn, raw_text: str, *, intake_id: int | None = None) -> int:
    """The full write path for one raw diary fact, end to end — returns the filed fact's id.

    Read the event clock first:
    happened_at is pulled off the raw text (None when it names no moment),
    resolved against now — the moment we record it, which the row's own created_at also captures —
    so a relative cue like "yesterday" lands on a concrete instant.
    It is the one temporal particular this thin path promotes into structure.

    Then name the concepts the fact expresses, route each to a type on its own, synthesize the thin
    payload, and persist the fact once. The routed ids are de-duplicated first-seen-first: two concepts
    can resolve to the same type (a fact "with a friend" naming both companionship and the friendship),
    and a fact is linked to a concept once, not once per phrase that led there — the join table's
    composite key would reject the second link anyway, so we collapse it here rather than trip it.
    The payload's `@type` names are read back from the store by id, so a freshly minted type carries
    the name the store actually holds, and both the payload and the link rows are ordered alphabetically
    by that name — the concepts are a set, so a stable order beats the order they happened to be named in.

    intake_id, when given, is the message this fact was distilled from —
    passed straight to persist, whose UNIQUE index keeps live ingestion exactly-once;
    a by-hand call omits it and files an unlinked fact.
    """
    happened_at = extract_happened_at(raw_text, reference=datetime.now(timezone.utc))
    routed = [route_concept(conn, concept) for concept in extract_concepts(raw_text)]
    ontology_ids = list(dict.fromkeys(routed))
    rows = conn.execute(
        "SELECT id, type_name FROM schema_ontology WHERE id = ANY(%s)", (ontology_ids,)
    ).fetchall()
    name_by_id = {r[0]: r[1] for r in rows}
    ontology_ids.sort(key=lambda ontology_id: name_by_id[ontology_id])
    payload = synthesize([name_by_id[o] for o in ontology_ids], raw_text)
    return persist(conn, raw_text, payload, ontology_ids, happened_at=happened_at, intake_id=intake_id)
