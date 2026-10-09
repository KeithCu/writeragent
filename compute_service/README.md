# Python Compute Service

Standalone HTTP service for Collabora Online / Collabora Office `=PY()` formulas.
coolwsd POSTs to `/v1/execute`; this process runs sandboxed Python and returns
JSON results. **It does not read `writeragent.json`.**

**Both ingress formats are supported now** (dispatch on `Content-Type`). Peel of
a single JSON object is **today’s Collabora/kit contract** and is **transitional**.
Multipart is the long-term preferred kit↔compute shape. See
[HTTP ingress: peel vs multipart](#http-ingress-peel-vs-multipart).

## Quick start

```bash
./compute_service/start.sh
# or
python compute_service/server.py --host 127.0.0.1 --port 8000
```

- `GET /health` → `{"status":"healthy","service":"python-compute","version":"<version>"}` (no auth required)
- `POST /v1/execute[?session_id=<id>]` — `application/json` (peel one object) **or** `multipart/form-data` (`meta` + raw `code` + optional `init_script` + optional `data`). Same execute fields; same egress. Peel is compatibility; multipart is the preferred wire.
  (`init_script` runs **once** per worker: shared uses `{session_id}:init`, isolated uses a hash of the script. Later cells are seeded from that namespace; a changed script replaces the snapshot.)
- `POST /v1/session/reset?session_id=<id>` — optional `{ "id?" }` → `{ "id?", "status": "ok" }`. Query-only `session_id` (same L7 sticky reason as execute). Idempotent: unknown / already-gone is still `ok`. Caller is coolwsd on DocumentBroker destroy / last view leave (service-side only today; Online still hard-codes `isolated`).
- Docker (hardened run flags): `./compute_service/start-docker.sh` — see **Production / Collabora Online** below.

---

## API & Wire Protocol

### 1. Health Endpoint (`GET /health`)

Unauthenticated health probe suitable for Kubernetes/Docker liveness and readiness checks.
Always unauthenticated even when Bearer authentication is configured for execution.
`version` is `compute_service.__version__`, the compute service package version.

- **Request**: `GET /health`
- **Response**: `200 OK`
  ```json
  {
    "status": "healthy",
    "service": "python-compute",
    "version": "<compute_service.__version__>"
  }
  ```

### 2. Execution Endpoint (`POST /v1/execute[?session_id=<id>]`)

Evaluates sandboxed Python code and emits kit-safe dumb JSON (`allow_nan=False`, `NaN`/`Inf` → `null`).

- **Sticky Routing via URL Query Parameter**: For stateful calculations (`mode="shared"`), `session_id` is supplied as a URL query parameter (`POST /v1/execute?session_id=<id>`) so Layer 7 routers, ingress proxies, and load balancers can route stickily without buffering and parsing JSON request bodies:
  - **HAProxy**: `balance url_param session_id`
  - **NGINX**: `hash $arg_session_id consistent;`
  - **Envoy**: `hash_policy: [query_parameter: { name: "session_id" }]`
  - **AWS ALB**: Query string routing conditions on `session_id`.

See **[HTTP ingress: peel vs multipart](#http-ingress-peel-vs-multipart)** for both
request shapes, why multipart exists, and the plan to retire peel.

- **Success Response (`200 OK`)**:
  ```json
  {
    "id": "req-123",
    "status": "ok",
    "result": 60.0,
    "stdout": ""
  }
  ```
  *(If Matplotlib plots are generated, they are returned in `images: [{"format": "png", "data_b64": "..."}]`)*

- **Evaluation Error Response (`200 OK`)**:
  Evaluation errors (e.g. `SyntaxError`, `ZeroDivisionError`, unauthorized imports) return `200 OK` with `status: "error"` so HTTP transport is distinguished from evaluated code errors:
  ```json
  {
    "id": "req-123",
    "status": "error",
    "error": "SyntaxError: invalid syntax (<string>, line 1)",
    "stdout": "",
    "message": "SyntaxError: invalid syntax (<string>, line 1)"
  }
  ```

- **Session Reset on Lost Kernel (`session_reset: true`)**:
  If a shared session's worker process crashed, was recycled, was killed (e.g. by `SIGKILL` on an unrecoverable timeout), or was evicted by idle TTL, the pool loses the session state. The subsequent call with that same `session_id` transparently lands on a fresh worker kernel and includes `"session_reset": true` in the response JSON. A sticky call already waiting on that worker still receives `session_reset` when the TTL reset finishes. A request that fails before the cell runs (`PAYLOAD_TOO_LARGE`, `QUEUE_TIMEOUT`, `WORKER_SPAWN_FAILED`, `WORKER_PIPE_BROKEN`) does not consume that flag:
  ```json
  {
    "id": "req-123",
    "status": "ok",
    "result": 42.0,
    "session_reset": true
  }
  ```
  This signals to the caller (e.g. coolwsd or spreadsheet runtime) that prior kernel variables were lost and initialization scripts or antecedent cells may need to be re-evaluated.

#### HTTP ingress: peel vs multipart

Both formats work **now**. Dispatch is strictly `Content-Type`.

| `Content-Type` | Body | Host does | Role |
|----------------|------|-----------|------|
| `application/json` (or missing) | One JSON object `{id?, code, data?, mode?, timeout_ms?, init_script?}` | Peel small keys; **forward the raw `data` value bytes** (no `json.loads` of the grid) | **Today’s Collabora/kit contract.** Compatibility. The walker/peel exists so we do not deserialize the nested grid. |
| `multipart/form-data` (or other `multipart/*`) | Parts: `meta` (`application/json`: `id?`, `mode?`, `timeout_ms?`), `code` (raw UTF-8), optional `init_script` (raw UTF-8), optional `data` (raw JSON bytes) | Boundary-scan. `json.loads` **meta only** (64 KiB cap). Byte-cap and UTF-8-decode `code` and `init_script`. **Forward `data` bytes untouched** | **Preferred kit↔compute shape.** Control JSON, source, and the grid are separate MIME parts. |

`session_id` stays on the URL (`?session_id=...`) for L7 affinity — not in the request body (`meta` or the peel object). Reserved namespaces (ending with `:init` or starting with `isolated:`) are rejected with `400 Bad Request`.

**Why multipart:** cleaner framing (tiny control JSON vs source vs the grid blob). No custom JSON walker, and no JSON unescape of formula source on the HTTP host. Same “forward bytes, don’t re-serialize” win for the grid. That is the long-term wire we want between kit and this service.

**Multipart subset** (kit↔compute, not a general MIME stack): named parts only (`meta`, `code`, `init_script`, `data`), any order. Delimiters are `--` + the `Content-Type` boundary, at the start of the body or after a linebreak; the introducing linebreak is not part of the body (CRLF or LF). Preamble and epilogue are ignored. `Content-Transfer-Encoding` may be omitted, `7bit`, `8bit`, or `binary`. `base64` and `quoted-printable` are rejected so the host never decodes a payload. `meta` must not contain `code`, `data`, `data_json`, or `init_script`. The encoder picks a boundary that does not occur in any part body.

**Migration:** support both until Collabora/kit switches to multipart. After that, retire the single-JSON peel (the walker). Peel is **transitional compatibility**, not the forever design. No kit date — eventually switch kit to multipart, then delete peel.

**Unchanged either way:** worker dumps kit JSON once (`result_json`); the HTTP host forwards those bytes (no host re-`dumps` of a large result). LibrePy desktop `=PY()` stays Pickle5 + `split_grid` both ways and never uses this HTTP hop.

### 3. Session Reset Endpoint (`POST /v1/session/reset?session_id=<id>`)

Drops the shared sandbox and init companion for one workbook kernel. Reuses `FormulaPool.reset_session` → worker `action: reset_session` → LibrePy `reset_sandbox_session` (no second reset path).

Intended caller is **coolwsd on DocumentBroker destroy / last view leave**. This endpoint lands ahead of Online shared-kernel work; Collabora Online still hard-codes `mode: isolated` and does not call reset yet.

- **Sticky routing:** `session_id` is **URL query only** (`POST /v1/session/reset?session_id=<id>`), same L7 reason as `/v1/execute` — the request must hit the host that owns the kernel. Reject if `session_id` is only in the JSON body (or present in the body at all), or uses reserved prefixes/suffixes (`:init`, `isolated:`).
- **Request body** (optional; empty body is fine):
  ```json
  { "id": "corr-1" }
  ```
  `id` is a correlation echo only. It is not a session identifier.
- **Success (`200 OK`)** — **idempotent**: unknown / already-gone still `ok`:
  ```json
  { "id": "corr-1", "status": "ok" }
  ```
- **Errors:** `400` missing/empty query `session_id`, `session_id` in the JSON body, or reserved `session_id`; `401` auth (case-insensitive Bearer); worker reset failure → `{ "id?", "status": "error", "error": "..." }` with HTTP `500`; worker lease failure → `{ "id?", "status": "error", "code": "WORKER_POOL_BUSY", "error": "..." }` with HTTP `503`.
- The supervisor drops its session map only when the worker reset returns `status: ok`. A failed reset is logged and the map stays: that process may still hold the namespace. Forgetting the id would make the next sticky cell look new on a kernel that is not empty.
- Idle TTL (`shared_kernel_ttl_sec`) remains the safety net if reset is missed. Do not remove it.

### 4. Vision & OCR Endpoint (`POST /v1/vision`)

Evaluates heavy document/image OCR and layout structure extraction in a dedicated, isolated worker subprocess pool. Supports an in-memory image buffer (`image_b64`) or a server-local path (`file_path`). Sending both is `400` with `INVALID_REQUEST`.

- **Request Schema (Option A: In-Memory Base64 Buffer)**:
  ```json
  {
    "id": "ocr-123",
    "helper": "extract_text",
    "image_b64": "<base64-encoded-image>",
    "params": {
      "engine": "docling",
      "fallback": true
    },
    "timeout_ms": 60000
  }
  ```

- **Request Schema (Option B: Server Filesystem Path)** — denied unless the resolved path is under `ocr.allow_paths` (default deny). The worker opens the file and checks the opened path against the same prefix list. On Linux that is `/proc/self/fd` for the descriptor, so a symlink swapped in after the name check cannot leave the allowlist. macOS and Windows have no `/proc`; realpath of that string used to deny every file, so those platforms realpath the path that was opened (a swap that landed before `open` returns is still rejected). An authenticated client cannot read an arbitrary file. Files larger than 32 MiB return HTTP 413 with `FILE_TOO_LARGE`. A missing file, a directory, an empty path, or a read error on that path returns HTTP 400 (`FILE_NOT_FOUND`, `NOT_A_FILE`, `INVALID_FILE_PATH`, `FILE_READ_ERROR`):
  ```json
  {
    "id": "ocr-124",
    "helper": "extract_structure",
    "file_path": "/shared/scans/invoice_2026.png",
    "params": {
      "table_mode": "accurate"
    },
    "timeout_ms": 60000
  }
  ```

- **Supported Helpers**:
  - `extract_text`: Extracts clean plaintext or Markdown from the image using Docling (or PaddleOCR fallback).
  - `extract_structure`: Extracts structured document hierarchy, table grids, and formatted sections.

- **Success Response (`200 OK`)**:
  ```json
  {
    "id": "ocr-123",
    "status": "ok",
    "text": "Extracted text content...",
    "format": "markdown",
    "metrics": { "duration_ms": 450.2 }
  }
  ```

### 5. HTTP Status Codes & Error Semantics

| HTTP Status | Condition | Response Payload Shape |
| :--- | :--- | :--- |
| **`200 OK`** | Evaluation completed (success or runtime evaluation error); session reset succeeded (including unknown / already-gone). A formula `WORKER_EXECUTION_ERROR` inside forwarded `result_json` stays 200 so the sheet shows the text | `{"id"?: "...", "status": "ok"\|"error", "result"\|"error": ...}` |
| **`400 Bad Request`** | Malformed JSON or multipart, missing `code`, `code` or `init_script` longer than `max_code_chars` (`CODE_TOO_LARGE`; peel and multipart), invalid UTF-8 in a multipart source part, `mode` other than `isolated` or `shared`, missing/empty reset `session_id`, `session_id` in the request body, reserved `session_id` namespace (`:init` or `isolated:`), a non-finite `id` (`NaN`, `Infinity`, `1e9999`) on execute, vision, or reset, vision `file_path` not under `ocr.allow_paths` (`FILE_PATH_DENIED`), a vision path that is empty, missing, not a regular file, or unreadable (`INVALID_FILE_PATH`, `FILE_NOT_FOUND`, `NOT_A_FILE`, `FILE_READ_ERROR`), vision `image_b64` and `file_path` together (`INVALID_REQUEST`), vision `params` that is not an object, a vision image that is not valid base64 (`INVALID_BASE64`, `INVALID_IMAGE`, `MISSING_IMAGE_SOURCE`), or `Transfer-Encoding: chunked` on any POST | `{"id"?: "...", "status": "error", "code"?: "...", "error": "..."}` |
| **`401 Unauthorized`** | Missing or incorrect `Authorization: Bearer <secret>` on `/v1/execute`, `/v1/session/reset`, or `/v1/vision` | `{"status": "error", "error": "Unauthorized"}` + `WWW-Authenticate: Bearer` |
| **`404 Not Found`** | Unknown path or unsupported HTTP method | Plaintext `Not Found` |
| **`413 Payload Too Large`**| Request body exceeds `max_body_bytes`, the execute frame exceeds the IPC limit (`PAYLOAD_TOO_LARGE`), the result frame does (`RESULT_TOO_LARGE`), or a vision image is larger than 32 MiB (`FILE_TOO_LARGE`) | `{"status": "error", "code"?: "RESULT_TOO_LARGE", "error": "..."}` |
| **`501 Not Implemented`** | `/v1/vision` when OCR is off (`VISION_SERVICE_DISABLED`, `ocr_workers=0`) | `{"id"?: "...", "status": "error", "code": "VISION_SERVICE_DISABLED", "error": "..."}` |
| **`503 Service Unavailable`** | `/v1/execute`, `/v1/session/reset`, or `/v1/vision` when the cell never ran and a proxy may retry (`WORKER_POOL_BUSY`, `VISION_POOL_BUSY`, `SERVICE_SHUTDOWN`, `WORKER_SPAWN_FAILED`, `WORKER_PIPE_BROKEN`, `QUEUE_TIMEOUT`, `VISION_UNAVAILABLE`). A full permit set queues until the request deadline (`default_timeout_sec`, 30s for execute and session reset; `ocr_timeout_sec` for vision). 503 is that timeout. A few milliseconds of queueing is the steady state at 200–400 rps and stays HTTP 200. Sticky execute and session reset share a listener cap. A vision request that never leased a worker is `VISION_POOL_BUSY` at 503, same as the route's accept-deadline pre-check. Eval errors inside `result_json`, and `EXECUTION_TIMEOUT`, stay HTTP 200. coolwsd may map 503 to `#N/A`. | `{"id"?: "...", "status": "error", "code": "...", "error": "..."}` |
| **`500 Internal Server Error`**| Unhandled server exception, JSON encoding failure, worker-side session reset error, a worker that died or returned no frame (`WORKER_CRASHED`, `EMPTY_RESPONSE`), or a dict-shaped internal fault (`VISION_WORKER_ERROR`, `WORKER_EXECUTION_ERROR`). A `WORKER_EXECUTION_ERROR` wrapped in forwarded `result_json` stays HTTP 200 | `{"id"?: "...", "status": "error", "code"?: "...", "error": "..."}` |

---

## Authentication (shared Bearer secret)

coolwsd sends `Authorization: Bearer <security.python_compute.api_key>` when that
key is non-empty. Configure the **same** secret on the service:

| Source | How |
|--------|-----|
| Environment | `PYTHON_COMPUTE_API_KEY=...` |
| Key file | `PYTHON_COMPUTE_API_KEY_FILE=/path` or `--api-key-file /path` |
| Config JSON | `"auth": { "api_key_file": "..." }`. A raw `api_key` in the file is a startup error. |

There is **no** `--api-key` CLI flag (secrets in argv are visible in `ps`).

Rules:

- **Loopback and no key** → `/v1/execute` and `/v1/session/reset` are open (local dev/test only).
- **Host header validation in keyless mode** → When running without an API key, the server rejects requests with non-loopback `Host` headers with `403 Forbidden` (for example, accessing via a Docker service name such as `http://python-compute:8000` gets 403). Only loopback Host headers (`localhost`, `127.0.0.1`, `[::1]`) are accepted.
- **Empty bind address (`--host ""`)** → Binding `--host ""` binds loopback interfaces only.
- **Any other bind without a key** → `load_settings` refuses to start. This includes `0.0.0.0` and `::`. The image entrypoint checks the same case before exec.
- **Key configured** → `/v1/execute`, `/v1/session/reset`, and `/v1/vision` require a `Bearer <token>` match
  (`hmac.compare_digest` on the UTF-8 bytes). The scheme prefix `Bearer ` is matched case-insensitively per RFC 7235 (`bearer `, `BEARER `, etc.). A non-ASCII token or key is a 401 or a match, not a dropped connection. Failures return HTTP 401 + `WWW-Authenticate: Bearer`.
- `PYTHON_COMPUTE_API_KEY` is not stripped. A key file drops one trailing newline and keeps surrounding spaces. The same secret text is the same bytes from either source.
- **Unknown JSON keys** are a startup error. A raw `api_key` in the file is a startup error. Aliases `max_workers` and `session_ttl_sec` are accepted.

Match coolwsd (`coolwsd.xml`):

```xml
<python_compute>
  <enable type="bool">true</enable>
  <url>http://127.0.0.1:8000/v1/execute</url>
  <api_key>same-secret-as-service</api_key>
  <timeout_secs type="int">60</timeout_secs>
</python_compute>
```

---

## Configuration & Ops (no writeragent.json)

Precedence (later wins): defaults → `--config` / `PYTHON_COMPUTE_CONFIG` JSON →
`PYTHON_COMPUTE_*` env → `--host` / `--port` / `--api-key-file`.

Example JSON: [`python-compute.example.json`](python-compute.example.json).

| Variable | Meaning | Default |
|----------|---------|---------|
| `PYTHON_COMPUTE_HOST` | Bind address (loopback default) | `127.0.0.1` |
| `PYTHON_COMPUTE_PORT` | Listening port | `8000` |
| `PYTHON_COMPUTE_API_KEY` | Shared Bearer secret (not stripped) | `""` |
| `PYTHON_COMPUTE_API_KEY_FILE` | Path to secret file (strip one trailing newline) | `""` |
| `PYTHON_COMPUTE_CONFIG` | Path to JSON config | `""` |
| `PYTHON_COMPUTE_LOG_LEVEL` | Log verbosity (`DEBUG`, `INFO`, `WARN`, `ERROR`) | `INFO` |
| `PYTHON_COMPUTE_MAX_BODY_BYTES` | Request body cap | `33554432` (32 MiB) |
| `PYTHON_COMPUTE_DEFAULT_TIMEOUT_SEC` | Default execution timeout in seconds | `30` |
| `PYTHON_COMPUTE_MAX_TIMEOUT_SEC` | Upper bound clamp for `timeout_ms` | `600` |
| `PYTHON_COMPUTE_WORKERS` | Number of formula worker subprocesses. `PYTHON_COMPUTE_MAX_WORKERS` is an accepted alias. | `2` |
| `PYTHON_COMPUTE_WORKER_MAX_TASKS` | Tasks before recycling formula worker | `500` |
| `PYTHON_COMPUTE_SHARED_KERNEL_TTL_SEC` | Session idle timeout in seconds before eviction. Finite and >= 0; Infinity, NaN, and `1e9999` are rejected. `0` disables eviction (the reaper does not start). `PYTHON_COMPUTE_SESSION_TTL_SEC` is an accepted alias. | `3600` (1 hour) |
| `PYTHON_COMPUTE_IDLE_WORKER_TTL_SEC` | Worker process idle timeout in seconds before termination. Finite and >= 0; Infinity, NaN, and `1e9999` are rejected. `0` disables eviction (the reaper does not start). | `3600` (1 hour) |
| `PYTHON_COMPUTE_OCR_WORKERS` | Dedicated OCR/Vision worker subprocesses | `0` (disabled by default) |
| `PYTHON_COMPUTE_OCR_TIMEOUT_SEC` | OCR/Vision execution timeout in seconds | `60` |
| `PYTHON_COMPUTE_OCR_MAX_TASKS` | Tasks before recycling OCR worker process | `100` |
| `PYTHON_COMPUTE_MAX_CODE_CHARS` | Max `code` string length | `262144` |
| `PYTHON_COMPUTE_OCR_ALLOW_PATHS` | `{os.pathsep}`-separated prefixes allowed for vision `file_path` (empty = deny) | `""` |

Key file permissions: readable only by the service user (e.g. mode `0400`).

### Production / Collabora Online

coolwsd is the only hop that should reach this process. Bind loopback, set the same Bearer secret as `security.python_compute.api_key`, and do **not** mount a host venv or docker.sock.

`file_path` on `/v1/vision` is **denied** unless `ocr.allow_paths` is set. The worker resolves the path and checks the same prefixes again before `open`, so a symlink inside an allowed directory cannot point outside. Prefer `image_b64`. A vision call waits for a free OCR worker until its timeout, then returns HTTP 503 with `VISION_POOL_BUSY` in the JSON body. A call that exceeds its own timeout returns `EXECUTION_TIMEOUT` and leaves the process up while the late frame is discarded; a second timeout then kills it. On Windows that drain does not see the late frame, so an OCR timeout does not keep the warm process and the next call reloads the model. Linux reuses the process.

`--network=none` cannot be combined with `-p` (published ports need a network namespace). Publish to loopback on the host, or use an internal bridge **without a default route**. Tenant sockets still fail via the AST sandbox plus missing egress.

`load_settings` refuses a non-loopback bind that has no API key, so `python compute_service/server.py --host 0.0.0.0` fails the same way as the image. `./compute_service/start-docker.sh` still requires `PYTHON_COMPUTE_API_KEY` or `PYTHON_COMPUTE_API_KEY_FILE` before it publishes a port. When the file variable is set, the script mounts that host file read-only at `/run/secrets/python_compute_api_key` and sets the container's `PYTHON_COMPUTE_API_KEY_FILE` to that path. Loopback with no key remains allowed.

```bash
PYTHON_COMPUTE_API_KEY=same-secret-as-coolwsd ./compute_service/start-docker.sh
# or:
docker build -f compute_service/Dockerfile -t python-compute .
docker run --read-only --tmpfs /tmp:rw,size=64m,mode=1777 \
  --memory=512m --memory-swap=512m --cpus=1 --pids-limit=256 \
  --security-opt no-new-privileges --cap-drop ALL \
  -p 127.0.0.1:8000:8000 \
  -e PYTHON_COMPUTE_API_KEY=same-secret-as-coolwsd \
  python-compute
```

Shared `mode=shared` **must** use a per-document `session_id` query parameter (`?session_id=<id>`) (not a user id). coolwsd should `POST /v1/session/reset?session_id=<id>` on DocumentBroker destroy / last view leave (caller not shipped; Online still hard-codes isolated). Idle TTL (`shared_kernel_ttl_sec`) remains the safety net if reset is missed.

---

## Lifecycle & Signal Handling

- **Graceful Shutdown**: The service traps `SIGTERM` and `SIGINT`.
- When `SIGTERM` is received (from Kubernetes pod termination or `docker stop`), the server stops accepting on a background thread. It immediately closes listening sockets so new connections are refused rather than hanging, then waits up to 30s for requests already taken to drain, terminates worker subprocesses, and cleans up resources. A cell still running at the end of that wait is abandoned.

---

## Two-Tier Isolated Process Pool Architecture

The Python Compute Service is structured as a resilient master HTTP server fronting two specialized subprocess worker pools:

### 1. Master HTTP Router (~20MB RAM)
- Ultra-thin network process that accepts HTTP connections, verifies Bearer authentication tokens, and forwards each job as a **length-prefixed Pickle 5 envelope** on the worker's stdin pipe. Large formula `data` / results are **raw JSON bytes** inside that envelope (not a second codec stage).
- **HTTP listener**: Accept pool is $\max(\max(8, T + 4),\; W + V + W + 2)$, where $T$ is formula workers plus OCR workers and $V$ is $\max(1, \text{ocr workers})$. That is one isolated permit and one sticky permit per formula worker, the vision permit (present even when OCR is off), and two threads left for `GET /health`. The default of 2 formula workers and OCR off stays at 8 listener threads and 3 sticky slots. With 1–2 formula workers the 8-thread floor leaves extra sticky slots. With 3 or more formula workers and OCR off, the sticky cap is $W$, so one workbook waiting on its worker does not block the others past their own deadline. An explicit `max_threads` on the server class (tests, benchmark) still uses $\max(8, n + 4)$ and does not add the sticky term again. `/v1/vision` has its own semaphore sized to the vision pool. Each permit waits until the request deadline instead of failing immediately. The worker pool lease (`lease_specific` / `lease_any`) is the gate for worker execution and uses that same deadline.
- **HTTP/1.0 & Transfer Semantics**: The server operates on HTTP/1.0 with no keep-alive or chunked transfer encoding. A chunked POST (`Transfer-Encoding: chunked`) returns `400 Bad Request` on every route. `/v1/execute` and `/v1/vision` also return 400 when `Content-Length` is missing. `/v1/session/reset` treats a missing or zero `Content-Length` as an empty body. Total read deadlines prevent slow clients from exhausting listener threads.
- **Sticky Session Semaphores**: Sticky sessions queue for their designated worker inside the pool. The route semaphore allows one in-flight sticky execute or session reset per formula worker (three on the default 2-worker pool, because of the 8-thread floor). Extra sticky calls wait until the request deadline, then return 503.
- **Unbreakable Design**: The master process never executes user code directly, ensuring that user errors, native crashes, or memory spikes cannot destabilize the HTTP service.

### Internal wire: JSON-forward

LibrePy desktop `=PY()` is a **different product** (Pickle5 + `split_grid`, no HTTP hop). Do not regress that path when changing compute. Desktop detail: [`docs/scripting/numpy-serialization.md`](../docs/scripting/numpy-serialization.md).

Kit HTTP is MIME-dispatched — [peel vs multipart](#http-ingress-peel-vs-multipart). Both are live. Peel of one JSON object is **today’s Collabora contract** and **goes away** once kit speaks multipart. Multipart is the preferred wire (control JSON, raw source parts, raw grid; boundary scan, no email parser). Egress does not care which ingress you used.

The host is a proxy: auth, sticky routing, timeouts, worker lease. One deserialize of `data` happens on the **worker**. Small control fields may be deserialized on the host. The expensive rule is **no host re-serialize** of large ingress or egress.

**Compute JSON-forward** ([`json_forward.py`](json_forward.py)):

- Peel (transitional): scan the top-level JSON object; `json.loads` only isolated small values. The `data` value is sliced from the request body unchanged. A kit `data_json` string field is also accepted (inner text becomes the forwarded blob). Retire this walker after kit switches to multipart.
- Multipart (preferred): `parse_multipart_execute` boundary-scans the body and `json.loads` only the small `meta` part (`id`, `mode`, `timeout_ms`; 64 KiB cap). `code` and `init_script` are raw UTF-8 parts. Peel passes those fields as strings. Both are character-capped with `max_code_chars` (not byte length) and both use the same mode check. `data` is `data_json` as-is. `meta` must not nest `code`, `data`, `data_json`, or `init_script`.
- Envelope: `{code, mode, timeout_sec, session_id, init_script, wire: "json_forward", data_json: <bytes>}`. That is the only payload. `wire` other than `json_forward` is rejected. Pickle copies the byte buffer; it does not walk the JSON tree and there is no `split_grid` field on this pipe.
- Worker: `json.loads(data_json)` → sandbox → [`json_egress.normalize_execute_response`](json_egress.py) → `json.dumps(..., allow_nan=False)` → `{status, result_json}`. Plots `find_image_payloads` returns move to `images` and become null in `result`. An image that scan did not return stays inline; it is not replaced with null.
- HTTP: `_start_raw_json` writes `result_json` as the response body.

**Pickle framing** (control envelope only — [`plugin/scripting/ipc.py`](../plugin/scripting/ipc.py), [`worker_base.py`](worker_base.py)):

- Write: `pickle.dumps(dict, protocol=5)` prefixed with a 4-byte big-endian length.
- Read: 4-byte size, then exactly *N* bytes, `pickle.loads`.
- Spawn handshake: the child writes `{status: "ready", pid: ...}` before the request loop.
- Formula workers allow up to ~33 MiB per frame so a 32 MiB HTTP body can travel as `data_json`.

Vision workers share the pickle framing. HTTP `image_b64` is decoded to raw `bytes` (`image_bytes`) on the pipe so the child does not re-decode Base64. Vision is unchanged (full JSON parse of a small OCR body).

Kit-side dumb JSON contract: [`docs/scripting/numpy-jailsafe.md`](../docs/scripting/numpy-jailsafe.md).

### 2. Tier 1: Formula Compute Pool (`FormulaProcessPool`)
- Manages persistent worker subprocesses (`workers`, default `2`).
- **Single-Threaded Child Subprocesses**: Each worker is a dedicated, single-threaded OS process running a synchronous IPC loop with exclusive lease occupancy (0 worker threads inside the child), ensuring determinism and zero race conditions.
- **GIL Elimination**: Each worker runs its own Python interpreter, achieving true parallel multi-core scaling for pure-Python and NumPy workloads.
- **Sticky Session Affinity**: For stateful calculations (`mode="shared"`), requests with the same `session_id` are routed to the process that owns that workbook. The supervisor maps are a cache of that pid: when the process exits, every session on it is dropped, and a respawn is not the same workbook. Clean workers without shared sessions are preferred for isolated work (`mode="isolated"`); however, when all idle workers hold shared sessions, isolated work falls back to the worker with the fewest sessions to avoid starving isolated calculations while waiting for shared session TTL. Shared sessions on one process still occupy it exclusively (one cell at a time). There are no explicit per-host caps on the number of active shared sessions: only coolwsd calls this service, and container OOM kills everything if memory limits are exceeded.
- **Stderr drain**: Each worker pipes stderr into `start_stderr_drain` (same helper as the desktop venv worker) so a noisy child cannot fill the OS pipe and deadlock the parent.
- **Timeouts**: The accept timestamp, the worker lease, and the child share one deadline. The child is given the time still left and returns an error frame when its alarm fires, so a normal timeout leaves the process up. `SIGKILL` is only when that frame never arrives. That kill drops every shared session on the pid.
- **Task Recycling**: Recycles worker processes after `worker_max_tasks` (default: 500) to keep memory fragmentation low. Workers holding active stateful sessions (`mode="shared"`) bypass normal recycling to preserve state indefinitely while active. Idle sessions auto-evict after `shared_kernel_ttl_sec` (default: 1 hour) of inactivity.
- **Idle Worker Reaper**: All worker pools terminate worker subprocesses that remain idle for > `idle_worker_ttl_sec` (default: 1 hour) to free system RAM. A dead pid is not idle. The next lease claims that slot and the following request respawns it after a ready handshake. A worker that still owns a shared session is not idle-evicted; session TTL clears those namespaces.

### 3. Tier 2: Isolated Vision & OCR Pool (`VisionProcessPool`)
- Dedicated worker subprocesses (`ocr_workers`, default `0`, disabled until configured) for heavy Docling and PaddleOCR tasks.
- Confines heavy Machine Learning models, C++ image decoders, and image buffers to a disposable child process so formula calculations are never blocked.

### 4. Performance Benchmarks: Why Process Pools?

To evaluate the trade-off between **In-Process Threaded Execution** and **Subprocess Pickle IPC**, we benchmarked real-world calculation latencies across 100 iterations per scenario using `scripts/benchmark_ipc_vs_inprocess.py`:

```text
================================================================================
Execution Architecture Benchmark: In-Process vs Subprocess Pickle IPC
================================================================================

1. Micro Calculation (result = 1 + 2):
  In-Process:            Mean =   0.31 ms | p50 =   0.29 ms
  Subprocess Pickle IPC: Mean =   0.47 ms | p50 =   0.42 ms
  Overhead vs In-Process: +0.16 ms (+160 microseconds)

2. NumPy Vector Math (1,000 floats):
  In-Process:            Mean =   1.35 ms | p50 =   1.19 ms
  Subprocess Pickle IPC: Mean =   3.33 ms | p50 =   3.21 ms

3. Tabular 2D Grid (100x10 matrix column means):
  In-Process:            Mean =   2.48 ms | p50 =   2.19 ms
  Subprocess Pickle IPC: Mean =   4.08 ms | p50 =   4.04 ms
================================================================================
```

#### Architectural Rationale & Benefits
1. **Negligible IPC Overhead**:
   - The IPC roundtrip over local binary pipes adds only **sub-millisecond latency**. Compared to standard browser-to-server HTTP network latency (typically 10–50 ms), this overhead is imperceptible (<1% of network roundtrip).
2. **Hard `SIGKILL` on Infinite Loops**:
   - In-process threads cannot be forcefully killed without destabilizing or terminating the entire Python interpreter. A formula timeout returns an error frame and leaves the process up. `SIGKILL` is reserved for a child that never writes that frame, and it drops every shared session on that pid.
3. **Total Fault & Crash Isolation**:
   - If user code or a third-party C/C++ extension triggers a segmentation fault (`SIGSEGV`) or abort, only that disposable child worker crashes. The master HTTP server and all other active sessions remain 100% unaffected and a replacement worker is automatically spawned.
4. **Complete GIL Bypass**:
   - Each worker runs in its own OS process with a dedicated Python interpreter, providing true linear multi-core scaling across all CPU cores for pure-Python loops.
5. **Periodic Memory Recycling**:
   - Workers are automatically recycled after `worker_max_tasks` (default: 500) to reclaim memory and prevent fragmentation over long-running deployments (active stateful sessions defer recycling until session reset).

---

## Logging & Observability

The service uses standard Python `logging` under the logger name `compute_service`.
Log format includes timestamps, log level, request IDs, modes, code size, execution durations, and status:

```text
2026-08-17 20:00:00,123 [INFO] compute_service: Starting Python Compute Service on 127.0.0.1:8000 (auth=yes, workers=2, ocr_workers=0)...
2026-08-17 20:00:01,456 [INFO] compute_service: exec /v1/execute id='req-123' mode=isolated session=None code_len=32 timeout=30s
2026-08-17 20:00:01,489 [INFO] compute_service: done /v1/execute id='req-123' status='ok' duration=32.40ms
```

---

## Docker & Container Hardening

```bash
docker build -f compute_service/Dockerfile -t python-compute .
docker run --rm -p 127.0.0.1:8000:8000 \
  --read-only --tmpfs /tmp:rw,size=64m,mode=1777 \
  --memory=1g --cpus=1 --pids-limit=256 \
  --security-opt no-new-privileges \
  --cap-drop ALL \
  -e PYTHON_COMPUTE_API_KEY_FILE=/run/secrets/key \
  -v /secure/key:/run/secrets/key:ro \
  python-compute
```

- The image and `start-docker.sh` set `PYTHON_COMPUTE_HOST=0.0.0.0` so the published port reaches the process. The host publish stays on `127.0.0.1`. `HOST` and `PORT` are not read.
- The multi-stage Dockerfile copies only pre-compiled packages into the runner image, drops root privileges (`USER appuser`), and excludes compiler build tools (`build-essential`).

---

## CLI

```bash
python compute_service/server.py --help
python compute_service/server.py --config compute_service/python-compute.example.json \
  --api-key-file /run/secrets/python_compute_api_key
```

## Tests & Benchmarks

### 1. Functional Tests
```bash
pytest tests/compute_service/
# JSON-forward peel / multipart / no host re-dumps:
pytest tests/compute_service/test_json_forward.py
```

### 2. Concurrency & Throughput Benchmarks
Run the built-in benchmark harness to evaluate throughput (RPS), latency percentiles, and multi-core scaling under simulated concurrent office loads:

```bash
# Quick formula worker scaling run (evaluates 1, 2, and 4 formula workers with 4 concurrent clients)
python scripts/benchmark_compute_service.py --quick

# Worker scaling across specific worker counts and concurrency
python scripts/benchmark_compute_service.py --workers 1,2,4 --concurrency 4

# Multi-concurrency client load benchmark (1 to 32 concurrent clients)
python scripts/benchmark_compute_service.py --concurrency 1,2,4,8,16,32 --requests 50 --threads 32
```

#### Benchmark Archetypes & Scaling Characteristics
- **`numpy_vector` (GIL Released)**: High throughput (280+ RPS), low median latency (~7–14ms) across 1–32 client threads as NumPy frees the GIL to all CPU cores.
- **`tabular_stats` (Mixed C/Python)**: Steady 180–195 RPS for 2D spreadsheet table filtering, summary statistics, and column aggregations.
- **`stateful_session` (`mode="shared"`)**: Fast in-memory stateful recalculations (400–430 RPS) with median latency under 10ms for multi-tenant sessions.
- **`pure_python` (GIL Held)**: Constant single-interpreter CPU throughput (~30 RPS) per worker process, scaling linearly across CPU cores as formula worker subprocesses are added (`--workers 1,2,4`).

See also [`docs/scripting/numpy-jailsafe.md`](../docs/scripting/numpy-jailsafe.md) (kit JSON contract). LibrePy desktop Pickle5 + `split_grid` is not this service: [`docs/scripting/numpy-serialization.md`](../docs/scripting/numpy-serialization.md).
