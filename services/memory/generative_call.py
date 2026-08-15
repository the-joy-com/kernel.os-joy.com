"""The ledger of which model actually answered: the write side of the generative-call record.

Every generative call resolves a *role* to a model name and then walks a fallback ladder (services/adapters/llm.py),
so the model that answers is often not the one the role names, and it never appears in the reply itself.
That fact used to live only in a log line, which is to say it lived until the log rotated —
and it is precisely the fact a symbiot reading a reply that came back wrong would want to inspect,
since the reply looks identical whichever rung composed it.
This module makes it durable (migration 0026), and the /observe hub's models card reads it back.

One row per recorded call, written from the model boundary the moment a rung answers.
The read is not here: it lives with the rest of the observability corner's reads (services/observe.py),
the same split the model configuration already keeps
between the store that owns the writes (memory/model_config.py)
and the resolver that reads them (adapters/models.py).

Two properties the write must hold, both enforced here rather than left to the caller.

It opens its own short-lived connection rather than taking one.
The reply is composed inside a spawned child process (loop/execution.run_with_deadline),
a fresh interpreter that inherits none of the parent's memory and so has no pool to borrow from —
the same constraint the model resolver already answers the same way (adapters/models._ensure_loaded).
One cheap insert against a small table, next to a call that just spent seconds at a provider.

And it never raises. A ledger is an account of the work, not a step in it:
a database that is briefly unreachable must cost the symbiot a row of audit, never their reply.
So a failed write is logged and swallowed, and the call it describes carries on as though nothing happened.
"""

import psycopg

from core import config
from core import logs

log = logs.get("generative_call")


def record(
    *, role: str, requested_model: str, served_provider: str, served_model: str, reply_chars: int
) -> None:
    """Write down that `role` was served by `served_provider`/`served_model`, and how much came back.

    requested_model is the model the role resolved to before the ladder was walked;
    served_model is the one that actually answered. Equal, the primary answered and nothing fell through;
    different, a rung outaged and a humbler model composed the words — the distinction the card exists to show.
    reply_chars is the length of the model's raw reply body, structured-output wrapper and all,
    measured raw because the output ceiling it is read against applies to the raw body too:
    the cheapest tell for a model degenerating against that ceiling.
    A length and never the words, which is why nothing of the symbiot's content is kept here.

    Best-effort by design: any failure is logged and swallowed,
    so a ledger that cannot be written never becomes a reply that cannot be sent.
    """
    try:
        with psycopg.connect(config.DATABASE_URL) as conn:
            conn.execute(
                "INSERT INTO generative_call "
                "(role, requested_model, served_provider, served_model, reply_chars) "
                "VALUES (%s, %s, %s, %s, %s)",
                (role, requested_model, served_provider, served_model, reply_chars),
            )
    except Exception:
        # The account of the work is never allowed to interrupt the work.
        log.warning("couldn't record the %s call served by %s/%s", role, served_provider, served_model)
