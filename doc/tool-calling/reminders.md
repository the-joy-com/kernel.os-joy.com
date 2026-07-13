# the reminder: schedule_reminder — the tool registry's first inhabitant

The reminder is the first tool The Joy can reach for. It does the humblest useful thing an acting loop can do: hear "remind me of X at Y" in the ordinary flow of conversation, and at Y, say X back. One message, one future time, one fire. It was chosen first not because reminders are important but because they are *clean* — no external driver, no third-party credential, only a durable row in our own store and the reply path already built — so what it proves is the [tool-calling machinery](../tool-calling.md), not the plumbing of an integration.

This document is the reminder in particular; the seam it rides — retrieve, decide, act, speak, and the invariant that the model decides and code does — is [`doc/tool-calling.md`](../tool-calling.md).

## what it is, as a tool

The reminder is a `Tool` like any other (`services/reminder.py`), joined to the machinery by its name:

- **name** — `schedule_reminder`, what the decision emits and the key its executor is registered under.
- **description** — the prose the catalog recall matches a message against and the decision reads to judge fit. Worded to surface on the obvious phrasings ("remind me", "don't let me forget") both by meaning and by wording.
- **argument schema** — `ReminderArgs`, two nullable fields: `reminder_message` (the line to say back, phrased the way it should be heard then) and `fire_at` (the resolved moment). Both nullable so the decision can name the tool yet leave one it couldn't read null.
- **executor** — `reminder._execute`, the code that carries out the effect.

## scheduling: the act

A "remind me…" message travels the ordinary [flow](../tool-calling.md#the-flow-retrieve-decide-act-speak): the catalog gate surfaces `schedule_reminder`, the decision call reads the sentence into the tool and its two arguments, and the executor runs on the worker's own thread. Two things are specific to the reminder here.

**Time is resolved in the symbiot's timezone, never the server's.** The decision call is given the symbiot's local "now" (see [timezones](../../services/zone.py)) and resolves a relative cue ("in 20 minutes", "tomorrow at 9") or an absolute one ("on the 14th at noon") into a concrete instant. The executor stores that as an absolute `TIMESTAMPTZ`, so the due check later compares two absolute instants and the summer-time shift is already baked in. A `fire_at` that arrives without a zone is read as the symbiot's local wall clock.

**When the time can't be read, it asks rather than guesses.** If the decision leaves `fire_at` (or the message) null, the executor stores *nothing* and returns an un-effected result, so the confirmation asks the human when they want it rather than filing a reminder it is unsure of — the reactive-ambiguity law, kept at the point of action.

**The write is exactly-once against the triggering message.** The `reminder` row carries the `intake_id` of the message that scheduled it, under a `UNIQUE` constraint. A retried message (a deadline bite, a crash) re-runs the executor harmlessly: the second write conflicts and does nothing, the reminder already stands, and only the spoken confirmation is re-derived.

The `reminder` table (migration `0017`):

| column | meaning |
| --- | --- |
| `intake_id` | the message this was scheduled from — `UNIQUE`, the exactly-once pin on scheduling |
| `symbiot_id` | whose reminder it is |
| `body` | the line to say back when it fires |
| `fire_at` | the resolved absolute instant it is due |
| `fired_at` | null until delivered, stamped when it fires — the exactly-once pin on firing, and the ledger of what fired |

## firing: the due sweep

Firing is a background sweep — `worker.run_reminder_sweep`, the sixth background loop, started in `main.py`'s lifespan beside the worker pool and the other sweeps. **It polls on an interval:** `REMINDER_SWEEP_INTERVAL_SECONDS` (default 10 s) is the *idle* poll — the sweep drains back-to-back while reminders are due, then waits that interval when there is nothing, so a reminder fires within about ten seconds of its moment. It has its own on/off switch, `REMINDER_ENABLED` (off under test, where the reminder tests drive `_fire_one` by hand).

Each pass (`worker._fire_one`):

1. **claims** the oldest reminder that is due (`fire_at <= now()`) and unfired (`fired_at IS NULL`), under `FOR UPDATE SKIP LOCKED` so two workers never claim the same one (`reminder.claim_due`);
2. **raises** the stored `body` as a missive, and **mirrors** it onto the conversation stream so a later reply remembers the machine said it;
3. **stamps** `fired_at` (`reminder.mark_fired`).

All three run in **one transaction**, so the send and the record commit together: a crash before the commit leaves the reminder unfired and simply due again, and a commit sends it and stamps it, so it is never delivered twice — exactly-once on the firing side, pinned in the database, not in the sweep being careful. The row is kept, not cleared: it is the ledger of what fired and when. After the transaction, a best-effort push nudge (`push.notify_inbox`) rides outside it, since the missive already stands to be read regardless.

## delivery: how the human sees it

A fired reminder is a missive — the kernel reaching out on its own — so the human discovers it through the inbox, on its own terms, not as an inline reply to a message the conversation has left behind. It reaches the shell over the two channels missives always ride, so first contact never depends on one holding up:

- **the push nudge** — when web push is configured, the sweep's `push.notify_inbox` wakes the shell the instant the reminder fires;
- **the inbox poll** — a gentle background poll the shell runs while the tab is open, so a reminder that fires while the human is looking at the shell surfaces within a beat even when push is off (a dev box, a visitor, a browser that refused notifications).

Either way the missive is recorded durably first, so it surfaces on the next inbox open no matter what.

## configuration

| variable | default | what it does |
| --- | --- | --- |
| `TOOLS_ENABLED` | on | the startup catalog reconcile (off under test, so startup never embeds) |
| `TOOL_DECISION_MODEL` | `RERANK_MODEL` | the model that decides which tool and extracts the arguments |
| `TOOL_CONFIRM_MODEL` | `REPLY_MODEL` | the model that composes the confirmation in the symbiot's voice |
| `TOOL_RECALL_MAX_DISTANCE` | `0.6` | the gate's cosine-distance threshold — coarse, generous, since the decision is the precision |
| `TOOL_RECALL_LIMIT` | `5` | the shortlist size handed to the decision |
| `TOOL_RECALL_EF_SEARCH` | `100` | the HNSW working-set width for the catalog search |
| `REMINDER_ENABLED` | on | the firing sweep (off under test) |
| `REMINDER_SWEEP_INTERVAL_SECONDS` | `10` | the firing sweep's idle poll |

## what it is not

One-shot only, by design. There is no recurrence ("every Monday"), no arbitrary future action, and no list/cancel surface beyond what falls out for free. Those are the general scheduler's remit, which will later absorb the reminder as its first concrete scheduled action and generalise the firing sweep across more than one kind of timed effect. The reminder is kept honest and small so the tool-calling machinery, not a half-built scheduler, is what it proves.
