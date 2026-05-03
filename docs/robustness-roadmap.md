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

- unified debug logging
- optional agent log
- global exception hooks
- a watchdog that can flag stalled activity

This is already a good base for practical reliability work. The next step is better classification, better docs, and better recovery behavior, not an elaborate telemetry stack.

### 1.3 Existing Network Resilience

Current network behavior in `plugin/modules/http/client.py` already includes:

- persistent connections
- bounded request timeouts
- a local HTTPS certificate fallback path
- a fresh-connection retry on some transient streaming failures
- defensive handling for malformed streaming payloads
- a guard against repeated streaming chunks

The roadmap should treat this as the baseline and improve it with clearer retry policy and tests.

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

Retries are valuable when they are narrow, explicit, and limited.

### Good retry targets

- network requests in `plugin/modules/http/client.py`
- network-adjacent MCP operations where transient failure is plausible
- file I/O around config/history persistence when partial transient failure is realistic

### Bad retry targets

- broad retries around arbitrary UNO operations
- hidden retries that ignore user stop/cancel intent
- retries that make side effects ambiguous

### Retry policy guidelines

- small attempt count, usually 2-3
- exponential backoff with jitter
- only retry clearly transient exceptions
- preserve stop/cancel semantics
- log retry count and final failure reason

### Example shape

```python
def with_retry(func, max_attempts=3, base_delay=0.1):
    attempt = 0
    while attempt < max_attempts:
        try:
            return func()
        except RetryableError:
            attempt += 1
            if attempt >= max_attempts:
                raise
            time.sleep(base_delay * (2 ** (attempt - 1)))
```

The roadmap should avoid presenting retries as a global abstraction first. Start with the concrete hot paths where it matters.

### 3.3 Priority 3: Verification, Invariants, and Contract Checks

This is a high priority because it improves confidence over time while keeping runtime complexity modest.

### Target areas

- pure parsing and normalization helpers
- state transitions with clear legal/illegal moves
- functions that transform data into structured payloads
- boundary functions where a bad argument quickly becomes a confusing downstream failure

### Good patterns

- `deal` preconditions and postconditions on critical pure entry points
- `deal` invariants where the target is stable and mostly pure
- optional postcondition checks for pure helper outputs
- invariant helpers reused by both tests and debug paths
- property-style tests that continuously exercise those invariants

### Example shape

```python
import deal

@deal.pre(lambda max_context: isinstance(max_context, int) and max_context > 0)
@deal.post(lambda result: isinstance(result, str))
def normalize_context_limit(max_context):
    return str(max_context)
```

### Guidance

- use this first where invariants are clear and stable
- keep most checks close to pure logic and boundary validation
- avoid turning every UNO interaction into a contract framework
- prefer `deal` or another established contract tool over custom decorator code
- let tests do the heavy lifting when runtime checks would be noisy

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

Testing should take more of the complexity burden so runtime code can stay straightforward.

### Property-based and fuzz testing

These are good fits when applied to code that is deterministic and pure or mostly pure:

- JSON parsing wrappers
- string/stream normalization helpers
- protocol parsing
- delta accumulation
- range and bounds logic
- config validation logic

### Example

```python
from hypothesis import given
from hypothesis.strategies import text, integers

@given(text(), integers(min_value=1, max_value=10000))
def test_safe_string_operation(s, max_len):
    result = safe_string_operation(s, max_len)
    assert isinstance(result, str)
    assert len(result) <= max_len
```

### Fuzzing guidance

Fuzz testing is explicitly in scope for this roadmap. It increases test complexity, but that is acceptable because it does not make production code more esoteric.

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

- [ ] Rewrite task priorities around practical recovery and graceful degradation
- [ ] Document the main failure modes and preferred recovery paths
- [ ] Identify pure helpers and boundary functions that are good candidates for `deal` contracts or invariant checks
- [ ] Reuse an existing contract library such as `deal` instead of building custom decorator infrastructure
- [ ] Tighten validation on a small set of critical boundaries
- [ ] Standardize bounded retry policy for network and file I/O hot paths
- [ ] Improve logging consistency for retry exhaustion and degraded-mode fallbacks
- [ ] Clarify current watchdog and health-check expectations in docs and code comments

### Phase 2: User-Visible Resilience

- [ ] Add graceful degradation to the most failure-prone document and chat paths
- [ ] Improve stale-object recovery where reacquisition is safe and obvious
- [ ] Extend `deal` contract coverage and invariant checks to high-value pure logic paths
- [ ] Expand lightweight health checks and diagnostics
- [ ] Ensure stop/cancel behavior remains correct when retries are introduced
- [ ] Add targeted tests for fallback behavior and recovery decisions

### Phase 3: Adversarial Verification

- [ ] Expand fuzz testing for parsers, streaming normalizers, and config/history loading
- [ ] Add property-style tests for pure helpers with clear invariants
- [ ] Use invariant helpers as test oracles where practical
- [ ] Add failure-injection tests for network retry and fallback behavior
- [ ] Extend integration tests only where the expected outcome is stable and valuable
- [ ] Use regression tests to lock in fixes from real bugs

### Phase 4: Revisit Only If Needed

- [ ] Evaluate whether any deferred complexity is justified by repeated real-world failures
- [ ] Only consider larger reliability mechanisms after a concrete pain point is measured
- [ ] Require a design note before introducing global coordination layers

## 7. Release Checklist

Use this checklist for robustness-oriented changes:

- [ ] `make check` passes
- [ ] `make typecheck` passes when the change is type-sensitive
- [ ] `make test` passes when the change affects behavior broadly enough to justify it
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
