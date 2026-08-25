# Formal Verification Strategy for WriterAgent

This document explores the theoretical foundation and practical application of formal verification (FV) to the WriterAgent Python codebase. 

**Critical Architectural Assumption:** WriterAgent relies heavily on LibreOffice's UNO API. For the scope of this document and any FV efforts, **we treat the UNO C++ bridge as an axiomatically sound, 100% reliable external environment.** We have no interest in verifying LibreOffice itself. If a UNO method is called with correct parameters, we assume it succeeds. Our FV scope is strictly constrained to proving the correctness of *our* Python code: our data transformations, parsing logic, state management, and algorithmic safety.

---

## 1. The Theoretical Landscape: Verifying Python

Formal verification is the application of mathematical proofs to demonstrate that a program satisfies its formal specifications for all possible inputs. Traditional unit testing suffers from the *coverage problem*—it samples a finite subset of an infinite state space. FV, via techniques like Symbolic Execution and Bounded Model Checking (BMC), attempts to explore the state space exhaustively by treating inputs as symbolic variables.

### The Dynamics of Python vs. SMT Solvers
Most modern FV tools rely on Satisfiability Modulo Theories (SMT) solvers, such as Microsoft's Z3. An SMT solver decides the satisfiability of first-order logic formulas with respect to background theories (e.g., bit-vectors, arrays, real numbers).

Applying this to Python introduces severe impedance mismatch:
1.  **Dynamic Typing & Late Binding:** A symbolic Python variable lacks a fixed memory footprint or operational semantic bounds until runtime. A single `+` operator could mean integer addition, string concatenation, or a custom `__add__` metaclass resolution.
2.  **State Space Explosion:** Python's highly mutable runtime (where dictionaries underpin classes, and functions are first-class objects) causes the state space to explode exponentially. Translating arbitrary Python bytecode into a finite set of logical constraints for an SMT solver is often undecidable.
3.  **The Halting Problem:** Pure formal provers (like `deal-solver`) struggle with unbounded loops and recursive calls in Turing-complete languages. They often require manual loop invariants (mathematical properties that hold true before and after each loop iteration) to prove termination, which are exceedingly rare in standard Python codebases.

Because of these constraints, *full* formal verification of Python is largely restricted to trivial, purely functional subsets. However, hybrid approaches—specifically **Concolic Execution**—offer a highly practical compromise.

---

## 2. Tooling: From Proofs to Concolic Execution

We evaluate the current Python verification ecosystem strictly for its utility on our pure Python algorithmic modules.

### A. Deal (Design by Contract) & Deal-Solver
[`deal`](https://deal.readthedocs.io/) implements Design by Contract (DbC), heavily inspired by Hoare logic and the Eiffel language. It uses decorators (`@deal.pre`, `@deal.post`, `@deal.inv`) to define axioms and theorems about functions.

*   **The Verifier (`deal-solver`):** `deal` includes an experimental static verifier that attempts to translate the Python AST and the contracts directly into Z3 theorems. 
*   **The CS Reality:** It is a fascinating academic exercise but practically unworkable for WriterAgent. It requires absolute referential transparency, does not support most of the Python standard library, cannot model sets or complex OOP structures, and fails on unbounded loops.

### B. CrossHair: The Concolic Testing Engine
[`CrossHair`](https://crosshair.readthedocs.io/) represents the most viable path forward. It is not a pure formal verifier; it is a **verifier-driven fuzzer** that utilizes **Concolic (Concrete + Symbolic) Execution**.

*   **How it Works:** CrossHair hooks into the Python interpreter. As a function executes, CrossHair maintains two states: a concrete state (actual values) and a symbolic state (Z3 equations representing the path constraints). When it encounters a branch (e.g., `if len(url) > 10:`), it queries the Z3 SMT solver: *"Is there an input that satisfies the current path constraints AND makes `len(url) > 10` false?"* If so, it forks the execution and explores both paths.
*   **Why it Fits Our Code:** Because it runs the actual CPython interpreter, it handles "magic", standard libraries, and complex types perfectly. It essentially exhaustively searches for a combination of inputs that will raise an unhandled exception or violate a `deal` contract. It trades mathematical certainty (it will time out on infinite state spaces) for immense practical utility in finding edge-case counterexamples.

#### CrossHair `check` vs `cover` (same engine, different goals)

CrossHair exposes two CLI commands that share the same symbolic engine but optimize for different outcomes:

| Command | Question it answers | Needs `@deal` contracts? | Typical output |
|---------|---------------------|--------------------------|----------------|
| **`crosshair check`** | Can I find inputs that **violate** a pre/post/ensure? | Yes (or asserts) | `file.py:line: error:` counterexamples; `info: Confirmed` / `Not confirmed` / `Unable to meet precondition` |
| **`crosshair cover`** | What inputs **exercise more bytecode paths**? | No | Printable example calls, e.g. `host_pack_split_grid([])` |

**`check` — contract verification**

- Targets functions with `@deal` decorators (auto-discovered).
- **Pass** means no counterexample was found in the time budget—not a full mathematical proof.
- **`Not confirmed`** is normal for complex ensures: CrossHair explored paths without finding a violation, but did not prove the property for all inputs.
- **`Unable to meet precondition`** means CrossHair could not synthesize valid inputs (common for `ndarray` parameters).
- Only **`file.py:line: error:`** lines are hard failures (counterexamples).

**`cover` — path exploration / example generation**

`cover` answers a different question than `check`: *“What weird inputs actually run through this code, and which branches do they hit?”* It is closest to **guided fuzzing for coverage**, not to proving invariants.

##### What CrossHair is doing

1. Pick a function in the target module (when you pass a file, it walks **all** top-level functions, not just `@deal`-decorated ones).
2. Synthesize symbolic arguments (lists, dicts, ints, empty strings, etc.) using the same SMT engine as `check`.
3. **Execute your real Python** with those arguments.
4. Score each input by how many **new bytecodes** (or paths, with `--coverage_type=path`) it executed.
5. Print the best examples as **copy-pasteable call syntax**, one per line, usually ordered from most to least new coverage.

It never evaluates `@deal.pre` / `@deal.post` / `@deal.ensure`. A line like `host_pack_split_grid([])` is simply CrossHair saying: *“I ran this; it taught me about a code path.”* It is **not** saying the result was correct, round-tripped, or safe for production wire data.

##### Three kinds of output (learn to tell them apart)

When you run `make crosshair-cover` or pipe raw `crosshair cover -v` through [`scripts/crosshair_stream.py`](../scripts/crosshair_stream.py), you see three categories:

| Category | Raw CrossHair shape | Filter tag | Meaning |
|----------|---------------------|------------|---------|
| **Example call** | `host_pack_split_grid([])` | `[COVER EXAMPLE]` | Callable with concrete args; use these to extend pytest |
| **Exploration noise** | `payload_codec child_unpack split_grid failed for envelope dict(keys=[])` | `[COVER EXPLORE]` | Your code ran, hit a branch, logged or caught an exception—**expected** during fuzzing |
| **Hard crash** | Python traceback / `TypeError:` at end of run | `[COVER FATAL]` | CrossHair itself broke (e.g. unsupported type hint)—fix tooling or annotations |

**Example calls** — printable invocations CrossHair found useful for coverage:

```text
host_pack_split_grid([])
host_pack_split_grid([0])
is_numeric_grid([False])
child_unpack_data([])
should_use_binary_envelope((), min_cells=0, force='always')
```

How to read them:

- **Empty containers** (`[]`, `{}`, `()`) — exercises early-return / empty-grid paths.
- **Degenerate shapes** (`[[]]`, `[False]`, nested junk) — probes type branches in flatten/unpack.
- **Garbage dicts** passed to unpack — CrossHair is not building valid split_grid envelopes; it is stress-testing `is_split_grid`, pre-checks, and error handlers.
- **Weird keyword combos** (`min_cells=0`, `force='never'`) — hits policy branches in `host_pack_data` / `should_use_binary_envelope`.

These are **starting points for tests**, not oracles. You still decide the expected result (round-trip, `ValueError`, etc.).

**Exploration noise** — your module’s logging and `try/except` firing on bad inputs:

```text
payload_codec: uneven row lengths [1, 0] in 2D grid ...
payload_codec child_unpack split_grid failed for envelope dict(keys=['shape'])
payload_codec child_unpack failed for wire list[2] sample=[False, -10]
```

Why this appears:

- CrossHair ** executes real code**; [`payload_codec.py`](../plugin/scripting/payload_codec.py) logs at `error`/`exception` before re-raising or returning.
- Invalid envelopes are **supposed** to fail inside `child_unpack_split_grid`; CrossHair counts that as “I reached this branch.”
- This is **not** a failed pytest run and **not** a `check` counterexample—unless an **uncaught** exception escapes or CrossHair prints `: error:` (that is `check`, not `cover`).

**Fatals** — CrossHair stopped analyzing (tooling limit, not your contract):

```text
TypeError: typing.Literal['int', 'float', 'bool'] is not a module, class, method, or function.
```

WriterAgent fixed this for `payload_codec` by using `str` in public signatures instead of `Literal` in parameters CrossHair must proxy. If `cover` dies with a traceback, fix annotations or narrow the target file—do not treat it as a serialization bug.

##### Filtered live output (`make crosshair-cover`)

```bash
make crosshair-cover
# same as:
crosshair cover -v plugin/scripting/payload_codec.py 2>&1 | python scripts/crosshair_stream.py cover
```

Sample filtered stream:

```text
[COVER EXAMPLE          ] host_pack_split_grid([])
[COVER EXAMPLE          ] host_pack_split_grid([0])
[COVER EXPLORE          ] payload_codec child_unpack split_grid failed for envelope dict(keys=[])
[COVER EXAMPLE          ] child_unpack_data({})

=== CrossHair COVER DONE (exit 0) ===
  lines read: 842 (suppressed 800)
  examples=42 explore=38 errors=0
```

- **`examples=`** — distinct call lines worth saving as test ideas.
- **`explore=`** — log/exception paths hit (noise, but confirms branches exist).
- **`errors=0`** — CrossHair did not crash; exit 0 does **not** mean your invariants hold.

Use `-q` on the formatter for examples + fatals only; `--raw` to see every suppressed `choose_possible` line from `crosshair -v`.

##### Turning `cover` results into tests

CrossHair can emit stub pytest files:

```bash
crosshair cover --example_output_format=pytest \
    plugin.scripting.payload_codec.host_pack_split_grid
```

That produces tests of the form `assert foo(args) == <whatever it got>`. **Do not commit blindly**—CrossHair records observed behavior, not required behavior. Workflow:

1. Run `cover` on a function or module; collect `[COVER EXAMPLE]` lines.
2. For each interesting call, decide the **oracle** (round-trip equals input, raises `ValueError`, returns empty envelope, etc.).
3. Add to [`tests/scripting/test_payload_codec.py`](../tests/scripting/test_payload_codec.py) or [`test_serialization_verification.py`](../tests/scripting/test_serialization_verification.py) with an explicit assert.
4. Use `check` + `@deal` on the same paths if you want concolic search for contract violations.

##### When to use `cover` vs `check`

| Goal | Use |
|------|-----|
| Prove no `@deal` violation found (in time budget) | `check` |
| Find inputs that hit rarely used branches | `cover` |
| Validate round-trip / Calc semantics | pytest oracles ([`VERIFICATION_GRIDS`](../tests/scripting/test_serialization_verification.py)) |
| CI gate on correctness | `make verify` (pytest `-k verification`) + optional `make crosshair-check` on `payload_codec` |
| Brainstorm edge-case inputs after a refactor | `cover` on full module |

**`cover` does not replace `check` or round-trip tests.** It tells you *where the code has been*; `check` tells you whether *contracts broke*; pytest tells you whether *product behavior matches intent*.

##### Scope and cost (WriterAgent defaults)

- **`make crosshair-cover`** runs on the **entire** [`plugin/scripting/payload_codec.py`](../plugin/scripting/payload_codec.py) with **no** `--per_condition_timeout`—correctness over speed; can take a long time.
- **`make crosshair-check`** is the contract pass on the same file; use both when hardening serialization.
- **`make crosshair-check-all`** discovers every `plugin/**/*.py` that contains `@deal.` and runs `crosshair check --analysis_kind=deal` **one FQN at a time** (live tee — not a process pool). Prints `[CHECK START] path :: fqn` **before** each spawn so a hang names the current callable (CrossHair `-v` often logs `Analyzing …` only after that condition finishes). After the first tagged line of the sweep, each `[CHECK …]` line includes `| Prev M:SS` (wall time since the previous emitted tagged line — GitHub Actions logs are append-only, so this is how a stuck post/pre shows up; first line has no Prev). Cover-all does not stamp Prev. Two presets only (same numbers as cover-all): **regular** (default) is `--max_uninteresting_iterations=25 --per_condition_timeout=5` plus a hard **120s per-module wall** (kill process group; timeout is exit 0 / not a sweep failure); **deep** (`make crosshair-check-all-deep` / `--deep`) is `--max_uninteresting_iterations=200` with no per-condition timeout and no wall (hour-scale). Speed comes from the short `WRITERAGENT_CROSSHAIR=1` deal table (A–Z, 20 rows, tiny shapes) plus complete `@deal.pre` caps / `# crosshair: off` on callable+regex helpers, not from timeouts. In **regular** mode only, `payload_codec` uses a tighter **5 / 5s** bound; deep and `make crosshair-check` stay the long codec dives. Formatted output is teed to [`build/crosshair-check-all.log`](../build/crosshair-check-all.log). Not part of `make test`. Failures are CrossHair **errors** / engine crashes only; `NOT_CONFIRMED` / `UNABLE` and wall timeouts are informational. List targets with `python scripts/crosshair_check_all.py --list`; pass explicit paths to check a subset. **`CROSSHAIR_CHECK_ALL_SKIP` is empty** — disable hostility in source (`# crosshair: off` per callable or column-0 module-level, and/or CrossHair shims), not via skip-list entries.
- **`make crosshair-cover-all`** uses the same `@deal.` discovery and the same short `WRITERAGENT_CROSSHAIR=1` deal table (set in `main`, as the process-pool `initializer`, and at each worker entry before CrossHair spawn — spawn workers re-import without inheriting a parent-set env). **`CROSSHAIR_COVER_ALL_SKIP` is empty.** Upstream `cover` ignores `# crosshair: off`, so cover-all expands each file to FQNs via [`cover_fqns_for_module`](../scripts/crosshair_stream.py): a **column-0 module** `# crosshair: off` yields no FQNs (auto-skip); otherwise per-callable offs are dropped. Prefer a `if "crosshair" in sys.modules:` shim when contracts should still run (see `duckdb_sql._template_body`, `json_utils.repair_json`); escalate to `# crosshair: off` when the engine still crashes; use **module-level** off when the file is ≥95% hostile; refactor large host+algorithm mixes. Runs in a **process pool** (default workers `max(2, cpu_count - 2)`; override with `--jobs N`). Submit order: modules **not** in `COVER_ALL_SCHEDULE_ORDER` first (stable by path — new `@deal.` files until timed), then **longest-first** known schedule (measured regular-run timings). Two presets only: **regular** (default) is `--max_uninteresting_iterations=25 --per_condition_timeout=5` (breadth over depth) plus a hard **120s per-module wall** (kill process group; timeout is exit 0 / not a sweep failure); **deep** (`make crosshair-cover-all-deep` / `--deep`) is `--max_uninteresting_iterations=200` with no per-condition timeout and no wall (hour-scale). In **regular** mode only, `payload_codec` uses a tighter **5 / 5s** bound; deep and `make crosshair-cover` stay the long codec dives. Cover/check are always **budgeted partial** exploration—not exhaustive proofs. Each worker still owns one CrossHair process per module; formatted output is buffered and printed as a whole block when that module finishes (completion order—no interleaved lines). Tee: [`build/crosshair-cover-all.log`](../build/crosshair-cover-all.log); per-module durations (longest first) in [`build/crosshair-cover-all-timings.json`](../build/crosshair-cover-all-timings.json). Failures are `CrossHairInternal` / process crashes only—few examples and wall timeouts do not fail the sweep. List targets with `python scripts/crosshair_cover_all.py --list`.

**WriterAgent reference module:** [`plugin/scripting/payload_codec.py`](../plugin/scripting/payload_codec.py) — see [`docs/scripting-serialization-verification.md`](scripting-serialization-verification.md).

**CrossHair + `typing.Literal`:** CrossHair cannot proxy `Literal[...]` (it calls `get_type_hints` on the literal itself). Use `str` in function **parameter** annotations and in **TypedDict fields** that may land on the type heap via imports; keep `Literal` aliases (e.g. `ColumnKind`, `StatusValue`, `HeaderMode`) for casts/comments only. Parameter case fixed in `payload_codec.py`; TypedDict case fixed in [`errors.py`](../plugin/framework/errors.py) (`ToolSuccess`/`ToolError`) after flaky check-all crashes on importers such as `stream_normalizer`; dataclass / param case for excel `HeaderMode` / `DepRole` in [`models.py`](../plugin/calc/excel_py_convert/models.py) + [`to_dag.py`](../plugin/calc/excel_py_convert/to_dag.py).

**FQN cover vs package `__init__`:** Cover-all loads targets by qualname, which runs the parent package `__init__.py`. Eager UNO/tool imports or import-time filesystem probes (e.g. `tempfile.gettempdir()` in [`format.py`](../plugin/writer/format.py)) trip CrossHair's auditwall (`SideEffectDetected`) even when the pure submodule itself is fine under file-path cover. Fix with a `if "crosshair" not in sys.modules:` guard on the heavy package imports (see [`plugin/writer/__init__.py`](../plugin/writer/__init__.py)) and/or a CrossHair-safe temp-dir shim — not a skip-list entry.

#### Live output while CrossHair runs

CrossHair without ``-v`` can stay silent for tens of seconds per condition. Pipe **``crosshair -v``** through [`scripts/crosshair_stream.py`](../scripts/crosshair_stream.py); it keeps milestone lines and suppresses SMT spam (``choose_possible``, stack traces).

```bash
# Pipe mode (recommended — full module, no time limit)
crosshair check -v --report_all plugin/scripting/payload_codec.py 2>&1 \
    | python scripts/crosshair_stream.py check

crosshair cover -v plugin/scripting/payload_codec.py 2>&1 \
    | python scripts/crosshair_stream.py cover

# Quieter: only counterexamples + final banner
crosshair check -v --report_all plugin/scripting/payload_codec.py 2>&1 \
    | python scripts/crosshair_stream.py check -q

make verify             # pytest formal verification suite (light Hypothesis)
make vhs                # deep Hypothesis: serialization A/B + chat/MCP FSMs
make slowtests          # extensive serialization fixtures + make vhs
make crosshair-check
make crosshair-cover
make crosshair-check-all        # all @deal. modules; regular budget (25/5s + 120s wall); log under build/
make crosshair-check-all-deep   # same set; deep budget (200 iters, no timeout/wall)
make crosshair-cover-all        # same set; regular budget (25/5s + 120s wall); log + timings under build/
make crosshair-cover-all-deep   # same set; deep budget (200 iters, no timeout/wall)
```

##### GitHub Actions (On-Demand CI Sweeps)

Deep CrossHair sweeps are also available on GitHub Actions via manual dispatch (`workflow_dispatch`):
- **Workflow**: `.github/workflows/crosshair-deep.yml`
- **Inputs**:
  - `target`: `both` (default), `check-all`, or `cover-all`
  - `deep`: `true` (default: 200 iterations, no per-condition wall) or `false` (regular: 25 iterations / 5s + 120s wall)
  - `runner_os`: `ubuntu-latest` (default), with support for `windows-latest` and `macos-latest`
  - `jobs`: worker pool size for cover-all
- **Artifacts**: Automatically uploads `build/crosshair-check-all.log`, `build/crosshair-cover-all.log`, and `build/crosshair-cover-all-timings.json` on completion or failure.

##### `make verify` vs `make vhs` vs `make slowtests`

| Target | Hypothesis budget | Scope |
|--------|-------------------|--------|
| **`make verify`** | Light defaults in each suite | All `*_verification.py` (deal oracles + light `@given` + optional slow CrossHair) |
| **`make vhs`** | `WRITERAGENT_VHS_EXTENSIVE=1` (alias: `WRITERAGENT_SERIALIZATION_EXTENSIVE`) | Deep fuzz (`-k hypothesis`): serialization A/B; chat/MCP FSMs; Phase 8 domains (done: `formula_edit`, `cors`, `word_diff_split`, `embeddings_split`); stream/response normalizers; sandbox path + scrub env; payload_codec policy; `address_utils` |
| **`make slowtests`** | Extensive | Serialization fixture pass, then `vhs` |

Shared helpers: [`tests/vhs_budget.py`](../tests/vhs_budget.py) (`vhs_extensive` / `vhs_max_examples`), FSM strategies [`tests/chatbot/fsm_hyp_support.py`](../tests/chatbot/fsm_hyp_support.py).

**Playbook for a new deep VHS domain:** (1) Tier-0 pure entry with `@deal`, (2) strategies with small alphabets, (3) oracles mirroring `@deal.ensure`, (4) `vhs_max_examples(light, extensive)` on `@settings`, (5) name tests so `-k hypothesis` selects them, (6) add the file to the `vhs` Make recipe, (7) register in `verification_status.json`. Do **not** dump every light `@given` into `vhs`—reserve deep budgets for round-trips, FSM legality, and security filters. Cap string alphabets for regex-heavy oracles (see §8.1 C).

Sample filtered **`check`** output:

```text
[CHECK PROGRESS        ] analyzing should_use_binary_envelope
[CHECK ERROR           ] plugin/scripting/payload_codec.py:500  TypeError: ...
  -> confirmed=0 not_confirmed=0 unable=0 errors=1 progress=4

=== CrossHair CHECK FAIL (exit 1) ===
  ...
=== ERRORS TO FIX ===
  1. plugin/scripting/payload_codec.py:500  TypeError: ...
```

`make crosshair-check` / `make crosshair-check-all` both end with that **ERRORS TO FIX** block (unique contract `: error:` lines, Traceback headers, and CrossHairInternal crashes). `check-all` also groups failures by module. `make crosshair-cover-all` uses the same grouping for cover fatals (not for low example counts). Under the pool, each module’s filtered block appears when that worker completes (not live line-by-line across modules). Each block starts and ends with the same `######## [i/n] path ########` marker, and the COVER DONE banner repeats `[i/n] path` so identity is visible after a long example dump. Here `[i/n]` is **completion progress** (ith module finished of n), not discovery/list order.

**Module skip lists:** `CROSSHAIR_CHECK_ALL_SKIP` and `CROSSHAIR_COVER_ALL_SKIP` are **empty**. Fix engine-hostile surfaces in source, not via skip lists:

1. **CrossHair shim** — `if "crosshair" in sys.modules:` replace `json.loads` / `json.dumps` / UNO / `time.perf_counter` with a simple substitute so the function stays a cover/check entry point (`duckdb_sql._template_body`, `json_utils.repair_json`, helper_domain outcome helpers).
2. **`# crosshair: off`** — when the engine still crashes despite a shim, or the callable is pure host (Tool.execute, drain loops). Check honors this natively; cover-all drops those FQNs via `cover_fqns_for_module`.
3. **Module-level `# crosshair: off`** — column-0 directive near the top of the file when **every** callable is hostile, or when **≥95%** already need offs (e.g. `config.py`, `web_research_cache.py`, `errors.py`). Prefer one module directive over spraying the body. Cover-all treats that as an empty FQN list (auto-skip success), same as check’s module `enabled=False`.
4. **Refactor** — large host+algorithm mixes: extract a pure `@deal` core; off/shim the host.

**`@deal` vs `# crosshair: off`:** they are not substitutes. `@deal.pre` / `@deal.post` still run under the **dev/pytest** `deal` package (Hypothesis and unit tests). LibreOffice uses `deal_shim` (no-op). `# crosshair: off` only disables CrossHair analysis for that callable/module — it does **not** remove pytest contract checks. Keep `@deal` when tests exercise the contracts; do not mass-delete them just because CrossHair is off. Skip adding `@deal` “for CrossHair” on an already module-off file unless pytest will use it.

**Cover streamer vs check:** both modes ignore CrossHair `-v` `File "…/plugin/…"` / `TypeError: LazyIntSymbolicStr` stacks from `CrosshairUnsupported` path exploration. Check hard-fails on `Traceback (most recent call last)`, contract `: error:` lines, `CrossHairInternal`, or a non-zero process exit. Cover treats mid-run Tracebacks from app `log.exception` (path exploration) as **COVER EXPLORE**, not fatals; cover still fails on `CrossHairInternal` or a non-zero CrossHair process exit.

**`# crosshair: off`:** put the directive alone on its line (no trailing prose). CrossHair parses the rest of the line as options; characters like `—` raise `InvalidDirective`. Prefer the first line of the function body for per-callable offs; for whole-file disable use column-0 after the module docstring. Cover-all honors both via `cover_fqns_for_module` (module off → `[]`; else drop off’d callables).

```text
[CHECK PROGRESS        ] analyzing host_pack_split_grid
[CHECK PROGRESS        ] post: isinstance(result, dict)
[CHECK NOT_CONFIRMED   ] payload_codec.py:396
  -> confirmed=0 not_confirmed=1 unable=0 errors=0 progress=2
```

Sample filtered **`cover`** output:

```text
[COVER EXAMPLE         ] host_pack_split_grid([])
[COVER EXPLORE         ] payload_codec child_unpack split_grid failed for envelope dict(keys=[])
  -> examples=12 explore=8 errors=0
```

See the **`cover`** subsection above for how to interpret examples vs exploration noise vs fatals.

### C. Bounded Model Checking (ESBMC-Python)
[ESBMC](https://github.com/esbmc/esbmc) uses Bounded Model Checking. It translates Python into a lower-level intermediate representation (IR) and "unrolls" loops up to a specific depth ($k$). It then converts the unrolled program into a single massive SMT formula to check for safety properties (e.g., buffer overflows, division by zero).
*   **Utility:** Excellent for verifying highly complex, isolated algorithms (like our Calc cell range parsers) up to a bounded size, but overkill for standard API plumbing.

---

## 3. Execution Roadmap: Hardening WriterAgent's Pure Logic

To implement FV at scale across a ~23 KLOC codebase (excluding tests and vendored contrib), we must employ **Assume-Guarantee Reasoning** (a standard composition formal method). We "assume" the correctness of UNO, and we "guarantee" the correctness of our logic under those assumptions. 

Attempting to verify 23 KLOC simultaneously is intractable. We must apply a tiered, incremental triage framework.

### Phase 1: Triage and "Hexagonal" Segregation
The codebase must be categorized by its distance from the UNO boundary.
1.  **Tier 0 (The Core - Immediate ROI):** Files with zero UNO dependencies. These are pure data-transformation pipelines (`config.py` URL helpers, `address_utils.py`, `pricing.py`, `async_stream.py` delta accumulation).
2.  **Tier 1 (The Adapters):** Code that parses complex UNO structures into pure Python data models (e.g., extracting an AST from LibreOffice).
3.  **Tier 2 (The Orchestrators):** State machines and side-effect-heavy UI controllers (`panel_factory.py`).

We begin verification exclusively at Tier 0. Moving forward, complex algorithms must be strictly decoupled from UNO calls. We extract data from UNO (Tier 1), pass it into pure, verifiable Tier 0 functions, and pass the output back via Tier 2 orchestrators.

### Phase 2: Axiomatic Definition via Contracts (Tier 0)
We begin by establishing the formal properties of our pure functions using strict type hints and `deal` contracts. This shifts our development model from "writing tests that pass" to "defining invariants that must never fail."

**Example: Verifying Calc Address Math**
Consider `column_to_index` in `address_utils.py`. We know mathematically that:
1. It must only accept uppercase alphabetical strings.
2. The output must always be a non-negative integer.
3. The inverse function (`index_to_column`) applied to the result must yield the original input.

```python
import deal

@deal.pre(lambda col_str: col_str.isascii() and col_str.isalpha() and 1 <= len(col_str) <= 3)
@deal.post(lambda result: result >= 0)
# The ultimate invariant: f^-1(f(x)) == x  (len bound keeps CrossHair tractable)
@deal.ensure(lambda col_str, result: index_to_column(result) == col_str.upper())
def column_to_index(col_str: str) -> int:
    result = 0
    for char in col_str.upper():
        result = result * 26 + (ord(char) - ord('A') + 1)
    return result - 1
```

### Phase 3: Concolic State Exploration with CrossHair
With contracts in place, we unleash CrossHair. 
`crosshair check plugin/calc/address_utils.py`

CrossHair's Z3 engine will not just throw random fuzzing data at the function; it will analytically dissect the bytecode. It will realize that `ord(char)` implies integer boundaries, and it will intentionally synthesize string inputs designed to trigger integer overflows, index out-of-bounds, or violate the `deal.ensure` inverse mapping contract. 

When CrossHair finds a counterexample, it provides the exact symbolic input required to break our algorithm. We patch the code, and the state space is secured.

### Phase 4: SMT-Driven Protocol Verification
Beyond utility functions, we can apply FV to state machines. For example, our LLM streaming chunk normalizer (in `plugin/framework/async_stream.py`). 

By defining contracts that assert *"No matter how a JSON delta stream is arbitrarily chunked or fragmented over the network, the final assembled output string will exactly match the output of a synchronous, unfragmented payload,"* we can use CrossHair to mathematically prove our streaming parser's resilience against arbitrary network fragmentation.

## Why we refactored orchestration into pure state machines

WriterAgent’s hardest code paths sit in **Tier 2 orchestration** (see §3 Phase 1): the chat tool loop, send handlers, sidebar button lifecycle, MCP request handling, and related wiring (`panel_factory.py`, `tool_loop.py`, `send_handlers.py`, HTTP MCP). In that layer, behavior was historically driven by **implicit state** scattered across instance fields, together with threads, network I/O, and UNO calls. That combination makes correct behavior **difficult to reason about**, **expensive to test** (full LibreOffice or heavy harnesses), and **easy to get wrong at the edges**—for example Stop versus stream completion, tool ordering and pending-tool queues, or Send/Stop mutual exclusion.

The refactor **separates concerns** in the same “hexagonal” spirit as the rest of this document: **pure transition functions** `next_state(state, event) -> (new_state, effects)` with **effects represented as data** (simple strings plus small dataclasses). Side effects—threads, HTTP, UNO, subprocesses—stay in **interpreters** that run outside the transition function. **Phase 5** below catalogs what was extracted and the pragmatic patterns we used (and deliberately did not use).

**Why this matters for formal verification:** We treat the UNO bridge as axiomatic (opening of this document); we want proofs about **our** Python. `deal` contracts and CrossHair apply to **pure** functions. Code that mixes UI updates, I/O, and state updates in one procedure is a poor verification target—see *Verification Anti-Patterns* (do not verify tangled orchestration; verify the extracted machine instead).

**Why this matters even before full FV:** Deterministic, fast **unit tests** over transition functions (`tests/chatbot/test_tool_loop_state.py`, `tests/chatbot/test_state_machine.py`, `tests/chatbot/test_send_state.py`, `tests/chatbot/test_audio_recorder_state.py`, etc.) document **allowed transitions**, catch regressions without a running office, and make refactors in chat, MCP, and audio paths safer.

This was a **pragmatic** foundation: Phase 5 records design tradeoffs and what we avoided over-engineering. Attaching `deal` and running CrossHair on every transition is **Phase 6**, not something we claim is already complete.

For **remaining** orchestration that could be extracted in the future, see [docs/ROADMAP.md](ROADMAP.md) and the FSM modules listed under Phase 5.

## Phase 5: Elevating Orchestration to Pure State Machines

**COMPLETED ✅**

The following summarizes the implemented modules and the simplified patterns we used:

### Key Design Decisions (Simplified Approach)

1. **Simple Effect Types**: Used strings for simple effects (`"exit_loop"`, `"trigger_next_tool"`) instead of creating separate dataclass types
2. **Union Types**: Used Python's native union types (`SendHandlerEffect = Type1 | Type2 | Type3`) for cleaner code
3. **Minimal Boilerplate**: Kept effect and event definitions concise and focused
4. **Direct State Updates**: Used `dataclasses.replace()` for state transitions instead of complex builders

### Implemented State Machines

1. **Tool Loop State Machine** (`plugin/chatbot/tool_loop_state.py`)
   - Pure transition function with comprehensive event handling
   - Simple string effects mixed with structured effect types
   - Full test coverage in `tests/chatbot/test_tool_loop_state.py`
   - **Host dual-mirrors removed:** round/pending/stop/async/max live only in `ToolLoopState` (`sidebar_state.tool_loop` via `_sm_state`). Remaining `_active_*` fields on the panel are session I/O handles (queues, client, tools, model) for the effect interpreter in `tool_loop_actions.py` — not a second control state.

2. **Send Handler State Machine** (`plugin/chatbot/state_machine.py`)
   - Handles audio, image, agent, and web workflows
   - Uses union types for events and effects (cleaner than inheritance hierarchies)
   - Comprehensive test coverage in `tests/chatbot/test_state_machine.py`

3. **Send Button State Machine** (`plugin/chatbot/send_state.py`)
   - Manages UI button state transitions
   - Simple enum-based events and union effects
   - Test coverage in `tests/chatbot/test_send_state.py`

4. **MCP State Machine** (`plugin/mcp/mcp_state.py`)
   - HTTP protocol state management
   - Document resolution and tool execution workflows
   - Uses dataclasses for structured effects

5. **Audio Recorder State Machine** (`plugin/chatbot/audio_recorder_state.py`)
   - Audio recording lifecycle management
   - Minimal state and effect types
   - Test coverage in `tests/chatbot/test_audio_recorder_state.py`

6. **MCP Tunnel Reconnection State Machine** (`plugin/mcp/tunnel_state.py`)
   - Lifecycle and exponential backoff retry management for public tunnels (`cloudflared`, `ngrok`, `bore`, `tailscale`)
   - Explicit typed effects (`StartProcessEffect`, `TerminateProcessEffect`, `ScheduleRetryTimerEffect`, `CancelRetryTimerEffect`, `NotifyUrlAcquiredEffect`)
   - Formal invariants and contracts (`@deal.ensure`) with Hypothesis test coverage in `tests/mcp/test_tunnel_state.py`

### What We Avoided (Over-Engineering Traps)

❌ **Complex Effect Hierarchies**: Did NOT create deep inheritance trees of effect types
❌ **Overly Generic State Machines**: Did NOT create abstract base classes or factories
❌ **Excessive Pattern Matching**: Used simple `match` statements and `isinstance` checks
❌ **Separate State Update Effects**: Combined related state updates in single effects

### Current Implementation Pattern

```python
# Simple, pragmatic approach used in production

@dataclass(frozen=True)
class ToolLoopState:
    round_num: int
    pending_tools: List[Dict[str, Any]]
    # ... other fields ...

# Simple string effects for common operations
effects.append("exit_loop")  # Simple string effect
effects.append(ToolLoopUIEffect(kind="status", text="Ready"))  # Structured effect

# Pure transition function
def next_state(state: ToolLoopState, event: ToolLoopEvent) -> Tuple[ToolLoopState, List[Any]]:
    effects: List[Any] = []
    
    match event.kind:
        case EventKind.STOP_REQUESTED:
            effects.append("exit_loop")
            return dataclasses.replace(state, is_stopped=True), effects
        # ... other cases ...
```

**Verification:** The `next_state` functions above are the intended surface for `deal` and CrossHair in Phase 6; the simplified design keeps that work tractable.

## Phase 6: Formal Verification of State Machines

**Reference implementation:** [`plugin/scripting/payload_codec.py`](../plugin/scripting/payload_codec.py) — see [`docs/scripting-serialization-verification.md`](scripting-serialization-verification.md).

**Status (partial):** `deal` on `send_state.next_state` (send/stop mutual exclusion), `audio_recorder_state.next_state` (valid status + error path), thin ensures on `tool_loop_state.next_state` (STOP→`ExitLoopEffect`, round bound), and `mcp_state.next_state` (missing tool_name → `SendErrorEffect`, `TOOL_COMPLETED` → `StreamResponseEffect`, `REQUEST_ERROR` sets `is_error`). Pytest oracles + Hypothesis (light in `make verify`, deep in `make vhs`) + slow CrossHair hooks in [`tests/chatbot/test_fsm_verification.py`](../tests/chatbot/test_fsm_verification.py) and [`tests/mcp/test_mcp_state_verification.py`](../tests/mcp/test_mcp_state_verification.py). Strategies: [`tests/chatbot/fsm_hyp_support.py`](../tests/chatbot/fsm_hyp_support.py). Tracking: [`verification_status.json`](../verification_status.json).

### Step 1: Add Design by Contract to State Machines

Contracts use `FsmTransition.state` / `.effects` (not tuple indexing):

```python
@deal.ensure(lambda state, event, result: not (result.state.is_busy and result.state.is_recording))
@deal.ensure(
    lambda state, event, result: event.kind != EventKind.STOP_REQUESTED
    or any(isinstance(e, ExitLoopEffect) for e in result.effects)
)
def next_state(...):
    ...
```

### Step 2: Run CrossHair Verification
```bash
crosshair check plugin/chatbot/send_state.py
crosshair check plugin/chatbot/audio_recorder_state.py
crosshair check plugin.mcp.mcp_state.next_state
# tool_loop_state: deal+pytest + CrossHair on pure helpers (next_state off)
```

### Step 3: Add Verification to CI
Integrate CrossHair into the CI pipeline to run verification on every commit. (Not wired yet.)

### Step 4: Document Verification Status
Maintain a `verification_status.json` file tracking which components have been verified.

## Phase 7: Expand Verification to Tier 0 Modules

**Status (partial):** large catalog below is in place; remaining gaps and promotions are tracked under **Phase 9** and [`verification_status.json`](../verification_status.json) (not every listed row is `verified`).

1. **`plugin/framework/url_utils.py`** — `deal` + Hypothesis + CrossHair ([`tests/framework/test_url_utils_verification.py`](../tests/framework/test_url_utils_verification.py))
2. **`plugin/calc/address_utils.py`** — inverse column/address contracts + Hypothesis ([`tests/calc/test_address_utils_verification.py`](../tests/calc/test_address_utils_verification.py))
3. **`plugin/mcp/cors.py`** — origin normalize / safety ([`tests/mcp/test_cors_verification.py`](../tests/mcp/test_cors_verification.py))
4. **`plugin/framework/config_schema.py`** — `as_bool` / `parse_int_robust` / `parse_float_robust` ([`tests/framework/test_config_coerce_verification.py`](../tests/framework/test_config_coerce_verification.py)); not whole-file CrossHair
5. **`plugin/framework/tool.py`** — `_normalize_schema_for_strict_providers`, `to_openai_schema`, `to_mcp_schema` ([`tests/framework/test_tool_schema_verification.py`](../tests/framework/test_tool_schema_verification.py))
6. **`plugin/framework/async_stream.py`** — `accumulate_delta`, `_format_agent_tool_stream_line` ([`tests/framework/test_accumulate_delta_verification.py`](../tests/framework/test_accumulate_delta_verification.py))
7. **FSM catch-up** — CrossHair on `state_machine.py` + `tool_loop_state.py` helpers (`next_state` is `# crosshair: off`) ([`tests/chatbot/test_fsm_verification.py`](../tests/chatbot/test_fsm_verification.py)); `mcp_state.next_state` ([`tests/mcp/test_mcp_state_verification.py`](../tests/mcp/test_mcp_state_verification.py))
8. **`plugin/framework/json_utils.py`** — `safe_json_loads` FQN ([`tests/framework/test_json_utils_verification.py`](../tests/framework/test_json_utils_verification.py))
9. **`plugin/framework/errors.py`** — `format_error_payload` and `format_error_message` ([`tests/framework/test_error_payload_verification.py`](../tests/framework/test_error_payload_verification.py))
10. **`plugin/scripting/sandbox.py`** — `scrub_subprocess_env`, `wrap_command_for_sandbox`, `is_safe_workspace_path` ([`tests/scripting/test_sandbox_path_verification.py`](../tests/scripting/test_sandbox_path_verification.py))
11. **`plugin/framework/i18n.py`** — `_` translation and `get_active_locale` ([`tests/framework/test_i18n_and_memory_verification.py`](../tests/framework/test_i18n_and_memory_verification.py))
12. **`plugin/chatbot/memory.py`** — `upsert_memory_arguments_dict`, `memory_key_from_tool_arguments`, `format_upsert_memory_chat_line` ([`tests/framework/test_i18n_and_memory_verification.py`](../tests/framework/test_i18n_and_memory_verification.py))
13. **`plugin/framework/openrouter_model_id.py`** — `_split_suffix`, `resolve_openrouter_catalog_id`, `openrouter_model_ids_equivalent` ([`tests/framework/test_framework_modules_verification.py`](../tests/framework/test_framework_modules_verification.py))
14. **`plugin/framework/ast_stmt_edit.py`** — `is_name_call_expr`, `remove_expr_statements` ([`tests/framework/test_framework_modules_verification.py`](../tests/framework/test_framework_modules_verification.py))
15. **`plugin/framework/default_models.py`** — `resolve_model_id`, `get_provider_defaults` ([`tests/framework/test_framework_modules_verification.py`](../tests/framework/test_framework_modules_verification.py))
16. **`plugin/framework/constants.py`** — `get_local_timezone`, `now_aware` ([`tests/framework/test_framework_modules_verification.py`](../tests/framework/test_framework_modules_verification.py))
17. **`plugin/framework/appearance.py`** — `_luminance`, `get_monaco_theme_info` ([`tests/framework/test_framework_phase3_verification.py`](../tests/framework/test_framework_phase3_verification.py))
18. **`plugin/framework/event_bus.py`** — `EventBus.emit` exception isolation ([`tests/framework/test_framework_phase3_verification.py`](../tests/framework/test_framework_phase3_verification.py))
19. **`plugin/framework/config_service.py`** — `_check_read_access`, `_check_write_access` ([`tests/framework/test_framework_phase3_verification.py`](../tests/framework/test_framework_phase3_verification.py))
20. **`plugin/chatbot/chat_sidebar_mode.py`** — `sidebar_mode_flags_for_doc_type`, `get_mode_labels`, `mode_from_label` ([`tests/chatbot/test_chatbot_pure_verification.py`](../tests/chatbot/test_chatbot_pure_verification.py))
21. **`plugin/chatbot/skills.py`** — `HUMANIZER_GUIDANCE` constant & skill store ([`tests/chatbot/test_chatbot_pure_verification.py`](../tests/chatbot/test_chatbot_pure_verification.py))
22. **`plugin/chatbot/research_cache_fluff.py`** — `translated_research_cache_fluff` ([`tests/chatbot/test_chatbot_pure_verification.py`](../tests/chatbot/test_chatbot_pure_verification.py))
23. **`plugin/chatbot/web_research_cache.py`** — `snowball_lang_from_locale_tag`, `parse_research_cache_key`, `format_research_cache_key`, `jaccard`, `research_cache_similarity` ([`tests/chatbot/test_chatbot_pure_verification.py`](../tests/chatbot/test_chatbot_pure_verification.py))
24. **`plugin/scripting/import_policy.py`** — `venv_authorized_top_level_modules`, `venv_blocked_modules`, `inprocess_authorized_modules`, `format_venv_import_policy_for_prompt` ([`tests/scripting/test_scripting_pure_verification.py`](../tests/scripting/test_scripting_pure_verification.py))
25. **`plugin/scripting/config_limits.py`** — `_clamp_timeout`, `resolve_python_exec_timeout` ([`tests/scripting/test_scripting_pure_verification.py`](../tests/scripting/test_scripting_pure_verification.py))
26. **`plugin/scripting/calc_range.py`** — `ensure_rectangular_2d`, `is_calc_range_payload`, `pack_calc_range_envelope`, `_dedupe_column_names`, `column_vector_as_2d` ([`tests/scripting/test_scripting_pure_verification.py`](../tests/scripting/test_scripting_pure_verification.py))
27. **`plugin/scripting/helper_domain.py`** — `header_prefix`, `parse_helper_script_header`, `parse_run_import_call_spec` ([`tests/scripting/test_scripting_phase2_verification.py`](../tests/scripting/test_scripting_phase2_verification.py))
28. **`plugin/scripting/trusted_action_registry.py`** — `get_trusted_action_wiring` ([`tests/scripting/test_scripting_phase2_verification.py`](../tests/scripting/test_scripting_phase2_verification.py))
29. **`plugin/scripting/duckdb_sql.py`** — `get_sql_script_templates`, `parse_sql_script_header` ([`tests/scripting/test_scripting_phase2_verification.py`](../tests/scripting/test_scripting_phase2_verification.py))
30. **`plugin/scripting/sandbox_cache.py`** — `validate_sandbox_ast` ([`tests/scripting/test_scripting_ast_verification.py`](../tests/scripting/test_scripting_ast_verification.py))
31. **`plugin/scripting/trusted_rpc.py`** — `parse_worker_dict_result` ([`tests/scripting/test_trusted_rpc.py`](../tests/scripting/test_trusted_rpc.py))
32. **`plugin/scripting/editor_ipc.py`** — `failure_detail`, `failure_message` ([`tests/scripting/test_scripting_high_value_verification.py`](../tests/scripting/test_scripting_high_value_verification.py))
33. **`plugin/scripting/excel_xl.py`** — `make_xl` ([`tests/scripting/test_scripting_high_value_verification.py`](../tests/scripting/test_scripting_high_value_verification.py))
34. **`plugin/calc/formula_dep_chain.py`** — `_resolve_sheet_and_cell` ([`tests/calc/test_calc_dep_and_filter_verification.py`](../tests/calc/test_calc_dep_and_filter_verification.py))
35. **`plugin/calc/sheet_filter_criteria.py`** — `filter_connection_code`, `resolve_filter_operator_code`, `parse_sheet_filter_criterion` ([`tests/calc/test_calc_dep_and_filter_verification.py`](../tests/calc/test_calc_dep_and_filter_verification.py))
36. **`plugin/calc/excel_py_convert/resolve_refs.py`** — `resolve_dep` ([`tests/calc/test_calc_dep_and_filter_verification.py`](../tests/calc/test_calc_dep_and_filter_verification.py))
37. **`plugin/mcp/wire_types.py`** — `parse_jsonrpc_request`, `is_jsonrpc_notification`, `initialize_result`, `call_tool_result_image` ([`tests/mcp/test_mcp_wire_verification.py`](../tests/mcp/test_mcp_wire_verification.py))
38. **`plugin/writer/word_diff_split.py`** — `tokenize`, `split_change` ([`tests/writer/test_writer_diff_and_html_verification.py`](../tests/writer/test_writer_diff_and_html_verification.py))
39. **`plugin/writer/xhtml_style_postprocess.py`** — `decode_lo_css_class_suffix`, `compact_lo_style_name`, `extract_autostyle_parents_from_fodt`, `parse_style_block` ([`tests/writer/test_writer_diff_and_html_verification.py`](../tests/writer/test_writer_diff_and_html_verification.py))
40. **`plugin/calc/calc_addin_data.py`** — `_unwrap_cell`, `normalize_python_data_shape`, `finalize_python_data`, `calc_addin_data_to_python` ([`tests/calc/test_calc_dep_and_filter_verification.py`](../tests/calc/test_calc_dep_and_filter_verification.py))
41. **`plugin/scripting/audio_silence_detector.py`** — `pcm_energy_int16` (pytest `@deal` / Hypothesis; CrossHair `# crosshair: off` — deep check-all run 32877875221 hung on the RMS post) ([`tests/scripting/test_scripting_phase2_verification.py`](../tests/scripting/test_scripting_phase2_verification.py), [`tests/scripting/test_audio_silence_detector.py`](../tests/scripting/test_audio_silence_detector.py))
42. **`plugin/calc/cells.py`** — `_parse_color` (string-only wrapper over [`plugin/doc/visual_helpers.py`](../plugin/doc/visual_helpers.py) `parse_color_to_uno_int`) ([`tests/scripting/test_scripting_phase2_verification.py`](../tests/scripting/test_scripting_phase2_verification.py))
43. **`plugin/framework/client/stream_normalizer.py`** — `accumulate_streaming_thinking`, `_merge_reasoning_details`, `_normalize_stream_delta`, `_thinking_text_from_delta`, `_normalize_delta` ([`tests/framework/test_stream_normalizer_verification.py`](../tests/framework/test_stream_normalizer_verification.py))
44. **`plugin/framework/client/response_normalizers.py`** — `strip_leaked_chat_template_control_tokens`, `extract_and_strip_images_from_message` ([`tests/framework/test_response_normalizers_verification.py`](../tests/framework/test_response_normalizers_verification.py))
45. **`plugin/calc/python/formula_edit.py`** — quoted/unquoted `=PY()` parse, sanitize/escape, rebuild, data-range formatters, data-binding display/text helpers ([`tests/calc/python/test_formula_edit_verification.py`](../tests/calc/python/test_formula_edit_verification.py))
46. **`plugin/calc/spreadsheet_import/preprocess.py`** — `normalize_lo_formula_for_parse` ([`tests/calc/python/test_formula_edit_verification.py`](../tests/calc/python/test_formula_edit_verification.py))
47. **`plugin/scripting/payload_codec.py`** — pack/unpack + policy helpers (`cell_count`, `is_numeric_coercible`, `is_numeric_grid`, `wire_cell_count`) + envelope detectors (`is_split_grid`, `is_multi_data`, `is_image_payload`, `is_dataframe_payload`, `is_calc_range_payload`) + `host_pack_multi_data` ([`tests/scripting/test_payload_codec_policy_verification.py`](../tests/scripting/test_payload_codec_policy_verification.py), [`tests/scripting/test_serialization_verification.py`](../tests/scripting/test_serialization_verification.py)); see [`docs/scripting-serialization-verification.md`](scripting-serialization-verification.md)

(`format_support.py` does not exist; Writer HTML paths are UNO-heavy and deferred.)

### Phase 8: Deep Hypothesis targets (in `make vhs`)

**COMPLETED ✅** (wired in `Makefile` `vhs` recipe; light budgets under `make verify`, deep via `vhs_max_examples` / `WRITERAGENT_VHS_EXTENSIVE=1`)

Named deep domains (oracles + `test_hypothesis_*` so `-k hypothesis` selects them):

| Domain | Module | Verification tests |
|--------|--------|-------------------|
| Calc `=PY()` parse/serialize | [`formula_edit.py`](../plugin/calc/python/formula_edit.py) | [`test_formula_edit_verification.py`](../tests/calc/python/test_formula_edit_verification.py) |
| CORS origin/host safety | [`cors.py`](../plugin/mcp/cors.py) | [`test_cors_verification.py`](../tests/mcp/test_cors_verification.py) |
| Writer word diff / redline split | [`word_diff_split.py`](../plugin/writer/word_diff_split.py) | [`test_writer_diff_and_html_verification.py`](../tests/writer/test_writer_diff_and_html_verification.py) |
| Embeddings sentence chunking | [`embeddings_split.py`](../plugin/embeddings/embeddings_split.py) | [`test_embeddings_split_verification.py`](../tests/embeddings/test_embeddings_split_verification.py) |

Also on the same `make vhs` line (same budget helper): serialization A/B Hypothesis; chat/MCP FSM oracles; stream/response normalizers; sandbox path + `scrub_subprocess_env`; payload_codec policy; `address_utils` column/address round-trips.

**Not the same as “verified” in `verification_status.json`:** several of these modules remain **`partial`** because CrossHair is engine-limited or `ci_integration` is still false—Phase 8 only means **deep Hypothesis is in place and selected by `vhs`**. Closing those partials is Phase 9.

Playbook for adding another deep VHS domain: see § “Playbook for a new deep VHS domain” under Live output / `make verify` vs `vhs` above.

### Phase 9: What to work on next (recommended order)

Snapshot after envelope-detector work on `payload_codec` (2026-08): **~41 verified / ~14 partial** in [`verification_status.json`](../verification_status.json). Phases 5 and 8 are done; 6–7 are partial by design.

**Prefer closing high-ROI partials before opening new Tier-0 files.** New pure modules (e.g. `spreadsheet_import/translate.py`) are optional once the list below is thin.

#### 1. Promote “deal+Hyp done, status lagging” partials → `verified` (fastest wins)

These already have contracts, light Hypothesis, and (where useful) slow CrossHair FQNs. Work is mostly: confirm oracles, set `ci_integration: true` when the verification file is in `make verify`, flip `status` to `verified`, refresh notes/dates.

| Module | Why still partial | Action |
|--------|-------------------|--------|
| [`address_utils.py`](../plugin/calc/address_utils.py) | Strong inverse contracts + deep VHS; `ci_integration: false` | Promote if CrossHair FQN stays clean |
| [`cors.py`](../plugin/mcp/cors.py) | Security boundary; deep VHS; `ci_integration: false` | Promote after one clean CrossHair pass |
| [`formula_edit.py`](../plugin/calc/python/formula_edit.py) + [`preprocess.py`](../plugin/calc/spreadsheet_import/preprocess.py) | Deep VHS; past IndexError fixed | Promote pair together |
| [`stream_normalizer.py`](../plugin/framework/client/stream_normalizer.py) / [`response_normalizers.py`](../plugin/framework/client/response_normalizers.py) | Deep VHS; some `# crosshair: off` | Promote with notes on offs; keep FQN targets that work |

#### 2. FSM partials (Phase 6 catch-up — do not fight engine)

[`send_state`](../plugin/chatbot/send_state.py), [`audio_recorder_state`](../plugin/chatbot/audio_recorder_state.py), [`mcp_state`](../plugin/mcp/mcp_state.py), [`tool_loop_state`](../plugin/chatbot/tool_loop_state.py) / [`state_machine`](../plugin/chatbot/state_machine.py) helpers: deal + Hypothesis already on `vhs`. **`next_state` stays `# crosshair: off`** (`event.data` is unbounded `dict[str, Any]`, plus Exception/JSON/host paths). Next work = stronger pure ensures / more FSM strategies in [`fsm_hyp_support.py`](../tests/chatbot/fsm_hyp_support.py), not removing offs.

#### 3. Engine-hostile partials (shim or accept partial)

| Module | Reality |
|--------|---------|
| [`json_utils.py`](../plugin/framework/json_utils.py) / [`errors.py`](../plugin/framework/errors.py) | Symbolic `json` / Traceback — keep deal+Hypothesis; do not force check-all |
| [`payload_codec.py`](../plugin/scripting/payload_codec.py) | Reference module; pack/unpack often off; detectors/policy preferred FQNs — stay **partial** until a written bar for offs (see [`scripting-serialization-verification.md`](scripting-serialization-verification.md)) |

#### 4. Optional new Tier-0 slice (only after 1–2)

Highest unused pure surface: **[`plugin/calc/spreadsheet_import/translate.py`](../plugin/calc/spreadsheet_import/translate.py)** (formula → Python codegen, no UNO). Secondary: pure color/units in [`plugin/doc/visual_helpers.py`](../plugin/doc/visual_helpers.py) (`parse_color_to_uno_int`, mm/px helpers)—extract if mixed with UNO. Skip vendored `plugin/lib/**`.

#### 5. Tooling (low code, high leverage)

- Optional CI job: `make verify` (not full CrossHair-all).
- Keep `CROSSHAIR_*_SKIP` empty; hostility = shim / `# crosshair: off` / refactor.
- Stale “Practical Implementation Guide” samples below still mention old paths—treat Phase 9 + this section as source of truth over the illustrative CI YAML.

**Suggested next session (single track):** close the **address_utils + cors + formula_edit/preprocess** partial cluster (status flips + any missing FQN slow tests), then either FSM ensures or `translate.py` contracts.

---

## 8. Development Guidelines & Best Practices

1. **`deal_shim` Import Pattern (CRITICAL)**:
   - Always import `deal` via `from plugin.framework.deal_shim import deal` in all `plugin/` files.
   - LibreOffice's bundled Python runtime does **not** install `deal`. Using `import deal` directly will break LibreOffice component initialization at runtime. The `deal_shim` transparently falls back to no-op decorators when `deal` is missing.
2. **Filtering Low-Value Targets**:
   - Do NOT add contracts to pure constant sets or trivial single-line wrappers (e.g. `calc_functions_common.py`, `_lazy_venv.py`). Focus SMT verification on pure algorithms, security boundaries (`validate_sandbox_ast`, `scrub_subprocess_env`), data codecs (`payload_codec.py`), and state transition machines.
3. **Excluding Evolving Modules**:
   - Skip rapidly changing or experimental modules (e.g. vector search, folder FTS indexers) until their APIs stabilize.
4. **CI & Verification Suite**:
   - All verification unit tests MUST be added to `make verify` in `Makefile` and registered in `verification_status.json`.

### 8.1 Contract pitfalls (read before writing `@deal`)

These mistakes keep recurring when AIs add verification. Fix them in the contract/test, not by weakening production behavior.

#### A. Optional / defaulted parameters break naive contract lambdas

`deal` only forwards **arguments the caller actually passed**. Omitted defaults are **not** filled into `@deal.pre` / `@deal.ensure` lambdas. `@deal.ensure` also receives the return value as a **keyword** `result=...` (not always as a trailing positional).

**Wrong** (TypeError at runtime when callers omit the default, or when `result=` is passed):

```python
@deal.pre(lambda message, strip_structured_image_blocks: isinstance(message, dict))
@deal.ensure(lambda message, strip_structured_image_blocks, result: ...)
@deal.ensure(lambda *args: _ok(args[0]))  # missing **kwargs → TypeError: unexpected keyword argument 'result'
def extract_and_strip_images_from_message(message: dict, strip_structured_image_blocks: bool = True) -> list:
    ...
```

**Right** (absorb missing defaults + `result=` kwarg):

```python
@deal.pre(lambda *args, **kwargs: bool(args) and isinstance(args[0], dict))
@deal.post(lambda result: isinstance(result, list))
@deal.ensure(lambda *args, result=None, **kwargs: _string_content_ok(args[0]))
def extract_and_strip_images_from_message(message: dict, strip_structured_image_blocks: bool = True) -> list:
    ...
```

For keyword-only / multi-default APIs (especially when CrossHair must see the return value reliably), prefer the [`payload_codec.py`](../plugin/scripting/payload_codec.py) pattern: `_DEAL_RETURN` sentinel + `_deal_return(*a, result=result)` and `@deal.pre(lambda arg, *_, **__: ...)`. See also [`docs/scripting-serialization-verification.md`](scripting-serialization-verification.md) (“Functions with keyword-only parameters…”).

**Rule of thumb:** if the function has any defaulted parameter, do **not** write a fixed-arity `lambda a, b, result: ...` unless every call site always passes `b` positionally.

#### B. Keep runtime guards when `deal` is shimmed

Under LibreOffice, `deal_shim` is a no-op — `@deal.pre` does **not** run. If you delete `if not isinstance(x, dict): return` because “the pre already checks,” production regains AttributeError on bad inputs.

**Wrong:** replace a defensive early-return with only `@deal.pre(lambda x: isinstance(x, dict))` and remove the body guard.

**Right:** keep the cheap runtime guard in the body; let `@deal.pre` tighten CrossHair/Hypothesis under the dev venv.

#### C. Hypothesis oracles vs greedy production regexes

When fuzzing extractors that use greedy character classes (e.g. base64 `data:image/...;base64,([A-Za-z0-9+/=\s]+)`), random **suffix/prefix** text often uses the same alphabet. The regex then consumes past your intended payload and the oracle fails even though production is correct.

**Wrong:** `content = prefix + uri + suffix` with unrestricted `st.text()` around a greedy URI match, then assert exact `b64` equality.

**Right:** force a delimiter outside the regex alphabet (e.g. suffix = `"!" + …`), or assert weaker invariants (`mime_type` present, URI gone, `[Image Ref]` inserted) without requiring exact greedy-span equality.

Same idea for any greedy tokenizer: Hypothesis neighbors must not be absorbable by the match.

#### D. CrossHair annotation / directive traps (already easy to reintroduce)

- Do **not** put `typing.Literal[...]` in **parameter** annotations, **TypedDict fields**, or **dataclass fields** CrossHair must proxy — use `str` (or similar); keep `Literal` aliases for casts/comments only ([`payload_codec.py`](../plugin/scripting/payload_codec.py), [`errors.py`](../plugin/framework/errors.py) tool result TypedDicts, excel `HeaderMode` in [`models.py`](../plugin/calc/excel_py_convert/models.py) / [`to_dag.py`](../plugin/calc/excel_py_convert/to_dag.py)). Skipping a module in `CROSSHAIR_CHECK_ALL_SKIP` does not protect importers that pull those TypedDicts onto the heap.
- `# crosshair: off` must sit alone on its line (no trailing prose). Extra characters can raise `InvalidDirective`.
- Prefer **FQN** `crosshair check plugin.pkg.mod.fn` in `@pytest.mark.slow` tests for new slices. Do not reintroduce module skip-list entries — mark hostile callables with a shim or `# crosshair: off` (cover-all FQN filter), or extract a pure core.
- **Callable / regex helpers hang deep check** (no per-condition timeout). CrossHair synthesizes `Callable` arguments and then loops `re.search` (e.g. [`formula_edit._rewrite_token_calls`](../plugin/calc/python/formula_edit.py)). Put `# crosshair: off` on that helper **and** on public wrappers that still call `_rewrite_token_calls_body` (`sanitize_inline_py_code`, `escape_code_for_formula`, `rebuild_python_formula`, `rebuild_python_formula_with_data`). Cheap `isinstance(result, str)` post is the parse_address-class hang (hours even at `DEAL_MAX_SOURCE=16`); do not weaken the post so CrossHair explores more. Keep `@deal` so pytest still checks contracts. Same off on [`address_utils.parse_address`](../plugin/calc/address_utils.py) / `parse_range_string` (sheet-prefix + A1 regex); `@deal` stays for pytest. Recursive dict-merge ([`accumulate_delta`](../plugin/framework/async_stream.py)), `Any` payloads, and FSM `event.data: dict[str, Any]` (`mcp_state` / `send_state` / `tunnel_state` `next_state`) are the same class — off the entry, keep pytest `@deal`. Missing `@deal.pre` size caps on `list`/`str` (only `isinstance`) are the other deep-check sink — cap with `DEAL_MAX_*` / `str_bounded`, do not grow timeouts.
- **`formula_edit` PY/PYTHON head regex:** CrossHair's relib re-parses `Pattern.pattern` on symbolic match and `PatternError`s on `normalize_formula_string('PY')` / `parse_python_formula('PY')` even though CPython's compiled `^(?:PY|PYTHON)\s*\(` is valid. Scan aliases with `startswith`, do not `re.compile` the function names.
- **Nested callee pre after growth:** `sanitize_inline_py_code('dtype=float\x00')` (and escape/rebuild) used to `PreconditionFailed` on `_rewrite_token_calls`'s `str_bounded` because `dtype=float` → `dtype=np.float64` grows past CrossHair's `DEAL_MAX_SOURCE=16`. Call the unwrapped rewrite body from sanitize; wrappers are `# crosshair: off` (keep `@deal` for pytest). Do not weaken the wrapper pre so a future on-check can enter the regex loop.
- **`formula_edit` nested ensures:** `parse_python_formula` (`_parts_result_ok` / `_py_call_open_end`) and `normalize_formula_string` (curly-quote membership) were 15–45m deep posts. Skip those ensures under CrossHair via `inverse_ensure`; keep the cheap `@deal.post` so the FQNs stay analyzed. Pytest still enforces the nested ensures. Same pattern on [`cors.normalize_origins_list`](../plugin/mcp/cors.py) (unique-length ensure hung at the 360-minute job wall, [run 32840960268](https://github.com/KeithCu/writeragent/actions/runs/32840960268) Prev 9:51) and [`preprocess.normalize_lo_formula_for_parse`](../plugin/calc/spreadsheet_import/preprocess.py) (two nested ensures ~7+7+6 min).
- **`formula_edit` range regex / control ASCII:** `ascii_bounded` uses `str.isascii()`, which allows NUL. CrossHair's relib `TypeError`s `ord()` on `re.match` of `'\x00.\x00'` in `format_py_data_range` / `format_excel_data_range` (same run, same class as the PY|PYTHON `PatternError`). Those two formatters use plain str/char checks (A1 prefix, identifier, `c.isspace()`) and a printable `ascii_bounded` pre. Do **not** change global `ascii_bounded()`. `build_data_suffix` calls the unwrapped bodies so the printable pre cannot nested-`PreconditionFailed` (NUL still in `_deal_data_args_ok`).
- **`format_data_binding_display` / `parse_data_binding_text`:** A1 / range tokens — `ascii_bounded` (still includes `\x1c` control whitespace). Unicode `str_bounded` + strip/split was hang-class (~1.36M lines) even when the check exited 0. Deep check-all run 32840960268 still exploded after `ascii_bounded` (Prev 95:09 / 15:20) — both FQNs are `# crosshair: off`. `parse_data_binding_text` also dropped `re.split` for `replace(';')` + `str.split(',')`.
- **Hang-class FQNs (same run, `# crosshair: off` in the body):** `format_empty_model_response_debug` (engine traceback after 1:53), `format_delegate_running_chat_line` (Prev 56:34), `format_upsert_memory_chat_line` (Prev 20:53), `mode_from_label` (Prev 10:13, gettext labels), `openrouter_model_ids_equivalent` (Prev 11:29). Leave `_split_suffix` / `resolve_openrouter_catalog_id` on (~2 min).
- **`pcm_energy_int16`:** Deep check-all run [32877875221](https://github.com/KeithCu/writeragent/actions/runs/32877875221) hung on the RMS/peak float `@deal.post` at [42/56] (symbolic int16 PCM + `struct.unpack` + sqrt). `# crosshair: off` in the body; keep `@deal.pre`/`@deal.post` for pytest. Do not sniff `sys.modules["crosshair"]` to slice PCM — CrossHair explores both branches and the post still hangs. Do not shrink `DEAL_MAX_ARGV` (wrap_command argv). Same pattern as PR #450.
- Avoid `time.perf_counter()` (and similar clocks) on cover entry points without a CrossHair shim — causes `NotDeterministic` / exit 2.

#### E. String contracts: closed alphabet vs open text

CrossHair explores unbounded Unicode strings for hours. Caps belong in `@deal.pre`, but **`isascii` is not a universal cap** — unstripped pytest runs real `deal`, so a too-tight pre is a **dev-test** failure.

**Length caps are not production limits.** LibreOffice uses the no-op `deal_shim`; `scripts/strip_code.py` removes `@deal.*` from release OXTs (`make release` pytest is that stripped tree). Do not grow `DEAL_MAX_*` to match real HTML/source sizes.

Helpers in [`deal_shim.py`](../plugin/framework/deal_shim.py):

| Helper | Use when |
|--------|----------|
| `ascii_bounded(s, max_len, min_len=0)` | Closed alphabet: column letters, A1 / range strings, error **codes**, CORS origins, stream prefixes, URL suffixes |
| `DEAL_MAX_XL_EXPR` | DAG ``xl("%Pn%",headers=False)`` length (32 CrossHair / 64 pytest). Do **not** reuse ``DEAL_MAX_TOKEN`` (16 under CrossHair is shorter than that emit) |
| `str_bounded(s, max_len, min_len=0)` | Open text: gettext `_()` (`DEAL_MAX_MSGID`), HTML, `=PY()` / Excel source (`DEAL_MAX_SOURCE`). Length still bounds CrossHair |

`DEAL_MAX_CELL_REF` is **32** because `parse_address` / `parse_range_string` reject sheet prefixes; the longest legal range is well under that. Do **not** put `isascii` on `_()` (msgids include `"✓ Copied!"`, `"Testing…"`), HTML strippers, or formula source. `_()` keeps `DEAL_MAX_MSGID` (**1024** in both profiles) so real UI strings survive import under `WRITERAGENT_CROSSHAIR=1`; check-all does **not** shrink that constant — it marks `_()` `# crosshair: off` instead (analyzing 1024-char Unicode msgids would dominate deep check). Pytest `DEAL_MAX_SOURCE` is **8192** (real `=PY()` / Excel scripts); CrossHair stays **16**. CrossHair `DEAL_MAX_ORIGIN` / `DEAL_MAX_URL` are **32** (pytest **256**); `DEAL_MAX_HTML_CHUNK` is **16** (pytest **512** so the 256-char tag-flush path stays live).

The pytest domain is the product (ZZZ, Calc max row, `CELL_REF=32`); the CrossHair domain is the short table in `deal_maxima(crosshair=True)`; `WRITERAGENT_CROSSHAIR=1` is set by the check-all runner, cover-all (including process-pool workers and `ProcessPoolExecutor` initializer), and `scripts/crosshair_stream.py run` (`make crosshair-check` / `make crosshair-cover`) before `deal_shim` import. Pytest / `make test` never set it. Do not grow `DEAL_MAX_COL_INDEX` / `DEAL_MAX_COL_LETTERS` out of sync: `parse_address("ZZZ1")` calls `column_to_index`, and a nested `PreContractError` is a CrossHair error. Nested inverse ensures (`format_address` → `parse_address`, `column_to_index` → `index_to_column`) are skipped under CrossHair via import-time `inverse_ensure`.

Hypothesis `st.text(..., max_codepoint=127)` in `*_verification.py` is a **fuzzer** bound, not a function contract — leave those ASCII alphabets in place. Int domains need the same named caps (`DEAL_MAX_*`) so deep check cannot wander on loops, products, exponents, or f-strings of a giant int.

#### F. Single-pass string stripping vs interleaved control whitespace

When sanitizing formulas, range bindings, or prefixes (e.g. `format_data_binding_display`), single-pass checks (`if s.startswith(";"): s = s[1:]`) fail when input strings contain multiple leading delimiters (`;;`) or interleaved Unicode control whitespace (`,\x1c;`). In Python, `str.strip()` strips ASCII 28 (`\x1c`) control whitespace; stripping after a single-pass `lstrip` exposes the second delimiter underneath.

**Wrong:** single-pass `s = s.strip().lstrip(";,").rstrip(")")` (leaves `;` on `,\x1c;`).

**Right:** use a fixed-point loop (`while True: prev = s; s = s.strip().lstrip(";,").rstrip(")"); if s == prev: break`) until the string reaches a clean invariant state.

#### G. Checklist when finishing a verification slice

1. Run existing unit tests for the module **with deal installed** (contracts execute in the dev venv).
2. Run the new `*_verification.py` file with `-m "not slow"`, then the slow CrossHair FQN tests.
3. Update [`verification_status.json`](../verification_status.json) and the Phase 7 list in this doc.
4. Do not leave broken doc links to non-existent roadmaps.

---

## Conclusion

By adopting concolic execution (CrossHair) and Design by Contract (`deal`), we can elevate the reliability of WriterAgent's pure algorithmic core from "empirically tested" to "mathematically robust." We acknowledge the intractability of verifying the entire application, and instead focus our SMT solvers exclusively on the pure data-transformation pipelines that feed our axiomatic UNO environment.

## Practical Implementation Guide for WriterAgent

### Step 1: Framework-First Verification (Recommended Starting Point)

**Priority Order for Framework Modules:**

1. **`plugin/framework/config.py`** (Highest Priority)
   - Combined URL and settings utilities
   - Pure string operations for URLs
   - Critical for web operations and API access
   - Example contracts:
     ```python
     @deal.pre(lambda url: isinstance(url, str))
     @deal.post(lambda result: result.startswith(('http://', 'https://')) or result == '')
     @deal.ensure(lambda url, result: not url or result)  # Empty in → empty out
     def ensure_scheme(url: str) -> str:
         """✅ VERIFIED: URL scheme enforcement"""
         # ... implementation ...
     
     @deal.post(lambda result: os.path.isabs(result))
     @deal.ensure(lambda result: os.path.exists(result) or True)  # May not exist yet
     def get_plugin_dir() -> str:
         """✅ VERIFIED: Returns absolute plugin directory path"""
         # ... implementation ...
     ```

2. **Pure Data Parsing & AST Transformation Modules**
   - Pure string operations, payload codecs, AST policy validators, and tokenizers
   - Used across Writer, Calc, and MCP protocols
   - Verify format preservation, mathematical bounds, and contract invariants

3. **`plugin/framework/tool.py`** (`to_openai_schema` / `to_mcp_schema` / `_normalize_schema_for_strict_providers`)
   - JSON schema transformations
   - Tool parameter validation
   - Prove schema equivalence properties

4. **`plugin/framework/config.py`** (Adapter Layer)
   - Configuration validation logic
   - Type safety guarantees
   - Verify config consistency invariants

### Step 2: Verification Workflow

**For each module:**

```bash
# 1. Add type hints and deal contracts
# 2. Run static type checking
mypy plugin/framework/config.py --strict

# 3. Run CrossHair verification
crosshair check plugin/framework/config.py --contracts

# 4. Add to test suite
pytest tests/test_url_utils_verification.py
```

**Sample test file:**
```python
# tests/test_url_utils_verification.py
import subprocess
import pytest

def test_url_utils_contracts():
    """Verify all contracts in url_utils module"""
    result = subprocess.run([
        "crosshair", "check",
        "plugin/framework/config.py",
        "--contracts",
        "--per_condition_timeout=5"
    ], capture_output=True, text=True, timeout=60)
    
    print(f"CrossHair output:\n{result.stdout}")
    if result.stderr:
        print(f"Errors:\n{result.stderr}")
    
    assert result.returncode == 0, "CrossHair found contract violations"
```

### Step 3: Verification Tracking System

**Maintain a `verification_status.json` file:**
```json
{
  "framework": {
    "config.py": {
      "status": "verified",
      "coverage": "100%",
      "contracts": 22,
      "functions_verified": [
        "normalize_endpoint_url",
        "get_url_hostname",
        "get_url_domain",
        "get_url_path",
        "get_url_query_dict",
        "get_url_path_and_query",
        "is_pdf_url",
        "get_plugin_dir"
      ],
      "last_verified": "2026-03-15",
      "tool": "crosshair",
      "ci_integration": true
    },
    "format_support.py": {
      "status": "partial",
      "coverage": "65%",
      "contracts": 12,
      "pending": ["normalize_paragraphs", "strip_html_tags"],
      "notes": "HTML parsing requires mocking - needs custom harness"
    }
  },
  "modules": {
    "calc": {
      "address_utils.py": {
        "status": "planned",
        "priority": "high"
      }
    }
  }
}
```

**Update verification status script:**
```python
# scripts/update_verification_status.py
import json
import subprocess
from pathlib import Path

def update_status(module_path: str, tool: str = "crosshair"):
    """Update verification status after successful run"""
    status_file = Path("verification_status.json")
    
    if not status_file.exists():
        status = {"framework": {}, "modules": {}}
    else:
        status = json.loads(status_file.read_text())
    
    # Parse module path to determine category
    parts = module_path.split('/')
    if parts[1] == 'framework':
        category = 'framework'
        module_name = parts[2]
    else:
        category = parts[1]
        module_name = parts[3] if len(parts) > 3 else parts[2]
    
    # Update status
    if category not in status:
        status[category] = {}
    
    status[category][module_name] = {
        "status": "verified",
        "last_verified": "2026-03-15",  # Use actual date
        "tool": tool,
        "ci_integration": False
    }
    
    status_file.write_text(json.dumps(status, indent=2))
    print(f"✅ Updated verification status for {module_path}")
```

### Step 4: CI Integration

**Add to `.github/workflows/verify.yml`:**
```yaml
name: Formal Verification

on: [push, pull_request]

jobs:
  verify:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        module: [
          "plugin/framework/config.py",
          "plugin/writer/format_support.py"
        ]
    
    steps:
    - uses: actions/checkout@v4
    
    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.12'
    
    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        pip install deal crosshair
    
    - name: Run CrossHair verification
      run: |
        crosshair check ${{ matrix.module }} --contracts --per_condition_timeout=10
    
    - name: Update verification status
      if: success()
      run: |
        python scripts/update_verification_status.py ${{ matrix.module }}
    
    - name: Commit updated status
      if: success() && github.ref == 'refs/heads/master'
      run: |
        git config --global user.name "Verification Bot"
        git config --global user.email "bot@example.com"
        git add verification_status.json
        git commit -m "chore: update verification status for ${{ matrix.module }}"
        git push
```

### Step 5: Documentation Standards

**Add verification badges to docstrings:**
```python
def ensure_scheme(url: str) -> str:
    """
    Ensure URL has proper scheme prefix.
    
    ✅ VERIFICATION STATUS:
    - Type safety: mypy (strict)
    - Contracts: deal (4/4 verified)
    - Concolic: CrossHair (100% coverage)
    - Last verified: 2026-03-15
    
    Args:
        url: Input URL string (may lack scheme)
        
    Returns:
        URL with http:// or https:// prefix
        
    Raises:
        ValueError: If url is empty after normalization
        
    Contracts:
        @deal.pre: Non-empty string input
        @deal.post: Result starts with http:// or https://
        @deal.ensure: Preserves path/query/fragment
    """
    # ... implementation ...
```

## Verification Anti-Patterns to Avoid

1. **❌ Don't verify UNO wrapper code**
   - Stick to the axiomatic boundary
   - UNO calls should be in unverified adapter layers

2. **❌ Avoid complex contracts on I/O functions**
   - File operations, network calls are hard to verify
   - Keep contracts simple for these cases

3. **❌ Don't over-specify**
   - Contracts should capture essential properties
   - Too many contracts make verification brittle

4. **❌ Avoid verifying *tangled* UI/orchestration code**
   - Do not attempt to attach FV contracts to functions that intermingle state mutation and I/O (e.g., updating UI side-by-side with calculating states).
   - Instead, extract the implied state machine into a pure transition function (as described in Phase 5), and strictly verify *that* function instead.

5. **❌ Don't write fixed-arity `@deal.ensure` / `@deal.pre` lambdas for defaulted parameters**
   - Causes `TypeError: missing … argument` / `unexpected keyword argument 'result'` under real call sites. See §8.1 A and the `_DEAL_RETURN` helpers in `payload_codec.py`.

6. **❌ Don't delete runtime type guards because `@deal.pre` “already checks”**
   - `deal_shim` is a no-op inside LibreOffice. See §8.1 B.

7. **❌ Don't build Hypothesis oracles that fight greedy regex alphabets**
   - Delimit fuzz neighbors outside the match class. See §8.1 C.

## Recommended Tool Chain

```
Pure Python Logic → [mypy] → [deal contracts] → [CrossHair] → ✅ Verified
                     ↑                                      ↓
               Type Safety                          Counterexamples
```

**Installation:**
```bash
pip install deal crosshair mypy
```

**Daily Workflow:**
```bash
# Develop with contracts
vim plugin/framework/config.py  # Add @deal decorators

# Verify locally
mypy plugin/framework/config.py --strict
crosshair check plugin/framework/config.py --contracts

# Commit with verification
git add plugin/framework/config.py
python scripts/update_verification_status.py plugin/framework/config.py
git add verification_status.json
git commit -m "feat: add verified URL utilities"
```

By following this framework-first approach, we build a solid foundation of verified code that all higher-level modules can rely on. The verification status becomes a living document that grows as we harden more of the codebase.
