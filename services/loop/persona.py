"""The machine symbiot's persona: the voice, and the fixed head every composed answer leads with.

The persona is not one text but two. The public half is versioned in the repo — the
character and the stance, in the open like the rest of The Joy — and it carries a single
{{ INJECT_SYMBIOSIS_CORE_PRIVATE }} token marking where the private half is spliced in.
The private half is never committed (it's gitignored, the same discipline the credentials
and the server secret already follow): it holds what the symbiot won't hand to the outside
World, and it fills that token.

`head` is what the composing calls actually reach for: the ground-rules preamble, then the assembled persona, as one block.
It leads every high-volume prose answer — the reply, the enrichment follow-up, the tool confirmation — for two reasons at once.
First, truth: the preamble is the contract that governs how the voice may speak, so it comes before the voice.
Second, cost: this block is the one part of every such prompt that is byte-for-byte identical call to call,
so a caching provider (the Gemini top rung) bills it once and then near-free.
That only pays if every compose site shares the *same* head rather than each building its own,
so the head is assembled here, in one place, and prepended uniformly.

It errs toward always returning a whole, coherent voice:
if the private half isn't on disk (a fresh clone, a contributor with no secrets),
the token collapses to nothing and the public persona stands alone rather than raising.
The token never survives into the assembled voice.
"""

from pathlib import Path

from core import config

# The slot cut into the public persona where the private half is spliced in.
PLACEHOLDER = "{{ INJECT_SYMBIOSIS_CORE_PRIVATE }}"


def head() -> str:
    """The fixed head every composed answer leads with: the ground-rules preamble, then the persona.

    The block that repeats identically across the reply, the enrichment follow-up, and the tool confirmation —
    so it is assembled once, here, and prepended by all three the same way,
    which is what lets a caching provider bill it a single time rather than once per call.
    The preamble leads because the truth contract governs the voice, not the other way round.
    """
    return f"{preamble()}\n\n{load()}"


def load() -> str:
    """The assembled persona: the public voice with the private half spliced into its slot.

    The public half must be present — it's the versioned file the repo always carries.
    The private half is optional: an absent private file collapses the token to empty, so
    the public persona stands on its own. Either way no literal PLACEHOLDER is left behind.
    """
    public = Path(config.PERSONA_PUBLIC_FILE).read_text(encoding="utf-8")
    return public.replace(PLACEHOLDER, _read_private())


def preamble() -> str:
    """The ground rules — the truth contract that binds every composed answer — read from its repo file.

    Versioned like the public persona (it says nothing secret, only how the machine must handle truth),
    and stripped of surrounding whitespace so it seats cleanly at the front of the head.
    """
    return Path(config.PREAMBLE_FILE).read_text(encoding="utf-8").strip()


def _read_private() -> str:
    """The private half, or an empty string when there's no private file to read.

    A missing file is not an error — it's the fresh-clone case, and it means the persona
    has no private colour yet, only its public frame.
    """
    try:
        return Path(config.PERSONA_PRIVATE_FILE).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
