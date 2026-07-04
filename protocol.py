"""Protocol: every word the kernel speaks to the shell, gathered in one place.

Each kernel response wears the envelope shape (see main.py) — a `msg` and a `data` —
and `msg` is the word this catalog owns: the line the shell reads to know what happened.
Some are matched on the exact word, the way "loud and clear" and "roger" are;
others are a human-legible line the shell shows as-is, like the login replies.
Either way they're the wire's vocabulary, kept here so the whole contract is legible in one place
and can be read against the shell that has to understand it —
rather than scattered one literal per route, where no one can see the set entire.

The message-outcome words (the ANSWER_* set) are the case that first earned this file:
they're read in two places — the /answers route and the push nudge (push.py) —
so a single home is what keeps those two from drifting into different vocabularies for the same shell.
The internal state machine has more states than these — received/working/failed are all still in flight —
but the wire only needs settled-or-not, and settled-how, so those three collapse to one ANSWER_PENDING.
The kernel's own status words stay kernel-side (intake.py); these are only what crosses to the shell.

Grouped by the round trip that emits them, and alphabetical within each group.
"""

# The bare host, and the health round trip the connectivity dot probes.
GREETING = "the ghost in the shell"  # GET / — a legible name on the door rather than a 404
OK = "loud and clear"  # GET /health — radio check: the kernel is up and reading you; only a real 200 here flips the dot green

# The intake round trip: the acknowledgement at receipt, then the outcome read back off it.
# The four words /answers collapses a message's state into — read here and by the push nudge.
ANSWER_ABANDONED = "abandoned"  # the kernel tried its budget of times and gave up
ANSWER_PENDING = "wait out"  # still in flight — received, working, or between retries; ask again, I'll call you back
ANSWER_READY = "answer"  # answered — the reply is in data.answer
ANSWER_UNKNOWN = "unknown"  # no message carries that id
ABANDONED_NOTICE = "no joy"  # the line the shell shows when a message is abandoned — retry budget spent, negative outcome
COPY = "roger"  # POST /intake — received all your last, durably written down (not the answer, which comes later)
REPLY = "reply"  # the push nudge's kind for a reply to the symbiot's own message — its outcome rides in status (an ANSWER_* word); the missive's counterpart is TRAFFIC_WAITING
STANDIN_ANSWER = "good copy"  # placeholder in data.answer until the kernel has real work to perform

# Traffic waiting: unsolicited messages the kernel raises for the symbiot, discovered on open.
TRAFFIC_WAITING = "traffic waiting"  # GET /inbox — the symbiot's unseen inbound; the messages ride in data.messages

# Identity: the login handshake and session state.
AUTHED = "authenticated"  # GET /status — a live session
LOGGED_IN = "logged in"  # POST /login/verify — the code was good; token in data
LOGGED_OUT = "out"  # POST /logout — signing off; session revoked (idempotent)
LOGIN_FAILED = "that code didn't work — try again"  # POST /login/verify — wrong or spent code
# POST /login — identical for a known address, an unknown one, or a recipient-smuggling string,
# so it's no oracle for who's registered.
LOGIN_SENT = "if that address is registered, a login code is on its way"
NOT_AUTHED = "not authenticated"  # GET /status — no live session

# Push: the reply-channel handshake.
PUSH_KEY = "push key"  # GET /push/key — the public app-server key (null in data when push is off)
SUBSCRIBED = "subscribed"  # POST /push/subscribe — channel registered; its id in data
