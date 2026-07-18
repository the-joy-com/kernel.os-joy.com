# kernel.os-joy.com

> the ghost in the shell

The kernel — the privileged core behind `kernel.os-joy.com`. The server-side counterpart to the [shell](../shell.os-joy.com): where the shell is the thin user-facing input layer, the kernel does the real work — intake/release, the buffer, identity, the Dead Man's Switch — mediating World ↔ symbiot.

It exposes a small HTTP surface: a health probe the shell's connectivity dot reads, a name on the door at `/`, a line-intake endpoint, the identity routes (`/login`, `/login/verify`, `/status`, `/logout`), the model-configuration routes (`/models`), and auto-generated API docs — see [Routes](#routes) below. Every response wears the same [envelope](#api-response-envelope). State lives in **Postgres**, reached over [psycopg](https://www.psycopg.org/) with **no ORM** — see [Database & migrations](#database--migrations).

> **Running it on a home box with no cloud API and no Gmail?** The kernel can run fully local — generation on your own [Ollama](https://ollama.com), login codes to a file instead of email. Jump to [Running fully local](#running-fully-local-no-cloud-api-no-gmail).

## Install & run (local)

Prerequisite: [`uv`](https://docs.astral.sh/uv/getting-started/installation/) installed and on your `PATH`.

The virtualenv is named `venv` (not uv's default `.venv`). uv reads its location from `UV_PROJECT_ENVIRONMENT`, so export that first — every `uv` command below then targets `venv/`:

```bash
export UV_PROJECT_ENVIRONMENT=venv
```

1. **Create the virtualenv and install the pinned deps** (exact versions from `uv.lock`):

   ```bash
   uv sync
   ```

2. **Start Postgres and create your `.env`:**

   ```bash
   docker compose up -d        # local Postgres on :5432
   cp .env.example .env        # then fill in SYMBIOT_EMAIL, KERNEL_SECRET (see Configuration)
   ```

   The default `DATABASE_URL` already points at this docker-compose Postgres, so a plain clone needs no edits to connect.

3. **Run the kernel locally** — uvicorn bound to localhost:

   ```bash
   uv run uvicorn main:app --host 127.0.0.1 --port 9713 --reload
   ```

   (`--reload` is for development; drop it in production.) Migrations and the symbiot seed run automatically on startup against `DATABASE_URL`.

4. **Confirm the round trip:**

   ```bash
   curl http://127.0.0.1:9713/health
   # {"msg":"loud and clear","data":{"version":"0.0.1"}}
   ```

## Managing dependencies

```bash
uv add <package>            # add a dependency (updates pyproject.toml + uv.lock)
uv remove <package>         # drop one
uv sync                     # bring venv in line with uv.lock
```

Commit `pyproject.toml` and `uv.lock`; never commit `venv/`.

## API response envelope

Every response the kernel returns wears the same shape:

```json
{ "msg": "string", "data": null }
```

- `msg` — a human-legible line about what happened (`"loud and clear"`, an error reason, a status word).
- `data` — the payload to act on: a JSON array, a JSON object, or `null` when there's nothing to carry.

So `GET /health` answers:

```json
{ "msg": "loud and clear", "data": { "version": "0.0.1" } }
```

## Routes

| Method & path | What it answers |
| --- | --- |
| `GET /` | A name on the door — `{ "msg": "the ghost in the shell", "data": { "version": "0.0.1" } }` — so the bare host is legible instead of a 404. |
| `GET /health` | The probe the shell's connectivity dot reads — `{ "msg": "loud and clear", "data": { "version": "0.0.1" } }`. |
| `POST /intake` | Takes one line off the shell's prompt — body `{ "line": "<text>" }` — and acknowledges it with `{ "msg": "roger", "data": null }`. `"roger"` means *received*, not *stored*: the line is dropped (holding it in the buffer is a separate concern that layers on top of this round trip). The `line` is required, non-empty, and capped at 4096 chars; anything else is a `422`. The request shape is validated by the `IntakeRequest` DTO in [`dtos.py`](./core/dtos.py). |
| `POST /login` | Body `{ "address": "<email>" }`. Issues a one-time code **only** on an exact match to a registered symbiot, delivering it to them — emailed when Gmail is configured, or written to `OTP.txt` on a mailboxless box (see [Running fully local](#running-fully-local-no-cloud-api-no-gmail)); otherwise does nothing. The reply is **identical either way** — `{ "msg": "if that address is registered, a login code is on its way", "data": null }` — so it's no oracle for who's registered (an unknown address, a blank one, and a recipient-smuggling string all get the same answer, and no code goes anywhere). |
| `POST /login/verify` | Body `{ "address": "<email>", "code": "<code>" }`. Spends a valid (unconsumed, unexpired, latest-issued) code for that address's session: `{ "msg": "logged in", "data": { "token": "…", "email": "…" } }`. A wrong code answers `{ "msg": "that code didn't work — try again", "data": null }` and leaves the caller unauthed, free to retry. The address names whose code the guess is charged against: after `MAX_VERIFY_ATTEMPTS` wrong tries the database burns that code (see [Rate limiting & abuse](#rate-limiting--abuse)). |
| `GET /status` | Reads `Authorization: Bearer <token>`. Reports `{ "data": { "authed": true, "email": "…" } }` for a live session, else `{ "data": { "authed": false, "email": null } }`. |
| `POST /logout` | Reads `Authorization: Bearer <token>` and revokes that session. Idempotent — no token, or an already-revoked one, is a clean no-op: `{ "msg": "out", "data": { "authed": false } }`. |
| `GET /models` | Authed (`Authorization: Bearer <token>`). Reports the model configuration — the catalog, the current role assignments, and the assignable roles — in `data`, so the shell's `/models` command opens on the current state. Box-level config, but still session-gated: only the logged-in operator sees or shapes it. Unauthed → `{ "msg": "not authenticated", "data": { "authed": false } }`. |
| `POST /models` | Authed. One change to the model configuration — body `{ "action": "register"\|"delete"\|"assign", … }` (see the `ModelConfigRequest` DTO in [`dtos.py`](./core/dtos.py)): register/edit an operator model, delete one, or point a role at a catalog model. A successful change returns the full fresh state (`{ "msg": "models", … }`); a refused one (editing a builtin, deleting a model in use, an unknown role) returns `{ "msg": "that model change didn't take", "data": { …, "reason": "…" } }` with the state unchanged. See [Running fully local](#running-fully-local-no-cloud-api-no-gmail). |
| `GET /docs` | Interactive API docs (Swagger UI), generated for free by FastAPI from the route signatures. `GET /redoc` and the raw `GET /openapi.json` come along with it. |

## CORS

The shell reads `/health` from a different origin (`shell.os-joy.com`, or `localhost` in dev), so the kernel sends an explicit CORS allow-list — without it the browser blocks the read and the shell's dot reads offline even when the kernel is up. The allowed origins are pinned in `main.py` (`ALLOWED_ORIGINS`): the production shell plus the two local dev origins (`http://localhost:5173`, `http://127.0.0.1:5173`), `GET` (the health probe) and `POST` (sending a line to `/intake`), nothing wildcarded. The `POST` carries a JSON body, so the browser preflights it with an `OPTIONS` request — the CORS middleware answers that itself, which is why `OPTIONS` isn't in the method list. Add an origin there when a new front-end needs to read the kernel from the browser.

## Identity

The kernel seeds one human — **the symbiot** — from `SYMBIOT_EMAIL` at startup. The `symbiot` table and the `/login` lookup already hold and match many addresses; today exactly one is seeded, so supporting more is a matter of seeding rather than a schema change. Logging in is a one-time emailed code: `POST /login` with a registered symbiot's address issues an eight-digit code and emails it; `POST /login/verify` spends that code for a session token. Two rules make it safe:

- **No enumeration oracle.** `/login` issues a code *only* on an exact match to a registered address, and its reply is byte-identical whether or not a match happened. An unknown address, a blank one, or a recipient-smuggling value (`a@x, b@y`, `a@x;b@y`, `a@x.evil`, `a+b@x`, an embedded newline) all get the same reply — and no email goes to anyone.
- **Nothing sensitive at rest.** Codes and session tokens are HMAC'd with `KERNEL_SECRET` before they touch the database, so a leaked table yields no usable code or token. Codes are single-use, short-lived, and only the latest-issued one verifies; sessions are revoked on `/logout`.

The code reaches the human through a small `EmailClient` interface ([`email_client.py`](./services/adapters/email_client.py)), and which implementation a box uses is chosen at startup by config:

- **Gmail wired** (`GMAIL_CREDENTIALS_FILE` *and* `GMAIL_SENDER` both set) → the code is emailed via the **Gmail API** (see [Email (Gmail API)](#email-gmail-api)).
- **Neither set** → the code is written to a local file, **`OTP.txt`** at the repo root (`OTP_FILE`), and a console line signposts that a code was written and where (never the code itself). This is the mailboxless path for a fully-local box — the operator, who controls the box, reads the code straight off disk. See [Running fully local](#running-fully-local-no-cloud-api-no-gmail).

Either way the `/login` reply is the same and reveals nothing; the test suite injects a fake that records messages instead, so the whole flow is exercised without credentials or a file on disk. The enumeration-safety above holds identically for both — a local file has no external observer at all.

Address **format** is never validated by the kernel — only matched. A malformed address takes the same no-match path as any unknown one (no code, no email, the one canonical reply), because a `422` on "that's not a valid email" would itself be an enumeration oracle. Catching typos is the shell's job, done locally before the request is sent — a kindness that costs the kernel no safety to omit.

## Rate limiting & abuse

Two layers guard the DB- and email-touching routes, deliberately split by what each can actually promise:

- **A soft edge limiter** ([`rate_limit.py`](./core/rate_limit.py)) — a small hand-rolled in-memory sliding window, no dependency. It sheds gross volume cheaply before a request reaches a route: `/login` and `/login/verify` get tight per-IP ceilings, every other route a generous one, and `/health` is exempt (throttling the connectivity probe would make a healthy kernel read offline). It's best-effort by nature — per-process, forgotten on restart, keyed by a spoofable/shared IP — so it's the gate, not the guarantee. Toggle with `RATE_LIMIT_ENABLED`. **It keys on the `X-Real-IP` header**, which nginx must set (see [Deploy](#deploy)) — without it every caller collapses into one bucket.
- **Hard guarantees in the database** — the locks that survive restarts, multiple processes, and IP rotation, because they're rows, not counters. (1) A **re-issue interval** (`LOGIN_REISSUE_INTERVAL_SECONDS`): a fresh code can't be minted while the live one is younger than the window, capping email bombing — and, as a bonus, a double-tap no longer invalidates the code already in the inbox. (2) A **per-code attempt budget** (`MAX_VERIFY_ATTEMPTS`): each live code absorbs a fixed number of wrong guesses, then the row burns itself. This is why `/login/verify` carries the address — so a wrong guess is charged to that symbiot's code and no one else's, making brute force a bounded budget rather than a race against the 1,000,000-code search space.

## Database & migrations

State lives in **Postgres**, reached with **psycopg 3** and **no ORM** — raw, parameterised SQL ([`db.py`](./core/db.py), [`identity.py`](./services/identity.py)).

Migrations are **plain ordered `.sql` files** under [`migrations/`](./migrations), applied at app startup: the runner ([`db.py`](./core/db.py)) creates a `schema_migrations` ledger, applies every file not yet recorded inside its own transaction, then idempotently seeds the symbiot from `SYMBIOT_EMAIL`. There's no separate migrate step and no ORM-generated migrations — startup always brings the schema current. To add a change, drop a new `NNNN_name.sql` beside the existing one; it runs on the next boot.

**Local** development uses a Postgres in Docker — [`docker-compose.yml`](./docker-compose.yml) ships one matching the default `DATABASE_URL`:

```bash
docker compose up -d        # start local Postgres on :5432
```

The **server** does *not* use Docker: it has a native Postgres reached over a unix socket with **peer auth** (tied to the OS user the service runs as), so its `DATABASE_URL` is genuinely different from local — see [Deploy](#deploy).

### pgvector

The ontology and embedding store lives in Postgres too, and it needs the [`pgvector`](https://github.com/pgvector/pgvector) extension — the type that lets a column hold an embedding and be searched by vector distance. There are two separate things here, and it's worth keeping them apart: the **binary** (the compiled extension, which has to be installed on the box before Postgres can load it) and **enabling it in a database** (`CREATE EXTENSION vector`, run once per database, which makes the `vector` type actually available inside `joy`). The binary is what differs between local and server; the `CREATE EXTENSION` is the same everywhere and is carried by a migration, so it runs at startup like every other schema change.

**Local** — nothing to do. The [`docker-compose.yml`](./docker-compose.yml) now runs the `pgvector/pgvector:pg16` image instead of plain `postgres:16`: same Postgres 16, but with the extension binary already baked in. The `joy` user the container creates is a superuser, so the migration's `CREATE EXTENSION vector` just works on the next boot. If you have an *old* `joy_pgdata` volume from before this change, recreate it so you get the new image cleanly:

```bash
docker compose down -v      # drops the old volume
docker compose up -d        # comes back up on pgvector/pgvector:pg16
```

**Server (bare metal, Ubuntu 24.04)** — two steps, once per box:

1. **Install the binary** from the PostgreSQL APT repository (PGDG), matching your server's Postgres major version. For Postgres 16:

   ```bash
   sudo apt install postgresql-16-pgvector
   ```

   (Swap `16` for whatever major your box runs — `psql -V` tells you. If `apt` can't find the package, the PGDG repo isn't set up; add it per [apt.postgresql.org](https://wiki.postgresql.org/wiki/Apt), then `sudo apt update`.)

2. **Enable it in the `joy` database, once, as a superuser.** `pgvector` is *not* a "trusted" extension, so creating it requires a Postgres superuser — the service's peer-auth role can't do it itself unless that role happens to be a superuser. Do it by hand as the `postgres` superuser:

   ```bash
   sudo -u postgres psql -d joy -c 'CREATE EXTENSION IF NOT EXISTS vector;'
   ```

   Once the extension exists in `joy`, the migration's own `CREATE EXTENSION IF NOT EXISTS vector` is a harmless no-op on every boot after. (If the role the service connects as *is* a superuser, you can skip this and let the migration create it on first startup — but doing it explicitly is the reliable path and costs nothing.)

No extension binary, and Postgres refuses to load the `vector` type — the migration fails at `CREATE EXTENSION` before any table is built. Install the binary first, then let the schema come up.

## Models: embedding (local) and generation (cloud, with a local fallback)

The kernel uses models in two roles, split by where they run.

**Embedding stays on the box**, served by [Ollama](https://ollama.com) — its vector width is what the pgvector tables are typed to, so it can't move without a re-embed. **`nomic-embed-text`** (768-dimensional output) turns every ontology definition and every incoming fact into the vector the recall pass ranks by. Install Ollama per the [official instructions](https://ollama.com/download) and pull it once per box:

```bash
ollama pull nomic-embed-text
ollama pull qwen3.5:4b        # the generative fallback — see below
```

Ollama serves on `http://127.0.0.1:11434` by default, native on both local and server; it is *not* part of [`docker-compose.yml`](./docker-compose.yml).

> **Heads-up on `nomic-embed-text`:** Ollama clips this model's context to 2048 tokens by default (its native window is 8192) and truncates *silently*, so long text must be embedded with `num_ctx: 8192` set — otherwise its tail vanishes from the vector with no error. The model also expects `search_document:` / `search_query:` task prefixes for its distances to mean anything. Both live in how the pipeline calls Ollama ([`services/embedding.py`](./services/embedding.py)), not in the schema.

**Generation is configurable at runtime**, and can run entirely on the box or lean on the cloud — your choice, changed with a command, never a code edit. Two things drive it, and it's worth keeping them apart (they map to two database tables, migration `0019`):

- **The catalog** — the set of models the kernel knows how to talk to, each with the characteristics it must be driven by: its **provider** (`scaleway`, `mistral`, or `ollama`), the context **window it reads well**, and its **output ceiling**. Seven ship built-in — `glm-5.2` and `gpt-oss-120b` (Scaleway), `mistral-large-latest`, `mistral-small-latest`, and `ministral-8b-latest` (Mistral), `qwen3.5:4b` (local Ollama), and `nomic-embed-text` (the embedder). You can register your own on top of these.
- **The role assignments** — which model, out of that catalog, does each generative job: `reply` (the composed answer), `rerank` (the router's classifications), `mint` (coining a new ontology type's definition), `enrich` (the follow-up gate), `tool_decision`, `tool_confirm`, and `conversation_compress`. Each role points at one catalog model. The defaults group into three tiers: a flagship (`glm-5.2`) for the reply, the enrichment gate, and the conversation fold; a cheaper `gpt-oss-120b` for the tool calls, minting, and the router's high-volume classifications.

Both are **operator-editable, box-level state** (not per-symbiot — a model is a property of the machine), reached through the authed **`/models` command in the shell** (see [Running fully local](#running-fully-local-no-cloud-api-no-gmail) for the walkthrough). Both are seeded from code at boot: the built-in models are reconciled from [`services/adapters/models.py`](./services/adapters/models.py), and each role is seeded from its config default (`REPLY_MODEL`, `RERANK_MODEL`, …) *only if you haven't set it* — so a fresh box behaves exactly as before these tables existed, and your own assignments are never overwritten by a later boot.

When a role points at a **cloud** model (a `scaleway` provider), the call goes through a **fallback ladder**, tried per request ([`services/llm.py`](./services/llm.py)):

1. **Scaleway** (primary), reached through the OpenAI-compatible client Scaleway advertises.
2. **Mistral — `mistral-large-latest`** (fallback), reached at Mistral's *own* web API through the official `mistralai` client — deliberately not Scaleway's Mistral, since the point is surviving Scaleway being down.
3. **Local Ollama — `qwen3.5:4b`** (last resort), the on-box engine, kept wired so a double cloud outage still gets an answer.

A cloud call tries the primary and falls to the next tier only on an *outage-class* failure (transport error, timeout, 5xx, 429); a 4xx (a bad request, a bad key) surfaces at once rather than being masked. When a role points at an **Ollama** model, there is no ladder at all — the call goes straight to your local Ollama and never touches the cloud. So **pointing every role at an Ollama model, via `/models`, is what makes generation fully local** (see below). The two fallback tiers themselves are named by `GENERATIVE_FALLBACK_MODEL` and `GENERATIVE_LOCAL_FALLBACK_MODEL`.

> **Heads-up on the generative calls:** thinking is off on every one — on Scaleway through `reasoning_effort`, set per model (`"none"` where the model accepts it, like `glm-5.2`; `"low"` for `gpt-oss`, whose floor is low but which emits no reasoning trace there), and on the Ollama tier with `think: false`. Structured output (the router's typed JSON) goes through each SDK's official `parse` structured-output helper, which binds the decoder to the caller's Pydantic schema; temperature is 0 on every call, the router's scored judgments and the spoken reply alike. These live in how [`services/llm.py`](./services/llm.py) calls each provider, not in the schema.

## Running fully local (no cloud API, no Gmail)

The kernel can run with **no paid API and no Google Workspace** — everything on a box with an [Ollama](https://ollama.com) serving a couple of models. This is the setup for a home server that can't (or won't) reach Scaleway/Mistral for generation or Gmail for login. Embedding was always local; the two things that used to assume the cloud — **login delivery** and **generation** — are each independently switchable, and neither is a single "mode" flag. Here's the whole picture.

### What actually toggles "local"

There is no master switch. Local-ness is the sum of two independent config decisions (plus one thing that's always local):

| Concern | What decides it | Local when… |
| --- | --- | --- |
| **Login code delivery** | Whether the two Gmail vars are set (`main.py` picks the client at startup) | `GMAIL_CREDENTIALS_FILE` **and** `GMAIL_SENDER` are both blank → the code is written to a file instead of emailed |
| **Generation** | The **provider of the model each role points at** (set via `/models`) | every role points at a model whose provider is `ollama` → calls go straight to your Ollama, the cloud is never touched |
| **Embedding** | Always local — `nomic-embed-text` on Ollama, not a toggle | always |

> **Important — blanking the cloud API keys is *not* enough by itself.** The roles still *default* to Scaleway models (`glm-5.2` for the flagship jobs, `gpt-oss-120b` for the cheaper ones), and a Scaleway call with no key returns a `401` — a `4xx`, which does **not** fall through the ladder to Ollama; it raises. To go local you must **reassign the roles to a local model** with `/models`. Blanking the keys is optional tidiness; reassigning the roles is the actual switch for generation.

### Step by step

1. **Install Ollama and pull the models** ([official install](https://ollama.com/download)). You always need the embedder; for generation, pull a capable local chat model (any Ollama model works — `qwen3.5:4b` ships as a built-in in the catalog, but you can register your own):

   ```bash
   ollama pull nomic-embed-text     # the embedder — required on every box
   ollama pull qwen3.5:4b           # a local generative model (or your own choice)
   ```

2. **Start Postgres and create `.env`**, leaving the cloud and Gmail vars blank:

   ```bash
   docker compose up -d
   cp .env.example .env
   ```

   In `.env`, set `SYMBIOT_EMAIL` (your login handle — see the note below), `KERNEL_SECRET`, and **leave these blank**:

   ```bash
   GMAIL_CREDENTIALS_FILE=          # blank → login code goes to a file, not email
   GMAIL_SENDER=                    # blank → same
   SCALEWAY_API_KEY=                # blank → no Scaleway (but you must still reassign roles, below)
   MISTRAL_API_KEY=                 # blank → no Mistral
   ```

   > `SYMBIOT_EMAIL` doesn't have to be a *deliverable* address on a mailboxless box — it's just the handle you log in as. `you@domain.com` is fine. It only needs to be a real mailbox if you use Gmail delivery.

3. **Run the kernel** (as in [Install & run](#install--run-local)):

   ```bash
   export UV_PROJECT_ENVIRONMENT=venv && uv run uvicorn main:app --host 127.0.0.1 --port 9713 --reload
   ```

4. **Log in — the code lands in a file, not your inbox.** In the shell, `/login` with your `SYMBIOT_EMAIL`. Because Gmail is unconfigured, the kernel writes the one-time code to **`OTP.txt` at the kernel repo root** and logs a line saying so (the code itself is never logged, only its location). Read it off the box and type it back:

   ```bash
   cat OTP.txt          # e.g. "Your one-time login code is 42424242 …"
   ```

   `OTP.txt` is gitignored, overwritten on each `/login` (only the newest code ever stands), and the trust root is simply *access to the box* — the same as its SSH key. Point it elsewhere with `OTP_FILE` if you like.

5. **Point the generative roles at your local model — this is the switch for generation.** Still in the shell, run **`/models`** (authed-only; you must be logged in). It opens on the current catalog and assignments, then loops on a prompt:

   - `add` → register your local model if it isn't a built-in. A bare name is enough — `qwen3.5:4b`, or `llama3.1:8b`, etc. — and the provider defaults to `ollama` with sensible window/output defaults filled in. (You can spell out the provider, context window, and output ceiling if you want.)
   - `assign` → point each role at your local model: assign `reply`, then `rerank`, `mint`, `enrich`, `tool_decision`, `tool_confirm`, `conversation_compress`. Assigning `qwen3.5:4b` (already a built-in) needs no `add` first.
   - blank line → done.

   Once every role points at an Ollama-provider model, every generative call goes straight to your local Ollama. Verify with `/models` again — the assignments should all name your local model. That's a fully-local symbiot: **Gmail off (→ `OTP.txt`) + all roles on Ollama (→ local generation) + embedding always local.**

### What `/models` can and can't do

- **Register / edit / delete your own models** freely — but the built-ins are code-owned: you can *assign* them to roles, but not edit their characteristics (a boot reconcile would overwrite the edit) or delete them (they ship with the kernel).
- **Delete is refused while a role still points at the model** — reassign the role first; the refusal names which roles are holding it.
- **`nomic-embed-text` (embedding) is a hard requirement, not a role you can reassign** — its 768-dimensional vector width is what the pgvector tables are typed to, so swapping it is a full re-embed migration, not a command edit. Every box runs it as-is.

## Configuration (`.env`)

Config is read from a gitignored `.env` at startup ([`config.py`](./core/config.py), via `python-dotenv`) — the same file format locally and on the server; only the values differ. Copy the template and fill it in:

```bash
cp .env.example .env
```

| Var | What it is |
| --- | --- |
| `DATABASE_URL` | Postgres connection. **Local:** `postgresql://joy:joy@localhost:5432/joy` (docker-compose). **Server:** `postgresql:///joy` — empty host = default socket = peer auth as the service's OS user. |
| `TEST_DATABASE_URL` | Optional; the test suite's database. Defaults to `DATABASE_URL` with a `_test` suffix. |
| `SYMBIOT_EMAIL` | The one human allowed to log in, seeded at startup. |
| `KERNEL_SECRET` | Server secret HMAC'ing codes + tokens. Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `GMAIL_CREDENTIALS_FILE`, `GMAIL_SENDER` | Gmail API service-account key path and the mailbox it sends as — see [Email (Gmail API)](#email-gmail-api). **Leave both blank to run mailboxless:** the login code is then written to `OTP_FILE` instead of emailed (see [Running fully local](#running-fully-local-no-cloud-api-no-gmail)). |
| `OTP_FILE` | Override; where the login code is written when Gmail is unconfigured. Default `OTP.txt` at the repo root (gitignored). Ignored entirely when Gmail *is* configured. |
| `LOGIN_REISSUE_INTERVAL_SECONDS` | Override; minimum gap between two issued codes for one symbiot. Default `60`. A second `/login` inside the window keeps the existing code and emails nothing. |
| `MAX_VERIFY_ATTEMPTS` | Override; wrong guesses a single live code absorbs before the database burns it. Default `5`. |
| `RATE_LIMIT_ENABLED` | Override; the edge limiter. Default on; set `0`/`false`/`no`/`off` to disable. |
| `GC_ENABLED` | Override; the offline ontology duplicate-merge sweep, run in-process on a slow cadence. Default on; set `0`/`false`/`no`/`off` to disable. No external cron — it rides the app the way the intake reconcile sweep does. |
| `GC_SWEEP_INTERVAL_SECONDS` | Override; how often that sweep wakes. Default `86400` (daily) — duplicates accrue slowly and the merge never sits on the read path. |
| `GC_DISTANCE` | Override; the cosine-distance pre-filter the sweep uses to nominate near-twin type pairs before the model confirms them. Default `0.2`. Loosen to catch synonyms that embed further apart; the by-hand smoke (`test/qa/0002_*`) prints real distances to tune against. |
| `SCALEWAY_API_KEY`, `SCALEWAY_API_BASE_URL` | The primary generative provider (see [Models](#models-embedding-local-and-generation-cloud-with-a-local-fallback)). Base URL defaults to `https://api.scaleway.ai/v1`. An empty key just means the primary tier can't answer and the ladder falls through to Mistral. |
| `MISTRAL_API_KEY` | The fallback generative provider, reached at Mistral's own web API. Empty means that tier is skipped and the ladder falls through to the local Ollama model. |
| `FLAGSHIP_MODEL`, `MID_MODEL`, `SMALL_MODEL`, `REPLY_MODEL`, `RERANK_MODEL`, `MINT_MODEL`, `ENRICH_MODEL`, `TOOL_DECISION_MODEL`, `TOOL_CONFIRM_MODEL`, `CONVERSATION_COMPRESS_MODEL` | Override; the **seed defaults** for each generative role's assignment, grouped into three tiers. These are read *once*, to seed a role's row the first time the box boots with the `model_role` table empty; after that the assignment lives in the database and is changed with the **`/models` command**, not here (see [Models](#models-embedding-local-and-generation-cloud-with-a-local-fallback) and [Running fully local](#running-fully-local-no-cloud-api-no-gmail)). The flagship roles (`reply`, `enrich`, `conversation_compress`) default to `glm-5.2` (Scaleway); the cheaper roles (`tool_confirm`, `tool_decision`, `mint`, and the router's `rerank`) default to `gpt-oss-120b` (Scaleway). Setting one here only changes what a *fresh* box seeds; to change a running box, use `/models`. |
| `GENERATIVE_FALLBACK_MODEL`, `GENERATIVE_LOCAL_FALLBACK_MODEL` | Override; the two tiers a *cloud* (`scaleway`) call falls through to on an outage — `mistral-large-latest` then `qwen3.5:4b`. Unlike the role assignments above, these are ladder mechanics, not `/models`-managed. |

## Email (Gmail API)

This is the **hosted** login-delivery path. If you're running mailboxless (a home box with no Workspace), skip this entire section — leave `GMAIL_CREDENTIALS_FILE`/`GMAIL_SENDER` blank and the code goes to `OTP.txt` instead (see [Running fully local](#running-fully-local-no-cloud-api-no-gmail)). The setup below is only for a box that *does* email the code.

The Gmail `EmailClient` sends through the **Gmail API** as a Google Workspace mailbox, authenticated by a **GCP service account with domain-wide delegation** (no interactive OAuth, ideal for a headless service). The service account holds no mailbox of its own — it *impersonates* a real Workspace user (`GMAIL_SENDER`) and sends as them, using the narrow `gmail.send` scope (send only, no mailbox read). The account's JSON key lives on each box (gitignored, never committed); `GMAIL_CREDENTIALS_FILE` points at it. Until both are set the client refuses to send rather than pretend, and the test suite never needs them (it uses the fake). The Google libraries and the key are loaded lazily on the first send, so import and tests never touch them ([`email_client.py`](./services/email_client.py)).

### One-time setup

**In the GCP console** (the project that will own the service account):

1. **Enable the Gmail API**: APIs & Services → Library → "Gmail API" → **Enable**.
2. **Create a service account**: IAM & Admin → Service Accounts → **Create**. No IAM roles are needed — it authorises via delegation, not IAM.
3. **Create a JSON key** for it (Keys → Add key → JSON) and download it. This is `GMAIL_CREDENTIALS_FILE`.
4. Copy the service account's **Unique ID** (its OAuth2 **client ID**) from the Details tab — the next step needs it.

**In the Google Workspace Admin console** (for the sender's domain):

5. Security → Access and data control → **API controls** → **Manage Domain-Wide Delegation** → **Add new**.
6. Paste the **client ID** from step 4 and authorise exactly this scope:

   ```
   https://www.googleapis.com/auth/gmail.send
   ```

Domain-wide delegation can take a few minutes to propagate; a first send that `403`s right after authorising usually just needs a short wait and a retry.

**On each box** (local and server):

7. Place the JSON key somewhere gitignored — e.g. `gmail-credentials.json` in the repo root (covered by `.gitignore`) — and set `GMAIL_CREDENTIALS_FILE` to its path and `GMAIL_SENDER` to the Workspace mailbox to impersonate (a *real* user in the delegated domain).

## Tests

`pytest` against a dedicated test database, entirely on the fake email client (no network, no Gmail):

```bash
docker compose up -d        # Postgres must be reachable
uv run pytest               # runs everything under test/
```

The suite ([`test/`](./test)) is one assertion-per-behaviour over the identity flow: code issuance, verification, the anti-enumeration reply, recipient-smuggling, latest-code-only, and logout idempotency. A green suite proves the **state machine** — not the wire; real delivery against the live kernel is verified by hand.

## Code layout

The kernel's Python lives in two packages, split by a single rule: **`core/` is the foundation, `services/` is the work, and imports only ever point one way.** `core/` holds the primitives everything leans on — config, the Postgres pool and migration runner, request DTOs, the wire protocol, logging, the edge limiter — and knows nothing of any feature. `services/` holds the actual work built on those primitives — identity, intake and its worker pool, the ontology router (recall/embedding/re-rank) and its offline duplicate garbage collector, email, push. The boundary is real because the imports keep it: `services/` imports from `core/` freely, and `core/` never imports from `services/`, so the foundation stays self-contained and cheap to test in isolation. [`main.py`](./main.py) sits above both and wires them into a running app. The full rationale, with each module placed, is in [`doc/architecture.md`](./doc/architecture.md).

## Stack

- **Python 3.12+**
- **[FastAPI](https://fastapi.tiangolo.com/)** on **[uvicorn](https://www.uvicorn.org/)**
- **[Postgres](https://www.postgresql.org/)** via **[psycopg 3](https://www.psycopg.org/)** (no ORM); migrations are plain SQL run at startup
- **[uv](https://docs.astral.sh/uv/)** for dependency + virtualenv management (deps pinned in `uv.lock`)

## Deploy

The kernel runs on a server as a **systemd unit** — uvicorn bound to `127.0.0.1:9713` — behind **nginx** as a reverse proxy for `kernel.os-joy.com`, with TLS terminated by a **certbot** certificate. The uvicorn port is never exposed to the internet directly; nginx is the only door.

**One uvicorn process, on purpose — do not add `--workers`.** The intake worker pool and the reconcile sweep are background *threads* started inside the single app process (see the lifespan in `main.py`), not uvicorn process workers. Tune their concurrency with `WORKER_CONCURRENCY` (config default 4), never with uvicorn's `--workers`: forking N uvicorn processes would give each its own pool *and* its own reconcile sweep — N redundant sweeps and N×`WORKER_CONCURRENCY` threads all racing the same queue. It stays correct (claims are race-safe, moves are guarded) but it's wasteful and not the intent.

Deployment is by hand from a clone of this repo on the box. **The Joy's apps all live under `~/apps` on the server** — this clone must sit at `~/apps/kernel.os-joy.com`, because the systemd unit resolves the working directory and the venv from that path. Every deploy is one command:

```bash
./deploy.sh
```

`deploy.sh` runs under `set -euo pipefail`: it pulls `main`, runs `uv sync --frozen` against `uv.lock`, ensures the systemd unit is current, and restarts the service. On its first run it asks once for the server user (the account that owns the clone), remembers it in a gitignored `.deploy-user`, renders [`deploy/kernel-os-joy.service`](./deploy/kernel-os-joy.service) from that, and installs + enables the unit so the process survives crashes and reboots. Migrations and the symbiot seed run on startup, so a restart always brings the schema current — no separate migrate step in the deploy.

**One-time database + `.env` setup on the box.** The server's Postgres is native (not Docker) and reached over its unix socket with **peer auth** — `psql` works without a password because it's tied to your Ubuntu user. So create the database as that user once, and point `.env` at the socket (note how the URL differs from local — no host, no password):

```bash
createdb joy                                    # as the OS user the service runs as
cp .env.example .env
# in .env, set:
#   DATABASE_URL=postgresql:///joy              # three slashes = default socket = peer auth
#   SYMBIOT_EMAIL=<the symbiot's address>
#   KERNEL_SECRET=<a fresh secret>
#   GMAIL_CREDENTIALS_FILE=<path to the service-account key on this box>
#   GMAIL_SENDER=<the Workspace mailbox to send as>
```

`.env` is gitignored, so it never ships in the clone — it's created once per box and persists across deploys. The systemd unit runs uvicorn from the clone directory, so `core/config.py` finds `.env` there with no unit changes.

The nginx server block and the certbot certificate are one-time setup per host: nginx `proxy_pass`es `kernel.os-joy.com` to `http://127.0.0.1:9713`, and `certbot --nginx -d kernel.os-joy.com` issues the certificate and adds the HTTP→HTTPS redirect.

**The proxy must pass the real client IP**, or the rate limiter ([Rate limiting & abuse](#rate-limiting--abuse)) sees every request as coming from `127.0.0.1` and throttles the whole world as one bucket. Add to the `location` block that proxies to the kernel:

```nginx
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
```

The limiter trusts `X-Real-IP` (set here from nginx's own `$remote_addr`, not from anything the client sent), so this header is the one thing standing between per-caller limiting and a single global bucket.

## License

[MIT](./LICENSE)
