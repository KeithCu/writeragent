# Robustness Roadmap: Practical Reliability for WriterAgent

## Executive Summary

This roadmap focuses on reliability improvements that make WriterAgent more dependable without adding a lot of production complexity. The goal is not to build a miniature SRE platform inside a LibreOffice extension. The goal is to make common failures easier to recover from, easier to diagnose, and less likely to break the user experience.

The guiding principle is:

- prefer bounded recovery over global coordination
- prefer graceful degradation over clever infrastructure
- prefer better tests even when test code is more complex
- prefer verification scaffolding when it stays lightweight
- prefer consistency with existing architecture over generic reliability patterns

That means this roadmap prioritizes:

- targeted retry and timeout handling for network and file I/O
- clearer recovery paths for stale UNO objects and disposed documents
- invariant checks, contract decorators, and verification-oriented tests
- stricter validation at critical boundaries
- lightweight health checks and diagnostic surfaces
- targeted fuzz and property-style testing for pure-Python logic

It explicitly de-prioritizes large, speculative features such as global checkpointing, predictive failure analysis, broad telemetry systems, and UNO-wide circuit breakers.

## 1. What Already Exists

WriterAgent already has a useful reliability baseline. The roadmap should build on that instead of replacing it with parallel systems.

### 1.1 Existing Error Handling

Current building blocks already exist in `plugin/framework/errors.py`:

- `WriterAgentException` as the common base error
- typed subclasses such as `ConfigError`, `NetworkError`, `UnoObjectError`, and `DocumentDisposedError`
- `safe_call`, `safe_uno_call`, and `check_disposed` helpers for wrapping UNO failures
- `format_error_payload()` for structured tool and API errors
- `safe_json_loads()` for defensive parsing

This means the roadmap should favor extending the current error model rather than inventing a second one.

### 1.2 Existing Logging and Diagnostics

Current diagnostics already exist in `plugin/framework/logging.py`:

- unified debug logging (`writeragent_debug.log` in the LO user config dir)
- optional agent traces in the same log when `enable_agent_log` is set
- global exception hooks
- a watchdog that can flag stalled activity

This is already a good base for practical reliability work. The next step is better classification, better docs, and better recovery behavior, not an elaborate telemetry stack.

### 1.3 Existing Network Resilience and Retry

Network behavior in `plugin/framework/client/` and `plugin/mcp/` includes a mature, bounded retry and pacing system:

- **Pacing and Delay Math (`plugin/framework/client/request_controls.py`)**:
  - OpenClaw `packages/retry` delay math port: jittered exponential backoff (`backoff_delay_sec`) bounded by `RETRY_MIN_DELAY_SEC` (0.3s) and `RETRY_MAX_DELAY_SEC` (30.0s).
  - Both symmetric and positive jitter modes (`_apply_jitter_ms`).
  - `parse_retry_after` and `parse_retry_after_ms`: parses delta-seconds, RFC HTTP-date, and fallback `retry-after-ms` formats; treats `Retry-After` within the 30s cap as a lower bound. Delays above 30s fail the attempt immediately with the server's wait in the error and remember a 30s gap.
  - Abortable sleep (`wait_abortable`): sleeps in small (0.05s) chunks checking `stop_checker`, so user Stop immediately terminates the retry wait without UI hang.
  - Sidebar status emission (`format_retry_wait_status`, `emit_retry_status`): displays real-time localized feedback (e.g. `"Provider busy, retrying in 3s…"`) in the sidebar.
  - Learned per-host pacing (`remember_host_gap`, `clear_host_gap`, `wait_host_gap`): caches backoff per `host:port` (brackets around IPv6) across separate requests to avoid stampeding busy backends; Stop during `wait_host_gap` rolls back the unconsumed slot reservation.
  - Dedicated OpenRouter free-tier pacing (`OPENROUTER_FREE_MIN_GAP_SEC = 3.0` for `:free` and `openrouter/free` models on OpenRouter endpoints, keyed as `{host}:{port}:free`).
  - `RequestPacer` enforcing client-level minimum intervals (`LLM_MIN_REQUEST_INTERVAL_SEC = 0.05`) between sends.
  - `LocalHttpsCertificateFallback` tracking local hosts that failed TLS verification (e.g., self-signed local Ollama/LM Studio) and safely retrying unverified only for local addresses.

- **Transport and Request Retries (`http_transport.py` & `llm_client.py`)**:
  - Bounded request timeouts and persistent HTTP connections.
  - Automatic retries on `CONNECTION_ERRORS` and `RETRYABLE_HTTP_STATUS` (429, 503) up to `RETRY_MAX_ATTEMPTS = 3` for both streaming and non-streaming (`request_with_tools`) calls.
  - **Text duplication guard**: Retries are strictly forbidden once any tokens have been emitted to the UI (`emitted_any`); raises `NetworkError(code="CONNECTION_LOST")` instead of re-requesting.
  - Credential redaction: API keys are redacted from error bodies and logs before recording.
  - Defensive handling for malformed SSE chunks and repeat-chunk suppression.

- **MCP Tunnel Retries (`plugin/mcp/tunnel_state.py` & `tunnel.py`)**:
  - Pure state machine exponential backoff (`compute_backoff_delay`) under `@deal` contracts.
  - Discrete side effects (`ScheduleRetryTimerEffect`, `CancelRetryTimerEffect`) managing reconnection timers without blocking the event loop.

- **Subprocess and Test Runner Retries**:
  - Headless LibreOffice harness bootstrap retry on pipe miss in `plugin/testing_runner.py`.
  - Venv worker stdin timeout retry in `plugin/scripting/venv_worker.py`.

### 1.4 Existing Health and Fallback Mechanisms

There are already a few useful fallback and health-style patterns in the codebase:

- history falls back from SQLite to JSON
- config validation strips bad data and merges validated output
- MCP exposes a simple health endpoint
- document diagnostics already exist for some document-health checks
- the chat FSM already separates state transitions from side effects

These are the kinds of mechanisms to expand: small, localized, easy to reason about.

## 2. Reliability Principles

### 2.1 Keep Production Complexity Low

Reliability work should improve the behavior of existing features, not create a second product surface area to maintain.

Good fit:

- small retry helpers
- clear fallback rules
- recovery procedures
- validation and bounds checks
- targeted logging improvements
- simple health probes

Bad fit unless justified by real failures:

- global coordination layers
- speculative orchestration frameworks
- predictive analytics
- desktop-extension versions of datacenter SLO dashboards

### 2.2 Prefer Local Recovery Over Global State

Most failures in this project are local:

- a stale document object
- a timed-out network request
- a malformed response chunk
- a missing config value
- a single feature path failing while the rest of the extension remains usable

The roadmap should optimize for local containment and recovery:

- reacquire the document model if safe
- retry network I/O a small number of times
- fall back to reduced functionality
- surface a clear user-visible message when the full path fails

### 2.3 It Is Fine For Test Code To Be More Sophisticated

Production code should stay simple. Test code can absorb more complexity if it improves confidence without burdening runtime behavior.

That makes the following especially reasonable:

- fuzz testing for parsers, normalization, and defensive helpers
- property-style tests for pure functions
- adversarial streaming tests
- failure-injection tests around retries and fallback behavior

### 2.4 Lightweight Verification Is A Good Trade

Some verification work belongs near the top of the roadmap precisely because it is mostly scaffolding.

Good fits:

- standard contract decorators from an existing library such as `deal`
- invariant helpers for pure state transitions and normalization code
- test-only invariant assertions
- verification-focused wrappers around existing pure helpers

This is different from adding a large runtime subsystem. A small amount of contract and assertion machinery can support better long-term verification without making the product code much more complicated. Prefer existing tools such as `deal` over homegrown decorator frameworks when they fit the code well.

## 3. Priority Areas

### 3.1 Priority 1: Recovery Paths and Graceful Degradation

This is the highest-value work because it directly reduces user-facing breakage.

### Target behaviors

- When full document-context extraction fails, fall back to a smaller or safer context.
- When a disposed or stale UNO object is detected, try to reacquire what is cheap and safe to reacquire.
- When a feature cannot continue safely, fail with a clear, localized message instead of cascading errors.
- When history or config persistence fails, continue with a safe degraded path if one already exists.

### Example pattern

```python
def get_document_content_with_fallback(model, max_length):
    try:
        return get_full_document_content(model, max_length)
    except UnoObjectError:
        try:
            return get_selection_only(model)
        except UnoObjectError:
            return "[Document content unavailable]"
```

### Why this is first

- directly improves perceived reliability
- does not require new infrastructure
- aligns with the current typed-error model
- works well with existing logging and UI messaging

### 3.2 Priority 2: Bounded Retries for Real I/O

Retries are valuable when they are narrow, explicit, bounded, and abortable.

### What is Shipped

1. **Outbound LLM Requests (`plugin/framework/client/`)**:
   - **Delay calculation (`request_controls.py`)**: Port of OpenClaw `packages/retry` delay math. Exponential backoff with jitter (`backoff_delay_sec`), clamped between `RETRY_MIN_DELAY_SEC` (0.3s) and `RETRY_MAX_DELAY_SEC` (30.0s).
   - **`Retry-After` header parsing**: Handles delta-seconds and RFC HTTP-date strings via `parse_retry_after()`, falling back to `parse_retry_after_ms()`; honors delays up to 30s as a lower bound with positive jitter. Delays above 30s fail immediately with a descriptive error and remember a 30s gap.
   - **Immediate abortability (`wait_abortable`)**: Sleeps in 50ms chunks, continuously checking `stop_checker`. If the user clicks Stop in the sidebar, the sleep terminates immediately without hanging the UI.
   - **Live UI status**: Emits real-time retry notices (e.g. `"Provider busy, retrying in 3s…"`) to the sidebar via `emit_retry_status`.
   - **Learned host cooldown (`remember_host_gap`, `wait_host_gap`)**: Process-wide gap cache keyed by `host:port` (brackets around IPv6) so subsequent or parallel requests space themselves rather than immediately hitting a busy host; Stop during `wait_host_gap` rolls back an unconsumed slot reservation.
   - **OpenRouter free-tier pacing**: Enforces a 3.0s minimum gap (`OPENROUTER_FREE_MIN_GAP_SEC`) keyed as `{host}:{port}:free`, applied only when the endpoint is OpenRouter.
   - **Client-level pacing (`RequestPacer`)**: Enforces minimum gap (`LLM_MIN_REQUEST_INTERVAL_SEC = 0.05s`) between consecutive sends.
   - **Streaming & Non-Streaming integration (`llm_client.py`, `http_transport.py`)**: Up to 3 attempts on `CONNECTION_ERRORS` and HTTP 429/503.
   - **Text duplication guard**: If any token has already reached the UI (`emitted_any is True`), retry is strictly blocked to prevent duplicating streamed text; raises `NetworkError(code="CONNECTION_LOST")`.
   - **Credential redaction**: Secrets/keys are scrubbed before logging error payloads.

2. **MCP SSE Tunnel (`plugin/mcp/tunnel_state.py` & `tunnel.py`)**:
   - Pure state machine exponential backoff (`compute_backoff_delay`, capped at `DEAL_MAX_BACKOFF`) under `@deal` contracts.
   - Emits `ScheduleRetryTimerEffect` and `CancelRetryTimerEffect` to schedule reconnection timers cleanly without blocking.

3. **Subprocess & Test Runner Retries**:
   - Headless LibreOffice bootstrap pipe retry in `plugin/testing_runner.py`.
   - Venv worker stdin write timeout retry in `plugin/scripting/venv_worker.py`.

### What Remains / Out of Scope

- **File I/O Persistence Retries**: Config and chat history currently use atomic writes (`atomic_write`) and fallback from SQLite to JSON. Bounded retries around transient OS file locks (e.g., Windows anti-virus locks) remain deferred unless concrete locking failures are observed in practice.
- **Arbitrary UNO Operations**: Retrying arbitrary UNO operations remains prohibited, as UNO failures typically stem from stale object handles, threading violations, or disposal where retry without reacquisition either hangs or crashes.

### 3.3 Priority 3: Verification, Invariants, and Contract Checks

This is a high priority because it improves confidence over time while keeping runtime complexity modest.

### Shipped Contract Infrastructure

WriterAgent has broadly adopted Design-by-Contract using `deal` (and `plugin/framework/deal_shim.py`) across 57+ modules in `plugin/`:

- **Pure logic and serialization**: `payload_codec.py` (`is_split_grid`, `is_multi_data`, array pack/unpack), `calc_range.py`, `cells.py`, `address_utils.py`.
- **Protocol and State Machines**: MCP wire types (`wire_types.py`), MCP state (`mcp_state.py`), MCP SSE tunnel (`tunnel_state.py`), chat send state (`send_state.py`), tool loop state (`tool_loop_state.py`).
- **Parsing and normalization**: `stream_normalizer.py`, `response_normalizers.py`, `html_stripper.py`, `url_utils.py`, `json_utils.py`.
- **AST and Scripting**: `ast_stmt_edit.py`, `import_policy.py`, `editor_ipc.py`, `sandbox.py`.

The lightweight `deal_shim.py` ensures that contracts are fully enforced during development, testing, and static analysis without adding runtime overhead to LibreOffice extension release builds.

### Concolic Execution and SMT Solving

Beyond runtime contract checks, the repository has implemented SMT-backed concolic execution using CrossHair:
- `make crosshair-check` / `make crosshair-cover`: concolic test generation and contract verification for critical pure modules like `payload_codec.py`.
- `make crosshair-check-all` / `make crosshair-cover-all`: repo-wide sweeps across all `@deal`-annotated modules using streaming subprocess wrappers (`scripts/crosshair_stream.py`).
- For complete design theory, see [docs/framework/formal-verification.md](formal-verification.md).

### 3.4 Priority 4: Lightweight Health Checks

Health checks are useful when they remain simple and local.

### Good health checks

- simple MCP liveness/readiness style checks
- document-health diagnostics
- watchdog-based “this looks stuck” reporting
- lightweight counters for repeated failures on a single path

### Not a priority

- process-wide anomaly detection
- CPU and memory telemetry requiring new runtime dependencies
- predictive monitoring
- dashboards and automated alerting

### Practical health-check goals

- quickly distinguish “feature is temporarily unavailable” from “extension is broken”
- give logs enough context to explain recurring failures
- provide cheap diagnostics for support and debugging

### 3.5 Priority 5: Stronger Validation at Critical Boundaries

Validation is cheap insurance when applied to the right seams.

### Best boundary targets

- document/context creation helpers
- tool arguments entering a mutating operation
- network request construction
- config values before use
- parser/normalizer inputs

### Example

```python
def create_document_context(model, max_context, ctx=None):
    if not isinstance(max_context, int) or max_context <= 0:
        raise ValueError("max_context must be positive integer")
    if not hasattr(model, "supportsService"):
        raise TypeError("model must be UNO document model")
```

### Guidance

- validate early
- make failure messages specific
- do not add noisy validation to every helper if the boundary is already guarded

### 3.6 Priority 6: Testing That Tries To Break Things

Testing takes the complexity burden so runtime code can stay straightforward.

### Shipped Property-Based Testing

WriterAgent uses `hypothesis` extensively across 35+ dedicated verification test suites (`tests/**/*_verification.py`), exercising:

- **Streaming and normalization**: `test_stream_normalizer_verification.py`, `test_accumulate_delta_verification.py`, `test_response_normalizers_verification.py`.
- **Parsing and error payloads**: `test_json_utils_verification.py`, `test_error_payload_verification.py`, `test_html_and_auth_verification.py`.
- **Serialization and AST edits**: `test_serialization_verification.py`, `test_payload_codec_policy_verification.py`, `test_scripting_ast_verification.py`.
- **Protocol and State Machines**: `test_mcp_wire_verification.py`, `test_tunnel_state.py`, `test_fsm_verification.py`.

These tests generate thousands of randomized, adversarial inputs (deeply nested dicts, malformed UTF-8/surrogates, extreme float bounds, degenerate grids) to verify that invariants and contracts hold without unexpected unhandled exceptions.

### Concolic Fuzzing via CrossHair

In addition to property-based tests, symbolic/concolic path exploration via `crosshair cover` automatically generates concrete counterexamples and probes dark corners in serialization and parsing code:

- `make crosshair-check`: SMT solver searches for contract violations.
- `make crosshair-cover`: Guided fuzzing for branch coverage, generating minimal reproducible test cases.
- `scripts/crosshair_check_all.py` / `scripts/crosshair_cover_all.py`: Parallel test sweeps across all contracted modules.

### Fuzzing Guidance

Best candidates:
- malformed JSON fragments
- broken SSE chunks
- repeated or partial deltas
- invalid tool arguments
- corrupt config/history payloads
- invariant-preserving randomized inputs for pure helper code

Less useful candidates:
- direct end-to-end UNO behavior where the oracle is unclear
- large integration fuzzers that mostly fail nondeterministically

## 4. De-Prioritized or Deferred Work

These items are not forbidden forever, but they should not be central to the roadmap right now.

### 4.1 UNO-Wide Circuit Breakers

Why deferred:

- UNO failures are often object-specific, threading-specific, or disposal-related
- a global breaker risks disabling useful functionality after unrelated failures
- the current architecture already routes side effects carefully through established paths

If revisited later, it should start as a narrow design spike for a single subsystem, not a repo-wide pattern.

### 4.2 Global State Checkpointing

Why deferred:

- broad state checkpointing adds substantial design and correctness risk
- some state is already persisted
- checkpointing tool loops or transient UI state is likely to create confusing recovery semantics

If revisited later, it should be limited to a very specific recovery need.

### 4.3 Predictive Monitoring and Anomaly Detection

Why deferred:

- too heavy for the current desktop-extension context
- requires baselines, metrics, and operational interpretation
- likely adds more machinery than reliability

### 4.4 Broad Resource-Manager and Locking Frameworks

Why deferred:

- easy to over-engineer
- risky around UNO object lifecycles
- should only be added in response to a concrete race or lifecycle bug

### 4.5 Formal State Systems Not Tied To Existing FSMs

Why deferred:

- WriterAgent already has real state machines in the sidebar/tool-loop code
- introducing parallel formal state structures risks duplication and drift

## 5. Failure Modes That Matter Most

This section should guide actual work and triage.

| Component | Failure Mode | Typical Impact | Preferred Detection | Preferred Recovery |
|-----------|--------------|----------------|---------------------|--------------------|
| Document model | Stale or disposed UNO object | Current action fails | Typed UNO error, failed operation | Reacquire model if safe, otherwise degrade or abort clearly |
| Network request | Timeout, dropped connection, transient protocol failure | Partial response or no response | Network exception, retry exhaustion | Bounded retry, reconnect, clear message |
| Stream parsing | Malformed chunk, repeated chunk loop, partial delta | Broken or stuck streaming output | Defensive parser checks, repeat guard | Skip bad chunk, abort stream safely if needed |
| Config persistence | Invalid or corrupt config data | Misconfiguration or startup issues | Validation failure | Use validated defaults where possible |
| History persistence | SQLite unavailable or failing | Loss of richer persistence path | Backend init failure | Fallback to JSON |
| Long-running operations | Hang or apparent stall | UI appears frozen | Watchdog, timeout, lack of progress | Log, show degraded status, allow abort |

## 6. Implementation Roadmap

### Phase 1: Baseline Reliability (near term)

- [x] Rewrite task priorities around practical recovery and graceful degradation
- [x] Document the main failure modes and preferred recovery paths
- [x] Identify pure helpers and boundary functions that are good candidates for `deal` contracts or invariant checks
- [x] Reuse an existing contract library such as `deal` (`plugin/framework/deal_shim.py`) instead of building custom decorator infrastructure
- [x] Tighten validation on a small set of critical boundaries (config schema, MCP schemas, tool schemas, payload codec)
- [x] Standardize bounded retry policy for network hot paths (`request_controls.py`, `http_transport.py`, `llm_client.py`, `tunnel_state.py`)
- [x] Improve logging consistency for retry exhaustion, backoff attempts, and degraded-mode fallbacks
- [x] Clarify current watchdog and health-check expectations in docs and code comments

### Phase 2: User-Visible Resilience

- [ ] Add graceful degradation to the most failure-prone document and chat paths (fallback to text/selection when full document XHTML export fails)
- [ ] Improve stale-object recovery where reacquisition is safe and obvious (reacquiring active document model on disposal in sidebar chat)
- [x] Extend `deal` contract coverage and invariant checks to high-value pure logic paths (57+ modules)
- [ ] Expand lightweight health checks and diagnostics (session failure counters, guided recovery hints in sidebar)
- [x] Ensure stop/cancel behavior remains correct when retries are introduced (`wait_abortable` with chunked sleep and `stop_checker`)
- [x] Add targeted tests for fallback behavior, retry math, and recovery decisions (`test_request_controls.py`, `test_http_transport.py`, `test_client_llm.py`)

### Phase 3: Adversarial Verification

- [x] Expand fuzz testing for parsers, streaming normalizers, and config/history loading (CrossHair concolic fuzzing via `scripts/crosshair_check_all.py`, `scripts/crosshair_cover_all.py`)
- [x] Add property-style tests for pure helpers with clear invariants (35+ Hypothesis test suites under `tests/`)
- [x] Use invariant helpers as test oracles where practical (`payload_codec` envelope detector, AST statement edits)
- [x] Add failure-injection tests for network retry and fallback behavior (`test_request_controls.py`, `test_http_transport.py`, `test_client_llm.py`)
- [x] Extend integration tests only where the expected outcome is stable and valuable
- [x] Use regression tests to lock in fixes from real bugs
- [ ] Continuous automated fuzzing pipeline in CI for streaming decoders and JSON parsers

### Phase 4: Revisit Only If Needed

- [ ] Evaluate whether any deferred complexity is justified by repeated real-world failures
- [ ] Only consider larger reliability mechanisms after a concrete pain point is measured
- [ ] Require a design note before introducing global coordination layers

## 7. Release Checklist

Use this checklist for robustness-oriented changes:

- [ ] `make typecheck` passes
- [ ] `make typecheck` passes when the change is type-sensitive
- [ ] Tests for the specific files modified pass (run full `make test` ONLY IF making large refactors or cross-cutting changes)
- [ ] New fallback behavior has a focused regression test when practical
- [ ] Retry behavior has clear bounds and does not ignore user stop/cancel
- [ ] User-visible degraded paths return a clear message
- [ ] Logs contain enough context to explain the failure without flooding
- [ ] Docs mention any new recovery or diagnostic behavior

## 8. Success Metrics

The best metrics here are practical and release-oriented.

### Leading indicators

- fewer bug reports about stuck or unrecoverable chat operations
- fewer crashes or broken sessions caused by stale UNO objects
- better recovery from transient network failures
- fewer “unknown error” reports without useful logs

### Engineering indicators

- critical fallback paths have regression tests
- high-value pure helpers have explicit invariants or contract checks
- high-risk parsing paths have adversarial tests
- retry behavior is explicit and consistent
- recovery procedures are documented for common failure classes

## 9. Decision Rules

When deciding whether a robustness proposal belongs in this roadmap, ask:

1. Does it improve an existing feature instead of adding a new subsystem?
2. Does it make a common failure easier to recover from?
3. Can it be localized to one or two hot paths?
4. Would tests carry most of the added complexity instead of runtime code?
5. Is it grounded in how WriterAgent already works?

If the answer to most of these is no, it probably belongs in the deferred section instead of the main roadmap.

## 10. Deferred framework hygiene

Smells worth remembering, with **designs we are not doing**. Smaller follow-ups only if a real change already touches the file.

| Smell | Do not do | Smaller if ever |
|---|---|---|
| `ConfigService._config_path` ifs in get/set/remove | `ConfigBackingStore` interface | **done:** the ifs are gone; `get` / `set` / `remove` / `get_dict` call the config module; tests patch `_config_path`; do not add `ConfigBackingStore` |
| `MODULES is _DEFAULT_MODULES` dual dotted-key paths | delete the loop path without a dedicated test pass | rebuild dicts only in `set_manifest_modules` later |
| `ToolBaseDummy` copies `get_collection` / `get_item` | mixin / second ABC | **done:** Dummy no longer copies those helpers (`_tool_error` stays) |
| `SafeLogger` vs `safe_log_exception` vs `log_exception` | do not merge into one wrapper | `SafeLogger` and `safe_log_exception` are removed (they had no production callers); production paths are `log.exception` and `log_exception` |
| `to_mcp_schema` name switches | `postprocess_mcp_schema` on every tool | move only `write_formula_range`/`values`; keep generic `range` string\|array in the converter |
| `ToolContext` many constructor fields | frozen dataclass + `ToolCallbacks` | keyword-only `__init__` if caller churn is acceptable |
| `load_modules` MRO `__name__` | `issubclass(attr, ModuleBase)` | keep string MRO (duplicate classes on LO `sys.path`) |
| Named test hooks (`_force_marshal_mode`, `_designated_main_thread`) | DI for tests | leave named hooks |

## Conclusion

WriterAgent does not need elaborate reliability theater. It needs dependable behavior on the failure modes it actually sees: stale UNO objects, flaky network calls, malformed streamed data, persistence fallbacks, and occasional hangs.

This roadmap therefore prioritizes:

1. recovery and graceful degradation
2. bounded retries for real I/O
3. verification-oriented work such as invariants, contract decorators, and stronger test oracles
4. lightweight health checks and diagnostics
5. focused validation
6. aggressive testing, including fuzzing where it gives confidence without increasing production complexity

That should make the extension more reliable without making it more esoteric.
