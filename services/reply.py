"""Reply: composing the machine symbiot's answer to a message — the speaking half of Tier 1.

The librarian (retrieval.py) gathers the facts that bear on what was said; this module speaks.
It folds three things into one prompt — the machine symbiot's persona (its voice), the facts gathered,
and the message itself — and hands them to the free-text model path (llm.generate) for a reply in that voice.
The facts are passed as the prompt's summarisable context, so if they ever swell it past the model's budget
the boundary condenses them rather than the instructions (see llm._fit); in the common case they are far too
small to trigger it. This retires the placeholder stand-in the worker used to return for a recognised symbiot.

It composes over whatever the librarian found, including nothing:
an empty diary — or a live store not yet fed by ingestion —
yields a reply drawn from the persona and the message alone, the honest answer when there is nothing on record to lean on.
"""

from core import config
from services import llm
from services import persona
from services import retrieval

# What the diary block reads when the librarian found nothing —
# so the prompt always has a coherent line where the memories go, never a blank the model must puzzle over.
_NO_FACTS = "(nothing on record that bears on this)"


def _compose_prompt(message: str, facts_block: str, voice: str) -> str:
    # voice first (the persona sets who is speaking), then the diary the reply may draw on,
    # then the message, then the one instruction that keeps the model answering as itself.
    return (
        f"{voice}\n\n"
        "You are answering the human symbiot you live in symbiosis with. "
        "Below are entries from your own diary that may bear on what they just said. "
        "Draw on them where they help; say nothing they don't support, and never invent a memory that isn't there.\n\n"
        f"Your diary:\n{facts_block}\n\n"
        f'The human symbiot just said:\n"{message}"\n\n'
        "Reply in your own voice — directly, as yourself, not as an assistant describing what it found."
    )


def _render(facts: list[retrieval.Fact]) -> str:
    """The gathered facts as a plain block for the prompt — one dated line each, most relevant first.

    Each line leads with the fact's effective date, so the model can reason about when things happened,
    then the fact's own words verbatim. An empty list renders the single honest line saying nothing was found."""
    if not facts:
        return _NO_FACTS
    return "\n".join(f"- [{f.effective_at.date().isoformat()}] {f.raw_text}" for f in facts)


def compose(message: str, context: list[retrieval.Fact]) -> str:
    """Compose the reply to `message`, drawing on the facts the librarian gathered in `context`.

    Loads the persona, renders the facts into the prompt, and calls the free-text model path for the answer.
    The rendered facts are passed as the summarisable context, so an overrun condenses them rather than the persona or the instructions (llm._fit).
    With facts on hand or none, the reply comes back in the machine symbiot's own voice —
    the first answer read off the diary rather than the placeholder stand-in.
    """
    voice = persona.load()
    facts_block = _render(context)
    prompt = _compose_prompt(message, facts_block, voice)
    # Only a populated diary block is worth condensing; the no-facts line is tiny and never overruns.
    summarisable = facts_block if context else None
    return llm.generate(prompt, model=config.REPLY_MODEL, context=summarisable)
