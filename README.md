# kernel.os-joy.com

> the ghost in the shell

The kernel — the privileged core behind `kernel.os-joy.com`. The server-side counterpart to the [shell](../shell.os-joy.com): where the shell is the thin user-facing input layer, the kernel does the real work — intake/release, the buffer, identity, the Dead Man's Switch — mediating World ↔ symbiot.

It exposes a small HTTP surface: a health probe the shell's connectivity dot reads, a name on the door at `/`, and auto-generated API docs — see [Routes](#routes) below. Every response wears the same [envelope](#api-response-envelope).

## API response envelope

Every response the kernel returns wears the same shape:

```json
{ "msg": "string", "data": null }
```

- `msg` — a human-legible line about what happened (`"ok"`, an error reason, a status word).
- `data` — the payload to act on: a JSON array, a JSON object, or `null` when there's nothing to carry.

So `GET /health` answers:

```json
{ "msg": "ok", "data": { "version": "0.0.1" } }
```

## Routes

| Method & path | What it answers |
| --- | --- |
| `GET /` | A name on the door — `{ "msg": "the ghost in the shell", "data": { "version": "0.0.1" } }` — so the bare host is legible instead of a 404. |
| `GET /health` | The probe the shell's connectivity dot reads — `{ "msg": "ok", "data": { "version": "0.0.1" } }`. |
| `POST /intake` | Takes one line off the shell's prompt — body `{ "line": "<text>" }` — and acknowledges it with `{ "msg": "copy", "data": null }`. `"copy"` means *received*, not *stored*: the line is dropped (holding it in the buffer is a separate concern that layers on top of this round trip). The `line` is required, non-empty, and capped at 4096 chars; anything else is a `422`. The request shape is validated by the `IntakeRequest` DTO in [`dtos.py`](./dtos.py). |
| `GET /docs` | Interactive API docs (Swagger UI), generated for free by FastAPI from the route signatures. `GET /redoc` and the raw `GET /openapi.json` come along with it. |

## CORS

The shell reads `/health` from a different origin (`shell.os-joy.com`, or `localhost` in dev), so the kernel sends an explicit CORS allow-list — without it the browser blocks the read and the shell's dot reads offline even when the kernel is up. The allowed origins are pinned in `main.py` (`ALLOWED_ORIGINS`): the production shell plus the two local dev origins (`http://localhost:5173`, `http://127.0.0.1:5173`), `GET` (the health probe) and `POST` (sending a line to `/intake`), nothing wildcarded. The `POST` carries a JSON body, so the browser preflights it with an `OPTIONS` request — the CORS middleware answers that itself, which is why `OPTIONS` isn't in the method list. Add an origin there when a new front-end needs to read the kernel from the browser.

## Stack

- **Python 3.12+**
- **[FastAPI](https://fastapi.tiangolo.com/)** on **[uvicorn](https://www.uvicorn.org/)**
- **[uv](https://docs.astral.sh/uv/)** for dependency + virtualenv management (deps pinned in `uv.lock`)

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

2. **Run the kernel locally** — uvicorn bound to localhost:

   ```bash
   uv run uvicorn main:app --host 127.0.0.1 --port 9713 --reload
   ```

   (`--reload` is for development; drop it in production.)

3. **Confirm the round trip:**

   ```bash
   curl http://127.0.0.1:9713/health
   # {"msg":"ok","data":{"version":"0.0.1"}}
   ```

## Managing dependencies

```bash
uv add <package>            # add a dependency (updates pyproject.toml + uv.lock)
uv remove <package>         # drop one
uv sync                     # bring venv in line with uv.lock
```

Commit `pyproject.toml` and `uv.lock`; never commit `venv/`.

## Deploy

The kernel runs on a server as a **systemd unit** — uvicorn bound to `127.0.0.1:9713` — behind **nginx** as a reverse proxy for `kernel.os-joy.com`, with TLS terminated by a **certbot** certificate. The uvicorn port is never exposed to the internet directly; nginx is the only door.

Deployment is by hand from a clone of this repo on the box. **The Joy's apps all live under `~/apps` on the server** — this clone must sit at `~/apps/kernel.os-joy.com`, because the systemd unit resolves the working directory and the venv from that path. Every deploy is one command:

```bash
./deploy.sh
```

`deploy.sh` runs under `set -euo pipefail`: it pulls `main`, runs `uv sync --frozen` against `uv.lock`, ensures the systemd unit is current, and restarts the service. On its first run it asks once for the server user (the account that owns the clone), remembers it in a gitignored `.deploy-user`, renders [`deploy/kernel-os-joy.service`](./deploy/kernel-os-joy.service) from that, and installs + enables the unit so the process survives crashes and reboots.

The nginx server block and the certbot certificate are one-time setup per host: nginx `proxy_pass`es `kernel.os-joy.com` to `http://127.0.0.1:9713`, and `certbot --nginx -d kernel.os-joy.com` issues the certificate and adds the HTTP→HTTPS redirect.

## License

[MIT](./LICENSE)
