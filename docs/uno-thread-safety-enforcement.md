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

GUARD_ON = os.environ.get("WRITERAGENT_UNO_THREAD_GUARD", "1") == "1"

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
worker aborts at the call site with a stack trace in non-release builds (guard on
by default). **Release OXT bundles replace this module with a no-op stub** so
production pays nothing. Opt out in dev: `WRITERAGENT_UNO_THREAD_GUARD=0`.

When the guard is on, violations also **log at ERROR** and show a **modal error
message box** on the LibreOffice main thread (via blocking `execute_on_main_thread`
→ `msgbox`; `post_to_main_thread` must not inline on workers when AsyncCallback
is missing),
deduped to once per background thread so the viral proxy cannot spam dialogs. UI is
skipped under `WRITERAGENT_TESTING=1` so pytest and the native test runner stay
headless.

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
when the guard is active (on by default in non-release bundles; release stubs
disable it entirely).

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

**Status:** Done. `make lo-test-threadguard` runs the full native suite with the
Layer A guard active (on by default in non-release bundles; the Makefile target
still sets `WRITERAGENT_UNO_THREAD_GUARD=1` explicitly for clarity). `WRITERAGENT_TESTING=1` (set by
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

## Layer C — Author-time / static analysis (Opengrep)

**Status:** Implemented (C1+C2+C3). Custom Opengrep rules in
[`tests/semgrep/uno_thread_safety.yml`](../tests/semgrep/uno_thread_safety.yml) (UNO taint) and
[`tests/semgrep/writeragent_security.yml`](../tests/semgrep/writeragent_security.yml) (project-specific);
vendored third-party rules under [`tests/semgrep/third_party/`](../tests/semgrep/third_party/) (pinned in
[`SOURCES.json`](../tests/semgrep/third_party/SOURCES.json), refresh via **`make opengrep-rules-sync`**).
`@background` in [`plugin/framework/thread_guard.py`](../plugin/framework/thread_guard.py);
**`make opengrep-lint`** (alias **`make uno-thread-lint`**) runs as part of **`make test`** only
(not `make check` / `make typecheck`). Install Opengrep: **`make opengrep-install`**. UNO taint scan
uses **`--taint-intrafile`**. On Windows, the installer uses upstream PowerShell support; Windows ARM64 currently installs the x64 Opengrep binary and runs it under emulation. Fixtures:
[`tests/semgrep/uno_thread_safety.violations.py`](../tests/semgrep/uno_thread_safety.violations.py),
[`tests/semgrep/uno_thread_safety.ok.py`](../tests/semgrep/uno_thread_safety.ok.py),
[`tests/semgrep/security_rules.violations.py`](../tests/semgrep/security_rules.violations.py);
pytest: [`tests/scripts/test_opengrep_lint.py`](../tests/scripts/test_opengrep_lint.py).
Optional manual registry sweep: **`make opengrep-rules-audit`** (`p/python`; not in `make test`).

Stock type-checkers (ty / mypy / pyright) **cannot** catch this: UNO objects are
typed `Any`, and no Python type encodes thread affinity. Layer C expresses the
"function coloring" rule as Opengrep **taint** (sources / sinks / sanitizers).

### C1. `@main_thread_only` / `@background` as the type system

Reuse the Layer A decorators as **machine-readable color annotations**:

- `@main_thread_only` → red (UNO-only). On [`uno_context`](../plugin/framework/uno_context.py)
  getters and key [`document_helpers`](../plugin/doc/document_helpers.py) UNO functions.
- `@background` → blue (warns if run on the main thread; Opengrep taint source).

**Module-level worker entrypoints** passed to `run_in_background` carry `@background`
(cross-file workers). UNO work inside a worker must go through
`execute_on_main_thread` / `post_to_main_thread` (see `_update_menu_icons` /
`run_periodic_embeddings_indexer` refactors).

### C2. Opengrep taint rules in `make test`

[`tests/semgrep/uno_thread_safety.yml`](../tests/semgrep/uno_thread_safety.yml) performs taint
with **`opengrep scan --taint-intrafile`**:

- **Blue roots (taint sources):** `@background` functions; lambdas passed to
  `run_in_background(...)`; nested `def $F` + `run_in_background($F, ...)` in the
  same outer function.
- **Red sinks (UNO-only):** [`uno_context`](../plugin/framework/uno_context.py)
  getters, [`document_helpers`](../plugin/doc/document_helpers.py) UNO readers,
  and [`writer/format.py`](../plugin/writer/format.py) document mutators (see YAML).
- **Sanitizers:** `execute_on_main_thread(...)`, `post_to_main_thread(...)`.

**Cross-function (in-file):** with `--taint-intrafile`, Opengrep tracks
`@background worker → nested helper → sink` within one file. Module-level helpers
called from a worker still need marshalling or nested helpers. **Cross-file** still
requires `@background` on the worker entrypoint; inter-file taint is out of scope.

Advisory rules (WARNING, `make opengrep-lint-advisory`): `background-worker-missing-decorator`,
`uno-source-needs-main-thread-decorator`, plus vendored WARNING rules (e.g. `exec-detected`,
`eval-detected`, `insecure-file-permissions`). Suppress false positives with
`# nosemgrep: rule-id`.

### C2b. Vendored security rules (curated Semgrep Registry subset)

**Do not** pull full registry rulesets (`p/default`, `p/security-audit`) into `make test` — too
many false positives (pickle IPC, dynamic imports, grammar debug logs). Instead, nine rules are
vendored from [semgrep/semgrep-rules](https://github.com/semgrep/semgrep-rules) and
[trailofbits/semgrep-rules](https://github.com/trailofbits/semgrep-rules) at pinned commits
(see [`SOURCES.json`](../tests/semgrep/third_party/SOURCES.json)):

- **ERROR gate:** defused-xml / defused-xml-parse, subprocess-shell-true, avoid-pyyaml-load,
  tarfile-extractall-traversal, `writeragent-no-tempfile-mktemp`
- **Advisory:** exec-detected, eval-detected, hardcoded-password-default-argument,
  insecure-file-permissions (with path excludes on vetted venv IPC in the sync script)

Overlap with **Bandit** (`make test`): Bandit still owns broad HTTP/pickle/sql checks with
project skips in [`pyproject.toml`](../pyproject.toml); Opengrep fills XXE/subprocess/YAML/tar
gaps. Bump pins: edit SHAs in [`scripts/sync-opengrep-rules.sh`](../scripts/sync-opengrep-rules.sh),
run **`make opengrep-rules-sync`**, triage new findings.

### C3. Ban raw `threading.Thread` / `Timer` outside the chokepoint

Rule `raw-uno-thread-ban` matches `threading.Thread`/`Timer` and bare `Thread`/`Timer`
outside [`plugin/framework/worker_pool.py`](../plugin/framework/worker_pool.py) and a
vetted allowlist (venv worker/editor/audio recorder, grammar queue, CDP supervisor,
`async_stream` batch timer, settings debounce timer, calc deferred spill).
`plugin/contrib/` is excluded via Makefile `--exclude` flags (documented in [`tests/semgrep/semgrepignore`](../tests/semgrep/semgrepignore)).

---

## Recommended rollout (least code first)

| Step | Layer | Effort | What you get |
|------|-------|--------|--------------|
| 1 | A1 + A2 | Hours | `assert_main_thread` / `@main_thread_only`, env toggle, tagged worker threads. Decorate `uno_context` getters + tool boundary. Immediate located failures on the dev machine with the guard on; harmless `log.warning(stack_info=True)` in the field with it off. |
| 2 | A3 + B1 | ½–1 day | Viral guarding proxy on UNO sources; `make lo-test-threadguard` runs the real UNO suite with the guard on. Catches arbitrary object-graph violations with zero per-site annotation. **Done.** |
| 3 | B2 + B3 | ~1 day | Thread-affinity mocks + synthetic main pump (`uno_thread_safety` fixture); deterministic CI coverage without LibreOffice. Seed regression in `test_thread_affinity.py`. **Done.** |
| 4 | C1 + C2 + C3 | ~1 day | Opengrep `--taint-intrafile` + `@background` on workers + broad sinks + raw-thread ban in **`make test`**. **Done.** |

Each step is independently shippable and strictly additive. Step 1 alone already
turns most "rare deadlock" reports into "deterministic exception with a stack
trace," which is the bulk of the user's pain.

## Tradeoffs summary

- **Runtime guard (A):** cheapest, most general (A3 needs no annotations), but only
  catches paths that actually execute. On by default in dev; release stub ⇒ zero release cost. Best ROI.
- **Test-time (B):** deterministic and automatable. B1 reuses real PyUNO (`make lo-test-threadguard`); B2 uses the `uno_thread_safety` fixture and synthetic pump (no LO required).
- **Static (C):** catches bugs before they run (Opengrep `--taint-intrafile`, cross-function in-file).
  `@background` on worker entrypoints; cross-file UNO still needs explicit marshalling.
- **Formal verification:** not applicable — thread affinity is an effect, not a
  value property; CrossHair/`deal` cannot model it.

## Where this plugs into the existing code

- Guard infra / reusable assert: [`plugin/framework/thread_guard.py`](../plugin/framework/thread_guard.py) (`set_designated_main_thread` for Layer B); replace the
  inline check in [`plugin/framework/tool.py`](../plugin/framework/tool.py) `execute_safe`.
- UNO sources to decorate / proxy: [`plugin/framework/uno_context.py`](../plugin/framework/uno_context.py),
  [`plugin/doc/document_helpers.py`](../plugin/doc/document_helpers.py).
- Background birthplace to tag / constrain: [`plugin/framework/worker_pool.py`](../plugin/framework/worker_pool.py).
- Marshalling boundary (the only legal recoloring): [`plugin/framework/queue_executor.py`](../plugin/framework/queue_executor.py) (`set_force_marshal_mode`, `set_test_poke_handler` for Layer B pytest; **`pump_ui_idle`** co-drains the work queue from [`run_stream_drain_loop`](../plugin/framework/async_stream.py) so async tools do not deadlock). **Sync tools** are also marshaled centrally in [`ToolRegistry.execute`](../plugin/framework/tool.py) via `execute_on_main_thread` (async tools and `bypass_thread_guard` stay on the caller thread).
- Layer B pytest: [`tests/framework/thread_safety.py`](../tests/framework/thread_safety.py), fixture in [`tests/framework/conftest.py`](../tests/framework/conftest.py), tests in [`tests/framework/test_thread_affinity.py`](../tests/framework/test_thread_affinity.py).
- Layer C Opengrep: [`tests/semgrep/uno_thread_safety.yml`](../tests/semgrep/uno_thread_safety.yml),
  [`tests/semgrep/writeragent_security.yml`](../tests/semgrep/writeragent_security.yml),
  [`tests/semgrep/third_party/`](../tests/semgrep/third_party/), [`tests/semgrep/semgrepignore`](../tests/semgrep/semgrepignore),
  `make opengrep-install`, `make opengrep-rules-sync`, `make opengrep-lint` (in `make test`).
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
