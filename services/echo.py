"""Echo: the one measure of "more or less the same thing", shared by the lens and the guard.

The machine repeats itself in its deep follow-ups — not word for word, which a hash would catch,
but the same thought in slightly different clothes, a semantic near-duplicate.
Two places need to put a number on that sameness, and they must agree on what it means:
the `/observe` echoes lens, which *sees* the redundancy (observe.py),
and the enrichment guard, which *stops* it — refusing to send a follow-up that echoes one already sent (enrichment.py).
So the measure lives here, once, and both call it: the instrument and the fix share one definition rather than drifting apart.

The measure is cosine closeness between the embeddings of two lines,
read the same way the diary's recall reads distance — only here it is turned into a similarity (1 is identical, 0 is unrelated)
and pointed at the machine's own utterances rather than the diary's facts.
The embedding itself is not done here — a caller embeds its lines through the embedding adapter and hands the vectors in —
so this module stays pure arithmetic over vectors, with no model call and no store of its own.
"""

import math

# Cosine closeness at or above which two of the machine's lines count as an echo — a semantic near-duplicate,
# the "more or less the same" a hash would miss. Tuned against real nomic-embed-text output (test/qa/0009):
# unrelated lines land around 0.55–0.60 and clear paraphrases around 0.80–0.88, so the bar sits in that gap —
# low enough to catch a loose paraphrase, high enough to leave unrelated lines alone. Still a knob; move it if real use drifts.
ECHO_THRESHOLD = 0.75


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors, 0 when either has no length — the closeness the echo test reads."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def max_similarity(target: list[float], others: list[list[float]]) -> float:
    """The closest any of `others` sits to `target`, as a cosine similarity — the guard's one question.

    Where the lens groups a whole page of lines into clusters, the guard asks only "how near is the nearest?":
    a candidate follow-up against every deep reply already sent, the largest closeness deciding whether it is an echo.
    An empty `others` — nothing has been said deeply before — is 0.0, the honest "nothing to echo", never a suppression.
    """
    return max((cosine(target, o) for o in others), default=0.0)
