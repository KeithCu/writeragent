---
name: Python-in-Calc Dev Plan
overview: Map the Microsoft Python-in-Excel feature ideas onto WriterAgent's existing infrastructure (=PYTHON(), venv worker, Monaco editor bridge, chat tools) and produce a phased, incremental development plan that builds on shipped code.
todos:
  - id: phase1-session
    content: "Phase 1: Session persistence -- shared kernel mode for =PYTHON() cells with row-major variable sharing"
    status: completed
  - id: phase2-matplotlib
    content: "Phase 2: Matplotlib visualization pipeline -- figure detection, PNG serialization, image insertion in Calc"
    status: completed
  - id: phase3-monaco
    content: "Phase 3: Enhanced Monaco editor for Calc cells -- cell-edit mode, sheet grouping, range insertion"
    status: in_progress
  - id: phase4-init
    content: "Phase 4: Initialization scripts -- per-workbook startup scripts for global imports and helpers"
    status: completed
  - id: phase5-objects
    content: "Phase 5: Python Object cards -- rich preview for DataFrames and complex returns (requires Phase 1)"
    status: pending
  - id: phase6-diagnostics
    content: "Phase 6: Diagnostics pane -- structured error display with cell navigation"
    status: pending
  - id: phase7-ai
    content: "Phase 7: AI code synthesis enhancements -- context-aware generation, error auto-fix, agentic workflows"
    status: pending
isProject: false
---

# Incremental Dev Plan: Python-in-Calc Feature Parity

## What Already Exists

WriterAgent already ships a substantial subset of the Python-in-Excel vision. Before planning new work, here is what maps directly:

| Excel Feature | WriterAgent Equivalent | Status |
|---|---|---|
| `=PY(code, return_type)` | `=PYTHON(code, data?)` | **Shipped** - [plugin/calc/python_function.py](plugin/calc/python_function.py) |
| `xl()` data bridge | `data` variable injection | **Shipped** - [plugin/calc/calc_addin_data.py](plugin/calc/calc_addin_data.py) |
| Multi-range references | Varargs IDL + `multi_data` envelope | **Shipped** - [plugin/calc/calc_addin_data.py](plugin/calc/calc_addin_data.py) |
| Anaconda package ecosystem | User-provided venv (numpy, pandas, scipy, etc.) | **Shipped** - local, no cloud dependency |
| Cloud sandbox execution | Venv subprocess + AST sandbox | **Shipped** - [plugin/scripting/](plugin/scripting/) |
| Monaco code editor | Webview-based Monaco child process | **Shipped** - [plugin/scripting/editor_host.py](plugin/scripting/editor_host.py) |
| AI code generation | `=PROMPT()` + `run_venv_python_script` chat tool | **Shipped** |
| High-perf serialization | Pickle5 + Split-Grid (20x faster than JSON) | **Shipped** - [plugin/scripting/payload_codec.py](plugin/scripting/payload_codec.py) |
| Matrix formula (indexed spill) | Ctrl+Shift+Enter + ROW() indexing + worker result session cache | **Shipped** (manual range; not Excel auto-spill) |
| Excel-style dynamic auto-spill | Auto-fill adjacent cells; `#SPILL!` when blocked | **Not shipped** — see [enabling_numpy §7](enabling_numpy_in_libreoffice.md#calc-ux-and-output-enhancements) |
| Error coercion (int->float, NaN->empty) | `to_calc_compatible` pipeline | **Shipped** |

**Key architectural advantage over Microsoft:** WriterAgent runs **locally** (user venv subprocess), not in a cloud container. This means zero network latency, offline support, and no compute tier restrictions -- the exact "competitive enhancements" that Section 9 of the spec calls out.

## Current Status (Updated 2026)

**This document was refreshed** because real implementation work has significantly outpaced the original plan.

Significant progress has occurred beyond this original plan. The document has been refreshed to reflect reality rather than remaining aspirational.

**Notable completed / substantially advanced work:**
- **Phase 2 (Matplotlib / Visualization)**: Largely complete in practice.
  - Worker-side figure detection + PNG serialization (`_figure_to_image_payload`, implicit `plt.show()` capture, Agg backend) in `venv_sandbox.py`.
  - Image payload envelope support in `payload_codec.py`.
  - `=PYTHON()` return path inserts images as `GraphicObjectShape` on the sheet (`python_function.py`).
  - Chat-side Python tool (`run_venv_python_script`) also handles image results and returns `image_path` for insertion via existing tools (`calc/venv_python.py`).
  - Not 100% Excel-compatible (no automatic "spill" behavior or perfect parity), but very usable today.

- **Phase 3 (Monaco for Calc cells)**: Partial but functional implementation exists.
  - `plugin/calc/python_editor.py` + context menu support for opening/editing `=PYTHON()` formulas in the Monaco editor.
  - Sheet-level awareness and data binding are partially present.

Other phases remain largely as originally described (see below). The plan is kept as a living document.

## What Needs Building (Gap Analysis)

These are the features from the spec that do **not** yet exist, ordered by incremental value and dependency:

1. **Session persistence** -- cells share state (row-major kernel) (**shipped** — Settings → Python → Shared kernel; WriterAgent → Reset Python Session)
2. **Matplotlib/visualization pipeline** -- chart output from Python code into cells (**largely complete** — see Current Status and Phase 2 section)
3. **Enhanced Monaco editor UX for Calc** -- sheet-level code grouping, per-cell editing (**partial implementation exists**)
4. **Initialization scripts** -- per-workbook init.py for global imports/helpers
5. **Python Object cards** -- rich metadata preview for non-scalar returns
6. **Diagnostics pane** -- structured error display with cell navigation
7. **AI-driven code synthesis enhancements** -- Copilot-style completion in Python cells

### Backlog not yet phased

These items are tracked in [enabling_numpy_in_libreoffice.md — Calc UX and output enhancements](enabling_numpy_in_libreoffice.md#calc-ux-and-output-enhancements) and [python-in-excel-ideas.md §10](python-in-excel-ideas.md#10-writeragent-calc-enhancement-backlog). They are **not** assigned to Phases 1–7 yet:

| Item | Suggested phase / area |
|------|------------------------|
| **Dynamic auto-spill** (2D result → adjacent cells, blocked-cell error) | New work on `python_function.py` + spill detection; related to matrix cache |
| **DataFrame → rich table** egress | Extend Phase 5 or **Phase 5b** (styled Calc table, not just object card) |
| **JSON-structured `result` envelope** | `payload_codec` + host apply path; agent-driven multi-cell updates |
| **Inline result preview** | Phase 3 (editor UX) or Phase 6 (diagnostics-adjacent) |
| **Formula-bar Jedi / IntelliSense** | [python-monaco-editor-dev-plan.md](python-monaco-editor-dev-plan.md) Phase 2D + Calc formula bar |
| **Named ranges / structured tables / headers in `data`** | **Phase 8** or `calc_addin_data.py` subsection (data handoff) |
| **AST / hot-path compile cache** | Performance; [enabling_numpy §7](enabling_numpy_in_libreoffice.md#calc-ux-and-output-enhancements) |
| **Cell-level traceback snippet** | Phase 6 (diagnostics); short form in cell until pane ships |

---

## Phase 1: Session Persistence (Shared Kernel Mode)

**Status (2026):** Shipped. Default remains **Isolated** (one namespace per `=PYTHON()` call). Enable **Shared kernel** in Settings → Python so variables persist across cells in the same Calc workbook. **WriterAgent → Reset Python Session** clears the workbook namespace (visible in Writer, Calc, and Draw; only applies when a Calc spreadsheet is active and shared mode is on).

> [!IMPORTANT]
> **Shared kernel contract:** One persistent global Python namespace per workbook. Any `=PYTHON()` cell can read or overwrite names from any other cell. Calc may invoke cells in dependency order (not strict row-major). The only reliable ordering guarantee is that cell code runs **after** the Initialization Script. Assume each cell **can run any time, any number of times** while the workbook is open — write **idempotent** code (safe to re-run; see below). See [Shared kernel lifecycle & recalc semantics](#shared-kernel-lifecycle--recalc-semantics) below.

**Original gap:** Each `=PYTHON()` cell used a fresh `LocalPythonExecutor` namespace. The Excel model uses row-major stateful execution where a DataFrame created in A1 is available in B2.

**What was built:**

- Add a **`PythonSessionManager`** alongside [PythonWorkerManager](plugin/scripting/venv_worker.py) that maintains a **persistent namespace** per workbook
- New config key `scripting.python_session_mode` (default `"isolated"`, option `"shared"`) in [plugin/scripting/module.yaml](plugin/scripting/module.yaml)
- Modify [worker_harness.py](plugin/scripting/worker_harness.py) to accept a `session_id` field; when present, reuse the `LocalPythonExecutor` instance instead of creating a new one
- Session reset command (`Ctrl+Alt+Shift+F9` equivalent): new `reset_session` request type in the worker protocol; wire to a menu item or keyboard shortcut
- **Safety:** session mode still runs in the venv subprocess (ABI-safe); the sandbox's iteration/operation limits still apply per-cell

**Key files to modify:**
- [plugin/scripting/worker_harness.py](plugin/scripting/worker_harness.py) -- add `session_id` routing, `reset_session` handler
- [plugin/scripting/venv_sandbox.py](plugin/scripting/venv_sandbox.py) -- session-aware executor cache
- [plugin/scripting/venv_worker.py](plugin/scripting/venv_worker.py) -- forward session_id
- [plugin/calc/python_function.py](plugin/calc/python_function.py) -- derive session_id from workbook URL
- [plugin/scripting/module.yaml](plugin/scripting/module.yaml) -- new config key

**Tests:** [`tests/scripting/test_session_persistence.py`](../tests/scripting/test_session_persistence.py) — cross-cell visibility, session reset, isolation between workbooks.

**Key files:** [`session_manager.py`](../plugin/scripting/session_manager.py) (workbook `calc:` session id + menubar reset), plus worker/sandbox/harness changes listed above.

### Shared kernel lifecycle & recalc semantics

Microsoft Python in Excel keeps one **global Python namespace** per workbook. Standard recalc (F9, auto on edit, dirty cells) does **not** reset that namespace — globals persist until the user chooses **Reset Runtime** (Ctrl+Alt+Shift+F9). Excel compensates with **co-volatility**: when any `=PY` cell recalculates, **all** PY cells in the workbook re-execute in row-major order, so each cell refreshes its contributions to the shared state. See [Python in Excel: PY Calculation, Globals & Co-Volatility](https://fastexcel.wordpress.com/2023/11/01/python-in-excel-py-calculation-globals-co-volatility/).

WriterAgent **Shared kernel** mode matches the persistence model (no auto-reset on recalc) but **does not** implement Excel co-volatility. Calc uses its native dependency DAG: only dirty cells and their dependents recalculate, and order is **not** guaranteed to be row-major. The model is unusual but workable if users understand the contract:

| Rule | Meaning |
|------|---------|
| **One namespace per workbook** | The `calc:…` worker session (`_SESSION_EXECUTORS` in [`venv_sandbox.py`](../plugin/scripting/venv_sandbox.py)) lives until **WriterAgent → Reset Python Session**, worker restart/crash, init-script hash change + re-seed, or (optional, not wired by default) document unload via [`python_workbook_lifecycle.py`](../plugin/calc/python_workbook_lifecycle.py). |
| **Any cell can clobber any name** | Variables are true shared mutable globals. Cell B1 can overwrite a name defined in A1; there is no per-cell isolation. |
| **Weak execution order** | Do not assume row-major or DAG order among `=PYTHON()` cells. Implicit cross-cell dependencies (e.g. `result = x + 1` where `x` was set in another cell) are fragile. The **only** hard ordering guarantee: cell code always runs **after** the workbook Initialization Script has completed in the `calc:…:init` session. |
| **Runs any time** | Each cell may be invoked zero, one, or many times while the workbook is open (partial recalc, matrix spill, manual F9, etc.). Treat every cell as **idempotent / restartable**. |
| **Escape hatch** | **WriterAgent → Reset Python Session** (Calc shared mode) clears the workbook namespace and init cache — the equivalent of Excel's Reset Runtime. |

**Authoring guidelines:**

1. **Define before use** when cells depend on each other — prefer explicit `data` range args so Calc's DAG tracks inputs, or keep dependent logic in one cell / the init script.
2. **Avoid unbounded accumulation** (`mylist.append(x)` every recalc without resetting) unless that is intentional.
3. **One-time expensive setup** belongs in the **Initialization Script**, not repeated in every cell.
4. **Side effects** (writing to sheets, files, network) inside cells should be idempotent or clearly intentional on re-run.

**What “idempotent” means:** A cell is idempotent when running it **again** (after F9, an edit elsewhere, or a partial recalc) produces the **same intended outcome** as running it once — it does not keep adding unwanted changes each time. Think “safe to re-run.”

| Pattern | Idempotent? | Why |
|---------|-------------|-----|
| `result = data * 2` | Yes | Recomputes from sheet `data` every time; same inputs → same `result`. |
| `result = df.groupby("col").sum()` | Yes | Derives output only from current `data`, not from a counter that grows. |
| `runs += 1; result = runs` | No | Every recalc increments `runs`; the cell “remembers” past runs in the shared namespace. |
| `cache.append(x)` with no reset | No | The list grows on every invocation unless you clear it on purpose. |
| Write a file / insert a shape every run | Usually no | Re-run duplicates side effects unless you guard with “only if changed” logic. |

If you **want** state to accumulate (e.g. a running total across recalcs), that is fine — but treat it as a deliberate choice, not an accident. When in doubt, compute `result` from `data` and init-script helpers only, and use **Reset Python Session** when you need a clean slate.

**Not reset automatically:** F9, Ctrl+Shift+F9 (hard recalc), or editing a single cell does **not** clear the shared kernel. A "reset on every full recalc" mode is **not** planned as default; if ever added, it would be an opt-in setting with best-effort detection only (not cell-position heuristics — Calc does not expose a reliable recalc-pass boundary to add-ins).

**Related:** [`WorkerResultSession`](../plugin/calc/python_function.py) is a separate, thread-local cache for matrix list results within one recalc pass; it does not manage cross-recalc shared variables.

---

## Phase 2: Matplotlib / Visualization Pipeline

**Status (2026):** Largely complete and usable in practice (though not 100% Excel-compatible).

**What has been implemented:**

- Worker-side detection of `matplotlib.figure.Figure` objects and automatic capture of open pyplot figures after execution (including implicit `plt.show()` cases).
- PNG serialization via `savefig` into a standardized `__wa_payload__: "image"` envelope.
- `=PYTHON()` cells: Image results are automatically inserted as `GraphicObjectShape` on the active sheet's draw page.
- Chat tool (`run_venv_python_script` / Python specialized domain): Image results are returned with a temp `image_path` that existing image tools can consume.
- Non-interactive backend forcing (`Agg`) and proper cleanup.

**Current behavior:**
- Works for both direct `return fig` and code that ends with `plt.show()` / open figures.
- Images appear in Calc sheets when returned from `=PYTHON()`.
- Good enough for real use, even if placement, sizing, and multiple-figure handling are not as polished as the Microsoft version.

**Remaining polish (lower priority):**
- More control over insertion location / anchoring.
- Better handling of multiple figures.
- Optional "replace existing chart" behavior.
- Tighter integration with Calc's drawing layer (z-order, grouping, etc.).

**Key files involved (already updated):**
- [plugin/scripting/venv_sandbox.py](plugin/scripting/venv_sandbox.py)
- [plugin/scripting/payload_codec.py](plugin/scripting/payload_codec.py)
- [plugin/calc/python_function.py](plugin/calc/python_function.py)
- [plugin/calc/venv_python.py](plugin/calc/venv_python.py) (chat tool side)

**Original goal (for reference):** Python code that calls `plt.show()` or returns a matplotlib `Figure` produces an image inserted into the sheet (or floating above it).

**Historical planned items (largely completed by 2026):**

- In [venv_sandbox.py](plugin/scripting/venv_sandbox.py), detect when `result` is a matplotlib Figure or pyplot object; serialize to PNG bytes (via `savefig` to BytesIO)
- New return envelope type `__wa_payload__: "image"` in [payload_codec.py](plugin/scripting/payload_codec.py) with the PNG bytes
- On the host side, [python_function.py](plugin/calc/python_function.py) intercepts image payloads and inserts a `com.sun.star.drawing.GraphicObjectShape` on the sheet's draw page
- For the chat tool `run_venv_python_script`, return the image as a temp file path and use existing image insertion tools

**Tests note:** Basic functionality is working. Dedicated UNO tests for shape insertion would still be valuable.

---

## Phase 3: Enhanced Monaco Editor for Calc Cells

**Status (2026):** Partial implementation exists and is functional.

**What currently works:**
- `plugin/calc/python_editor.py` provides the ability to open and edit `=PYTHON()` formulas from Calc cells in the Monaco editor.
- Context menu integration for Python cells.
- Basic load/save roundtrip for cell formulas.

**What is still missing (from the original plan):**
- Full sheet-level grouping view of all Python cells in the workbook.
- Robust point-and-click range insertion from Calc while the editor is open.
- Dedicated toolbar button / more polished UX for "Edit Python in cell".

**What exists (shared infrastructure):** [editor_host.py](plugin/scripting/editor_host.py) (spawn, bridge, session launch), and the webview Monaco process (already used by the "Run Python Script" feature).

**Remaining work (from original plan):**

- **Cell-edit mode:** When user double-clicks a `=PYTHON()` cell (or uses a keyboard shortcut), open the Monaco editor pre-loaded with that cell's code. On save, write back to the formula
- Wire a Calc cell selection listener that detects `=PYTHON()` and enables an "Edit in Python Editor" toolbar button or context menu entry
- **Sheet-level grouping view:** Add a `list_python_cells` message type in [editor_protocol.py](plugin/scripting/editor_ipc.py) -- host enumerates all `=PYTHON()` cells in the workbook and sends grouped-by-sheet metadata to Monaco
- **Point-and-click range insertion:** When the editor is open, cell range selections in Calc send an `insert_reference` message to Monaco with the range address (e.g. `A1:B10`)
- The `data_binding` field in the save message already exists in [editor_bridge.py](plugin/scripting/editor_host.py) -- use it for the data range argument

**Key files to modify:**
- [plugin/scripting/python_runner.py](plugin/scripting/python_runner.py) -- add Calc cell-edit launch path
- [plugin/scripting/editor_host.py](plugin/scripting/editor_host.py) -- `insert_reference` message handler
- [plugin/scripting/editor_ipc.py](plugin/scripting/editor_ipc.py) -- new message types
- New: cell selection listener (UNO `XSelectionChangeListener`) in a Calc-specific module
- [extension/Addons.xcu](extension/Addons.xcu) -- toolbar button or menu entry

---

## Phase 4: Initialization Scripts

**Status (2026):** Shipped. One init script per Calc workbook in `UserDefinedProperties` (`WriterAgentCalcInitScript`). **WriterAgent → Edit Initialization Script…** (Calc only) opens Monaco; save persists to the document and clears in-memory init/cell sessions.

**Semantics:** Init is **orthogonal** to **Python session mode**. Isolated mode still runs the init script **once** in a persistent `calc:…:init` worker session; each `=PYTHON()` cell gets a fresh namespace **seeded** from that snapshot (imports/helpers shared, cell variables not). Shared kernel runs init once, seeds the workbook session once, then reuses it for cells. **Reset Python Session** clears both. Script changes are detected via SHA-256 hash on each eval; saving in the init editor also calls `reset_python_session`. Optional **OnUnload** reset ([`python_workbook_lifecycle.py`](../plugin/calc/python_workbook_lifecycle.py)) is implemented but callers are commented out—reopen may reuse cached init until the script changes or the worker restarts.

**Key files:** [`init_scripts.py`](../plugin/scripting/init_scripts.py), [`init_script_editor.py`](../plugin/calc/init_script_editor.py), [`venv_sandbox.py`](../plugin/scripting/venv_sandbox.py) (`init_script` / `init_session_id` on worker requests), [`python_function.py`](../plugin/calc/python_function.py), [`session_manager.py`](../plugin/scripting/session_manager.py).

**Tests:** [`tests/scripting/test_init_scripts.py`](../tests/scripting/test_init_scripts.py).

---

## Phase 5: Python Object Cards (Rich Preview)

**Goal:** When `=PYTHON()` returns a complex object (DataFrame, dict, class instance), show a preview card instead of `#VALUE!`.

**What to build:**

- In session mode, allow cells to hold object **references** (string key like `__pyobj_42__`) while the actual object stays in the worker namespace
- Display the reference as a compact cell value (e.g. `[DataFrame 150x4]`) via a short summary string
- On cell hover/click, send a `inspect_object` request to the worker that returns shape, dtypes, head(5), etc.
- Display the result in an XDL dialog (reuse `DialogProvider` patterns from [plugin/chatbot/dialogs.py](plugin/chatbot/dialogs.py))
- Optional: "Spill to Grid" action that extracts the DataFrame into adjacent cells (already works with matrix formulas; this adds a UI gesture)

**Dependency:** Requires Phase 1 (session persistence) -- objects must survive beyond a single cell evaluation.

**Key files to modify:**
- [plugin/scripting/venv_sandbox.py](plugin/scripting/venv_sandbox.py) -- object registry, `inspect_object` handler
- [plugin/scripting/worker_harness.py](plugin/scripting/worker_harness.py) -- `inspect_object` protocol message
- [plugin/calc/python_function.py](plugin/calc/python_function.py) -- object reference return + summary string
- New: `plugin/calc/python_object_inspector.py` -- XDL dialog for preview

---

## Phase 6: Diagnostics Pane

**Goal:** When a `=PYTHON()` cell fails, show the traceback and error context in a structured pane (Section 7 of the spec).

**What to build:**

- Currently errors return as cell error text. Enhance to also log structured error details (cell address, traceback, code snippet)
- Add a "Python Diagnostics" sidebar panel or docked pane (XDL dialog) that lists all cells with errors, grouped by sheet
- Click-to-navigate: selecting an error entry navigates to the offending cell
- Filter by: errors only, stdout/print output, all cells
- Wire error details from the worker response (`message`, `stdout`, `traceback` fields already exist in the protocol)

**Key files to modify:**
- [plugin/calc/python_function.py](plugin/calc/python_function.py) -- collect error details per-cell
- New: `plugin/calc/python_diagnostics.py` -- diagnostics panel UI
- [plugin/scripting/worker_harness.py](plugin/scripting/worker_harness.py) -- ensure full traceback in error responses

---

## Phase 7: AI Code Synthesis Enhancements

**Goal:** Bring Copilot-style AI features into the Python workflow (Section 6 of the spec).

**What to build:**

- **Context-aware code generation:** When the LLM generates `=PYTHON()` formulas via chat, inject Calc context (nearby data ranges, column headers) into the prompt so generated code references the right cells
- **Natural language → `=PYTHON()` bridge:** Enhance `=PROMPT()` with a mode that returns executable `=PYTHON()` code (already partially possible; formalize the prompt template)
- **Error auto-fix:** When `run_venv_python_script` returns an error, automatically retry with the error context (the chat tool loop already supports this pattern)
- **Agentic compute workflow:** The two-phase LLM workflow (compute in venv -> insert with tools) is already the architecture. Enhance the python specialized domain prompt to encourage multi-step analysis (clean data -> compute stats -> insert chart)

**Key files to modify:**
- [plugin/calc/venv_python.py](plugin/calc/venv_python.py) -- enhanced system prompt for the python domain
- [plugin/calc/prompt_function.py](plugin/calc/prompt_function.py) -- `=PROMPT()` Python mode
- Existing chat tool loop handles retry naturally

---

## Recommended Build Order

```mermaid
graph TD
    P1["Phase 1: Session Persistence"] --> P4["Phase 4: Init Scripts"]
    P1 --> P5["Phase 5: Object Cards"]
    P2["Phase 2: Matplotlib Pipeline"] --> P5
    P3["Phase 3: Monaco Editor for Calc"] --> P6["Phase 6: Diagnostics Pane"]
    P1 --> P6
    P5 --> P7["Phase 7: AI Enhancements"]
    P6 --> P7
```

- **Phase 1** and **Phase 2** are independent and can be developed in parallel
- **Phase 3** is independent of 1 and 2 (editor infrastructure already exists)
- **Phase 4** benefits from Phase 1 (init runs in session namespace)
- **Phase 5** requires Phase 1 (object references need persistent state)
- **Phase 6** and **Phase 7** are polish/UX layers that build on everything above

## What to Skip (Not Applicable to LibreOffice)

- **Cloud container architecture** (Section 3) -- WriterAgent's local venv is already the "competitive advantage" the spec describes in Section 9.1
- **Compute tiers / monetization** (Section 3.2) -- not relevant for an open-source extension
- **Custom `xl()` function** -- the `data` variable injection already serves this purpose; adding an `xl()` proxy in the venv is possible but lower priority (deferred in [enabling_numpy doc Section 7](docs/enabling_numpy_in_libreoffice.md))
- **True DAG recalculation** (Section 9.2) -- this is a LibreOffice core change, not an extension-level feature
