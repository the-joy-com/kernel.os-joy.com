"""Reply: composing the machine symbiot's answer to a message — the speaking half of Tier 1.

The librarian (retrieval.py) gathers the long-term facts that bear on what was said,
and the conversation module (conversation.py) gathers the short-term thread the message sits inside;
this module speaks.
It folds four things into one prompt — the machine symbiot's persona (its voice),
the diary facts (long-term memory), the recent conversation (short-term memory, the Gist then the verbatim tail),
and the message itself — and hands them to the free-text model path (llm.generate) for a reply in that voice.

Two things in that prompt are **sacred** and are never shrunk:
the **persona** (who is speaking) and the **live input** (the human symbiot's words this turn, or the agent's own line).
The little instruction sentences that tell the model how to behave are sacred too —
tiny, and shrinking them would be self-defeating.
Everything the reply *remembers* — the diary facts and the whole conversation —
is assembled into **one compressible context block**,
and that block is what the budget guard may squeeze if the prompt would overrun the model's window (see llm._fit).
So a reply never fails to compose for want of room:
it degrades by condensing what it remembers, never by dropping who it is, how to answer, or what was just said.
In the common case the prompt is far under budget and nothing is squeezed at all — the guard is a backstop, not a routine step.

It composes over whatever was found, including nothing:
an empty diary and no conversation yet — a fresh symbiot, a live store not yet fed by ingestion —
yields a reply drawn from the persona and the message alone,
the honest answer when there is nothing on record and nothing yet said to lean on.
"""

from datetime import datetime

from core import config
from services.adapters import llm, models
from services.loop import persona, zone
from services.memory import conversation, retrieval

# What the diary block reads when the librarian found nothing —
# so the prompt always has a coherent line where the memories go, never a blank the model must puzzle over.
_NO_FACTS = "(nothing on record that bears on this)"


# The members below are ordered alphabetically, as far as the code allows:
# the private helpers first in alphabetical order, then compose —
# none of the helpers call each other, so define-before-use imposes no further constraint.
def _compose_prompt(message: str, memory_block: str, voice: str, time_line: str | None) -> str:
    # voice first (the persona sets who is speaking), then the framing instructions,
    # then the symbiot's local time (when known),
    # then the one compressible block of everything remembered (diary + Gist + verbatim tail),
    # then the live message, then the closing instruction.
    # Persona, instructions, time, and message bracket the memory block and are never condensed;
    # the memory block is the only region the budget guard may touch.
    # The time line is a small sacred fact like the persona — tiny, and shrinking it would be self-defeating —
    # so it sits outside the compressible region,
    # right after the framing, where the model reads it before the memory.
    now = f"{time_line}\n\n" if time_line else ""
    return (
        f"{voice}\n\n"
        "You are answering the human symbiot you live in symbiosis with. "
        "Below is what you know that may bear on this and the conversation you are already in — "
        "first your diary in time order, oldest entry first, then the earlier conversation summarised, then the most recent turns word-for-word. "
        "Draw on them where they help; say nothing they don't support, and never invent a memory that isn't there; "
        "use the recent turns to keep continuity, so a pronoun or a brief follow-up resolves against what was just said.\n\n"
        f"{now}"
        f"{memory_block}\n\n"
        f'The human symbiot just said:\n"{message}"\n\n'
        "Reply in your own voice — directly, as yourself, not as an assistant describing what it found."
    )


def _render(facts: list[retrieval.Fact], zone_name: str) -> str:
    """The gathered facts as a plain block for the prompt — one dated line each, oldest first.

    The librarian picks the facts by relevance, but they are rendered here in time order, oldest to newest,
    so the block reads as the timeline it is and the model never mistakes relevance rank for recency:
    a model reads a list top-to-bottom as a sequence whatever it is told, so the order is made to be the true one
    rather than captioned as something to reason around. Which facts appear is still relevance's call; only their order is time's.
    Each line leads with the fact's effective date read in the symbiot's local zone,
    so the model can reason about when things happened, then the fact's own words verbatim.
    The date is localised, not taken straight off the UTC column,
    so it lands on the same calendar as the "now" the prompt states —
    without that a fact from late evening reads as the next day, and the model compares two clocks that don't agree.
    An empty list renders the single honest line saying nothing was found."""
    if not facts:
        return _NO_FACTS
    ordered = sorted(facts, key=lambda f: f.effective_at)
    return "\n".join(f"- [{zone.local_date(f.effective_at, zone_name).isoformat()}] {f.raw_text}" for f in ordered)


def _render_memory(facts_block: str, gist_block: str, tail_block: str) -> str:
    """The one compressible context block: the diary, then the Gist, then the verbatim tail.

    Everything the reply remembers, assembled into a single contiguous region —
    the past before the present, so the model reads the diary and the summarised backstory and then walks into the live exchange.
    This block, and only this block, is what the budget guard may condense on an overrun;
    the persona, the instructions, and the current message sit outside it and are never touched."""
    return (
        f"Your diary — entries that may bear on what they just said:\n{facts_block}\n\n"
        f"The conversation so far, summarised:\n{gist_block}\n\n"
        f"The most recent turns, verbatim:\n{tail_block}"
    )


def _render_tail(tail: list[conversation.Turn], zone_name: str) -> str:
    """The verbatim tail as a plain block — one stamped, role-tagged line per turn, oldest first.

    Each turn leads with the local time it was said and who spoke,
    so the exchange reads top-to-bottom as it happened
    and the model can order things said the same day rather than guessing from how they read.
    An empty tail renders the single honest line saying the conversation has only just begun."""
    if not tail:
        return conversation._NO_TAIL
    return "\n".join(
        f"[{conversation._stamp(t.created_at, zone_name)}] {conversation._speaker(t.role)}: {t.text}" for t in tail
    )


def compose(
    message: str,
    context: list[retrieval.Fact],
    conv: conversation.Conversation,
    *,
    now_local: datetime | None = None,
    zone_name: str | None = None,
) -> str:
    """Compose the reply to `message`, drawing on the long-term facts in `context` and the short-term thread in `conv`.

    Loads the persona, assembles everything the reply remembers — the diary facts, the Gist, and the verbatim tail —
    into one compressible context block, and calls the free-text model path for the answer.
    That memory block is the sole region the budget guard may condense if the prompt would overrun the model's window (llm._fit):
    the persona, the framing instructions, and the current message bracket it and are never shrunk.
    So the reply degrades gracefully — it condenses what it remembers, never who it is or what was just said —
    and never fails to compose for want of room; in the common case the prompt is far under budget and nothing is condensed.
    With memory on hand or none, the reply comes back in the machine symbiot's own voice —
    read off the diary and the running conversation rather than the placeholder stand-in.

    now_local and zone_name are the symbiot's local time and its IANA zone (services/zone.py),
    gathered on the worker's thread before this composes:
    when present, the prompt states the human's current local time,
    so the reply reasons about time in their day rather than the server's UTC.
    Both absent (an anon stand-in never reaches here, a by-hand call that names no clock)
    simply omits the time line rather than asserting a wrong one.
    """
    voice = persona.load()
    # Render fact dates in the symbiot's local zone so they share the calendar the time line states;
    # a by-hand or anon call that names no zone falls back to UTC, the old server-clock behaviour made explicit.
    facts_block = _render(context, zone_name or zone.DEFAULT_ZONE)
    gist_block = conv.gist if conv.gist else conversation._NO_GIST
    tail_block = _render_tail(conv.tail, zone_name or zone.DEFAULT_ZONE)
    memory_block = _render_memory(facts_block, gist_block, tail_block)
    time_line = zone.render_now(now_local, zone_name) if now_local is not None and zone_name else None
    prompt = _compose_prompt(message, memory_block, voice, time_line)
    # The whole remembered block is the compressible region — diary and conversation alike —
    # so an overrun condenses what is remembered, never the persona, the instructions, or the live message bracketing it.
    # The reply's model is resolved from the store by role (models.role_name), not read from a config constant,
    # so an operator's reassignment through /models takes effect here — resolved in the parent and, when this
    # runs in a spawned child, from the store the child loads for itself.
    return llm.generate(prompt, model=models.role_name("reply"), context=memory_block)
