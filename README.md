# kernel.os-joy.com

> the ghost in the shell

The kernel — the privileged core behind `kernel.os-joy.com`. The server-side counterpart to the [shell](../shell.os-joy.com): where the shell is the thin user-facing input layer, the kernel does the real work — intake/release, the buffer, identity, the Dead Man's Switch — mediating World ↔ symbiot.

It exposes a small HTTP surface: a health probe the shell's connectivity dot reads, a name on the door at `/`, a line-intake endpoint, the identity routes (`/login`, `/login/verify`, `/status`, `/logout`), and auto-generated API docs — see [Routes](#routes) below. Every response wears the same [envelope](#api-response-envelope). State lives in **Postgres**, reached over [psycopg](https://www.psycopg.org/) with **no ORM** — see [Database & migrations](#database--migrations).

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
| `POST /login` | Body `{ "address": "<email>" }`. Issues a one-time code **only** on an exact match to a registered symbiot, emailing it to them; otherwise does nothing. The reply is **identical either way** — `{ "msg": "if that address is registered, a login code is on its way", "data": null }` — so it's no oracle for who's registered (an unknown address, a blank one, and a recipient-smuggling string all get the same answer, and never an email). |
| `POST /login/verify` | Body `{ "address": "<email>", "code": "<code>" }`. Spends a valid (unconsumed, unexpired, latest-issued) code for that address's session: `{ "msg": "logged in", "data": { "token": "…", "email": "…" } }`. A wrong code answers `{ "msg": "that code didn't work — try again", "data": null }` and leaves the caller unauthed, free to retry. The address names whose code the guess is charged against: after `MAX_VERIFY_ATTEMPTS` wrong tries the database burns that code (see [Rate limiting & abuse](#rate-limiting--abuse)). |
| `GET /status` | Reads `Authorization: Bearer <token>`. Reports `{ "data": { "authed": true, "email": "…" } }` for a live session, else `{ "data": { "authed": false, "email": null } }`. |
| `POST /logout` | Reads `Authorization: Bearer <token>` and revokes that session. Idempotent — no token, or an already-revoked one, is a clean no-op: `{ "msg": "out", "data": { "authed": false } }`. |
| `GET /docs` | Interactive API docs (Swagger UI), generated for free by FastAPI from the route signatures. `GET /redoc` and the raw `GET /openapi.json` come along with it. |

## CORS

The shell reads `/health` from a different origin (`shell.os-joy.com`, or `localhost` in dev), so the kernel sends an explicit CORS allow-list — without it the browser blocks the read and the shell's dot reads offline even when the kernel is up. The allowed origins are pinned in `main.py` (`ALLOWED_ORIGINS`): the production shell plus the two local dev origins (`http://localhost:5173`, `http://127.0.0.1:5173`), `GET` (the health probe) and `POST` (sending a line to `/intake`), nothing wildcarded. The `POST` carries a JSON body, so the browser preflights it with an `OPTIONS` request — the CORS middleware answers that itself, which is why `OPTIONS` isn't in the method list. Add an origin there when a new front-end needs to read the kernel from the browser.

## Identity

The kernel seeds one human — **the symbiot** — from `SYMBIOT_EMAIL` at startup. The `symbiot` table and the `/login` lookup already hold and match many addresses; today exactly one is seeded, so supporting more is a matter of seeding rather than a schema change. Logging in is a one-time emailed code: `POST /login` with a registered symbiot's address issues a six-digit code and emails it; `POST /login/verify` spends that code for a session token. Two rules make it safe:

- **No enumeration oracle.** `/login` issues a code *only* on an exact match to a registered address, and its reply is byte-identical whether or not a match happened. An unknown address, a blank one, or a recipient-smuggling value (`a@x, b@y`, `a@x;b@y`, `a@x.evil`, `a+b@x`, an embedded newline) all get the same reply — and no email goes to anyone.
- **Nothing sensitive at rest.** Codes and session tokens are HMAC'd with `KERNEL_SECRET` before they touch the database, so a leaked table yields no usable code or token. Codes are single-use, short-lived, and only the latest-issued one verifies; sessions are revoked on `/logout`.

Email goes out through an `EmailClient` interface ([`email_client.py`](./services/email_client.py)) — the real one sends via the **Gmail API** (see [Email (Gmail API)](#email-gmail-api)); the test suite injects a fake that records messages instead of sending, so the whole flow is exercised without credentials.

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

## Ollama (local models)

The ontology routing that files a diary fact into structure leans on two **local** models, served by [Ollama](https://ollama.com) on the box — no external inference API, the same sovereignty (and cost) stance as the rest of the kernel. One turns text into vectors; the other judges how well a candidate type fits a fact. The routing depends on both:

- **`nomic-embed-text`** — the embedding model, 768-dimensional output. Every ontology definition and every incoming fact is embedded through it for the vector recall pass.
- **`qwen3.5:4b`** — the generative re-ranker. Prompted with the fact and each recalled candidate's definition, it scores in one call how well each candidate categorises the fact, and that score — not the raw vector distance, which only nominates — is what decides the match-or-mint call. It's run with thinking off and its output constrained to JSON (see the heads-up below).

Install Ollama per the [official instructions](https://ollama.com/download), then pull both models once per box:

```bash
ollama pull nomic-embed-text
ollama pull qwen3.5:4b
```

Ollama serves them on `http://127.0.0.1:11434` by default, and the kernel reaches them there. This is the same on local and server — Ollama runs natively on the host in both cases; it is *not* part of [`docker-compose.yml`](./docker-compose.yml).

> **Heads-up on `nomic-embed-text`:** Ollama clips this model's context to 2048 tokens by default (its native window is 8192) and truncates *silently*, so long text must be embedded with `num_ctx: 8192` set — otherwise its tail vanishes from the vector with no error. The model also expects `search_document:` / `search_query:` task prefixes for its distances to mean anything. Both live in how the pipeline calls Ollama, not in the schema.

> **Heads-up on `qwen3.5:4b`:** it's a thinking model, so the router calls it with `think: false` — the re-rank is a fast classification-style judgment, not a problem that wants a visible reasoning trace, and the trace would only cost tokens and latency. The call also sets `format: "json"` and `temperature: 0`, so the reply is a parseable object and the same fact scores the same way twice. These live in how the pipeline calls Ollama ([`services/llm.py`](./services/llm.py)), not in the schema.

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
| `GMAIL_CREDENTIALS_FILE`, `GMAIL_SENDER` | Gmail API service-account key path and the mailbox it sends as — see [Email (Gmail API)](#email-gmail-api). |
| `LOGIN_REISSUE_INTERVAL_SECONDS` | Override; minimum gap between two issued codes for one symbiot. Default `60`. A second `/login` inside the window keeps the existing code and emails nothing. |
| `MAX_VERIFY_ATTEMPTS` | Override; wrong guesses a single live code absorbs before the database burns it. Default `5`. |
| `RATE_LIMIT_ENABLED` | Override; the edge limiter. Default on; set `0`/`false`/`no`/`off` to disable. |

## Email (Gmail API)

The real `EmailClient` sends through the **Gmail API** as a Google Workspace mailbox, authenticated by a **GCP service account with domain-wide delegation** (no interactive OAuth, ideal for a headless service). The service account holds no mailbox of its own — it *impersonates* a real Workspace user (`GMAIL_SENDER`) and sends as them, using the narrow `gmail.send` scope (send only, no mailbox read). The account's JSON key lives on each box (gitignored, never committed); `GMAIL_CREDENTIALS_FILE` points at it. Until both are set the client refuses to send rather than pretend, and the test suite never needs them (it uses the fake). The Google libraries and the key are loaded lazily on the first send, so import and tests never touch them ([`email_client.py`](./services/email_client.py)).

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

The kernel's Python lives in two packages, split by a single rule: **`core/` is the foundation, `services/` is the work, and imports only ever point one way.** `core/` holds the primitives everything leans on — config, the Postgres pool and migration runner, request DTOs, the wire protocol, logging, the edge limiter — and knows nothing of any feature. `services/` holds the actual work built on those primitives — identity, intake and its worker pool, the ontology router (recall/embedding/re-rank), email, push. The boundary is real because the imports keep it: `services/` imports from `core/` freely, and `core/` never imports from `services/`, so the foundation stays self-contained and cheap to test in isolation. [`main.py`](./main.py) sits above both and wires them into a running app. The full rationale, with each module placed, is in [`doc/architecture.md`](./doc/architecture.md).

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
