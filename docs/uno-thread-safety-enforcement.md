# Enforcing UNO Main-Thread Safety (Compile / Test / Run time)

## The problem we are trying to kill

LibreOffice's VCL/UNO layer is **single-threaded**. A UNO call from a background
thread can corrupt internal C++ state, draw black menus, or — most painfully —
take an internal lock that the main thread is already waiting on and **deadlock**
the whole application. See [`docs/threading_architecture.md`](threading_architecture.md)
and [`docs/streaming-and-threading.md`](streaming-and-threading.md) for the model
we already use to avoid this (worker threads do I/O only; UNO is marshalled back
to the main thread via [`execute_on_main_thread`](../plugin/framework/queue_executor.py)).

The model is correct. The problem is **enforcement**. Today we find each
violation by hand, usually after a hang. The fix in commit
`0cfc6891b679f3fcc2ad4a47107763a1b5bd93d7` ("fix potential hangs in charts") is
the canonical example: `_process_events()` in [`plugin/calc/charts.py`](../plugin/calc/charts.py)
was calling `toolkit.processEventsToIdle()` on a path that could run without an
active frame, and the patch added a guard plus a `WRITERAGENT_TESTING` short-circuit.
That is whack-a-mole. Each such bug:

- May **not reproduce on the developer's machine** (timing/GIL/doc-size dependent).
- Manifests far away from the cause (a deadlock has no useful stack at the call site).
- Is invisible to our current test suite (UNO is mocked, and the executor runs
  inline under `WRITERAGENT_TESTING=1`, so the thread boundary is never exercised).

The goal of this document: **make an off-main-thread UNO call fail loudly and
deterministically — ideally at author time, otherwise in CI, and at worst the
instant it happens on the dev machine — instead of as a rare production deadlock.**

## Why formal verification ([`docs/formal_verification.md`](formal_verification.md)) does **not** help here

It is worth stating plainly so we don't spend effort in the wrong place.

`deal` + CrossHair (our FV toolchain) prove **value-level** properties of **pure,
single-threaded** functions: "for all inputs, this post-condition holds." CrossHair
runs the function under symbolic execution **in one thread**; it models neither
real threads, the GIL, nor UNO's thread affinity. There is no `@deal.pre` that can
express "this object may only be touched from `threading.main_thread()`."

Thread-affinity is not a value property — it is an **effect/typestate** property
("which thread is the program counter on when this call happens"). The correct
analogy is **function coloring** (like `async`/`await`, or Rust's `Send`/`!Send`):
some functions are "red" (main-thread-only) and some contexts are "blue"
(background). A blue context may not call a red function except through a
recoloring boundary (`execute_on_main_thread`). That discipline is enforced by
**linting + runtime guards**, not by an SMT solver. The rest of this doc is about
building that coloring cheaply on top of what we already have.

## The single invariant to enforce

> **No PyUNO access off the LibreOffice main thread. A background thread that needs
> UNO must cross the boundary via `execute_on_main_thread` / `post_to_main_thread`
> (or the `QueueExecutor`).**

"PyUNO access" = constructing a UNO service, or calling any method / reading any
property on a UNO object (`ctx`, `desktop`, the document model, frames,
controllers, the toolkit, text cursors, cells, shapes, dialogs, …).

Two facts make enforcement tractable:

1. **There is one place background threads are born:**
   [`run_in_background`](../plugin/framework/worker_pool.py) (plus a small set of
   siblings: `threading.Thread` direct use, `threading.Timer`, `AsyncProcess`
   reader/exit callbacks, the `BatchingStreamQueue` timer, and smolagents worker
   entrypoints). If those are the only "blue roots," the linter has a finite set
   of roots to walk.
2. **There is a small set of places UNO objects are born:** the getters in
   [`plugin/framework/uno_context.py`](../plugin/framework/uno_context.py)
   (`get_ctx`, `get_desktop`, `get_toolkit`, `get_active_document`,
   `get_package_info`) and document resolution in
   [`plugin/doc/document_helpers.py`](../plugin/doc/document_helpers.py). If those
   are the only "red sources," a runtime guard can be viral from a handful of
   chokepoints.

We already have a partial version of this: the runtime guard in
[`ToolBase.execute_safe`](../plugin/framework/tool.py) (≈ lines 320–325) raises a
`Thread Safety Violation` if a **synchronous tool** runs off the main thread, with
a `bypass_thread_guard` escape hatch for the DSPy eval worker. The weakness is that
it only fires at the **tool boundary**. The charts hang, the grammar workers, the
embeddings UI, and every direct `uno_context` getter live *below* or *beside* that
boundary and are unguarded. The proposals below generalize that one check into a
defense-in-depth system.

---

## Layer A — Runtime tripwire (cheapest; deterministic "catch on my machine")

**Status:** Implemented (A1+A2+A3). Guard module + tagging + decoration of sources + viral proxy in `plugin/framework/thread_guard.py`. See `tests/framework/test_thread_guard.py`.

Make the **first** illegal UNO touch raise immediately, with a full Python stack
trace pointing at the exact offending line — long before any lock is taken. This
is the highest value-per-effort option and directly satisfies the user's
"catch it on my machine rather than wait for a deadlock" requirement.

### A1. Reusable assert + `@main_thread_only` decorator (hours)

Extract the existing tool-boundary check into a tiny shared helper, e.g. in
`plugin/framework/thread_guard.py`:

```python
import os, threading, logging
log = logging.getLogger("writeragent.threadguard")

GUARD_ON = os.environ.get("WRITERAGENT_UNO_THREAD_GUARD") == "1"

def on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()

def assert_main_thread(what: str) -> None:
    """Raise (guard on) or warn-with-stack (guard off) if off the main thread."""
    if on_main_thread():
        return
    msg = ("UNO thread violation: %r touched UNO from %s; marshal via "
           "execute_on_main_thread()." % (what, threading.current_thread().name))
    if GUARD_ON:
        raise RuntimeError(msg)
    log.warning(msg, stack_info=True)   # full stack, no crash, for release/field logs

def main_thread_only(fn):
    def wrapper(*a, **k):
        assert_main_thread(getattr(fn, "__qualname__", fn))
        return fn(*a, **k)
    return wrapper
```

Then:

- Decorate the UNO **sources** in `uno_context.py` (`get_desktop`,
  `get_active_document`, `get_toolkit`, `get_package_info`) and the hottest doc
  helpers / appliers (`document_helpers` resolution, `format_support.apply_*`).
- Have `ToolBase.execute_safe` call `assert_main_thread(self.name)` instead of its
  inline check (single source of truth; keep `bypass_thread_guard`).

Cost: a few decorators, one module. Payoff: any decorated function called from a
worker aborts at the call site with a stack trace when `WRITERAGENT_UNO_THREAD_GUARD=1`.
**Default-off so release builds pay nothing**; the developer (and CI) run with it on.

### A2. Tag background threads at their one birthplace (≈1 hour)

In `run_in_background`, set a thread-local marker (and a clear thread name). The
guard message can then say *which* background task is at fault
("inside background task `run_search`"), which makes triage trivial. Also lets the
guard distinguish "legitimately on a non-main thread that never touches UNO" from
"a worker that reached a red function."

### A3. Viral guarding proxy on the UNO sources (half day; strongest runtime option)

A decorator only guards functions we remembered to decorate. To cover **arbitrary**
UNO object graphs (e.g. `doc.getCurrentController().getViewCursor().getText()`),
wrap the few UNO *sources* in a debug-only proxy that:

1. On every attribute access / call, runs `assert_main_thread(...)`.
2. **Recursively wraps** any returned PyUNO object, so the guard follows the object
   graph from `ctx` / `desktop` / the document model outward.

PyUNO objects are identifiable at runtime (e.g. `type(obj).__module__` is `pyuno`,
or presence of `__pyunostruct__` / `XInterface` query support); the proxy only
wraps those and passes plain Python values through untouched. Install it **only**
when `WRITERAGENT_UNO_THREAD_GUARD=1` so production is byte-for-byte unchanged.

This converts "any UNO call anywhere off the main thread" into an immediate,
located exception, with **zero per-call-site annotation**. It is the closest thing
to a hardware watchpoint we can get in Python.

> Note on the existing `WRITERAGENT_TESTING=1` shortcut: `QueueExecutor.execute`
> and charts `_process_events` currently *skip* real behavior under testing. That
> is fine for unit tests, but it means the thread boundary is never crossed in
> tests. The guard must be exercised in a mode where marshalling actually happens
> (see Layer B).

---

## Layer B — Test-time enforcement (deterministic in CI; no LibreOffice needed)

**Status:** Implemented (B1+B2+B3). Makefile targets `lo-test-threadguard` /
`lo-test-threadguard-visible`; pytest helpers in
[`tests/framework/thread_safety.py`](../tests/framework/thread_safety.py) and
opt-in fixture `uno_thread_safety` in
[`tests/framework/conftest.py`](../tests/framework/conftest.py); tests in
[`tests/framework/test_thread_affinity.py`](../tests/framework/test_thread_affinity.py).

The aim: a `run_in_background` worker that forgets to marshal a UNO call should
**fail a test**, not pass quietly.

### B1. Run the real UNO suite with the guard on (low effort, high value)

**Status:** Done. `make lo-test-threadguard` runs the full native suite with
`WRITERAGENT_UNO_THREAD_GUARD=1`. `WRITERAGENT_TESTING=1` (set by
[`plugin/testing_runner.py`](../plugin/testing_runner.py)) only short-circuits
`QueueExecutor` inline execution — it does **not** disable the Layer A guard.

The native UNO tests (`plugin/testing_runner.py`, `make test-visible`) use **real
PyUNO objects**. The Makefile target runs them with the guard on:

```make
lo-test-threadguard:
	WRITERAGENT_UNO_THREAD_GUARD=1 $(LO_PYTHON) -m plugin.testing_runner; \
	EXIT_CODE=$$?; $(MAKE) lo-kill; exit $$EXIT_CODE
```

Any test path that drives a real send / MCP call / grammar pass and touches UNO
from a worker now aborts with a stack trace. This is the cheapest way to get real
coverage because it reuses an existing harness.

### B2. Thread-affinity mocks for pytest (medium effort)

**Status:** Done. Opt-in `uno_thread_safety` fixture:

1. `make_thread_affine_mock` / `ThreadAffineMock` stamp mocks for the synthetic
   main pump thread (`TestMainPump` in `tests/framework/thread_safety.py`).
2. `set_designated_main_thread` in [`thread_guard.py`](../plugin/framework/thread_guard.py)
   makes `on_main_thread()` follow the pump.
3. `set_force_marshal_mode` + `set_test_poke_handler` in
   [`queue_executor.py`](../plugin/framework/queue_executor.py) replace the
   `WRITERAGENT_TESTING` inline shortcut for that session: workers enqueue and
   block; the pump thread drains the queue.

Now a worker that calls a UNO mock directly (instead of via
`execute_on_main_thread`) touches the mock from the wrong thread → assertion →
red test. This is the unit-level mirror of B1 and runs in plain CI.

### B3. Targeted regression tests per fixed bug

**Status:** Done (seed test). `test_charts_process_events_regression_must_marshal`
in `test_thread_affinity.py` documents the charts hang class (commit
`0cfc6891`). Add more tests here as violations are found.
`tests/framework/test_tool_registry_bypass_thread.py` remains the template for
`bypass_thread_guard` behavior.

---

## Layer C — Author-time / static analysis (highest payoff, most work)

Stock type-checkers (ty / mypy / pyright) **cannot** catch this: UNO objects are
typed `Any`, and no Python type encodes thread affinity. We need the "function
coloring" rule expressed as a bespoke check.

### C1. `@main_thread_only` / `@background` as the type system (no new deps)

Reuse the Layer A decorators as **machine-readable color annotations**:

- `@main_thread_only` → red (UNO-only). Already needed for the runtime guard, so
  it does double duty.
- `@background` → blue (asserts it is *not* on the main thread; documents intent).

A function's color is then visible in the source for both humans and tooling.

### C2. A small AST linter wired into `make check` (start narrow)

Add `scripts/uno_thread_lint.py` (modeled on the project's existing custom scripts
such as [`scripts/manifest_registry.py`](../scripts/manifest_registry.py) and
[`scripts/crosshair_stream.py`](../scripts/crosshair_stream.py)) and call it from
the `check` / `test` targets. It performs a pragmatic taint/escape pass:

- **Blue roots** (background contexts):
  - the function/lambda passed as `target=`/first arg to `run_in_background`,
    `threading.Thread`, `threading.Timer`, `AsyncProcess(...)` callbacks,
    `BatchingStreamQueue` timer, and smol worker entrypoints;
  - any function decorated `@background`.
- **Red sinks** (UNO-only):
  - any function decorated `@main_thread_only`;
  - a curated allowlist of known UNO modules/symbols (`uno_context` getters,
    `document_helpers` resolution, writer/calc/draw document mutators).
- **Rule:** inside a blue root's body (and shallow intra-module callees), a call to
  a red sink that is **not** lexically wrapped by `execute_on_main_thread(...)` /
  `post_to_main_thread(...)` / `QueueExecutor.execute(...)` is an **error**.

Keep the first version deliberately narrow to keep false positives near zero:
flag only **direct** red-sink calls that appear lexically inside a `def worker()`
body or inside an `@background`-decorated function. Expand the root/sink lists and
the inter-procedural depth over time as confidence grows. Because the annotations
are the same ones the runtime guard uses, Layers A and C never drift apart.

### C3. (Optional, later) ban raw `threading.Thread` so roots stay finite

A trivial ruff/grep gate that forbids `threading.Thread(` outside `worker_pool.py`
keeps "where background threads are born" to the single chokepoint the linter
knows about. We already prefer `run_in_background` per AGENTS.md; this just makes
it enforceable.

---

## Recommended rollout (least code first)

| Step | Layer | Effort | What you get |
|------|-------|--------|--------------|
| 1 | A1 + A2 | Hours | `assert_main_thread` / `@main_thread_only`, env toggle, tagged worker threads. Decorate `uno_context` getters + tool boundary. Immediate located failures on the dev machine with the guard on; harmless `log.warning(stack_info=True)` in the field with it off. |
| 2 | A3 + B1 | ½–1 day | Viral guarding proxy on UNO sources; `make lo-test-threadguard` runs the real UNO suite with the guard on. Catches arbitrary object-graph violations with zero per-site annotation. **Done.** |
| 3 | B2 + B3 | ~1 day | Thread-affinity mocks + synthetic main pump (`uno_thread_safety` fixture); deterministic CI coverage without LibreOffice. Seed regression in `test_thread_affinity.py`. **Done.** |
| 4 | C1 + C2 | Days | AST linter in `make check`, starting narrow; grow sink/root lists. Moves detection to author time. |

Each step is independently shippable and strictly additive. Step 1 alone already
turns most "rare deadlock" reports into "deterministic exception with a stack
trace," which is the bulk of the user's pain.

## Tradeoffs summary

- **Runtime guard (A):** cheapest, most general (A3 needs no annotations), but only
  catches paths that actually execute. Default-off ⇒ zero release cost. Best ROI.
- **Test-time (B):** deterministic and automatable. B1 reuses real PyUNO (`make lo-test-threadguard`); B2 uses the `uno_thread_safety` fixture and synthetic pump (no LO required).
- **Static (C):** the only layer that catches a bug **before** it runs, but it is
  the most work, needs annotation discipline, and a custom linter to maintain.
- **Formal verification:** not applicable — thread affinity is an effect, not a
  value property; CrossHair/`deal` cannot model it.

## Where this plugs into the existing code

- Guard infra / reusable assert: [`plugin/framework/thread_guard.py`](../plugin/framework/thread_guard.py) (`set_designated_main_thread` for Layer B); replace the
  inline check in [`plugin/framework/tool.py`](../plugin/framework/tool.py) `execute_safe`.
- UNO sources to decorate / proxy: [`plugin/framework/uno_context.py`](../plugin/framework/uno_context.py),
  [`plugin/doc/document_helpers.py`](../plugin/doc/document_helpers.py).
- Background birthplace to tag / constrain: [`plugin/framework/worker_pool.py`](../plugin/framework/worker_pool.py).
- Marshalling boundary (the only legal recoloring): [`plugin/framework/queue_executor.py`](../plugin/framework/queue_executor.py) (`set_force_marshal_mode`, `set_test_poke_handler` for Layer B pytest). **Sync tools** are also marshaled centrally in [`ToolRegistry.execute`](../plugin/framework/tool.py) via `execute_on_main_thread` (async tools and `bypass_thread_guard` stay on the caller thread).
- Layer B pytest: [`tests/framework/thread_safety.py`](../tests/framework/thread_safety.py), fixture in [`tests/framework/conftest.py`](../tests/framework/conftest.py), tests in [`tests/framework/test_thread_affinity.py`](../tests/framework/test_thread_affinity.py).
- Linter: new `scripts/uno_thread_lint.py`, wired into `check` / `test` in the
  [`Makefile`](../Makefile).
- Tests: extend [`tests/framework/test_tool_registry_bypass_thread.py`](../tests/framework/test_tool_registry_bypass_thread.py);
  `make lo-test-threadguard` over [`plugin/testing_runner.py`](../plugin/testing_runner.py).
- **Specialized sub-agents:** [`plugin/doc/specialized_base.py`](../plugin/doc/specialized_base.py)
  (`DelegateToSpecializedBase.execute`) runs on a background worker when `is_async()`; UNO
  scaffolding (`get_tools(doc=…)`, shapes canvas, open-documents list, embeddings index wakeup)
  and sync domain tools (via `SmolToolAdapter(main_thread_sync=True)`) must marshal through
  `execute_on_main_thread`. Async domain tools (`generate_image`, `delegate_read_document`, …)
  must marshal UNO inside their own `execute()`. Tests:
  [`tests/doc/test_specialized_delegation_threading.py`](../tests/doc/test_specialized_delegation_threading.py).

## Cross-references

- [`docs/threading_architecture.md`](threading_architecture.md) — the model being enforced.
- [`docs/streaming-and-threading.md`](streaming-and-threading.md) — drain loop, Stop/cancellation, the `execute_on_main_thread` checklist.
- [`docs/formal_verification.md`](formal_verification.md) — why FV is the wrong tool for this class of bug.
- Reference fix this doc generalizes: commit `0cfc6891b679f3fcc2ad4a47107763a1b5bd93d7` (charts hang).
