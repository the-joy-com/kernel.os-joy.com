"""Tool calling: how the symbiot decides to act, and how code carries the act out.

Everything the read path does is speech.
This is the one seam where the loop stops talking and *acts* —
and the single invariant the whole thing holds is: the model decides and describes; code does.
The full design is doc/tool-calling.md;
this module is its machinery, and it is deliberately general —
the registry holds one tool today (the reminder, services/reminder.py),
and the second is a new entry, not a rewrite.

A tool is four things joined by its name:
a name (what the model emits when it chooses it),
a description (the prose recall matches and the decision reads),
an argument schema (a Pydantic model, the decoder's grammar and the reply's validation),
and an executor (the Python callable that carries out the effect).
The first three — the descriptor — live in the store as a searchable row with an embedding (migration 0017);
the executor is code, in REGISTRY, keyed by name.
The store is the index you search, the code registry is the dispatch table you land on,
the name is the join —
which is what makes "code executes, never the model" structural:
the model can only ever produce a name, and a name resolves to a callable we wrote.

The flow is retrieve, decide, act, speak
(see worker._answer, which sequences it across the fork):
search the catalog and let that search be the gate —
nothing near enough, and the message is ordinary;
when a candidate surfaces, one decision call names a tool and emits its arguments, or answers "none";
a named tool's executor runs in code, exactly once;
and a second call composes the confirmation in the voice.
This module owns the retrieve (search_catalog), the decide (decide), the act's dispatch (execute),
and the speak (compose_confirmation);
the executor of each tool lives with that tool.

The boundary is the kernel's own structured-output one (llm.generate_json),
never a provider's native function-calling API —
the same stance the ontology router keeps,
so the internals stay provider-independent
rather than hostage to an API surface that churns.
"""

from dataclasses import dataclass
from typing import Callable, Literal

from pydantic import BaseModel, Field, create_model

from core import config
from services.adapters import embedding
from services.adapters import llm
from services.adapters import models
from services.loop import persona
from services.memory import conversation

# The `tool` value the decision returns when a candidate surfaced but nothing truly fit —
# the precise "no" that corrects the coarse recall, and the always-legal choice in the decision schema.
# A NO_TOOL verdict hands the message back to the ordinary reply (worker._answer).
NO_TOOL = "none"


@dataclass(frozen=True)
class ToolResult:
    """What an executor hands back for the confirmation to speak — the facts, never the voice.

    effected is whether the effect actually happened:
    true when the tool did its work,
    false when it could not and the human must be asked for more —
    the reactive-ambiguity law, ask rather than guess,
    so the confirmation asks instead of confirming.
    summary is the facts the confirmation speaks — what was done, or what is missing —
    in plain words the composing call renders in the persona's voice;
    the model never re-invents what the executor decided."""

    effected: bool
    summary: str


@dataclass(frozen=True)
class Tool:
    """One tool: a name, the prose that describes it, its argument schema, and the code that runs it.

    args_model is a Pydantic model whose fields are the tool's arguments, each nullable —
    the decoder is bound to it and the reply validated against it,
    and a field left null is how the model says "I couldn't fill this",
    which the executor reads to decide whether it can act or must ask.
    executor is the callable that carries out the effect,
    signature (conn, symbiot_id, intake_id, args, now_local, zone_name) -> ToolResult —
    run on the worker's own thread, in its own transaction,
    never in the killable child (see worker._execute_tool)."""

    name: str
    description: str
    args_model: type[BaseModel]
    executor: Callable[..., ToolResult]


@dataclass(frozen=True)
class ToolCandidate:
    """One tool the catalog search surfaced for a message, and how near it fell.

    distance is the cosine distance from the message to the tool's descriptor
    when recall reached it that way,
    and None when it came in through the lexical match instead —
    it orders the shortlist, never decides fit,
    which is the decision call's job,
    the same two-stage shape the ontology re-ranker keeps."""

    name: str
    description: str
    distance: float | None


@dataclass(frozen=True)
class Decision:
    """The decision call's verdict: which tool to run, and the arguments it extracted.

    tool is a shortlisted tool's name, or "none" when a candidate surfaced but nothing truly fit.
    args is that tool's arguments as a plain dict of primitives (empty for "none") —
    plain so it crosses the reply's process boundary (the killable child) cleanly,
    re-validated through the tool's own args_model before the executor sees it (execute).
    """

    tool: str
    args: dict


REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> None:
    """Add a tool to the code registry — the source of truth for which tools exist.

    Called at import by each tool module (services/reminder.py),
    so the registry is assembled by importing the tools.
    The catalog in the store is derived from this, never the other way round (reconcile_catalog)."""
    REGISTRY[tool.name] = tool


def reconcile_catalog(conn) -> None:
    """Bring the store's catalog in line with the code registry — the once-at-startup sync.

    The code registry is the source of truth for which tools exist;
    the catalog is derived from it.
    For each registered tool the descriptor row is upserted by name,
    and its embedding is (re)built when the tool is new, when its description changed,
    or when the active set holds no vector for it —
    so an unchanged catalog costs no embedding calls on a boot,
    while a model swap
    (which repoints the active view at a fresh, empty set)
    refills itself on the next reconcile,
    with no hand-written backfill of the kind the ontology and diary sets need.
    A catalog row whose name is no longer registered is dropped (its embedding cascades),
    so a removed tool leaves nothing behind for recall to still offer.
    Idempotent, so startup can always call it, and so can a hot reload.
    """
    for tool in REGISTRY.values():
        row = conn.execute(
            "SELECT id, description FROM tool_catalog WHERE name = %s", (tool.name,)
        ).fetchone()
        if row is None:
            tool_id = conn.execute(
                "INSERT INTO tool_catalog (name, description) VALUES (%s, %s) RETURNING id",
                (tool.name, tool.description),
            ).fetchone()[0]
            _embed_descriptor(conn, tool_id, tool.description)
            continue
        tool_id, stored_description = row
        if stored_description != tool.description:
            conn.execute(
                "UPDATE tool_catalog SET description = %s WHERE id = %s", (tool.description, tool_id)
            )
        # (Re)embed on a changed description,
        # or when the active set carries no vector for this tool.
        # The second case is what makes a model swap automatic:
        # repoint active_tool_embedding at the new model's empty table,
        # and the next reconcile fills it,
        # rather than leaving the catalog unsearchable until a description happens to change.
        has_vector = conn.execute(
            "SELECT 1 FROM active_tool_embedding WHERE tool_id = %s", (tool_id,)
        ).fetchone()
        if stored_description != tool.description or has_vector is None:
            _embed_descriptor(conn, tool_id, tool.description)
    # Drop catalog rows for tools the code no longer carries —
    # the registry is the source of truth,
    # so a name absent from it should be absent from the store too
    # (the embedding cascades on delete).
    names = list(REGISTRY.keys())
    conn.execute("DELETE FROM tool_catalog WHERE NOT (name = ANY(%s))", (names,))


def _embed_descriptor(conn, tool_id: int, description: str) -> None:
    """Embed a tool's description and land the vector in the active model's set, replacing any prior one.

    The document-side embedding of the descriptor, keyed back to the catalog row,
    written through the active view —
    so a model swap never touches this write and it never names a versioned table,
    the same stance the ontology minter keeps.
    Delete-then-insert rather than an upsert, because the write goes through a view:
    a re-embed (a changed description) replaces the old vector cleanly,
    and a first embed simply inserts.
    """
    vector = embedding.embed(description, task="document")
    # pgvector has no psycopg adapter installed, so the vector crosses as its text literal and casts ::vector.
    vector_literal = "[" + ",".join(repr(x) for x in vector) + "]"
    model_id = conn.execute("SELECT id FROM embedding_model WHERE is_active").fetchone()[0]
    conn.execute("DELETE FROM active_tool_embedding WHERE tool_id = %s", (tool_id,))
    conn.execute(
        "INSERT INTO active_tool_embedding (tool_id, model_id, embedding) VALUES (%s, %s, %s::vector)",
        (tool_id, model_id, vector_literal),
    )


def search_catalog(conn, message: str) -> list[ToolCandidate]:
    """The retrieve step, and the gate: the tools a message might be reaching for, or an empty list.

    Coarse recall by design —
    its job is to not miss a candidate, not to be sure,
    the precise judgment being the decision call's.
    A tool is a candidate if its descriptor is near the message by vector,
    or its description matches the message lexically —
    text and vector both,
    so an obvious "remind me" is caught even when the distance is loose.
    An empty list is the gate closed:
    the message asks for no tool and takes the ordinary reply path untouched,
    which is almost every message.

    An empty catalog short-circuits before embedding anything —
    there is nothing to match, so no local embed call is spent,
    which is also what keeps the gate inert (and cheap) wherever no tools are reconciled in.
    ef_search is opened per query like the other recalls,
    and reverts at transaction end rather than leaking onto the pool.
    """
    if conn.execute("SELECT count(*) FROM tool_catalog").fetchone()[0] == 0:
        return []
    vector = embedding.embed(message, task="query")
    # pgvector has no psycopg adapter installed, so the vector crosses as its text literal and casts ::vector.
    vector_literal = "[" + ",".join(repr(x) for x in vector) + "]"
    with conn.transaction():
        conn.execute(
            "SELECT set_config('hnsw.ef_search', %s, true)",
            (str(config.TOOL_RECALL_EF_SEARCH),),
        )
        rows = conn.execute(
            """
            SELECT tc.name, tc.description, e.embedding <=> %(q)s::vector AS distance
            FROM active_tool_embedding e
            JOIN tool_catalog tc ON tc.id = e.tool_id
            WHERE (e.embedding <=> %(q)s::vector) <= %(maxd)s
               OR to_tsvector('english', tc.description) @@ websearch_to_tsquery('english', %(msg)s)
            ORDER BY e.embedding <=> %(q)s::vector
            LIMIT %(limit)s
            """,
            {
                "q": vector_literal,
                "maxd": config.TOOL_RECALL_MAX_DISTANCE,
                "msg": message,
                "limit": config.TOOL_RECALL_LIMIT,
            },
        ).fetchall()
    return [ToolCandidate(r[0], r[1], r[2]) for r in rows]


def decide(
    message: str,
    candidates: list[ToolCandidate],
    tail: list[conversation.Turn],
    now_local,
    zone_name: str,
) -> Decision:
    """The decide step: name a tool and extract its arguments, or answer "none".

    One structured call, memory-light on purpose —
    it sees the shortlist and the recent conversation tail, not the full diary —
    so it stays cheap on every message that merely sits near a tool.
    The tail is there because arguments refer back
    ("remind me about *that* at six" resolves only against what was just said),
    and the local now is there because a time argument is resolved against it.
    The reply is a flat schema (see _decision_model):
    a `tool` field naming a shortlisted tool or "none",
    plus every shortlisted tool's arguments as nullable fields.
    "none" is the precise judgment correcting the coarse recall —
    it hands back to the ordinary reply, which has the full memory to answer well,
    so this call is never asked to compose the reply itself.
    """
    reply = llm.generate_json(
        _decide_prompt(message, candidates, tail, now_local, zone_name),
        _decision_model(candidates),
        model=models.role_name("tool_decision"),
    )
    if reply.tool == NO_TOOL:
        return Decision(NO_TOOL, {})
    # Pull just the named tool's own argument fields out of the flat reply —
    # the other tools' fields, if any were folded in,
    # are not this tool's business and are left behind.
    args = {name: getattr(reply, name) for name in REGISTRY[reply.tool].args_model.model_fields}
    return Decision(reply.tool, args)


def execute(conn, decision: Decision, symbiot_id: int, intake_id: int, now_local, zone_name: str) -> ToolResult:
    """The act step: run the named tool's executor, exactly once, and return what it did for the voice to speak.

    Dispatches on the name to the callable in the registry —
    code we wrote, never anything the model emitted —
    re-validating the extracted arguments through the tool's own args_model on the way in,
    so the executor only ever sees a checked object.
    The executor carries out the effect and guards its own exactly-once against intake_id
    (see the reminder).
    Runs inside the transaction worker._execute_tool opened on the worker's thread,
    never in the killable child,
    so a severed child can never leave a half-done effect.
    """
    tool = REGISTRY[decision.tool]
    args = tool.args_model(**decision.args)
    return tool.executor(conn, symbiot_id, intake_id, args, now_local, zone_name)


def compose_confirmation(message: str, result: ToolResult, now_local, zone_name: str) -> str:
    """The speak step: say back what the tool did, or ask for what it needs, in the symbiot's own voice.

    The facts come from the executor's result, the voice from the persona;
    the model never re-invents what the tool decided.
    When the tool acted (result.effected) the confirmation confirms it;
    when it could not, the confirmation asks the human for what was missing
    rather than pretending anything was done.
    A free-text call like the reply, so no schema is imposed on prose (llm.generate).
    """
    voice = persona.load()
    return llm.generate(_confirm_prompt(message, result, voice, now_local, zone_name), model=models.role_name("tool_confirm"))


def _decision_model(candidates: list[ToolCandidate]) -> type[BaseModel]:
    """Build — at runtime — the flat Pydantic model the decision reply must match for *this* shortlist.

    Like the ontology re-ranker's reply model, the legal set isn't known until the shortlist is in hand,
    so each call constructs a fresh model whose `tool` field is a Literal over exactly the shortlisted names,
    plus the always-legal "none" —
    the model can't name a tool that wasn't offered, and it always has a way to decline.
    Every shortlisted tool's argument fields are folded in flat,
    each made nullable with a null default:
    flat rather than a root-level union so all three strict decoders handle it,
    and nullable so the model can name a tool yet leave an argument it couldn't read null,
    which the executor reads as "ask".
    """
    names = tuple(c.name for c in candidates) + (NO_TOOL,)
    fields: dict = {"tool": (Literal[names], ...)}
    for candidate in candidates:
        for name, info in REGISTRY[candidate.name].args_model.model_fields.items():
            # Nullable with a null default:
            # the field may be absent from the reply,
            # or present-but-null when the model couldn't fill it.
            # The annotation is already nullable on the tool's own args_model,
            # so `| None` here is belt-and-braces and keeps the default explicit.
            # The tool's own per-field description is carried across, not dropped:
            # it is where a field says *when* to leave it null
            # (the channels arg, say, must stay null unless a channel is explicitly named),
            # and that guidance only reaches the decoder
            # if it survives the fold into this flat schema.
            fields[name] = (info.annotation | None, Field(default=None, description=info.description))
    return create_model("_ToolDecision", **fields)


def _decide_prompt(
    message: str,
    candidates: list[ToolCandidate],
    tail: list[conversation.Turn],
    now_local,
    zone_name: str,
) -> str:
    # The shortlist by name and description,
    # so the model judges the tool by what it does, not its label;
    # the recent tail, so an argument that refers back resolves;
    # the local now, so a time resolves against it.
    tools_block = "\n".join(f"- {c.name} — {c.description}" for c in candidates)
    tail_block = (
        "\n".join(f"{conversation._speaker(t.role)}: {t.text}" for t in tail)
        if tail
        else "(nothing said yet)"
    )
    return (
        "You decide whether the human symbiot's message is asking you to use one of your tools, "
        "and if so, which one and with what arguments.\n\n"
        "For reference, the human symbiot's local date and time right now is "
        f"{now_local.strftime('%Y-%m-%d %H:%M')} ({zone_name}). "
        "Resolve any time in the message against this — "
        'a relative one ("in 20 minutes", "tomorrow at 9") '
        'and an absolute one ("on the 14th at noon") '
        "both become a concrete local date and time. "
        "Give it as the human's own wall-clock reading (for example 2026-07-14 20:05); "
        "do not convert it to UTC, and do not attach a timezone offset.\n\n"
        f"Your tools (name — what it does):\n{tools_block}\n\n"
        f"The recent conversation, so an argument that refers back resolves:\n{tail_block}\n\n"
        f'The human symbiot just said:\n"{message}"\n\n'
        "Almost every message asks for no tool at all — it is talk, not a request to act — "
        f'so "{NO_TOOL}" is the expected answer, '
        "and you reach for a tool only when the message plainly asks you to do that thing. "
        "A tool fits when the human is asking you to act; "
        "it does not fit when they are telling you something, thinking aloud, or naming a plan — "
        "a message that merely mentions a future task or event "
        '("I need to call the dentist tomorrow", "the meeting is at 3") '
        "is not by itself a request to act on it. "
        "Set `tool` to a tool's name only on a clear, explicit request for it, "
        "and fill that tool's argument fields; "
        f'otherwise set `tool` to "{NO_TOOL}" and leave the arguments null. '
        "Fill an argument only when the message gives it clearly; "
        "if you cannot read one with confidence — a time you are unsure of, say — "
        "leave it null rather than guessing, so the human can be asked. "
        "For a delivery-channel argument, "
        "read the channel straight from the request when the human names one "
        '("by email" is email, "push me" is web push), '
        "and leave it null when they name none — "
        "a null there means the default of reaching them on every channel, "
        "so never invent one they didn't mention."
    )


def _confirm_prompt(message: str, result: ToolResult, voice: str, now_local, zone_name: str) -> str:
    # voice first (who is speaking), then what happened (the tool's own result), then the instruction —
    # confirm when it acted, ask when it could not,
    # always in the persona's voice and never inventing facts.
    if result.effected:
        instruction = (
            "You have just done this for them. Confirm it back in your own voice — briefly and directly, "
            "as yourself — speaking only what the result says you did, inventing nothing."
        )
    else:
        instruction = (
            "You could not do it yet — you need more from them. "
            "In your own voice, ask for exactly what the result says is missing, "
            "briefly and directly, without pretending anything was done."
        )
    return (
        f"{voice}\n\n"
        f"For reference, the symbiot's local date and time right now is {now_local.isoformat()} ({zone_name}).\n\n"
        f'The human symbiot said:\n"{message}"\n\n'
        f"What happened when you acted on it:\n{result.summary}\n\n"
        f"{instruction}"
    )


# Import the tool implementations so they register themselves
# (services/reminder.py calls register at load).
# Placed at the end, after the base types and register() are defined,
# so the tool module can import them back without a half-initialised cycle —
# the standard "register on import" assembly, kept in one place.
from services.tools import reminder  # noqa: E402,F401
