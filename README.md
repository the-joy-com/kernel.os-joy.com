# kernel.os-joy.com

> the ghost in the shell

The kernel — the privileged core behind `kernel.os-joy.com`. The server-side counterpart to the [shell](../shell.os-joy.com): where the shell is the thin user-facing input layer, the kernel does the real work — intake/release, the buffer, identity, the Dead Man's Switch — mediating World ↔ symbiot.

**First slice:** one endpoint, `GET /health`, answering `200` so the shell's connectivity dot has something real to probe. Everything else lands on top of this round trip, never beside it.

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
| `GET /docs` | Interactive API docs (Swagger UI), generated for free by FastAPI from the route signatures. `GET /redoc` and the raw `GET /openapi.json` come along with it. |

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

## License

[MIT](./LICENSE)
