# Calc Analysis Sub-Agent — Development Plan (MVP: Calc-focused)

**Goal (for now):** A **Calc-only** specialized sub-agent (domain `"analysis"` or `"data"`) that can **discover relevant numeric/semi-structured data** primarily in Calc documents (active sheet or siblings in the folder), **prepare it** (ranges, tables, pivot data, named ranges, etc.) for scientific Python, **execute reliable analysis** using the full numpy/pandas/scipy/sklearn/etc. stack in the user's venv (via trusted code, avoiding sandbox limitations), and return compact, actionable results that the main agent can synthesize, explain, or apply back — primarily into the Calc document (new tables, charts, fitted values, cleaned data, etc.).

**Dual access model (clarified):** We provide **both**:
- High-level, reliable "standard functions" and helpers (e.g. `describe_data`, `clean_and_prepare`, `run_regression`, `cluster_numeric`, KPI summaries, formatting utilities, reusable analysis classes) that the LLM/sub-agent can call by name or spec. These are implemented in trusted modules (following the `embeddings_index.py` pattern) + exposed via the analysis domain tools. This mirrors the curated helpers that Microsoft puts into Python-in-Excel initialization scripts and object classes.
- Full raw power via the existing `python` domain (`=PYTHON()` / `run_venv_python_script` with the full scientific stack in the venv). The LLM can still write arbitrary pandas/numpy/sklearn code when needed for novel tasks.

Writer tables are deprioritized for MVP: they are messy to extract reliably (merged cells, complex formatting, tracked changes, varying structures), and users rarely keep primary analytic data in Writer tables. Data lives in Calc. (We will support Writer tables later as a first-class data source.)

This is the focused evolution of the high-level analysis ideas previously sketched in [enabling_numpy_in_libreoffice.md](enabling_numpy_in_libreoffice.md) (now condensed with a pointer here) and builds directly on existing patterns. Later phases may expand Writer "cleanup" / presentation support. We aim to copy useful *features* from Microsoft Python-in-Excel (see comparison below) while keeping WriterAgent's superior local/offline/explicit architecture.

**Cross-document note (MVP):** Analysis execution always runs in a **Calc context**. A Writer-side main agent can be made aware of an open Calc document's analysis capabilities (via prompt descriptions or delegation) and can surface the Calc file (via `document_research`), but the actual data extraction + numpy work happens against the Calc model. Users can copy/paste compact results into Writer, or in supported cases the system can perform "cleanup" (nicely formatting and inserting results into the active Writer doc). See the dedicated Cross-Document Workflows section below.

**Related / prior art to draw from:**
- [multi-document-dev-plan.md](multi-document-dev-plan.md): two-tier (outer discovery + inner per-file) delegation for `document_research`.
- [embeddings.md](embeddings.md): primary per-directory semantic index for cross-sibling discovery (outer router only; locators → precise inner reads). No other search indexes.
- [writer-specialized-toolsets.md](writer-specialized-toolsets.md), [calc-specialized-toolsets.md](calc-specialized-toolsets.md), [draw-impress-specialized-toolsets.md](draw-impress-specialized-toolsets.md): nested delegation via `delegate_to_specialized_*_toolset(domain=...)`, tier filtering, ephemeral sub-agents.
- [calc-analysis-tools.md](calc-analysis-tools.md): existing narrow `"analysis"` / `"solvers"` domain (Goal Seek + Solver). This plan generalizes it to data-driven numpy work.
- [enabling_numpy_in_libreoffice.md](enabling_numpy_in_libreoffice.md): trusted extension code pattern (e.g. `embeddings_index.py`), payload_codec / split-grid for efficient numeric handoff, full venv stack without AST sandbox for shipped modules.
- [smol-main-chat-tool-architecture.md](smol-main-chat-tool-architecture.md): sub-agents (librarian, specialized) often run via smol/ ReAct for focused tasks.
- Chat / tool loop for main agent coordination.
- [python-in-excel-dev-plan.md](python-in-excel-dev-plan.md) and [python-in-excel-ideas.md](python-in-excel-ideas.md): Detailed mapping of Microsoft Python-in-Excel features (we copy useful *features* such as curated init-script helpers, rich object previews, strong data handoff for tables/named ranges/headers, AI-assisted workflows, etc., while keeping our local venv + explicit `data`/`result` architecture).

---

## Current Code State (grounded in actual implementation)

**Existing narrow analysis (as of inspection of `plugin/calc/`):**
- `plugin/calc/analysis.py`: `GoalSeekTool` (`calc_goal_seek`) and `SolverTool` (`calc_solver`). Direct UNO (`doc.seekGoal`, `XSolver` / solver services). Careful handling for headless (prefers CoinMP/Lpsolve over Java NLPSolver engines that require a frame). Lives under `specialized_domain = "solvers"` (see `plugin/calc/base.py` `ToolCalcSolverBase`).
- `plugin/calc/analyzer.py`: `SheetAnalyzer.get_sheet_summary()` — structural (used range, row/col counts, headers, chart_count, annotations, merges, shapes). Not numeric analysis.
- `plugin/calc/calc_addin_data.py` + `inspector.py`: Solid data extraction and shaping for Python (`read_range` → values, `pack_calc_data_for_wire`, NaN handling, size limits).
- Python execution (the current "escape hatch" for real numpy work):
  - `plugin/calc/venv_python.py`: `RunVenvPythonScript` (`run_venv_python_script`, domain="python", `ToolCalcPythonBase`). Supports `code`, `data_range` (injected as `data`), or direct `data`. Uses `run_code_in_user_venv` + payload codec. Works cross-app via `specialized_cross_cutting`.
  - Full scientific stack is already available in the user venv (as per `sandbox_imports.py`, `enabling_numpy_in_libreoffice.md`).
- Delegation & sub-agents:
  - `plugin/calc/specialized.py`: `DelegateToSpecializedCalc` (gateway, `delegate_to_specialized_calc_toolset`).
  - `plugin/calc/base.py`: Many `ToolCalc*Base` with `specialized_domain` (solvers, python, pivot_tables, charts, ranges, search, sheets, conditional_formatting, etc.).
  - `plugin/doc/specialized_base.py` (shared): When `USE_SUB_AGENT` (default True), spins smol `ToolCallingAgent` with `SmolToolAdapter`s from `registry.get_tools(..., active_domain=domain)`. Uses `build_toolcalling_agent` + `SmolAgentExecutor`. Supports `specialized_workflow_finished`.
  - In-process fallback (no sub-agent) just switches `active_domain` via callback.
- No broad "data_analysis", "numeric_analysis", or expanded "analysis" domain yet that combines data discovery + trusted heavy compute. The "python" domain + raw code is the current way to do pandas/numpy work. Sheet summary and solvers are the only dedicated analysis surface.

**Gaps vs. desired:**
- No high-level "find the relevant data in this (or linked) Calc file and analyze it" tool surface.
- No trusted module for safe, high-level analysis helpers (e.g. `clean_and_describe`, `run_regression`, `cluster_numeric`) that the LLM calls by spec instead of writing code.
- No first-class integration between document_research (for surfacing Calc siblings from Writer) and Calc analysis.
- "Cleanup" (turning analysis results into nice Writer content) is ad-hoc today.

This dev plan turns the conceptual analysis sub-agent into concrete Calc work, reusing the above infrastructure heavily.

---

## Dev Plan: Calc Data / Numeric Analysis Sub-Agent (and Writer "Cleanup" Awareness)

---

## Problem (Calc-focused for MVP)

Users and the main agent frequently want to do real analysis on data living *inside their Calc documents* (active workbook or siblings):

- "Clean the sales data in Sheet1, compute YoY growth by region, flag outliers, and suggest a pivot chart."
- "Run a quick Monte Carlo on the budget assumptions in this Calc file."
- "Cluster the numeric performance metrics across these three project Calc files and summarize the groups."
- "Fit a simple regression to the experimental data range and write the predicted values + R² back as a new column."

Writer tables are out of scope for core analysis in this MVP: they are messy to parse reliably (merged cells, varying structures, tracked deletions, rich formatting), extraction is error-prone, and users simply don't keep primary tabular/numeric data in Writer tables in practice. Data lives in Calc.

Today this is awkward even in Calc:
- Raw `run_venv_python_script` or `=PYTHON()` requires the LLM to write correct pandas code every time (error-prone inside the AST sandbox, and the sandbox intentionally limits some capabilities).
- Data discovery across open siblings is limited (or requires the full `document_research` outer).
- Extraction of ranges/sheets/pivots into analysis-friendly form is repetitive boilerplate.
- Results need to flow back cleanly for synthesis or document application (in Calc, or presented back to a Writer user).

The expensive part for "many documents" is discovery (solved by embeddings as the *primary per-directory semantic index* — see [embeddings.md](embeddings.md)); once the right small set of Calc sources is identified, precise extraction + trusted heavy compute + LLM interpretation wins.

Calc documents are an excellent fit (ranges, sheets, named ranges, pivot caches, and chart data sources are first-class and relatively clean to extract into numpy/pandas). This is in contrast to pure code search, where literal symbols + grep + LSP are often sufficient (see domain discussion in [embeddings.md](embeddings.md#why-embeddings-semantic-search-vs-pure-lexicalgrep-and-why-the-difference-is-bigger-for-office-documents-than-code)).

---

## Design principles (reuse existing, Calc-focused for MVP)

- **Dual access model (high-level helpers + full raw power)**: 
  - High-level "standard functions" and reusable helpers/classes (e.g. `describe_data`, `clean_and_prepare`, `run_regression`, `cluster_numeric`, `kpi_summary`, formatting utilities, analytical classes) that the LLM or sub-agent can reliably invoke by name or high-level spec. These are implemented via trusted modules (unsandboxed, full stack) and exposed through the analysis domain. This directly copies the spirit of Microsoft Python-in-Excel's initialization scripts (global helpers + OOP analysis classes like `QuickStats`) and object-oriented extensibility.
  - Full raw access remains available via the `python` domain (`=PYTHON` / `run_venv_python_script`) for anything novel or not covered by the standard helpers. The LLM can still write arbitrary code when needed.
- **Two-tier (or multi-step) like document_research, but Calc-centric**: Outer layer (when cross-doc) focuses on *finding* relevant Calc data sources (sheets, ranges, named ranges, pivot tables, chart data — using `list_nearby_files`, the primary per-directory embeddings index, `get_sheet_summary`, range heuristics, etc.). Inner / execution layer does preparation + numpy work on specific extracted Calc data. Main chat (whether Writer or Calc) stays stable.
- **Specialized delegation primarily on Calc**: Expose via `delegate_to_specialized_calc_toolset(domain="analysis")`. The gateway can be called from a Writer main agent when a Calc document is open (via document awareness or explicit delegation). Specialized analysis tools are not on the main wire schema. Builds directly on the existing narrow `"analysis"` / `"solvers"` domain in [calc-analysis-tools.md](calc-analysis-tools.md) (Goal Seek + Solver).
- **Trusted execution for the heavy lifting (always in Calc context)**: Never ask the LLM to write the pandas/sklearn code for real work when a standard helper exists. Use shipped trusted modules (e.g. `plugin/calc/analysis.py` or shared under `plugin/scripting/`) called from the host with fixed stubs — full venv access (numpy stack, optional DBs/caches), efficient data handoff via existing `payload_codec` / split-grid. This is the pattern proven by `embeddings_index.py`. Analysis execution is always against a Calc model/context. Raw code via the python domain is the escape hatch.
- **Data discovery reuses the primary index + Calc tools**: The per-directory embeddings index (see [embeddings.md](embeddings.md)) is the *sole* semantic cross-sibling router. Within a Calc file, use existing precise tools (`read_cell_range`, sheet summaries, named ranges, etc.). No new parallel indexes. Writer data sources are out of scope for core analysis in MVP.
- **Host extracts (from Calc), venv computes, results are compact**: Host (UNO) pulls Calc ranges/sheets into arrays/records (reuse and extend `calc_addin_data` patterns). Passes via IPC. Trusted code returns summaries, transformed data, metrics, suggested writes, chart inputs, etc. The *calling* main agent (Writer or Calc) decides what to apply.
- **LLM role is planning + synthesis**: The sub-agent (main-style FSM or smol ReAct, running in the appropriate Calc-specialized context) decides *what data* to fetch and *what analysis* (high-level spec or call to a standard helper). It interprets results. It does *not* write low-level analysis code when helpers are available.
- **Graceful / optional + Calc python surface**: Works with or without a rich venv. Builds on existing Calc strengths (`=PYTHON()`, `run_venv_python_script`, the python specialized domain) as fallback/escape hatch. Existing Calc analysis tools (Goal Seek/Solver) are natural sub-steps.
- **Ephemeral sub-agents**: Like other specialized domains and document_research inners — focused context, compact result folded back to main history.
- **Cross-doc awareness without full symmetry**: A Writer main agent can know about (and delegate toward) Calc analysis capabilities when a Calc doc is open, but the heavy lifting and data model stay Calc. See Cross-Document Workflows section.

---

## Architecture overview (Calc context)

```
Main agent (active doc — can be Writer or Calc)
  |
  +-- (optional) document_research delegation to surface open Calc sibling
  |
  +-- delegate_to_specialized_calc_toolset(domain="analysis", task="Clean sales data, compute YoY by region, flag outliers")
        |
        v
Analysis sub-agent (specialized, ephemeral — runs with Calc ToolContext / model)
  |
  +-- Discovery / data finding (Calc-focused; outer layer when cross-doc)
  |     - list_nearby_files / embeddings search (primary per-dir index) for relevant Calc siblings
  |     - get_sheet_summary, read_cell_range, named ranges, pivot inspection, chart data sources on the target Calc model
  |     - "Which sheets/ranges contain the sales data?"
  |
  +-- Extraction (host, Calc UNO)
  |     - Pull ranges/sheets/pivots into 2D arrays or records (reuse/extend calc_addin_data patterns)
  |     - Shape for pandas/numpy (headers, types, missing values → NaN)
  |
  +-- Execution handoff (trusted, always Calc context)
  |     - Call fixed stub into plugin/calc/analysis.py (or shared scripting/analysis)
  |     - data= via split-grid / payload_codec (or path ref)
  |     - Full stack in venv: pandas cleaning/groupby/agg, scipy.stats, sklearn (cluster, regress, etc.),
  |       Monte Carlo (numpy.random), fitting, optimization, etc.
  |     - Can read/write small per-folder analysis caches (same discipline as embeddings `index.db`)
  |
  +-- Results (compact)
        - key metrics, cleaned/transformed tables (as records or formula-ready), cluster labels + exemplars,
          fitted params + predictions, suggested new ranges/charts, "key findings" narrative-ready summary
        - Return to calling main agent (via final_answer / tool result)
        |
        v
Main agent: synthesize + apply (in Calc via write_formula_range etc.; or "cleanup" presentation into active Writer doc in supported cases)
```

The sub-agent runs in a **Calc context** (ToolContext points at a Calc model). Discovery can be triggered from a Writer main agent (after using `document_research` to identify the Calc file), but execution and data model stay Calc.

For pure active-Calc cases it can be lighter (direct specialized delegation, minimal outer discovery).

See the Cross-Document Workflows section for how a Writer LLM becomes aware of the Calc analysis capability.

---

## Data finding ("somehow find the relevant data") — Calc focused

- **Cross-folder (Calc siblings)**: Primary tool is the embeddings index (see [embeddings.md](embeddings.md) — per-directory only, outer router for `document_research`). A semantic query ("sales data", "budget assumptions") surfaces candidate Calc `doc_url` + sheet/paragraph hints. Then use Calc-specific tools on the opened (hidden/read-only) Calc model. (We copy the *feature* of rich cross-workbook data awareness from Python-in-Excel, but implement it via our per-directory embeddings + document_research rather than their cloud `xl()` mechanism.)
- **Within a Calc file**: `get_sheet_summary`, `read_cell_range`, named range inspection, pivot table / cache access, chart data source enumeration, range heuristics (numeric columns + header detection). These are the natural, reliable sources. (Future: copy more of Excel's structured table / named range / `headers=True` ergonomics via `calc_addin_data.py` enhancements.)
- **User hints + LLM planning**: The task from main (or from a Writer caller) often contains clues ("the sales table in the budget file"). The analysis sub-agent LLM uses those + the tools above to locate the right data.
- **Metadata / structure**: Leverage Calc's own structures (named ranges, database ranges, pivot fields, chart series) far more than Writer-style paragraphs or tables.
- **No duplication**: Once the right small set of Calc sources is identified, use live reads on the model (not the embeddings cache). The index is purely for *routing* to the right Calc file/sheet.

Writer-side data (tables, text with numbers) is out of scope for core discovery + extraction in this MVP. If a user has important numbers in Writer, they are expected to copy them to Calc or the system can surface them at a high level for manual handoff.

**Cross-folder discovery can be initiated from a Writer main agent** (via `document_research` to list/open the Calc sibling), after which control can flow to the Calc-specialized analysis sub-agent for the actual work.

Extraction helpers (new or extended from existing range/table code) turn raw UNO structures into analysis-ready form on the host side before the IPC hop. We will evolve these to better support Excel-like features (structured tables, headers, named ranges) for data handoff into both the high-level helpers and raw Python.

---

## Cross-Document Workflows (Writer + Calc open at the same time)

A very common real-world pattern the user described: a **Writer document** is the "main" deliverable (report, proposal, story, policy doc), while the **Calc document** holds the actual data and is open in another window (or tab).

### How the Writer LLM learns about Calc analysis

- The main Writer agent (chat) can be given awareness of an open Calc document via:
  - The existing `document_research` / multi-document machinery (it can list nearby/open Calc files and return high-level descriptions or summaries).
  - Prompt engineering / system context that describes "when a Calc workbook is open, you can delegate numeric analysis, cleaning, modeling, simulations, etc. to it via the analysis specialized toolset."
  - Explicit user @-mentions or UI that surfaces open Calc docs as analysis targets.
- The Writer LLM does **not** need full Calc tools on its wire schema. It issues a high-level delegation (or a `document_research` task that leads to analysis), and receives compact results back.

### Analysis always runs in the Calc context

- Data extraction, range/sheet/pivot access, and the trusted numpy execution all happen with a Calc `ToolContext` / model.
- The analysis sub-agent (and its trusted modules) see a proper Calc document model. This is clean, leverages all the existing Calc range tools, `=PYTHON()` / `run_venv_python_script` surface, chart integration, etc.
- No need to force messy Writer table extraction for the compute phase.

### Results and "cleanup"

- **Primary path (MVP)**: Results are applied directly in the Calc document (new sheets/columns, charts, cleaned data, named ranges with results). The Writer user then manually copies what they need (tables, key numbers, a chart image) into the Writer doc. This is simple and matches how people already work.
- **Compact results for Writer**: The analysis sub-agent can return LLM-friendly summaries, markdown tables, key metrics, or "suggested Writer content" blocks. The Writer main agent can then use `apply_document_content` (or insert as table/text) to place them.
- **"It can do the cleanup" (supported cases)**: In some flows the system can perform a post-processing "cleanup" step:
  - Take the raw analysis output (DataFrame, metrics, predictions).
  - Format it nicely (as a Writer table, bulleted findings, a formatted section with headings).
  - Insert it at a user-specified or heuristically chosen location in the active Writer document (using `apply_document_content`, rich text helpers, or table creation tools).
  - Optionally create a linked chart in Writer or embed a Calc chart object.
- This "cleanup" can be triggered explicitly by the user ("run the analysis on the budget and clean it up nicely in my report") or offered by the Writer LLM after receiving results from the Calc analysis delegate.
- Cleanup is **presentation / insertion only** — the real numeric work still happened in Calc. This avoids trying to do heavy pandas inside a Writer context.

### Prompt / delegation example (Writer side)

User (in Writer report): "Pull the latest sales data from the Budget.ods file, clean it, compute growth rates, and put a nice summary table plus key findings into this document."

Writer main agent:
1. Uses `document_research` (or direct awareness) to confirm Budget.ods is open / nearby.
2. Delegates something like: `delegate_to_specialized_calc_toolset(domain="analysis", task="From the sales data in Budget.ods: clean the data, compute YoY growth by region, flag outliers, return compact results + suggested Writer-friendly summary table and bullet findings")`.
3. Receives compact payload.
4. Either:
   - Applies directly to the Writer doc (cleanup path), or
   - Presents the results and lets the user say "yes, insert the table here" (or does a lightweight insert).

The Calc analysis sub-agent never mutates the Writer doc; it only returns data. The Writer agent (or an explicit cleanup helper) does the final presentation work on the Writer side.

This keeps concerns separated, respects that Calc is the natural home for the data and the compute, and still gives the user a seamless "tell the Writer LLM about my Calc data" experience.

Future phases could make the cleanup step more automatic and richer (e.g., automatically creating a linked Calc range inside Writer, or using rich HTML insertion for nicely styled result tables).

---

## Execution and trusted numpy handoff

Follow the exact pattern from embeddings:

- Host calls `run_code_in_user_venv` with a tiny fixed stub:
  ```python
  from plugin.scripting.analysis import run_analysis
  result = run_analysis(spec, data, context_hints)
  ```
- The imported module runs unsandboxed (full `numpy`, `pandas`, `scipy`, `sklearn`, etc.).
- Bulk data via `data=` (split-grid for efficiency on grids/tables).
- Optional: references to per-folder caches (SQLite side tables, Parquet, etc.) for repeated or large analyses.
- Returns only compact serializable results (no huge matrices back unless asked).
- Long-running support (existing timeout / flag mechanisms).

The LLM in the sub-agent passes a *high-level spec* or structured request ("clean this DF, compute YoY by region, flag >2σ outliers, return summary + cleaned data + chart inputs"), not raw code. The trusted module implements the reliable, tested logic (or dispatches to well-known patterns).

This is far more robust than hoping the model emits correct pandas inside the sandbox on every turn.

Existing Calc analysis (Goal Seek/Solver in `calc-analysis-tools.md`) can be exposed under the same domain or called as sub-steps.

---

## Integration with main chat and other agents

- Main registers the gateway + describes the domain (like other specialized toolsets).
- Sub-agent prompt (in constants, per specialized) explains: "You are the analysis specialist. Use data discovery tools + embeddings search to locate relevant tables/ranges/sections. Extract and hand off to trusted analysis execution. Return compact results + interpretation guidance. Only the main agent writes to the active document."
- Results fold back as a single tool response (keeps main history stable).
- Can be composed: document_research outer → analysis sub-agent on the discovered files.
- For pure Calc users: strong integration with sheets/ranges + the existing python tool as fallback.
- Librarian / memory features can feed thematic clusters into analysis.

---

## Implementation notes and phasing (Code-Grounded)

**Trusted module + standard helpers (Phase 0 — implemented)** — [`plugin/scripting/analysis.py`](../plugin/scripting/analysis.py) + [`plugin/scripting/analysis_coerce.py`](../plugin/scripting/analysis_coerce.py). Host RPC: [`plugin/framework/client/analysis_client.py`](../plugin/framework/client/analysis_client.py) (`run_trusted_analysis` re-export). Follows the [`embeddings_index.py`](../plugin/scripting/embeddings_index.py) pattern: fixed venv stub, unsandboxed numpy/pandas/sklearn/statsmodels stack.

**Borrowed from (ideas, not vendored deps):**
- Microsoft Python-in-Excel init-script helpers (`kpi_summary`, `format_currency`) — [python-in-excel-ideas.md](python-in-excel-ideas.md)
- `QuickStats` card layout — adapted from [community gist](https://gist.github.com/summerofgeorge/646140d175ada739efd2d57b5cea9a5e)
- JSON-serializable EDA stats — inspired by [DataPrep compute_* pattern](https://docs.dataprep.ai/user_guide/eda/introduction.html) (no `dataprep` dependency)

**Default Calc init-script snippet** (paste manually; not auto-injected):

```python
from plugin.scripting.analysis import (
    QuickStats, describe_data, kpi_summary, format_currency, run_analysis,
)
```

**Host entry:** `run_trusted_analysis(ctx, spec, data, context=...)` or venv stub:

```python
from plugin.scripting.analysis import run_analysis
result = run_analysis(spec, data, context)
```

**`spec` schema:**

| Field | Type | Notes |
|-------|------|-------|
| `helper` | string | Required — see helper list below |
| `params` | object | Helper-specific kwargs |
| `headers` | bool | Default `true` — first row is column names |
| `header_row` | int | Default `0` |
| `return_data` | bool | Default `false` — when true, some helpers add `data_records` |

**Implemented helpers:**

| Helper | Purpose |
|--------|---------|
| `describe_data` | Extended EDA + column quality + optional IQR outlier counts |
| `kpi_summary` | Aggregate mean/min/max/sum for selected metrics |
| `detect_outliers` | IQR (default), z-score, or `isolation_forest` |
| `quick_stats` | `QuickStats(...).tooltip()` compact metric card |
| `format_currency` / `format_percent` | Display formatters |
| `clean_and_prepare` | Dedupe, simple imputation |
| `pivot_aggregate` | `pd.pivot_table` wrapper |
| `group_summary` | Group-by aggregates |
| `compare_periods` | YoY/QoQ/MoM via resample + pct_change |
| `correlation_matrix` | Top correlated pairs |
| `run_regression` | statsmodels OLS or sklearn fallback |
| `cluster_numeric` | sklearn KMeans centroids |
| `monte_carlo` | Normal perturbation percentiles |

**Result contract** (compact, LLM-friendly):

```python
{
  "status": "ok",           # or "error"
  "helper": "describe_data",
  "metrics": {...},
  "columns": [...],         # optional column summaries
  "tables": [{"name", "columns", "rows", "truncated", "total_rows"}],
  "flags": [...],
  "writer_cleanup_hints": {"markdown_table", "bullets"},
  "metadata": {...},        # coerce metadata (n_rows, numeric_cols, …)
  "context": {...},         # echoed sheet_name / range_a1 / task_hint when provided
}
```

Table rows capped at 50 (`MAX_TABLE_ROWS`). Errors use `code` + `message` (e.g. `UNKNOWN_HELPER`, `MISSING_PARAM`).

**Still TODO (post Phase 0):** Calc tools (`analyze_data`, …), analysis domain wiring, sub-agent delegation, discovery bridge, Writer cleanup tools, analysis cache.

Previously planned helpers (not yet separate tools):

**Tool / domain surface (reuse existing machinery)**:
- In `plugin/calc/analysis.py` (the existing file): Keep the solvers. Add a few high-level tools (`analyze_data`, `find_and_analyze_relevant_ranges`, `get_analysis_result_for_writer_cleanup`, etc.) that call the trusted helpers or fall back to the python domain. Register them under a `ToolCalcAnalysisBase` (or expand the "solvers" base + rename the domain to "analysis" for discoverability; see `base.py`).
- `plugin/calc/base.py`: Add the base class + domain definition (reuse the "intent = 'analyze'" pattern).
- The delegation gateway (`specialized.py`) and smol sub-agent path (`specialized_base.py`) will automatically pick up the new tools when `domain="analysis"`.
- For Calc callers: full power (high-level helpers preferred; raw python domain as escape hatch).
- For Writer callers: the gateway is already there; just make sure the domain is listed in prompts and the delegate description. The Writer LLM gets the "standard functions" surface plus the ability to request raw Python when needed.

**Tool surface** (specialized tier):
- Discovery helpers (or reuse via document_research).
- `extract_data(source, format="pandas"|"numpy"|"records")`
- `run_analysis(spec, data_ref, ...)` → results (the gateway to trusted).
- `suggest_visualization(results)` or direct chart data prep.
- `apply_analysis_results` (suggestions only; actual writes via main's tools after review).

**Phasing (MVP-first, heavily reusing existing code)**

1. **Phase 0 (quick win, mostly glue + standard helpers layer)**: 
   - Add the trusted analysis helpers module (the "standard functions" layer, modeled on Excel init-script helpers + OOP classes).
   - Wire 3-5 high-level tools in `analysis.py` (e.g. `analyze_data`, `describe_range`) that call the trusted helpers (preferred) or fall back to the python domain.
   - Update `calc-analysis-tools.md` (expand it to cover the new data/numeric tools alongside the existing Goal Seek/Solver) and constants with the new tools/descriptions + hints that high-level helpers are preferred for reliability.
   - Expose under the existing "solvers" domain or a new/renamed "analysis" enum value. Update delegation prompts.
   - Also enhance init-script support (already shipped) to ship good default helpers (copying the Excel feature of curated global utilities).
   - Result: In a Calc context (or delegated from Writer), the agent prefers reliable standard helpers ("use `describe_data` + `run_regression` on this range") but can still drop to raw Python for anything else.

2. **Phase 1 (sub-agent surface + discovery)**:
   - Make "analysis" (or keep/expand "solvers" → "analysis") a proper delegable domain with its own focused toolset.
   - Add discovery-oriented tools inside it (compose with existing `get_sheet_summary`, range tools, and document_research when needed). Support the Excel-like feature of rich data awareness (we implement via embeddings + our explicit data handoff rather than `xl()` string parsing).
   - Full sub-agent support (smol with limited toolset for the analysis task).
   - Define a standard compact result schema (metrics + data_tables + suggested_writes + writer_cleanup_hints) — this helps both Calc application and Writer "cleanup".

3. **Phase 2 (cross-doc + cleanup + Excel feature parity items)**:
   - Update Writer prompts / specialized descriptions so a Writer main agent knows it can delegate numeric work to any open Calc context (via document_research discovery + the Calc analysis domain). Copy the *feature* of conversational AI that understands data across workbooks.
   - Add thin Writer-side "cleanup" helpers (or just document how to use existing `apply_document_content` + rich text on the compact results). This is our version of turning analysis into nice presentation (Excel has spill + object cards; we have explicit cleanup + future rich table egress).
   - Result contract as above.
   - Start adopting more Excel data handoff ergonomics (named ranges, structured table awareness, headers) in `calc_addin_data.py` so both raw Python and the high-level helpers feel more like `xl(..., headers=True)`.
   - Update `docs/analysis-sub-agent.md` (this file) and cross-reference from writer-specialized-toolsets.md and the python-in-excel docs.

4. **Phase 3+ (deeper parity + advanced)**:
   - Caches, dedicated worker pool for long jobs, vision digitization of charts found in Writer (feed numbers into Calc analysis — copying the image + data analysis workflow).
   - Richer object previews / cards for analysis results (copying Excel Python Object mode).
   - More advanced trusted helpers.
   - Eventual proper Writer table support (still route heavy compute through the trusted/standard-helpers path; we copy the *feature* of treating document tables as first-class data sources).
   - Diagnostics enhancements, better AI code synthesis that prefers the standard helpers.

**Prompt / LLM guidance updates (ongoing)**:
- Calc side: Strong encouragement to use the analysis domain / standard helpers for common numeric tasks ("prefer `run_regression` over writing pandas yourself"); raw python domain remains the powerful escape hatch.
- Writer side: Clear "delegate heavy analysis (via the standard helpers or raw Python) to any open Calc context you discover using document_research; you'll receive compact results you can narrate or clean up into this document." This copies the agentic multi-workbook data analysis experience from Python-in-Excel.

**Testing & validation**:
- Unit tests for the new trusted helpers (pure data in/out).
- Extend existing Calc specialized + document_research integration tests.
- Manual cross-doc scenarios (Writer proposal + open budget.ods); verify delegation + cleanup. Verify that high-level helpers are used preferentially by the agent in prompts.

This plan reuses almost everything that already exists (delegation, smol sub-agents, python venv execution + data injection, calc_addin_data shaping, SheetAnalyzer, document_research cross-doc, trusted module pattern, init scripts). The new surface area is mainly the trusted analysis helper module (the "standard functions" layer) + a few high-level spec-driven tools, plus the "analysis" domain wiring, Writer cleanup awareness, and targeted adoption of useful Excel Python features (curated helpers, better data ergonomics, rich previews, cross-workbook agentic analysis) while preserving our architectural advantages (local/offline, explicit `data` for native DAG, no cloud lock-in, deterministic `result` assignment).

We will copy *features* from Microsoft Python-in-Excel (initialization-script helpers and classes, rich object/data handling, strong support for tables/named ranges/headers, agentic data workflows across files, etc.) but implement them on top of WriterAgent's local venv + explicit signature model rather than copying their `=PY` + `xl()` string parsing or row-major co-volatility.

---

## Open questions / future

- Exact domain name (`"analysis"` vs `"data"` vs extending the existing Calc analysis domain from calc-analysis-tools.md).
- How much "planning" lives in the sub-agent LLM vs. a thin trusted orchestrator (or even a small set of high-level trusted entry points).
- Caching policy for analysis results (similar to embeddings: mtime + hash driven, ~60s debounce). Per-folder or per-Calc-doc?
- Support for very large data (sampling, chunked processing, out-of-core via the venv).
- User control / visibility (e.g. "show me the steps the analysis sub-agent took" or "export the actual pandas code that was executed").
- Cleanup fidelity: how smart should the Writer-side "nice formatting + insert" step be? (tables, headings, cross-references back to the source Calc ranges, etc.)
- Composition with web_research, librarian, or other specialists (e.g. "research the assumptions and then run sensitivity analysis on them in the attached budget").
- Future expansion beyond Calc: if users start keeping serious data in Writer tables, how (if ever) to bring them into the analysis flow without making extraction too fragile.

This keeps the implementation small by maximal reuse of delegation, embeddings for discovery, trusted execution (Calc context), data handoff patterns, and the existing document_research cross-doc machinery.

See also the condensed discussion that used to live in [enabling_numpy_in_libreoffice.md](enabling_numpy_in_libreoffice.md) (now a pointer here). The cross-doc "tell the Writer LLM about the Calc analysis" + "cleanup" ideas are the main refinements for this iteration.