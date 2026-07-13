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
from services.memory import conversation
from services.adapters import llm
from services.loop import persona
from services.memory import retrieval

# What the diary block reads when the librarian found nothing —
# so the prompt always has a coherent line where the memories go, never a blank the model must puzzle over.
_NO_FACTS = "(nothing on record that bears on this)"


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
        "first your diary, then the earlier conversation summarised, then the most recent turns word-for-word. "
        "Draw on them where they help; say nothing they don't support, and never invent a memory that isn't there; "
        "use the recent turns to keep continuity, so a pronoun or a brief follow-up resolves against what was just said.\n\n"
        f"{now}"
        f"{memory_block}\n\n"
        f'The human symbiot just said:\n"{message}"\n\n'
        "Reply in your own voice — directly, as yourself, not as an assistant describing what it found."
    )


def _render_now(now_local: datetime, zone_name: str) -> str:
    """The one-line local-time reference the reply reasons about time against — the human's clock, not the server's.

    States the symbiot's current local date and time and its zone,
    so a mention of the hour, a "this evening", or a "how long ago" is read in the human's day rather than UTC —
    which is what the reply spoke in when it had no clock at all.
    Kept to a single sentence, and held outside the compressible memory block,
    so it is never squeezed away on an overrun."""
    stamp = now_local.strftime("%A %d %B %Y, %H:%M")
    return (
        f"For reference, the human symbiot's local date and time right now is {stamp} ({zone_name}). "
        "Reason about any mention of time — today, tonight, how long ago — in their local time, not UTC."
    )


def _render(facts: list[retrieval.Fact]) -> str:
    """The gathered facts as a plain block for the prompt — one dated line each, most relevant first.

    Each line leads with the fact's effective date, so the model can reason about when things happened,
    then the fact's own words verbatim.
    An empty list renders the single honest line saying nothing was found."""
    if not facts:
        return _NO_FACTS
    return "\n".join(f"- [{f.effective_at.date().isoformat()}] {f.raw_text}" for f in facts)


def _render_tail(tail: list[conversation.Turn]) -> str:
    """The verbatim tail as a plain block — one role-tagged line per turn, oldest first.

    Each turn is tagged with who spoke, so the exchange reads top-to-bottom as it happened.
    An empty tail renders the single honest line saying the conversation has only just begun."""
    if not tail:
        return conversation._NO_TAIL
    return "\n".join(f"{conversation._speaker(t.role)}: {t.text}" for t in tail)


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
    facts_block = _render(context)
    gist_block = conv.gist if conv.gist else conversation._NO_GIST
    tail_block = _render_tail(conv.tail)
    memory_block = _render_memory(facts_block, gist_block, tail_block)
    time_line = _render_now(now_local, zone_name) if now_local is not None and zone_name else None
    prompt = _compose_prompt(message, memory_block, voice, time_line)
    # The whole remembered block is the compressible region — diary and conversation alike —
    # so an overrun condenses what is remembered, never the persona, the instructions, or the live message bracketing it.
    return llm.generate(prompt, model=config.REPLY_MODEL, context=memory_block)
