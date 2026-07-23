# Python Compute Service

Standalone HTTP service for Collabora Online / Collabora Office `=PY()` formulas.
coolwsd POSTs dumb JSON to `/v1/execute`; this process runs sandboxed Python and
returns JSON results. **It does not read `writeragent.json`.**

## Quick start

```bash
./compute_service/start.sh
# or
python compute_service/server.py --host 127.0.0.1 --port 8000
```

- `GET /health` → `{"status":"healthy"}` (no auth)
- `POST /v1/execute` → `{ "code", "data?", "mode?", "session_id?", "timeout_ms?" }`

## Authentication (shared Bearer secret)

coolwsd sends `Authorization: Bearer <security.python_compute.api_key>` when that
key is non-empty. Configure the **same** secret on the service:

| Source | How |
|--------|-----|
| Environment | `PYTHON_COMPUTE_API_KEY=...` |
| Key file | `PYTHON_COMPUTE_API_KEY_FILE=/path` or `--api-key-file /path` |
| Config JSON | `"auth": { "api_key_file": "..." }` (no raw key in the JSON file) |

There is **no** `--api-key` CLI flag (secrets in argv are visible in `ps`).

Rules:

- **No key configured** → `/v1/execute` is open (insecure; fine for local/dev/test).
- **Key configured** → `/v1/execute` requires an exact `Bearer <token>` match
  (`hmac.compare_digest`). Failures return HTTP 401 + `WWW-Authenticate: Bearer`.

Match coolwsd:

```xml
<python_compute>
  <enable type="bool">true</enable>
  <url>http://127.0.0.1:8000/v1/execute</url>
  <api_key>same-secret-as-service</api_key>
  <timeout_secs type="int">60</timeout_secs>
</python_compute>
```

## Configuration (no writeragent.json)

Precedence (later wins): defaults → `--config` / `PYTHON_COMPUTE_CONFIG` JSON →
`PYTHON_COMPUTE_*` env (plus legacy `HOST`/`PORT`) → `--host` / `--port` /
`--api-key-file`.

Example JSON: [`python-compute.example.json`](python-compute.example.json).

| Variable | Meaning |
|----------|---------|
| `HOST` / `PYTHON_COMPUTE_HOST` | Bind address (default `127.0.0.1`) |
| `PORT` / `PYTHON_COMPUTE_PORT` | Port (default `8000`) |
| `PYTHON_COMPUTE_API_KEY` | Shared Bearer secret |
| `PYTHON_COMPUTE_API_KEY_FILE` | Path to secret file (strip one trailing newline) |
| `PYTHON_COMPUTE_CONFIG` | Path to JSON config |
| `PYTHON_COMPUTE_MAX_BODY_BYTES` | Request body cap (default 32 MiB) |
| `PYTHON_COMPUTE_DEFAULT_TIMEOUT_SEC` | Default exec timeout (30) |
| `PYTHON_COMPUTE_MAX_TIMEOUT_SEC` | Clamp for request `timeout_ms` (600) |

Key file permissions: readable only by the service user (e.g. mode `0400`).

## Docker

```bash
docker build -f compute_service/Dockerfile -t python-compute .
docker run --rm -p 127.0.0.1:8000:8000 \
  -e PYTHON_COMPUTE_API_KEY_FILE=/run/secrets/key \
  -v /secure/key:/run/secrets/key:ro \
  python-compute
```

For cross-container networking set `HOST=0.0.0.0`. Add an API key when you want
Bearer auth (recommended outside pure local/dev).

The image copies only the sandbox import closure (`compute_service`,
`plugin/scripting`, `plugin/framework/constants.py`, `plugin/contrib/smolagents`) —
not WriterAgent Settings / `config.py`.

## CLI

```bash
python compute_service/server.py --help
python compute_service/server.py --config compute_service/python-compute.example.json \
  --api-key-file /run/secrets/python_compute_api_key
```

## Tests

```bash
pytest tests/compute_service/
```

See also [`docs/numpy-jailsafe.md`](../docs/numpy-jailsafe.md).
