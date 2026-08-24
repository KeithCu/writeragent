# Enforcing UNO Main-Thread Safety & Deadlock Prevention (Compile / Test / Run time)

## 1. The Problem We Are Trying to Kill

LibreOffice's internal architecture is written in C++ and relies heavily on VCL (Visual Class Library) and the UNO (Universal Network Objects) component model. **LibreOffice's VCL/UNO layer is strictly single-threaded.**

When Python code running in the WriterAgent extension touches a PyUNO object from a background worker thread, catastrophic and erratic failures occur:
- **C++ Memory Corruption & Crashes**: Concurrent invocation of UNO interfaces corrupts internal reference counters and dispatch tables.
- **Visual Glitches**: Concurrent UI operations cause the VCL rendering pipeline to draw black menus, blank sidebars, or freeze desktop windows.
- **Deadlocks (Lock Inversion)**: A background worker thread making a blocking UNO call can take an internal C++ solar mutex or dispatch lock while LibreOffice's main thread is waiting on the worker, deadlocking the entire office suite without a Python traceback.

See [`docs/framework-threading.md`](framework-threading.md) and [`docs/framework-streaming-and-threading.md`](framework-streaming-and-threading.md) for the core architectural model: **worker threads perform network I/O, heavy LLM processing, and subprocess IPC; all PyUNO interactions are marshalled back to the main UI thread via [`execute_on_main_thread`](../plugin/framework/queue_executor.py) or [`post_to_main_thread`](../plugin/framework/queue_executor.py).**

### Why Concurrency Bugs Are "Whack-a-Mole"
Historically, threading bugs in this codebase were uncovered only after mysterious production hangs:
- **Timing & Doc-Size Dependent**: Race conditions often do not reproduce on a developer's machine with small documents, but reliably deadlock on large documents or slower machines under GIL contention.
- **No Stack Traces**: When two threads deadlock, neither crashes; the process simply stops responding, leaving no stack trace or error log at the offending call site.
- **Test Invisibility**: Standard unit tests mock UNO calls, and `QueueExecutor` runs inline under `WRITERAGENT_TESTING=1`, meaning unit tests never exercise real thread boundary crossings.

**The Goal**: Make any off-main-thread UNO violation and any synchronous host-dispatch deadlock fail **loudly, deterministically, and immediately** — at author time via linters, in CI via thread-affine mocks, and at runtime via viral proxies — instead of surfacing as rare production deadlocks.

---

## 2. Why Formal Verification (CrossHair / deal) Does Not Help Here

It is critical to understand why our formal verification toolchain ([`docs/framework-formal-verification.md`](framework-formal-verification.md)) cannot solve this problem:

- `deal` and CrossHair prove **value-level properties of pure, single-threaded functions** (e.g. "for all integer inputs $x > 0$, $f(x)$ returns a non-empty string").
- CrossHair executes functions under symbolic execution **in a single thread**. It models neither operating system threads, the Python GIL, nor UNO's C++ thread-affinity constraints. There is no `@deal.pre` contract that can express "this PyUNO object pointer may only be dereferenced from `threading.main_thread()`."
- **Thread affinity is an effect / typestate property** ("which thread is the CPU executing on when this instruction runs"), not a data value property.

The proper computer science model for thread affinity is **Function Coloring & Execution Contexts** (analogous to `async`/`await` in JavaScript/Python or `Send`/`!Send` in Rust):
- **Red functions** are Main-Thread-Only operations (PyUNO, UI, direct document mutations).
- **Blue contexts** are Background Worker threads (I/O, LLM network requests, venv IPC).
- **Yellow contexts** are Synchronous Host/Bridge Execution Contexts (Calc add-in evaluation `=PY()` / `=PROMPT()`, remote PyUNO socket dispatches).

A Blue context may transition to Red across an explicit recoloring boundary (`execute_on_main_thread`). A Yellow context is forbidden from calling blocking Red boundaries because the host/main thread is already waiting. This discipline is enforced through a combination of **runtime tripwires, thread-affine test fixtures, and static taint analysis**.

---

## 3. Function Colors & Execution Contexts Architecture

```
  ┌─────────────────────────────────────────────────────────────┐
  │                           RED                               │
  │                  (Main-Thread / UNO)                        │
  │  - PyUNO services & objects (ctx, desktop, doc models)      │
  │  - UI controllers, frames, windows, dialogs                │
  │  - Document modifications (format, insert, styles)          │
  └──────────────────────────────▲──────────────────────────────┘
                                 │
     recoloring boundary via     │   ILLEGAL from Yellow Context
     execute_on_main_thread()    │   (Deadlock Hazard #402)
                                 │
  ┌──────────────────────────────┴──────────────────────────────┐
  │                           BLUE                              │
  │                   (Background Workers)                      │
  │  - HTTP requests & LLM streaming                            │
  │  - File I/O, local caching, embeddings computation          │
  │  - Subprocess IPC (venv worker, audio recorder)             │
  └─────────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────┐
  │                          YELLOW                             │
  │           (Synchronous Host/Bridge Dispatch)                │
  │  - Calc add-in formula eval: =PY(...), =PROMPT(...)         │
  │  - Remote PyUNO bridge dispatches & XJob triggers           │
  │                                                             │
  │  INVARIANT: May run on worker threads while main thread is  │
  │  synchronously waiting. MUST NOT block on main thread!      │
  └─────────────────────────────────────────────────────────────┘
```

### Understanding Red vs Blue vs Yellow

| Color / Context | Execution Environment | Permitted Operations | Forbidden Operations |
|---|---|---|---|
| **RED** | LibreOffice Main UI Thread | Direct PyUNO calls, UI dialogs, document reads/edits, VCL pump. | Long-running blocking network calls or CPU-heavy loops (freezes UI). |
| **BLUE** | Background Workers (`run_in_background`) | Network I/O, LLM calls, venv IPC, disk access. | Direct PyUNO access (must marshal to Red via `execute_on_main_thread`). |
| **YELLOW** | Synchronous Host Dispatch Context | In-memory computation, venv execution, non-blocking `post_to_main_thread`. | Calling `execute_on_main_thread` (deadlocks against waiting main thread). |

**Note on Yellow as a Context**: Yellow is a dynamic **thread execution context**, not a static property of a function. Functions like `session_key()`, `confirm_unsaved_cell_edit()`, or helper routines may be executed on the main UI thread during user interaction (Red) or on a remote bridge worker thread during formula recalculation (Yellow). The runtime flag `in_sync_host_dispatch()` tracks whether the current thread is currently executing inside a synchronous host dispatch.

### The Two Foundational Facts That Make Enforcement Tractable

1. **Background threads have a single birthplace**:
   All background work in WriterAgent is spawned through [`run_in_background`](../plugin/framework/worker_pool.py) (or a strictly allowlisted set of dedicated server/reader loops: `AsyncProcess` pipes, MCP server daemon, venv worker).
2. **UNO objects have a finite set of sources**:
   All PyUNO objects originate from factory getters in [`plugin/framework/uno_context.py`](../plugin/framework/uno_context.py) (`get_ctx`, `get_desktop`, `get_toolkit`, `get_active_document`, `get_package_info`, `resolve_document_by_url`, `get_document_from_frame`).

By wrapping the sources and tagging the birthplaces, we achieve complete defense-in-depth across three enforcement layers:

---

## 4. Layer A — Runtime Tripwire & Viral Proxy (Catch Immediately on Dev Machine)

**Status:** Shipped in [`plugin/framework/thread_guard.py`](../plugin/framework/thread_guard.py).
**Configuration**: Active by default in all dev and non-release builds (`WRITERAGENT_UNO_THREAD_GUARD=1`). Opt-out via `WRITERAGENT_UNO_THREAD_GUARD=0`.

Layer A converts what would be a silent race condition or production freeze into an **immediate exception with a complete Python stack trace** pointing directly at the offending line.

### A1. Reusable Assert & `@main_thread_only` Decorator
```python
def assert_main_thread(what: str) -> None:
    """Raise (if guard on) or log warning+stack (if guard off) when off the main thread."""
    if on_main_thread():
        return
    task = get_background_task_name() or threading.current_thread().name
    msg = "UNO thread violation: %r touched UNO from background task %r; marshal via execute_on_main_thread()." % (what, task)
    if GUARD_ON:
        _notify_thread_violation(msg)
        raise RuntimeError(msg)
    log.warning(msg, stack_info=True)
```
- Decorates primary UNO entry points (`get_desktop`, `get_active_document`, `confirm_unsaved_cell_edit`, etc.).
- **Dev Builds (`GUARD_ON=1`)**: Displays a deduplicated modal error box on the UI thread and raises `RuntimeError`.
- **Dev Builds with Guard Disabled (`GUARD_ON=0`)**: Logs `log.warning(msg, stack_info=True)` so call sites are captured in logs without crashing user sessions.
- **Production Release OXTs (`make release`)**: Code packaging via `scripts/strip_code.py` replaces `thread_guard.py` with a minimal zero-overhead stub (`GUARD_ON = False`, `assert_main_thread` no-op, proxy unwrapped), while keeping `sync_host_dispatch()` and `in_sync_host_dispatch()` active for deadlock prevention.

### A2. Thread Tagging at Birth
In `run_in_background`, a thread-local task name is stamped on the worker thread for the duration of the task. Pooled workers (`wa-bg-*`) clear the tag in a `finally` block so recycled threads do not carry stale task identifiers. The runtime error message explicitly names the culprit task (e.g. `"touched UNO from background task 'web-search-embeddings'"`).

### A3. Viral Guarding Proxy (`_UnoThreadGuardProxy` / `guard_uno`)
Decorators only guard functions we remember to decorate. To protect arbitrary UNO object graphs (such as `doc.getCurrentController().getViewCursor().getText().getEnd()`), all UNO sources wrap returned objects in `_UnoThreadGuardProxy`:
1. On every attribute lookup (`__getattr__`), method call (`__call__`), property setter (`__setattr__`), item lookup (`__getitem__`), and interface query (`queryInterface`), the proxy invokes `assert_main_thread(...)`.
2. Any PyUNO object returned by an attribute access or method call is **recursively wrapped** in another `_UnoThreadGuardProxy`. Plain Python values (strings, integers, booleans, lists) pass through untouched.
3. If a guarded proxy is passed back into a property setter on a UNO object, the proxy automatically unwraps itself (`_unwrap_uno`) to prevent wrapping overhead from leaking into LibreOffice C++.

### A4. Yellow Context Refusal (`sync_host_dispatch`)
When Calc evaluates an add-in formula like `=PY("1+1")` or `=PROMPT(...)` via a remote PyUNO bridge, execution occurs in a **Yellow Context**:
- **Wrapped Entry Roots**:
  1. [`plugin/calc/python/function.py`](../plugin/calc/python/function.py): `execute_python_addin` enters `with sync_host_dispatch():`. Public add-in entry points `py()` and `python()` forward directly to `execute_python_addin`.
  2. [`plugin/calc/prompt_function.py`](../plugin/calc/prompt_function.py): `execute_prompt_addin` enters `with sync_host_dispatch():`. Public add-in entry point `prompt()` forwards directly to `execute_prompt_addin`.
  3. [`plugin/framework/thread_guard.py`](../plugin/framework/thread_guard.py): `_notify_thread_violation` enters `with sync_host_dispatch():`.
- **Why UI Listeners Are NOT Wrapped**: Standard UNO listeners (e.g. `actionPerformed` in `uno_listeners.py`) execute on LibreOffice's main UI thread during interactive user operations. Wrapping them would unnecessarily tag normal UI thread execution as host dispatches.
- **Refusal Mechanism**: Inside [`QueueExecutor.execute()`](../plugin/framework/queue_executor.py), if `in_sync_host_dispatch()` is True on a non-main thread, execution is **refused immediately** with:
  ```
  RuntimeError: marshal refused: execute_on_main_thread called from synchronous host dispatch context (deadlock hazard #402, fn=...)
  ```
- This prevents the 30-second timeout and lock inversion whenever the Yellow context is entered.

---

## 5. Layer B — Test-Time Enforcement & Determinism

**Status:** Shipped in [`tests/framework/thread_safety.py`](../tests/framework/thread_safety.py) and [`tests/framework/test_thread_affinity.py`](../tests/framework/test_thread_affinity.py).

### B1. Real PyUNO Test Suite with Active Guard (`make lo-test-threadguard`)
Native UNO tests (`plugin/testing_runner.py`) run against a live LibreOffice instance with real C++ PyUNO objects. `make lo-test-threadguard` executes the full suite with `WRITERAGENT_UNO_THREAD_GUARD=1`:
```make
lo-test-threadguard:
	WRITERAGENT_UNO_THREAD_GUARD=1 $(LO_PYTHON) -m plugin.testing_runner; \
	EXIT_CODE=$$?; $(MAKE) lo-kill; exit $$EXIT_CODE
```
Any worker thread that reaches a real UNO object without marshalling aborts the test with a stack trace.

### B2. Pytest Thread-Affine Mocks & Synthetic Pump (`uno_thread_safety` Fixture)
For fast CI tests where LibreOffice is not running:
1. `make_thread_affine_mock(raw_mock)` wraps unit test mocks in a `ThreadAffineMock` that asserts access is only made from the designated main thread.
2. `set_designated_main_thread(pump_thread)` instructs `thread_guard.on_main_thread()` to follow a synthetic test pump thread.
3. `set_force_marshal_mode(True)` disables the `WRITERAGENT_TESTING=1` inline shortcut, forcing `QueueExecutor.execute` to enqueue real `_WorkItem` objects and block until the `TestMainPump` thread drains the queue.
4. If a worker touches a mock directly without `execute_on_main_thread()`, the mock immediately raises `AssertionError`, turning concurrency bugs into deterministic red CI tests.

### B3. Concurrency Regression Test Suite
- `test_yellow_context_refuses_execute_on_main_thread`: Asserts immediate `RuntimeError` when off-main host dispatch attempts blocking marshal.
- `test_yellow_context_allows_inline_when_on_main_thread`: Asserts GUI formula evaluation on main thread executes inline without errors.
- `test_notify_thread_violation_never_blocks`: Asserts guard violation reporting uses non-blocking `post_to_main_thread`.
- `test_charts_process_events_regression_must_marshal`: Prevents regressions of the chart event loop hang (commit `0cfc6891`).

---

## 6. Layer C — Build-Time Static Analysis & Linters

Executed automatically via **`make test`** and **`make uno-thread-lint`** (**`make opengrep-lint`** + **`make thread-safety-lint`**).

```
                      ┌────────────────────────┐
                      │    make uno-thread-lint │
                      └───────────┬────────────┘
                                  │
         ┌────────────────────────┴────────────────────────┐
         │                                                 │
┌────────▼───────────────┐                     ┌───────────▼────────────┐
│   make opengrep-lint   │                     │ make thread-safety-lint│
│ (tests/semgrep/*.yml)  │                     └───────────┬────────────┘
└────────────────────────┘                                 │
                                   ┌───────────────────────┴───────────────────────┐
                                   │                                               │
                      ┌────────────▼───────────────┐                  ┌────────────▼───────────────┐
                      │ scripts/lint_thread_safety │                  │ scripts/analyze_thread_     │
                      │ (AST structural visitor)   │                  │  deadlocks.py (Call Graph) │
                      └────────────────────────────┘                  └────────────────────────────┘
```

### C1. Opengrep Taint Analysis ([`tests/semgrep/uno_thread_safety.yml`](../tests/semgrep/uno_thread_safety.yml))
Uses `opengrep scan --taint-intrafile` to track cross-function dataflow within files:
- **Blue Roots (Taint Sources)**: Functions decorated with `@background`, worker functions passed to `run_in_background()`, and add-in entry points.
- **Red Sinks (UNO Operations)**: `uno_context` getters, `createUnoService`, `createInstanceWithContext`, `uno.getComponentContext`, document format/edit helpers.
- **Sanitizers**: `execute_on_main_thread()`, `post_to_main_thread()`, and enclosing `if on_main_thread():` branches.
- **Core Rules**:
  - `uno-off-main-thread` (ERROR): Flags direct UNO access in background workers.
  - `raw-uno-thread-ban` (ERROR): Rejects raw `threading.Thread`/`Timer` instantiation outside approved subsystems.
  - `blocking-marshal-in-sync-dispatch` (ERROR): Rejects `execute_on_main_thread` inside add-in evaluations and synchronous callbacks.
  - `raw-process-events-to-idle` (ERROR): Rejects direct VCL event pumps outside approved queue drain points.

### C2. Custom AST Linter ([`scripts/lint_thread_safety.py`](../scripts/lint_thread_safety.py))
Parses Python ASTs across add-in and scripting boundary modules to enforce:
1. `unguarded-uno-access`: UNO source getters (`get_desktop`, `get_ctx`, `_get_calc_doc`) must be structurally protected by an `if on_main_thread():` block or `@main_thread_only` decorator.
2. `blocking-marshal-in-sync-dispatch`: Synchronous add-in functions (`execute_python_addin`, `execute_prompt_addin`, `session_key`) cannot contain calls to `execute_on_main_thread`.

### C3. Static Lock Hierarchy & Transition Analyzer ([`scripts/analyze_thread_deadlocks.py`](../scripts/analyze_thread_deadlocks.py))
Builds a global class-aware function call graph across `plugin/` starting from `SYNC_HOST_ENTRYPOINTS`:
- **Add-ins & Scripting Roots**: `execute_python_addin`, `execute_prompt_addin`, `py`, `python`, `prompt`, `session_key`, `_notify_thread_violation`.
- Walks call edges to detect if any synchronous host dispatch can reach `BLOCKING_OPERATIONS` (`execute_on_main_thread`).
- **Why Listeners Are Excluded from Static Yellow Roots**: Standard listener methods (`actionPerformed`, `textChanged`) are UI handlers on the main thread that legitimately initiate asynchronous worker pipelines. Treating them as static Yellow roots creates ongoing false positives across UI code. Static analysis focuses strictly on wrapped add-in and bridge roots; runtime Layer A (`in_sync_host_dispatch()`) protects against any off-main dispatch.
- **Suppression Mechanism**: Supports inline `# nodeadlock: <reason>` annotations if an edge needs manual verification override.

---

## 7. Infection-Start Chokepoints (Layer A Reference)

All UNO objects must be wrapped at birth using `guard_uno(obj)` or obtained via `get_ctx()` (which is pre-wrapped). Direct unmanaged calls to `uno.getComponentContext()` are strictly prohibited.

| Location | File Path | Guard Mechanism / Role |
|---|---|---|
| Primary UNO getters | `plugin/framework/uno_context.py` | Wrapped via `guard_uno()` on `get_ctx()`, `get_desktop()`, `get_active_document()`, `get_toolkit()`, `get_package_info()`. |
| Document Model Resolver | `plugin/framework/uno_context.py` | `resolve_document_by_url()` returns `guard_uno(doc)`. |
| Panel Frame Resolver | `plugin/chatbot/panel.py`, `panel_factory.py` | `_get_document_model()` resolves frame controller model with `guard_uno`. |
| Hidden Document Loader | `plugin/doc/document_research.py` | `open_document_for_read()` guards hidden component model. |
| Desktop Enumeration | `plugin/doc/document_research.py` | `_office_model_from_desktop_element()` guards enumerated desktop models. |
| Scripting Calc Resolver | `plugin/scripting/document_scripts.py` | `get_calc_document_from_ctx()` wraps active sheet document. |
| Calc Add-in Doc Lookup | `plugin/calc/python/function.py` | `_get_calc_doc()` returns `None` off-main (#402), guards on-main. |
| Calc Cell Editor Selection | `plugin/calc/python/editor.py` | `_get_active_calc_cell()` guards active cell interface. |
| Graphic Export Bridge | `plugin/writer/images/image_tools.py` | `export_graphic_to_bytes()` resolves via `get_ctx()`. |
| Locale Resolution | `plugin/framework/i18n.py` | `get_lo_locale()` uses `get_ctx()` on-main, falls back to `en_US` off-main. |
| MCP Context Resolution | `plugin/mcp/mcp_protocol.py` | Context resolution uses `get_ctx()`, not raw bootstrap context. |

### Intentionally Unwrapped Boundaries (By Design)
- `QueueExecutor._get_async_callback`: Unwraps context before creating `com.sun.star.awt.AsyncCallback` service to avoid bootstrap deadlocks during executor initialization.
- `main.py` Menu-Icon `GraphicProvider`: Runs exclusively on the main UI thread during extension load; does not leak document model references.

---

## 8. Case Studies & Resolved Deadlocks

### Case Study 1: Synchronous Bridge & Add-in Deadlock (Issue #402)
- **The Bug**: Assigning `=PY(...)` via remote PyUNO (`sheet.getCellByPosition(0, 0).FormulaLocal = '=PY("1+1")'`) deadlocked LibreOffice against `MainThread`:
  1. The remote UNO dispatch executed Calc formula recalculation synchronously on a remote PyUNO bridge worker thread (`Dummy-2`).
  2. `workbook_session_id()` called `execute_on_main_thread(_workbook_session_id_impl)` (waiting up to 30s).
  3. LibreOffice's main thread was synchronously blocked waiting for the remote UNO RPC dispatch to finish, so it could not pump the `QueueExecutor` work queue.
  4. `session_key()`, `get_python_init_kwargs()`, and `_diagnostics_workbook_key()` called `get_desktop` without checking `on_main_thread()`, tripping the Layer A guard.
  5. The runtime guard's `_notify_thread_violation()` attempted a blocking `execute_on_main_thread(_show_popup, timeout=5.0)`, freezing the thread.
  6. When 30s elapsed, `_format_error_for_display()` mapped the timeout to a misleading `"Error: Python timed out. Open Settings → Python..."` message.
- **The Fix Applied**:
  - **Config-first mode check**: `workbook_session_id()` checks `python_session_mode(ctx)` before touching threads or marshalling.
  - **Off-main guard on all add-in UNO lookups**: `session_key()`, `get_python_init_kwargs()`, and `_diagnostics_workbook_key()` check `on_main_thread()` and return safe no-UNO defaults off-main.
  - **Yellow context refusal**: `sync_host_dispatch()` context manager marks Yellow thread state; `QueueExecutor.execute` immediately refuses blocking calls off-main.
  - **Non-blocking guard notifications**: `_notify_thread_violation()` uses non-blocking `post_to_main_thread()` only when `AsyncCallback` is ready, and never blocks the worker thread.
  - **Distinct timeout classification**: `TimeoutError` from host execution formats distinctly from venv worker execution timeouts.

### Case Study 2: Calc Charts Process Events Hang (Commit `0cfc6891`)
- **The Bug**: `_process_events()` in `plugin/calc/charts.py` called `toolkit.processEventsToIdle()` on a path that could run without an active frame or on background worker threads.
- **The Fix**: Direct VCL event pumps are restricted to approved UI drain chokepoints (`pump_ui_idle` / `process_events_to_idle`) and verified by Semgrep rule `raw-process-events-to-idle`.

### Case Study 3: Shared-Kernel =PY() Recalc on Yellow Context & UI-Thread Cached Sessions (Issue #411)
- **The Bug**: During Calc formula recalculations on background/bridge threads (`sync_host_dispatch()`), `=PY()` formulas in shared mode evaluated without an explicit `doc` argument (`PythonFunction(ctx)` default constructor). Attempting to resolve the active document via `_calc_document(ctx)` / `desktop.getCurrentComponent()` / `desktop.getComponents()` off-main tripped the Layer A thread guard in dev builds (`UNO call wrapper failed`), and risked first-matching the wrong workbook in release builds. Furthermore, after Resetting the Python Session, custom helper functions defined in the init script threw sandbox `Forbidden function evaluation` if helper tools were not re-seeded into the worker executor.
- **The Fix Applied**:
  - **Yellow context desktop query prohibition**: Formula execution under `sync_host_dispatch()` is strictly forbidden from querying `desktop.getCurrentComponent()` or `desktop.getComponents()`.
  - **UI-Thread-Cached Session String & Init Kwargs**: The active Calc session ID and init kwargs are recorded on the UI thread (`record_active_calc_session`) during workbook load, focus, and reset. Off-main formula recalculations retrieve the cached session string and init kwargs (`get_cached_calc_session_id`, `get_cached_calc_init_kwargs`) without touching UNO.
  - **Proxy Unwrapping & Fallback Resilience**: `_workbook_session_key()` unwraps `_UnoThreadGuardProxy` (`_unwrap_uno(doc)`) before accessing `getURL()` / custom document properties, avoiding nested proxy failures.
  - **Immediate Init Script & Helper Re-Seed**: `reset_workbook_python_session()` clears both the base `calc:…` and `:init` companion session in the worker sandbox, and immediately re-evaluates the init script to restore custom tools (`custom_tools`) and bindings across both shared and isolated sessions.
  - **`safe_uno_call` Distinction**: `safe_uno_call` re-raises `DocumentDisposedError` / `DisposedException` while returning `default` for non-disposal bridge failures.

---

## 9. Specialized Sub-Agents & Tools Threading

Specialized sub-agents (`plugin/doc/specialized_base.py`) run `DelegateToSpecializedBase.execute` on background worker threads when `is_async()` is True.
- **Scaffolding**: `get_tools(doc=...)`, shapes canvas, and open-documents enumeration must marshal through `execute_on_main_thread()`.
- **Sync Domain Tools**: Run via `SmolToolAdapter(main_thread_sync=True)` which marshals tool execution to the main thread.
- **Async Domain Tools** (`image_generate`, `delegate_read_document`): Run on caller worker threads and must marshal PyUNO access internally inside their own `execute()` methods. Verified in [`tests/doc/test_specialized_delegation_threading.py`](../tests/doc/test_specialized_delegation_threading.py).

---

## 10. Summary of Architectural Invariants

1. **No PyUNO Off-Main**: All PyUNO service instantiation, method calls, property reads/writes, and interface queries must execute on `threading.main_thread()`.
2. **Workers Spawn via `run_in_background`**: Raw `threading.Thread` and `threading.Timer` instantiation is banned outside vetted allowlists.
3. **No Blocking Marshal in Yellow Context**: Functions executing inside synchronous host dispatches (Calc add-in evaluation) must never call `execute_on_main_thread()`.
4. **Viral Proxy on UNO Sources**: All new UNO object sources must return `guard_uno(obj)` to propagate runtime checking across object graph traversals.
5. **Non-Blocking Error Reporting**: Concurrency guard notifications must use `post_to_main_thread()` and never block background workers.

---

## 11. Open Items & Ongoing Audits (Living Document)

The following items are tracked for future enhancement:

| Item | Description & Rationale |
|---|---|
| **Native Socket-Bridge `=PY("1+1")` under `lo-test-threadguard`** | GUI formula bar recalculation executes on the main thread and hides bridge worker issues. Adding a native test case that assigns formulas over a socket bridge will exercise remote bridge execution paths against live LibreOffice. |
| **Opengrep Inter-File Taint (`--taint-interfile`)** | Opengrep inter-file taint is currently in alpha (`v1.28.0-interfile.alpha.2`). Until mature, the gate uses `--taint-intrafile` and cross-file workers rely on explicit `@background` decorators. |
| **AST Linter Target Scope** | Default scan targets add-in and scripting directories (`plugin/calc/python`, `plugin/scripting`). As async tools expand in `plugin/chatbot` and `plugin/embeddings`, consider extending custom AST visitor rules to additional specialized tool modules. |
| **`uno_thread_safety` Pytest Fixture Adoption** | The fixture is currently opt-in for unit tests. Expanding its default use in tests that touch document helpers ensures off-main mock access is caught early in unit suites. |
| **Infection-Start Chokepoint Audits** | The viral proxy (`_UnoThreadGuardProxy`) relies on all factory origins wrapping returned objects in `guard_uno`. Any new UNO service factory or model loader must be audited to ensure it wraps returned objects at birth. |
| **`MainThreadToken` Deprecation / Adoption** | `plugin/framework/thread_token.py` provides nominal type tokens for static checkers. Since type coloring is currently handled by Opengrep taint rules and runtime guards, evaluate whether to plumb strict tokens across red APIs or deprecate the module. |

---

## Cross-References

- [`docs/framework-threading.md`](framework-threading.md) — Pool architecture, drain ownership, and subprocess IPC pipe safety.
- [`docs/framework-streaming-and-threading.md`](framework-streaming-and-threading.md) — Drain loop, cancellation, and `execute_on_main_thread` checklist.
- [`docs/framework-formal-verification.md`](framework-formal-verification.md) — Why value-level formal verification (CrossHair/deal) does not apply to thread affinity effect typing.
