# Investigate: worker spawn handshake timeouts in full pytest

**Status:** Open — intermittent on `make pytest` / `make test-run` (2026-09-01)  
**Severity:** Unit-test flake that can fail ~30–40 tests at once; production spawn path is the same code  
**Do not:** raise `_SPAWN_READY_TIMEOUT_SEC`, permanently cap `PYTEST_WORKERS`, or “fix” it by skipping tests

This note is a **starting brief for an agent**. Work **one hypothesis at a time**, keep a log of what you tried, and prefer git bisect / a minimal repro over a pile of speculative fallbacks.

Related: compute pool docs in [`compute_service/README.md`](../../compute_service/README.md); IPC in [`plugin/scripting/ipc.py`](../../plugin/scripting/ipc.py); threading in [../framework/threading.md](../framework/threading.md).

---

## Symptom

A full `make pytest` (xdist, `PYTEST_WORKERS=auto` or `6`) can fail with **dozens of tests** that all spawn a Python child over stdio pickle:

| Area | Typical tests | Log / assertion |
|------|----------------|-----------------|
| Compute formula pool | `tests/compute_service/test_formula_pool.py`, `test_compute_service.py` HTTP execute | `Formula worker #1 spawn handshake timed out` then execute status ≠ `ok` |
| Vision pool | `tests/compute_service/test_vision_pool.py` | same handshake path (`worker_base.py`) |
| Venv worker | `test_venv_worker.py::test_harness_main_loop_integration`, `test_warm_venv_worker_resolves_and_warms`, `test_serialization_ab.py::test_venv_transform_parity[…_subprocess]`, `test_writeragent_alias.py::test_venv_worker_bidirectional_tool_call` | first IPC read times out / `None` / error status |
| Compute HTTP bench | `tests/scripts/test_benchmark_compute_service.py` | `failed_requests != 0`; stderr shows handshake timeouts ~15s apart |

Example from a 2026-09-01 full run: **38 failed, 6392 passed** in ~320s. Captured HTTP log:

```text
POST /v1/execute  200   (then ~15s)
POST /v1/execute  200
ERROR compute_service.worker: Formula worker #1 spawn handshake timed out
```

The 15s gap is `_SPAWN_READY_TIMEOUT_SEC` in [`compute_service/worker_base.py`](../../compute_service/worker_base.py), not CPU saturation.

UNO (`make test-uno`) is **out of scope** unless you prove the same spawn path runs there.

---

## What this is not (already checked)

| Hypothesis | Result (2026-09-01) |
|------------|---------------------|
| Machine has too few cores / `make test-run` at `-n 8` | **Rejected as root cause.** CPU had headroom. Capping `test-run` at `PYTEST_WORKERS=6` did **not** stop the flake. Isolated compute/venv tests with `-n 6` **passed**. |
| Handshake always broken | **Rejected.** Idle spawn of `formula_worker.py` is ~0.2s. 12 parallel handshakes outside pytest: 0.3–0.8s each, all `{status: ready}`. |
| Eval / OpenRouter / stream-normalizer | **Unrelated.** Different stack. |
| `PYTHON_COMPUTE_IDLE_WORKER_TTL_SEC` set tiny in the shell | **Not set** in the environment of the 2026-09-01 repro machine. Still worth checking on the next failure (`env \| grep PYTHON_COMPUTE`). |

So: **full-suite, intermittent, wait-not-compute.** Treat it as a pipe/import/env race, not a load-shed problem.

---

## How to work (agent protocol)

1. **Do not change production timeouts or xdist `-n` as the fix.** Those hide the hang.
2. **One hypothesis per experiment.** Record: command, pass/fail, duration, stderr snippet, git SHA.
3. **Prefer a small repro** that fails *reliably* over re-running all 6k tests after every tweak.
4. **If you cannot repro:** bisect or add diagnostics first (stderr on handshake timeout is currently dropped — see H2).
5. After a real fix: tests for that failure mode, then `make pytest` on the files you touched plus `make typecheck`. Full `make test-run` only to confirm the flake is gone.
6. Update **this doc** (status, what you proved, what you ruled out). Do not grow `AGENTS.md`.

---

## How to reproduce

### A. Isolated (usually green — use as control)

```bash
.venv/bin/python -m pytest \
  tests/compute_service \
  tests/scripting/test_venv_worker.py::test_harness_main_loop_integration \
  tests/scripting/test_venv_worker.py::test_warm_venv_worker_resolves_and_warms \
  tests/scripting/test_writeragent_alias.py::test_venv_worker_bidirectional_tool_call \
  tests/scripting/test_serialization_ab.py::test_venv_transform_parity \
  tests/scripts/test_benchmark_compute_service.py \
  -n 6 --dist=loadgroup -q --tb=line
```

Expected: all pass (~30s). If **this** fails, the bug is local to these modules (easier). If it passes, you need the full suite or a contention harness.

### B. Full suite (how it actually failed)

```bash
make pytest
# or
make pytest PYTEST_WORKERS=6
```

Look for `spawn handshake timed out` on stderr, not only the pytest summary.

### C. Handshake microbench (no pytest)

```bash
.venv/bin/python -c "
import subprocess, sys, time
from plugin.scripting.ipc import read_pickle_frame_with_timeout
p = subprocess.Popen([sys.executable, 'compute_service/formula_worker.py'],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
t0 = time.monotonic()
try:
    d = read_pickle_frame_with_timeout(p.stdout, 15.0)
    print('ready', d, 'in', round(time.monotonic()-t0, 3))
except Exception as e:
    print(type(e), e, 'in', round(time.monotonic()-t0, 3))
    print('stderr', p.stderr.read(2000) if p.stderr else b'')
p.kill()
"
```

### D. Contention harness (write this if A is green and B is red)

Spawn N `formula_worker` children **while** the parent (or sibling processes) import `plugin.scripting.payload_codec` / `plugin.scripting.venv.venv_sandbox`, or while pytest-xdist workers run unrelated tests. Goal: a script that fails in <1 min without 6k tests.

---

## Call path

```text
FormulaProcessPool / get_formula_pool
  → BaseProcessWorker.__init__ → _spawn
      Popen([sys.executable, formula_worker.py], stdin/stdout/stderr=PIPE, bufsize=0)
      optimize_popen_pipes (Linux F_SETPIPE_SZ)
      start_stderr_drain (dedicated thread, worker_pool.run_in_background)
      read_pickle_frame_with_timeout(stdout, 15s, is_alive=self.is_alive)
        POSIX: select() + stream.read(); TimeoutExpired on deadline

formula_worker.py (child), today:
  sys.path insert repo root
  import execute_code, run_worker_stdio_loop, load_cython_accelerator
  load_cython_accelerator()          # BEFORE any pickle write
  main() → run_worker_stdio_loop
      write_pickle_frame({status: ready, pid})   # first stdout bytes IF import stayed quiet
      loop: read request, handler, write response
```

Venv path is different: [`plugin/scripting/venv/worker_harness.py`](../../plugin/scripting/venv/worker_harness.py) **never sends ready**. Parent writes a request immediately; child only reads after importing `venv_sandbox` / payload_codec / alias importer. Warm timeout is `WARM_WORKER_TIMEOUT_SEC` (30s) plus grace — still fails in a full run when the child is stuck or stdout is not pickle.

Handshake read does **not** pass `max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES`. If the first 4 bytes are not a length prefix (a log line, warning, progress), `_validate_frame_size` allows a huge `size` and the parent waits 15s for bytes that never come — **CPU idle**.

On `TimeoutExpired`, `_spawn` logs one line and `kill()`s. It does **not** attach `_stderr_snippet()`. A `None`/non-dict ready is **not** treated as spawn failure.

---

## When it likely landed (bisect hints)

Start here; confirm with `git bisect` + a failing command (B or D), not guesswork.

| Commit | Why it is a suspect |
|--------|---------------------|
| `76a4077b` | Introduced `_SPAWN_READY_TIMEOUT_SEC`, `read_pickle_frame_with_timeout`, stderr drain, `optimize_popen_pipes` on compute workers. Before this, handshake was a **blocking** `read_pickle_frame` (hang until pytest `--timeout=300` instead of a clean 15s fail). |
| `89fae560` | Host `_SafeUnpickler`. Ready dict is builtins-only (should decode). Still verify nothing in the ready frame uses a forbidden global. |
| `e21097a4` / `822a1462` / `6dc44cfe` | Session/idle TTL reapers. `_worker_last_active` is stamped with `now` **captured before** the spawn loop. Harmless at 3600s; bad if TTL is small. |
| `c8014402` | `max_payload_bytes` on many reads — **not** on compute handshake. |
| Payload_codec `load_cython_accelerator()` at **module import** (and again in `formula_worker`) | Child does Cython/sys.path/config work before ready. `user_config_dir()` → `init_config()` → `get_ctx()` is try/except, but a **hang** inside `get_ctx()` would not raise. |

Bisect recipe: `git bisect start HEAD <known-good>`; at each step run **the same** command (full pytest is slow; prefer a contention harness once you have one). Known-good is likely **before `76a4077b`** only in the sense that failures looked like 300s hangs instead of 15s timeouts — confirm.

---

## Why it only happens sometimes

Work these as **separate** experiments. Intermittency is the point.

1. **Full xdist vs subset.** Other tests spawn Harper, embeddings, MCP, extra venv workers. FD count, import of the same `.pyc`/`.so`, and inherited env only collide in the full graph.
2. **Stdout is not pickle.** Any `print`, warning, or logging to stdout before the ready frame desyncs the length prefix. Default logging is stderr; `WRITERAGENT_DEBUG_LOG_PATH`, pytest-cov, or `WRITERAGENT_PYTEST_PROGRESS` inherited by the child could change that. Compute Popen does **not** use `scrub_subprocess_env` (venv host does).
3. **Import-before-ready.** `formula_worker` / `worker_harness` do a large import graph first. Usually <1s; under lock/contention it can exceed 15s with little CPU.
4. **`select` + `read` treating empty as EOF.** In `read_pickle_frame_with_timeout`, `read()` returning `b''` after `select` ready returns a short header → `None`, not always `TimeoutExpired`. Fail-open spawn then looks like a later execute failure.
5. **Idle reaper + stale `last_active`.** Only if TTL is short or tests mutate it. Check settings JSON / env on the failing machine.
6. **xdist worker reuse / leaked pools.** `test_formula_pool.py` autouse-calls `shutdown_formula_pool`; `test_compute_service.py` HTTP tests may leave a global pool. Cross-module order on one gw can matter (`--dist=loadgroup` keeps a module on one worker, not the whole suite).

---

## Hypotheses to try **one at a time**

Order is cheapest / highest signal first. After each: revert unrelated edits; keep the diagnostic if it is still useful.

### H1 — Child writes non-pickle bytes on stdout before ready

**Probe:** In `_spawn`, dump `repr(first 16 bytes)` on timeout / bad ready (temporary). Or wrap `formula_worker` so the first action is `os.write(1, b'HELLO')` in a unit test and assert spawn fails fast with a clear error (today it may wait 15s).

**Fix if true:** never print to stdout in workers; handshake read uses `max_payload_bytes`; spawn fails closed.

### H2 — Handshake timeout drops the only evidence (stderr)

**Probe:** On `TimeoutExpired` in `_spawn`, log `_stderr_snippet()` and `proc.returncode`. Re-run full pytest **once**. If stderr shows ImportError, deal, UNO, or coverage — chase that. If stderr is empty, child is alive and not writing (H3/H4).

**Fix if true:** keep the log line in production (small, required for the next hang).

### H3 — Ready is sent too late (import graph)

**Probe:** Move `{status: ready}` to the first lines of `formula_worker` (before `load_cython_accelerator` / `execute_code` import), then import the handler lazily. Microbench + full pytest. Same idea for `worker_harness`: ready frame or import after first stdin read.

**Fix if true:** that *is* the production fix. Do not leave a 60s timeout as the solution.

### H4 — Inherited pytest/coverage env

**Probe:** Pass `env=scrub_subprocess_env({...})` (or a compute-specific scrub) into compute `Popen`. Compare with/without `PYTEST_*`, `COV_*`, `WRITERAGENT_PYTEST_PROGRESS`, `WRITERAGENT_DEBUG_LOG_PATH`.

**Fix if true:** scrub in production compute workers too (LibreOffice host should not leak pytest env; still good hygiene).

### H5 — `load_cython_accelerator` / `user_config_dir` hang in the child

**Probe:** Time each import in `formula_worker` with `print(..., file=sys.stderr, flush=True)` (stderr only). Watch `init_config` / `get_ctx` / Cython canary.

**Fix if true:** do not call host config/UNO from the compute child at import; load Cython on first execute.

### H6 — Fail-open `_spawn`

**Probe:** Unit test: child exits before ready; child sleeps 20s; child writes `not-a-frame`. Today timeout vs `None` vs “looks spawned” differ.

**Fix if true:** require `status == ready`; otherwise kill and return `WORKER_SPAWN_FAILED` on execute.

### H7 — Idle TTL / `last_active` stamped too early

**Probe:** Log `now - last_active` at reaper start. Set `idle_worker_ttl_sec` very small in a dedicated test (already have reaper tests). Confirm HTTP/default settings are still 3600.

**Fix if true:** stamp `last_active` after each successful spawn; do not reuse a pre-loop `now`.

### H8 — FD leak / pipe buffer in the full suite

**Probe:** `ls /proc/self/fd | wc -l` from a failing xdist worker (or `resource.getrlimit(RLIMIT_NOFILE)`). Count live `formula_worker` / `worker_harness` PIDs during the suite.

**Fix if true:** shutdown leaks, not a longer handshake.

### H9 — Safe unpickler / framing regression

**Probe:** Decode a captured ready payload with `_SafeUnpickler`. Should be a dict. If not, that commit is the bisect hit.

---

## Suggested production shape (only after a hypothesis wins)

Keep this small. Do not implement the whole list “just in case.”

- Ready frame **before** heavy imports (formula, vision, optionally venv harness).
- Handshake read: `max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES`; require dict `status=ready`.
- Timeout/EOF: stderr snippet + kill; execute surfaces `WORKER_SPAWN_FAILED`.
- Tests for: stdout garbage, late import with early ready, timeout includes stderr.
- Optional: scrub child env on compute Popen.

Revert the `Makefile` `test-run` `PYTEST_WORKERS=6` cap if it was only a workaround (`test-run` currently passes `PYTEST_WORKERS=6` into pytest).

---

## Files to read first

| File | Why |
|------|-----|
| [`compute_service/worker_base.py`](../../compute_service/worker_base.py) | `_spawn`, handshake, fail-open, missing stderr on timeout |
| [`compute_service/formula_worker.py`](../../compute_service/formula_worker.py) | Import + Cython before ready |
| [`compute_service/vision_worker.py`](../../compute_service/vision_worker.py) | Same loop, lighter imports |
| [`plugin/scripting/ipc.py`](../../plugin/scripting/ipc.py) | `read_pickle_frame_with_timeout`, empty-read → EOF, `max_payload_bytes` |
| [`plugin/scripting/venv/worker_harness.py`](../../plugin/scripting/venv/worker_harness.py) | No ready frame; import-before-read |
| [`plugin/scripting/venv_worker.py`](../../plugin/scripting/venv_worker.py) | Host spawn, `scrub_subprocess_env`, warm timeout |
| [`plugin/scripting/sandbox.py`](../../plugin/scripting/sandbox.py) | `optimize_popen_pipes`, scrub |
| [`plugin/framework/worker_pool.py`](../../plugin/framework/worker_pool.py) | `start_stderr_drain` |
| [`tests/compute_service/test_formula_pool.py`](../../tests/compute_service/test_formula_pool.py) | Autouse `shutdown_formula_pool` |

---

## Experiment log

Fill this in as you go. Do not delete failed experiments.

| Date | SHA | Hypothesis | Command | Result |
|------|-----|------------|---------|--------|
| 2026-09-01 | tree at investigation | CPU / `-n 8` | `make pytest`; then isolated `-n 6` on compute/venv | Full suite 38 fail (handshake 15s). Isolated 138 pass. 12-way idle handshake OK. CPU headroom. |
| 2026-09-01 | `2f96c06a` | Control A / C / D | isolated compute/venv `-n 6`; idle `formula_worker` ×3; 12-child contention + parent `load_cython_accelerator` | A: 138 passed in 22s, no handshake timeouts. C: ready in 0.25–0.30s. D: 12/12 ready, max 0.45s. Env had no `PYTHON_COMPUTE`/`PYTEST`/`COV_`/`WRITERAGENT_*` leak. Flake not reproduced outside full suite. |
| 2026-09-01 | `2f96c06a` + local | H2 | log `_stderr_snippet()` + `returncode` on spawn `TimeoutExpired`; test `test_spawn_timeout_logs_stderr_snippet` | Diagnostic only (no root-cause fix). Did not raise 15s timeout or cap workers. |
| 2026-09-02 | Keith laptop full `make test` | H1 / H2 | PR 538 logging on full xdist | 38 failed, 6407 passed in 385s. Every compute timeout: `returncode=None stderr=<empty>`. Venv: size `1165128303` (= `b'Erro'`). Flood worker too (not Cython). |
| 2026-09-02 | after #543 | H1 | full `make test` with handshake cap | 37 failed, 6412 passed in **28s**. No 15s timeouts. Every spawn: `Invalid IPC frame size: 1165128303 (header=b'Erro') stderr=<empty>`. Still missing the rest of the stdout line. |

When the flake is gone, set **Status** at the top to Fixed, name the commit, and point at the tests that lock the behavior.
