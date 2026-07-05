"""The machine symbiot's persona: the voice, kept as two strings split by who may read them.

The persona is not one text but two. The public half is versioned in the repo — the
character and the stance, in the open like the rest of The Joy — and it carries a single
{{ INJECT_SYMBIOSIS_CORE_PRIVATE }} token marking where the private half is spliced in.
The private half is never committed (it's gitignored, the same discipline the credentials
and the server secret already follow): it holds what the symbiot won't hand to the outside
World, and it fills that token.

Nothing here reads the persona into an answer yet — that's a later rung. This module only
assembles the two stored strings into the one persona string. It errs toward always
returning a whole, coherent voice: if the private half isn't on disk (a fresh clone, a
contributor with no secrets), the token collapses to nothing and the public persona stands
alone rather than raising. The token never survives into the assembled voice either way.
"""

from pathlib import Path

import config

# The slot cut into the public persona where the private half is spliced in.
PLACEHOLDER = "{{ INJECT_SYMBIOSIS_CORE_PRIVATE }}"


def load() -> str:
    """The assembled persona: the public voice with the private half spliced into its slot.

    The public half must be present — it's the versioned file the repo always carries.
    The private half is optional: an absent private file collapses the token to empty, so
    the public persona stands on its own. Either way no literal PLACEHOLDER is left behind.
    """
    public = Path(config.PERSONA_PUBLIC_FILE).read_text(encoding="utf-8")
    return public.replace(PLACEHOLDER, _read_private())


def _read_private() -> str:
    """The private half, or an empty string when there's no private file to read.

    A missing file is not an error — it's the fresh-clone case, and it means the persona
    has no private colour yet, only its public frame.
    """
    try:
        return Path(config.PERSONA_PRIVATE_FILE).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
