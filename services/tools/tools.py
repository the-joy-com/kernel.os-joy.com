"""Tool calling: how the symbiot decides to act, and how code carries the act out.

Everything the read path does is speech.
This is the one seam where the loop stops talking and *acts* —
and the single invariant the whole thing holds is: the model decides and describes; code does.
The full design is doc/tool-calling.md;
this module is its machinery, and it is deliberately general —
the registry holds four tools today, all of them the reminder's
(scheduling, cancelling, editing and reading it, services/tools/reminder.py),
and each is a new entry rather than a rewrite of this seam, which is the claim the second one tested.

A tool is four things joined by its name, and optionally a fifth:
a name (what the model emits when it chooses it),
a description (the prose recall matches and the decision reads),
an argument schema (a Pydantic model, the decoder's grammar and the reply's validation),
an executor (the Python callable that carries out the effect),
and an observe hook, when the tool needs to know something before it acts.
The first three — the descriptor — live in the store as a searchable row with an embedding (migration 0017);
the executor and the hook are code, in REGISTRY, keyed by name.
The store is the index you search, the code registry is the dispatch table you land on,
the name is the join —
which is what makes "code executes, never the model" structural:
the model can only ever produce a name, and a name resolves to a callable we wrote.

The flow is retrieve, decide, look, act, speak
(see worker._answer, which sequences it across the fork):
search the catalog and let that search be the gate —
nothing near enough, and the message is ordinary;
when a candidate surfaces, one decision call names a tool and emits its arguments, or answers "none";
a tool that declares an observe hook has it read what the machine already holds,
and one small judging call rules on what that read found;
a named tool's executor runs in code, exactly once, handed that verdict;
and a second call composes the confirmation in the voice.
This module owns the retrieve (search_catalog), the decide (decide),
the look (observe, judge_observation), the act's dispatch (execute), and the speak (compose_confirmation);
the executor and the hook of each tool live with that tool.

The look is the step that makes a decision contingent on what the machine *observed*
rather than only on what the symbiot *said* —
whether "remind me to email Sam" is a duplicate, or which reminder "the dentist one" points at,
are facts about what is held, not about the sentence.
It is one step and not a loop: bounded, of known shape, adding at most one model call,
and skipped entirely by a tool that declares no hook.

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

# The members below are ordered alphabetically, as far as the code allows,
# in the four groups the code's own dependencies carve out:
# the plain constants, then the types,
# then the registry and the instruction table, whose annotations name those types,
# then the functions, publics ahead of the private helpers.
# Three pairs keep their dependency order rather than the alphabetical one,
# since an annotation is evaluated where it is written and needs the name it reads to exist already:
# Outcome ahead of ALL_OUTCOMES, ObservedCandidate ahead of Observation (and so of ObservationVerdict),
# ToolResult ahead of Tool.
# The order the steps run in is the flow the module docstring lays out,
# which is what leaves the file free to sort.

# The `tool` value the decision returns when a candidate surfaced but nothing truly fit —
# the precise "no" that corrects the coarse recall, and the always-legal choice in the decision schema.
# A NO_TOOL verdict hands the message back to the ordinary reply (worker._answer).
NO_TOOL = "none"

# How an act can end, as named outcomes rather than a did-it-or-not flag.
# The axis the five turn on is who has to change something for the act to succeed:
# ACTED and SATISFIED need nobody — it happened, or it was already so;
# UNCLEAR needs the symbiot, who has to be asked;
# UNABLE needs something no retry can supply;
# REPORTED is the one that steps outside the axis rather than sitting on it,
# and it is there because a tool can be asked a question rather than asked to act.
# Nothing was asked to change, so nobody had to change anything and nothing happened —
# which is why it cannot borrow ACTED's line ("it happened") without saying something untrue of a read.
# A flag could only ever draw the first distinction,
# so a tool that declined because the thing already stood had to hand back the word for "I need more from you",
# and the confirmation would ask for what it had just been given.
# UNABLE is the same line the wire draws between "no joy" and "unable" (core/protocol.py),
# drawn a second time at the tool seam,
# and it matters most for a tool that reaches a third party:
# an executor's only way to report failure otherwise is to raise, and a raise means retry,
# so a permanently refused act would loop forever against a door that always says no.
# Returned rather than raised, it completes the message and speaks —
# the no-retry behaviour falls out of the control flow already there.
# Sorted, so the vocabulary reads the same wherever it is spelled out.
Outcome = Literal["ACTED", "REPORTED", "SATISFIED", "UNABLE", "UNCLEAR"]
ALL_OUTCOMES: tuple[Outcome, ...] = ("ACTED", "REPORTED", "SATISFIED", "UNABLE", "UNCLEAR")


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


@dataclass(frozen=True)
class ObservedCandidate:
    """One thing an observe hook found, offered to the judge for a verdict.

    ref is the code-side handle the judge may name — a row id, never anything the model invented,
    which is what keeps "the model decides and describes; code does" true one level deeper:
    the judge can only ever pick from refs we produced.
    description is the candidate in plain words, the only thing the judge reads to decide."""

    ref: int
    description: str


@dataclass(frozen=True)
class Observation:
    """What a tool's observe hook saw, and the one question the judge must answer about it.

    The tool phrases its own question, because the same shape serves two quite different asks —
    "is one of these the same intent as what they just asked for?" (the deduplication)
    and "which of these are they pointing at?" (a cancel or an edit by language) —
    and only the tool knows which it means.
    candidates is what was found, already narrowed and ranked by the hook;
    an empty list is the hook saying "nothing to judge", and no judging call is spent on it."""

    question: str
    candidates: list[ObservedCandidate]


@dataclass(frozen=True)
class ObservationVerdict:
    """The judging call's answer about what the hook found: one candidate's ref, or nothing.

    match is the ref of the candidate the judge picked,
    and None is the judge saying none of the candidates is the one.
    A missing verdict means the same thing to the executor as that None:
    either no judging call ran, or the one that ran did not come back (worker._answer),
    and in both cases the executor carries on as though the hook had found nothing,
    because a check that could not run must leave the act exactly as it would have been without the check.
    The ref is always one the hook itself offered:
    the reply schema (_observation_verdict_model) admits only the refs of these candidates,
    so the judge has no way to name a row nobody showed it."""

    match: int | None


@dataclass(frozen=True)
class ToolResult:
    """What an executor hands back for the confirmation to speak — the facts, never the voice.

    outcome is how the act ended, one of the five words above —
    read by the confirmation to know which of its lines to speak (_CONFIRM_INSTRUCTIONS).
    summary is the facts the confirmation speaks — what was done, what already stood,
    what is missing, or why it cannot be carried out —
    in plain words the composing call renders in the persona's voice;
    the model never re-invents what the executor decided."""

    outcome: Outcome
    summary: str


@dataclass(frozen=True)
class Tool:
    """One tool: a name, the prose that describes it, its argument schema, the code that runs it, and what it looks at first.

    args_model is a Pydantic model whose fields are the tool's arguments, each nullable —
    the decoder is bound to it and the reply validated against it,
    and a field left null is how the model says "I couldn't fill this",
    which the executor reads to decide whether it can act or must ask.
    executor is the callable that carries out the effect,
    signature (conn, symbiot_id, intake_id, args, now_local, zone_name, verdict) -> ToolResult —
    run on the worker's own thread, in its own transaction,
    never in the killable child (see worker._execute_tool).
    observe is optional, and it is the step that makes a decision contingent on the world
    rather than only on the sentence:
    a pure read, signature (conn, symbiot_id, args, now_local, zone_name) -> Observation | None,
    run on the worker's own thread before the executor,
    handed the arguments the decision extracted, returning what it found for the judge to rule on.
    A tool without one is single-pass, exactly as every tool was before."""

    name: str
    description: str
    args_model: type[BaseModel]
    executor: Callable[..., ToolResult]
    observe: Callable[..., "Observation | None"] | None = None


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


class _ConfirmationReply(BaseModel):
    """The spoken confirmation: the words to say back, and nothing wrapped around them.

    A plain module-level model — its shape is fixed at a single string, with nothing per-call to fold in.
    The same reason the reply carries one (reply._SpokenReply):
    this is spoken to the symbiot verbatim, so an "Of course! Here's the confirmation:" opener
    or a ``` fence has no field to land in, where in free text it would reach them as the machine's own words.
    `min_length=1` is folded into the decoder grammar and re-checked on the way back:
    an empty confirmation would leave a tool that acted saying nothing about it."""

    confirmation: str = Field(min_length=1)


REGISTRY: dict[str, Tool] = {}

# One instruction for the confirmation to follow, written beside each outcome and looked up by it.
# There is no fallback row on purpose.
# A fallback would take an outcome nobody has written a line for
# and speak it as one of the outcomes that do have a line,
# which is precisely the mistake the flag made:
# no line for an outcome, no reply.
# The lookup fails, loudly, on the first message that produces one,
# rather than saying the wrong thing convincingly
# (test_tools walks ALL_OUTCOMES and asserts every word has an instruction beside it,
# so a word added later can't sit in the code with nothing to say).
_CONFIRM_INSTRUCTIONS: dict[Outcome, str] = {
    "ACTED": (
        "You have just done this for them. Confirm it back in your own voice — briefly and directly, "
        "as yourself — speaking only what the result says you did, inventing nothing."
    ),
    "REPORTED": (
        "They asked you something and the result is your answer to it. "
        "In your own voice, tell them what it says — briefly and naturally, the way somebody would say it aloud, "
        "keeping whatever grouping and order the result already has. "
        "Speak only what the result carries, inventing nothing, "
        "and don't narrate the looking or claim to have done anything: you were asked, and this is the answer."
    ),
    "SATISFIED": (
        "Nothing needed doing — it was already so, and the result says how. "
        "In your own voice, tell them it already stands, briefly and directly, "
        "without pretending you have just done it and without asking them for anything."
    ),
    "UNABLE": (
        "You cannot carry this out, and the result says why — trying again would not change that. "
        "In your own voice, say so plainly and briefly, "
        "without pretending anything was done and without asking them for something that would not help."
    ),
    "UNCLEAR": (
        "You could not do it yet — you need more from them. "
        "In your own voice, ask for exactly what the result says is missing, "
        "briefly and directly, without pretending anything was done."
    ),
}


def compose_confirmation(message: str, result: ToolResult, now_local, zone_name: str) -> str:
    """The speak step: say back what the tool did, or ask for what it needs, in the symbiot's own voice.

    The facts come from the executor's result, the voice from the persona;
    the model never re-invents what the tool decided.
    Which line it speaks — confirming, answering a question, saying it already stood,
    asking for what is missing, or saying plainly that it cannot be done —
    is looked up by the result's outcome (_CONFIRM_INSTRUCTIONS), never branched on.
    Held to _ConfirmationReply on the way back, like the reply it sits beside:
    what comes across is spoken to the symbiot verbatim, so the words are the only thing the model may emit.
    """
    head = persona.head()
    return llm.generate_json(
        _confirm_prompt(message, result, head, now_local, zone_name),
        _ConfirmationReply,
        model=models.role_name("tool_confirm"),
    ).confirmation


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


def execute(
    conn,
    decision: Decision,
    symbiot_id: int,
    intake_id: int,
    now_local,
    zone_name: str,
    verdict: ObservationVerdict | None = None,
) -> ToolResult:
    """The act step: run the named tool's executor, exactly once, and return what it did for the voice to speak.

    Dispatches on the name to the callable in the registry —
    code we wrote, never anything the model emitted —
    re-validating the extracted arguments through the tool's own args_model on the way in,
    so the executor only ever sees a checked object.
    verdict is what the judge made of whatever the tool's observe hook saw,
    and None means there was nothing to see, nothing to judge, or the judging call could not run —
    three cases the executor is meant to treat alike,
    since a check that could not run must leave the act as it would have been without the check.
    The executor carries out the effect and guards its own exactly-once against intake_id
    (see the reminder).
    Runs inside the transaction worker._execute_tool opened on the worker's thread,
    never in the killable child,
    so a severed child can never leave a half-done effect.
    """
    tool = REGISTRY[decision.tool]
    args = tool.args_model(**decision.args)
    return tool.executor(conn, symbiot_id, intake_id, args, now_local, zone_name, verdict)


def judge_observation(message: str, observation: Observation, now_local, zone_name: str) -> ObservationVerdict:
    """The look step's judgment: which of the candidates the hook found is the one, or none of them.

    One small structured call, and the narrowest one the seam makes —
    it sees the symbiot's message, the hook's own question, and the candidates as plain lines,
    and answers with a ref or a null.
    Never the whole store: the hook has already windowed, ranked and capped what it hands over,
    which is what keeps this cheap enough to spend on every message that reaches a tool with a hook,
    and cheap on a small local model too.
    The reply schema is built from exactly the refs offered (_observation_verdict_model),
    so a ref the judge invents is not a possible answer rather than a mistake to catch afterwards.
    """
    reply = llm.generate_json(
        _judge_observation_prompt(message, observation, now_local, zone_name),
        _observation_verdict_model(observation),
        model=models.role_name("tool_observation_judge"),
    )
    # The ref crosses the boundary as text and comes back as the int the hook produced (see _observation_verdict_model).
    return ObservationVerdict(None if reply.match is None else int(reply.match))


def observe(conn, decision: Decision, symbiot_id: int, now_local, zone_name: str) -> Observation | None:
    """The look step: run the named tool's observe hook, if it has one, and return what it saw.

    A pure read, dispatched on the name like the executor, and run on the worker's own thread —
    never in the killable child, and never inside the executor's open transaction,
    since a provider round trip inside an open transaction is how a slow API becomes a stuck database.
    A tool with no hook returns None and the flow is single-pass, exactly as it was.
    The arguments are re-validated through the tool's own args_model first,
    so the hook, like the executor, only ever sees a checked object.
    """
    tool = REGISTRY[decision.tool]
    if tool.observe is None:
        return None
    args = tool.args_model(**decision.args)
    return tool.observe(conn, symbiot_id, args, now_local, zone_name)


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


def register(tool: Tool) -> None:
    """Add a tool to the code registry — the source of truth for which tools exist.

    Called at import by each tool module (services/reminder.py),
    so the registry is assembled by importing the tools.
    The catalog in the store is derived from this, never the other way round (reconcile_catalog)."""
    REGISTRY[tool.name] = tool


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


def _confirm_prompt(message: str, result: ToolResult, head: str, now_local, zone_name: str) -> str:
    # head first (the truth-rules preamble, then the persona that says who is speaking), then what happened
    # (the tool's own result), then the instruction the outcome names —
    # always in the persona's voice and never inventing facts.
    # The head is the same fixed, cacheable prefix the reply and the enrichment lead with (persona.head).
    # The lookup is deliberately unguarded: an outcome with no line written for it raises here,
    # rather than falling back to a line meant for a different ending.
    instruction = _CONFIRM_INSTRUCTIONS[result.outcome]
    return (
        f"{head}\n\n"
        f"For reference, the symbiot's local date and time right now is {now_local.isoformat()} ({zone_name}).\n\n"
        f'The human symbiot said:\n"{message}"\n\n'
        f"What happened when you acted on it:\n{result.summary}\n\n"
        f"{instruction}\n\n"
        "Put what you say in the `confirmation` field — those words only, "
        "with no preamble, heading, or code fence around them."
    )


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


def _judge_observation_prompt(message: str, observation: Observation, now_local, zone_name: str) -> str:
    # The symbiot's own words, then the candidates the hook found as numbered plain lines,
    # then the hook's question and the one shape of answer allowed.
    # Deliberately spare: no persona, no diary, no conversation tail —
    # this call makes one narrow judgment about a handful of lines, and nothing else helps it.
    # The local now is there because every candidate carries a time,
    # and "the same one" often turns on whether two times are the same afternoon.
    candidates_block = "\n".join(f"- [{c.ref}] {c.description}" for c in observation.candidates)
    refs = ", ".join(str(c.ref) for c in observation.candidates)
    return (
        "You are looking at what the human symbiot already has on record, "
        "so that acting on their latest message can take it into account.\n\n"
        "For reference, the human symbiot's local date and time right now is "
        f"{now_local.strftime('%Y-%m-%d %H:%M')} ({zone_name}).\n\n"
        f'The human symbiot just said:\n"{message}"\n\n'
        f"What is already on record (each line begins with its number):\n{candidates_block}\n\n"
        f"{observation.question}\n\n"
        f"Answer with the number of the one you mean ({refs}), "
        "or null if none of them is the one. "
        "Only ever answer with a number from that list — never a number that isn't on it. "
        "Be exact rather than generous: two records that merely touch the same subject are not the same one, "
        "and answering null is the right answer whenever you are not confident."
    )


def _observation_verdict_model(observation: Observation) -> type[BaseModel]:
    """Build — at runtime — the reply model the judging call must match for *these* candidates.

    The same shape and the same reason as _decision_model:
    the legal set isn't known until the candidates are in hand,
    so each call constructs a fresh model whose `match` field is a Literal over exactly the refs offered,
    plus null — which is what makes "the judge can only name a row we showed it" structural.
    Null is always legal, so the judge always has a way to say "none of these" —
    the same always-available decline the decision call's "none" is.

    The refs are spelled as *text* in the schema even though they are row ids,
    because one of the strict decoders behind llm.generate_json takes only string literals in an enum
    and a numeric one is rejected outright.
    That constraint is met at our own boundary rather than let into the internals:
    the hook deals in ints, judge_observation() reads the text back to an int, and only the schema knows the difference —
    the same stance every provider quirk is kept behind.
    """
    refs = tuple(str(c.ref) for c in observation.candidates)
    return create_model(
        "_ObservationVerdict",
        match=(
            Literal[refs] | None,
            Field(default=None, description="the number of the record you mean, or null if none of them is the one"),
        ),
    )


# Import the tool implementations so they register themselves
# (services/reminder.py calls register at load).
# Placed at the end, after the base types and register() are defined,
# so the tool module can import them back without a half-initialised cycle —
# the standard "register on import" assembly, kept in one place.
from services.tools import reminder  # noqa: E402,F401
