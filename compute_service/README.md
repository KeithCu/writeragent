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

- `GET /health` → `{"status":"healthy","service":"python-compute","version":"<version>"}` (no auth required)
- `POST /v1/execute[?session_id=<id>]` → `{ "id?", "code", "data?", "mode?", "timeout_ms?", "init_script?" }`
  (`init_script` runs **once** per worker: shared uses `{session_id}:init`, isolated uses a hash of the script. Later cells are seeded from that namespace; a changed script replaces the snapshot.)
- Docker (hardened run flags): `./compute_service/start-docker.sh` — see **Production / Collabora Online** below.

---

## API & Wire Protocol

### 1. Health Endpoint (`GET /health`)

Unauthenticated health probe suitable for Kubernetes/Docker liveness and readiness checks.
Always unauthenticated even when Bearer authentication is configured for execution.

- **Request**: `GET /health`
- **Response**: `200 OK`
  ```json
  {
    "status": "healthy",
    "service": "python-compute",
    "version": "0.8.59"
  }
  ```

### 2. Execution Endpoint (`POST /v1/execute[?session_id=<id>]`)

Evaluates sandboxed Python code and emits kit-safe dumb JSON (`allow_nan=False`, `NaN`/`Inf` → `null`).

- **Sticky Routing via URL Query Parameter**: For stateful calculations (`mode="shared"`), `session_id` is supplied as a URL query parameter (`POST /v1/execute?session_id=<id>`) so Layer 7 routers, ingress proxies, and load balancers can route stickily without buffering and parsing JSON request bodies:
  - **HAProxy**: `balance url_param session_id`
  - **NGINX**: `hash $arg_session_id consistent;`
  - **Envoy**: `hash_policy: [query_parameter: { name: "session_id" }]`
  - **AWS ALB**: Query string routing conditions on `session_id`.

- **Request Schema**:
  ```json
  {
    "id": "req-123",
    "code": "result = float(np.sum(data))",
    "data": [10, 20, 30],
    "mode": "isolated",
    "timeout_ms": 5000,
    "init_script": "optional-init-code"
  }
  ```
  *(Note: `session_id` must be passed as the URL query parameter `?session_id=...`, not in the JSON body).*

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

### 3. Vision & OCR Endpoint (`POST /v1/vision`)

Evaluates heavy document/image OCR and layout structure extraction in a dedicated, isolated worker subprocess pool. Supports both in-memory image buffers (`image_b64`) and server-local/mounted filesystem paths (`file_path`).

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

- **Request Schema (Option B: Server Filesystem Path)** — the worker reads this path as the service user; any authenticated client can open any readable file:
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

### 4. HTTP Status Codes & Error Semantics

| HTTP Status | Condition | Response Payload Shape |
| :--- | :--- | :--- |
| **`200 OK`** | Evaluation completed (success or runtime evaluation error) | `{"id"?: "...", "status": "ok"\|"error", "result"\|"error": ...}` |
| **`400 Bad Request`** | Malformed JSON, missing `code`, `code` longer than `max_code_chars` (`CODE_TOO_LARGE`), or vision `file_path` not under `ocr.allow_paths` (`FILE_PATH_DENIED`) | `{"id"?: "...", "status": "error", "code"?: "...", "error": "..."}` |
| **`401 Unauthorized`** | Missing or incorrect `Authorization: Bearer <secret>` | `{"status": "error", "error": "Unauthorized"}` + `WWW-Authenticate: Bearer` |
| **`404 Not Found`** | Unknown path or unsupported HTTP method | Plaintext `Not Found` |
| **`413 Payload Too Large`**| Request body exceeds `max_body_bytes` | `{"status": "error", "error": "Request body too large"}` |
| **`503 Service Unavailable`** | Process or per-session in-flight cap (`INFLIGHT_LIMIT` / `SESSION_INFLIGHT_LIMIT`). coolwsd may map this to `#N/A`. | `{"id"?: "...", "status": "error", "code": "...", "error": "..."}` |
| **`500 Internal Server Error`**| Unhandled server exception or JSON encoding failure | `{"id"?: "...", "status": "error", "error": "..."}` |

---

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
| `PYTHON_COMPUTE_API_KEY` | Shared Bearer secret | `""` |
| `PYTHON_COMPUTE_API_KEY_FILE` | Path to secret file (strip one trailing newline) | `""` |
| `PYTHON_COMPUTE_CONFIG` | Path to JSON config | `""` |
| `PYTHON_COMPUTE_LOG_LEVEL` | Log verbosity (`DEBUG`, `INFO`, `WARN`, `ERROR`) | `INFO` |
| `PYTHON_COMPUTE_MAX_BODY_BYTES` | Request body cap | `33554432` (32 MiB) |
| `PYTHON_COMPUTE_DEFAULT_TIMEOUT_SEC` | Default execution timeout in seconds | `30` |
| `PYTHON_COMPUTE_MAX_TIMEOUT_SEC` | Upper bound clamp for `timeout_ms` | `600` |
| `PYTHON_COMPUTE_THREADS` / `PYTHON_COMPUTE_MAX_THREADS` | Number of HTTP server listener threads | `2` |
| `PYTHON_COMPUTE_WORKERS` / `PYTHON_COMPUTE_MAX_WORKERS` | Number of formula worker subprocesses | `1` |
| `PYTHON_COMPUTE_WORKER_MAX_TASKS` | Tasks before recycling formula worker | `500` |
| `PYTHON_COMPUTE_SHARED_KERNEL_TTL_SEC` | Session idle timeout in seconds before eviction | `3600` (1 hour) |
| `PYTHON_COMPUTE_IDLE_WORKER_TTL_SEC` | Worker process idle timeout in seconds before termination | `3600` (1 hour) |
| `PYTHON_COMPUTE_OCR_WORKERS` | Dedicated OCR/Vision worker subprocesses | `0` (disabled by default) |
| `PYTHON_COMPUTE_OCR_TIMEOUT_SEC` | OCR/Vision execution timeout in seconds | `60` |
| `PYTHON_COMPUTE_OCR_MAX_TASKS` | Tasks before recycling OCR worker process | `100` |
| `PYTHON_COMPUTE_MAX_CODE_CHARS` | Max `code` string length | `262144` |
| `PYTHON_COMPUTE_MAX_INFLIGHT` | Process-wide concurrent `/v1/execute` (HTTP 503 over) | `max(threads, workers)*2` |
| `PYTHON_COMPUTE_MAX_INFLIGHT_PER_SESSION` | Concurrent shared-kernel jobs per `session_id` | `2` |
| `PYTHON_COMPUTE_OCR_ALLOW_PATHS` | `{os.pathsep}`-separated prefixes allowed for vision `file_path` (empty = deny) | `""` |

Key file permissions: readable only by the service user (e.g. mode `0400`).

### Production / Collabora Online

coolwsd is the only hop that should reach this process. Bind loopback, set the same Bearer secret as `security.python_compute.api_key`, and do **not** mount a host venv or docker.sock.

`file_path` on `/v1/vision` is **denied** unless `ocr.allow_paths` is set. Prefer `image_b64`.

`--network=none` cannot be combined with `-p` (published ports need a network namespace). Publish to loopback on the host, or use an internal bridge **without a default route**. Tenant sockets still fail via the AST sandbox plus missing egress.

```bash
./compute_service/start-docker.sh
# or:
docker build -f compute_service/Dockerfile -t python-compute .
docker run --read-only --tmpfs /tmp:rw,size=64m,mode=1777 \
  --memory=512m --cpus=1 --pids-limit=256 \
  --security-opt no-new-privileges --cap-drop ALL \
  -p 127.0.0.1:8000:8000 \
  -e PYTHON_COMPUTE_API_KEY=same-secret-as-coolwsd \
  python-compute
```

Shared `mode=shared` **must** use a per-document `session_id` query parameter (`?session_id=<id>`) (not a user id). Idle kernels are reset after `shared_kernel_ttl_sec`.

---

## Lifecycle & Signal Handling

- **Graceful Shutdown**: The service traps `SIGTERM` and `SIGINT`.
- When `SIGTERM` is received (from Kubernetes pod termination or `docker stop`), the server initiates `server.shutdown()` on a background thread, terminates worker subprocess pools cleanly, stops accepting new connections, drains in-flight evaluations, and closes listening sockets.

---

## Two-Tier Isolated Process Pool Architecture

The Python Compute Service is structured as a resilient master HTTP server fronting two specialized subprocess worker pools:

### 1. Master HTTP Router (~20MB RAM)
- Ultra-thin network process that accepts HTTP connections, verifies Bearer authentication tokens, and forwards each job as a **length-prefixed Pickle 5 frame** on the worker's stdin pipe.
- **Multi-Threaded HTTP Listener (`threads`, default `2`)**: Uses a `ThreadPoolExecutor` to handle concurrent HTTP connections, Kubernetes `/health` probes, and requests waiting on worker leases without socket stalls.
- **Unbreakable Design**: The master process never executes user code directly, ensuring that user errors, native crashes, or memory spikes cannot destabilize the HTTP service.

### Internal wire: HTTP JSON vs Pickle + split_grid

Two stacked protocols:

| Hop | Format | What travels |
|-----|--------|--------------|
| coolwsd → HTTP server | Dumb JSON (`POST /v1/execute[?session_id=...]`, `POST /v1/vision`) | `code`, `data` as nested lists, `mode`, … (session in URL query) / vision `image_b64` or `file_path` |
| HTTP server → formula/vision workers | Length-prefixed **Pickle 5** on stdio | Request/response **dicts**; large formula `data` may be a `split_grid` envelope |

**Pickle framing** ([`plugin/scripting/ipc.py`](../plugin/scripting/ipc.py), [`worker_base.py`](worker_base.py)):

- Write: `pickle.dumps(dict, protocol=5)` prefixed with a 4-byte big-endian length.
- Read: 4-byte size, then exactly *N* bytes, `pickle.loads`.
- Spawn handshake: the child writes `{status: "ready", pid: ...}` before the request loop.

**split_grid** ([`plugin/scripting/payload_codec.py`](../plugin/scripting/payload_codec.py)):

- [`FormulaProcessPool.execute`](formula_pool.py) calls `host_pack_data(data, min_cells=1000)` when `data` is a non-empty list (desktop `=PY()` uses `BINARY_MIN_CELLS = 100`; this service uses a higher bar so small HTTP grids stay nested lists).
- ≥ 1000 cells → `{__wa_payload__: "split_grid", dtype, column_kinds, shape, buffer: <float64 bytes>, strings: {flat_index: str}}` inside the pickled request dict.
- Below threshold → nested Python lists in that same dict.
- The worker unpacks with `child_unpack_data` (numeric-only grids materialize via `np.frombuffer`). Large ndarray results may pack as `split_grid` on the way back; [`json_egress`](json_egress.py) unpacks them to nested lists / scalars before the HTTP JSON response so the kit never sees the envelope.

Vision workers share the pickle framing. HTTP `image_b64` is decoded to raw `bytes` (`image_bytes`) on the pipe so the child does not re-decode Base64.

Wire-format detail for `split_grid` and Pickle5: [`docs/scripting/numpy-serialization.md`](../docs/scripting/numpy-serialization.md). Kit-side dumb JSON contract: [`docs/scripting/numpy-jailsafe.md`](../docs/scripting/numpy-jailsafe.md).

### 2. Tier 1: Formula Compute Pool (`FormulaProcessPool`)
- Manages persistent worker subprocesses (`workers`, default `1`).
- **Single-Threaded Child Subprocesses**: Each worker is a dedicated, single-threaded OS process running a synchronous IPC loop with exclusive lease occupancy (0 worker threads inside the child), ensuring determinism and zero race conditions.
- **GIL Elimination**: Each worker runs its own Python interpreter, achieving true parallel multi-core scaling for pure-Python and NumPy workloads.
- **Sticky Session Affinity**: For stateful calculations (`mode="shared"`), requests with the same `session_id` are consistently routed to the specific worker holding that workbook's state in memory. Isolated and sticky jobs **exclusively occupy** a worker (idle set + condition); they never run concurrently on the same process.
- **Stderr drain**: Each worker pipes stderr into `start_stderr_drain` (same helper as the desktop venv worker) so a noisy child cannot fill the OS pipe and deadlock the parent.
- **Hard `SIGKILL` Watchdogs**: If a user formula triggers an uncatchable loop or timeout, the pool terminates the hanging process via `SIGKILL`, returns a clean timeout error, and automatically spawns a fresh worker.
- **Task Recycling**: Recycles worker processes after `worker_max_tasks` (default: 500) to keep memory fragmentation low. Workers holding active stateful sessions (`mode="shared"`) bypass normal recycling to preserve state indefinitely while active. Idle sessions auto-evict after `shared_kernel_ttl_sec` (default: 1 hour) of inactivity.
- **Idle Worker Reaper**: All worker pools terminate worker subprocesses that remain idle for > `idle_worker_ttl_sec` (default: 1 hour) to free system RAM; processes lazily re-spawn on the next incoming request.

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
   - In-process threads cannot be forcefully killed without destabilizing or terminating the entire Python interpreter. Subprocess workers can be immediately destroyed via `SIGKILL` on timeout, guaranteeing that rogue formulas or uncatchable loops cannot stall the service.
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
2026-08-17 20:00:00,123 [INFO] compute_service: Starting Python Compute Service on 127.0.0.1:8000 (auth=yes)...
2026-08-17 20:00:00,125 [INFO] compute_service: Cython Accelerator: Active (Optimized, source: contrib.vec_pack)
2026-08-17 20:00:01,456 [INFO] compute_service: exec /v1/execute id='req-123' mode=isolated session=None code_len=32 timeout=30s
2026-08-17 20:00:01,489 [INFO] compute_service: done /v1/execute id='req-123' status='ok' duration=32.40ms
```

### Cython Binary Acceleration & Canary Verification

The service automatically detects compiled Cython binaries (`pack.*.so` / `pack.*.pyd`) from:
1. In-tree git repository checkouts (`contrib/vec_pack`)
2. Installed LibrePy user profile locations (`audio_binaries/writeragent_vec`)

On startup, a runtime canary verification test (`_verify_accelerator`) runs to ensure binary integrity and compatibility before enabling Cython binary acceleration for `split_grid` 2D array packing. If no compatible binary is found or the canary check fails, the service logs a warning and falls back to pure Python without interrupting execution.

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

- For cross-container networking within a private bridge network, set `HOST=0.0.0.0`.
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
```

### 2. Concurrency & Throughput Benchmarks
Run the built-in benchmark harness to evaluate throughput (RPS), latency percentiles, and multi-core scaling under simulated concurrent office loads:

```bash
# Quick sanity run
python scripts/benchmark_compute_service.py --quick

# Full multi-concurrency benchmark (1 to 32 concurrent clients)
python scripts/benchmark_compute_service.py --concurrency 1,2,4,8,16,32 --requests 50 --threads 32
```

#### Benchmark Archetypes & Scaling Characteristics
- **`numpy_vector` (GIL Released)**: High throughput (280+ RPS), low median latency (~7–14ms) across 1–32 client threads as NumPy frees the GIL to all CPU cores.
- **`tabular_stats` (Mixed C/Python)**: Steady 180–195 RPS for 2D spreadsheet table filtering, summary statistics, and column aggregations.
- **`stateful_session` (`mode="shared"`)**: Fast in-memory stateful recalculations (400–430 RPS) with median latency under 10ms for multi-tenant sessions.
- **`pure_python` (GIL Held)**: Constant CPU throughput (~30 RPS) bounded by single-interpreter bytecode execution.

See also [`docs/scripting/numpy-jailsafe.md`](../docs/scripting/numpy-jailsafe.md) (kit JSON contract) and [`docs/scripting/numpy-serialization.md`](../docs/scripting/numpy-serialization.md) (Pickle5 + `split_grid`).
