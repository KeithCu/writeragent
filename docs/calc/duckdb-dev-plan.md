---
name: DuckDB Calc Integration
overview: Add DuckDB as a horizontal analytics layer in the user venv — SQL over folder files (CSV, Parquet, XLSX) and over live Calc ranges materialized via the existing split_grid → DataFrame bridge. Phased delivery; no venv↔LO tool RPC required for MVP.
todos:
  - id: phase-a-spec
    content: "Phase A: Spec + sandbox whitelist + Settings probe group + path policy for scoped_dir"
    status: completed
  - id: phase-a-venv
    content: "Phase A: Trusted venv module plugin/scripting/venv/duckdb_sql.py (folder read-only SQL)"
    status: completed
  - id: phase-a-host
    content: "Phase A: Host tool + Run Python Script [SQL] folder templates; scoped_dir; sibling XLSX/ODS via LO import + read_range (not DuckDB read_xlsx)"
    status: completed
  - id: phase-a-plus-ods-cache
    content: "Phase A+: writeragent_ods_cache/ beside folder; mtime invalidation for xlsx/xls → cached ods; open cache on hit"
    status: completed
  - id: phase-a-tests
    content: "Phase A: pytest for path guard + folder SQL round-trip (temp dir fixtures)"
    status: completed
  - id: phase-b-sheet
    content: "Phase B: Single Calc range → coerce_to_dataframe → duckdb.register → SQL → result egress"
    status: completed
  - id: phase-c-multi
    content: "Phase C: Multi-table catalog (named ranges + optional folder files in one SQL request)"
    status: completed
  - id: phase-d-cache
    content: "Phase D: Shared-kernel DuckDB session / table cache across =PY() cells"
    status: completed
isProject: false
---

# DuckDB for Calc & Folder Analytics — PM / Senior Dev Plan

Back to [Enabling NumPy & Python in LibreOffice](../enabling_numpy_in_libreoffice.md).

**Status:** Phase A + A+ (including ODS mtime cache) + B + C + D landed (shared-kernel DuckDB session cache). Honesty polish: 200-row result cap is visible; COPY/escape fail loud. See execution plan and implementation notes below.

### Pretty demo (SQL / DuckDB sheet)

The same ODS as the `=PY()` showcase — [`tests/fixtures/python_showcase_demo.ods`](../../tests/fixtures/python_showcase_demo.ods) — includes a **SQL_DuckDB** sheet. It reuses the Sales and Marketing ranges, keeps SQL **only in cells** (one line per row), and joins sheet sales to sibling [`zip_income.csv`](../../tests/fixtures/zip_income.csv) (ACS 2024 5-year B19013 / S1903-equivalent ZCTA median household income).

**RESULTS contract (sheet-only scenarios):** the live cell is a **short** `=PY()` / add-in formula — not a novel of nested quotes. SQL is an explicit formula argument:

```text
=PY("sql=chr(10).join(str(c) for r in data[1] for c in r if c); import duckdb; …",
    Sales_Analytics!A4:J39, A11:A16)
```

- `data[0]` is the sheet range (registered as `sales` / `marketing`).
- `data[1]` is the SQL cell or multi-row block (joined with newlines).
- Editing the SQL cells dirties RESULTS through Calc's normal DAG.
- `data[` in the payload is required so the trailing SQL cell is not peeled as a matrix index.
- Scenario 3 (ZIP ⨝ `zip_income.csv`) stays a `query_folder_sql` pointer — the sibling CSV is not a Calc argument.

```bash
python scripts/generate_pretty_demo_spreadsheet.py --format all
```

That writes the ODS/XLSX and copies `zip_income.csv` next to them. Happy-path proof is `query_folder_sql` in [`tests/scripting/test_duckdb_sql.py`](../../tests/scripting/test_duckdb_sql.py) (sheet GROUP BY + sibling ZIP join), not the pretty file alone. See [`tests/fixtures/zip_income.README.md`](../../tests/fixtures/zip_income.README.md).

### Current Implementation (as of latest increment)
- Core: `query_folder_sql` in venv (supports `sql`, legacy `files` list, `preloaded` grids, `flat_files` for named direct reads).
- Tool: `query_folder_sql` (analysis domain) accepts `sql`, `files` (list or `{name: spec}`), `tables` (stable sheet / named-range / frozen A1 identity), `data_range`, `headers`.
- Host handles: scoped dir resolution, hidden LO opens for .xlsx/.ods, active doc reads for ranges, size limits, preloading.
- Worker: registers preloaded via `coerce_to_dataframe`; flat files via suffix binders (`read_csv` / `read_parquet` / `read_json`, including `.jsonl` / `.ndjson`) under provided names. Unknown suffixes and missing files fail loud (`UNSUPPORTED_FILE_TYPE` / `MISSING_FILE`) instead of falling through to CSV. Read-only guards (`COPY`/`EXPORT`/`ATTACH`/`INSTALL`/`LOAD` + path/URI escapes). In-memory `CREATE VIEW` / register stay allowed.
- Templates: `[SQL] query_folder_sql` and `query_sheet_sql` in Run Python Script (sheet egress shows truncation flags).
- `=PY()`: prefer `run_sql` / `session_duckdb()` (same firewall + honesty fields) over raw `import duckdb`. Shared-kernel `=PY()` reuses one DuckDB connection per workbook until Reset; Isolated / chat tools stay per-request. See [Phase D](#phase-d--shared-kernel-session-cache).
- Limitations: no write-back, default first sheet for office files when `#SheetName` is omitted. Sibling `.xlsx`/`.ods` now read the sheet **used range** (same `createCursor` / `gotoStartOfUsedArea` / `gotoEndOfUsedArea` path as `SheetAnalyzer` / ingest) — open / missing-sheet / empty-range failures are tool errors. Sibling `.xlsx`/`.xls` reuse `writeragent_ods_cache/` (mtime+size key; `calc.ods_cache_enabled`, default true). Native `.ods` and the live workbook are not cached. Result grids cap at **200 rows** (`MAX_TABLE_ROWS`); `truncated` / `warning` / `flags` / `message` are set when the query returned more.
- Usage: Mix tables + files for joins, e.g. live sheet identity + sibling CSVs. See [Table source identity](#table-source-identity).

**Audience:** Product, senior engineers, and future implementers. This doc captures why DuckDB fits WriterAgent, what users get, and how to build on existing Calc↔venv infrastructure without a new architectural pillar.

---

## Executive summary

**Product goal:** Let Calc and chat users run **SQL locally** against (1) spreadsheet files in the same folder as their document and (2) live Calc ranges — without loading entire workbooks into memory with pandas, without cloud analytics, and without teaching the LLM forty lines of groupby code for every question.

**Technical goal:** Add [DuckDB](https://duckdb.org/) as a **venv-only** dependency (like Docling, sentence-transformers, scipy). DuckDB never talks to UNO. LibreOffice reads sheet data on the host; the existing **split_grid wire** and **`coerce_to_dataframe`** path produce pandas tables; DuckDB registers them and runs SQL; compact results return via the same **`result`** egress as analysis helpers.

**Why now:** Analysis helpers (`describe_data`, `run_regression`, …) cover curated single-table workflows. Users with **folders of CSV/XLSX exports** and **multi-range joins** need a horizontal layer. DuckDB is the standard embedded answer (“SQLite for analytics”), local-first, one `pip install`, strong LLM familiarity with SQL.

**Explicit non-goals (MVP):** Replace `corpus.db` / embeddings SQLite; replace pandas for single-range `=PY()`; venv↔LO tool RPC; write-back via SQL `INSERT` (results egress through existing `write_formula_range` / tool paths only).

### Decision: sibling XLSX via LibreOffice import (not DuckDB `read_xlsx`)

**Policy:** For Excel files (`.xlsx`, `.xls`) in the scoped folder, use **LibreOffice’s native Calc import filter** — not DuckDB’s spreadsheet extension and not zip/XML shortcuts ([`embeddings_ooxml_extract.py`](../../plugin/embeddings/venv/embeddings_ooxml_extract.py) is for text FTS only).

**Rationale:** LO’s import produces **high-fidelity** evaluated sheet semantics (types, dates, locale, merged cells, used range) aligned with what users see when they open the file in Calc. DuckDB `read_xlsx` and lightweight parsers are acceptable for quick analytics elsewhere; WriterAgent already depends on UNO for live Calc and should **one-path** sibling spreadsheets through the same bridge.

**Mechanism (host, main thread):**

1. Hidden read-only open via [`open_document_for_read`](../../plugin/doc/document_research.py) (`loadComponentFromURL` + `Hidden` + `ReadOnly`) — same pattern as document research.
2. Read target sheet / used range with `CellInspector.read_range` → `host_pack_data` → worker `coerce_to_dataframe` → `duckdb.register(table_name, df)`.
3. **ODS disk cache (recommended for XLSX/XLS):** see [§ ODS cache directory](#ods-cache-directory) below.

CSV / Parquet / JSON remain **direct DuckDB file reads** in the venv (no UNO).

### ODS cache directory {#ods-cache-directory}

**Question:** Should WriterAgent maintain an `ods_cache` (or `writeragent_ods_cache/`) beside the document folder and reuse converted ODS files instead of re-importing XLSX every time?

**Recommendation: yes, with mtime invalidation.** Shipped in Phase A+ (`plugin/calc/ods_cache.py`).

| Approach | Pros | Cons |
|----------|------|------|
| **Re-import XLSX every request** | Simplest; always fresh | LO open + filter cost; painful for repeated SQL / large files |
| **In-memory only (session)** | Fast within one worker request / chat turn | Lost on restart; no cross-session reuse |
| **Per-folder ODS cache on disk** | Amortizes LO conversion; opens native `.ods` on hit; matches embeddings cache mental model | Invalidation logic; disk use; must handle stale entries |

**Proposed layout** (mirror [`writeragent_embeddings/`](../embeddings.md)):

```text
~/project/
  budget.xlsx
  report.ods
  writeragent_ods_cache/
    meta.json                    # optional global schema version
    a1b2c3….ods                  # cached conversion
    a1b2c3….meta.json            # source path, mtime, size, converter version
```

**Cache key:** hash of **absolute source path** + **mtime** + **size** (or content hash if mtime unreliable on network FS). On hit, open cached `.ods` via UNO. On miss, LO import XLSX → **Save As** cache path → read range(s) → write sidecar meta.

**Invalidate when:** source `mtime`/`size` changes, cache meta missing, LO import version bump (store `cache_format_version` in meta), user **Rebuild ODS cache** in Settings or search dialog analogue.

**Do not cache:** native `.ods` / live active workbook (open source directly). **Do cache:** `.xlsx`, `.xls` only.

**Settings:** `calc.ods_cache_enabled` (default `true`; plan name was `duckdb.ods_cache_enabled`). `duckdb.ods_cache_max_mb` LRU prune is still optional / not shipped.

**Why not skip cache:** SQL workflows often re-query the same sibling Excel file many times (chat iterations, `=PY()` recalc, analysis sub-agent). LO’s XLSX filter is the fidelity win; cache makes that win **affordable**.

**Shipped MVP:** mtime+size-checked cache only (no LRU). Disable with `calc.ods_cache_enabled: false` in `writeragent.json`.

---

## What is DuckDB? (for PMs)

DuckDB is an **in-process analytical database**. No server, no network, no separate install step beyond the user’s Python venv.

```python
import duckdb

# Query a CSV on disk
duckdb.sql("SELECT year, SUM(amount) FROM 'sales.csv' GROUP BY 1").df()

# Query an in-memory pandas DataFrame (Calc range after host wire)
duckdb.register("sheet1", df)
duckdb.sql("SELECT dept, AVG(revenue) FROM sheet1 GROUP BY 1").df()
```

| Compared to | DuckDB’s role |
|-------------|----------------|
| **LibreOffice Calc** | Interactive editing, formulas, charts — DuckDB does not replace the sheet UI |
| **pandas** | Great for one table already in `data`; DuckDB shines for **SQL**, **joins**, and **files on disk** |
| **SQLite** (`corpus.db`, chat history) | App state and FTS/vectors — keep separate; DuckDB is for **analytic queries on user files + sheet snapshots** |

---

## Benefits

### User-facing

- **Folder analytics:** “Sum actuals across all `budget_*.csv` next to this spreadsheet” without opening each file in Calc.
- **Join sheet to files:** Active range as one table, sibling Parquet/CSV as another — one SQL statement.
- **LLM-friendly:** Models often emit correct `SELECT … GROUP BY` faster than idiomatic pandas pipelines.
- **Local-first:** Offline, no API cost, fits NGOs/gov/homelab positioning (same story as OCR and embeddings).
- **Complements analysis helpers:** Helpers stay for curated one-click reports; DuckDB for ad hoc and multi-source questions.

### Engineering

- **Reuses shipped bridge:** `read_range` → `host_pack_data` → `child_unpack_data` → `coerce_to_dataframe` ([`plugin/calc/inspector.py`](../../plugin/calc/inspector.py), [`plugin/scripting/payload_codec.py`](../../plugin/scripting/payload_codec.py), [`plugin/scripting/venv/coerce.py`](../../plugin/scripting/venv/coerce.py)).
- **Same execution shell as Analysis/Viz:** Warm venv worker, trusted module, no LLM-submitted arbitrary imports beyond whitelist.
- **No ABI risk:** DuckDB runs in the child interpreter only.
- **Build-on mountain:** Future Parquet export ([pyarrow](../enabling_numpy_in_libreoffice.md), deferred) makes DuckDB faster; shared-kernel session cache (Phase D) reuses one connection across `=PY()` cells.

### Competitive

- Excel Python-in-Excel: cloud containers, curated Anaconda set — not local SQL over arbitrary folder files.
- Raw `=PY()` + pandas: works but scales poorly to many files and multi-table joins.
- DuckDB in Calc-adjacent workflows is a **distinctive local analytics** story next to the analysis helper suite.

---

## Architecture principle: DuckDB never touches UNO

```text
┌─────────────────────────────────────────────────────────────────┐
│ LibreOffice host (main thread for UNO)                          │
│  • CellInspector.read_range  (values + formulas metadata)       │
│  • get_document_directory    (scoped folder for sibling files)  │
│  • host_pack_data / split_grid                                  │
└────────────────────────────┬────────────────────────────────────┘
                             │ length-prefixed worker request
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ User venv worker                                                │
│  • child_unpack_data                                            │
│  • coerce_to_dataframe (per table)                              │
│  • session_duckdb() or duckdb.connect(); register(name, df)        │
│  • read_csv_auto / read_parquet / JSON (scoped paths only)       │
│  • con.sql(query) → result DataFrame / scalars                  │
└────────────────────────────┬────────────────────────────────────┘
                             │ JSON-serializable result
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ Host egress (existing)                                          │
│  • write_formula_range, charts, chat summary, =PY() cell        │
└─────────────────────────────────────────────────────────────────┘
```

**Key insight from research:** The hard part is not DuckDB — it is **defining the table catalog** (which ranges, which files, column names, size limits). Calc ingress is already solved for analysis; DuckDB sits **after** `coerce_to_dataframe`.

**Venv↔LO tool RPC:** Not required. The main agent can continue “JSON result → host tools” for write-back. RPC remains a future elegance for hand-written `=PY()` scripts, not a blocker for this feature.

---

## Data sources and limitations

| Source | MVP approach | Notes |
|--------|----------------|-------|
| **Sibling CSV / Parquet / JSON** | DuckDB `read_*` on paths under `scoped_dir` | Primary Phase A win |
| **Sibling `.xlsx` / `.xls`** | **LO Calc import** → UNO `read_range` → wire → `register()` | High fidelity; reuse [`open_document_for_read`](../../plugin/doc/document_research.py). **Not** DuckDB `read_xlsx`. |
| **Sibling `.ods`** | Same UNO path as XLSX | Native format; hidden open + range read |
| **Live active workbook range** | UNO → wire → DataFrame → `register()` | Phase B; **evaluated values** from `getDataArray()` |
| **Unsaved active workbook** | Wire path only | No on-disk file |
| **PDF** | Out of scope | Separate document pipeline (not this plan) |

Folder discovery aligns with [document research](../chat/multi-document-dev-plan.md) (`get_document_directory`, same parent as active saved doc). **CSV/XLSX in folder are not today indexed for embeddings text search** ([../embeddings.md](../embeddings.md)); DuckDB addresses the **numeric/tabular** side of the same folder.

---

## Phased implementation

### Phase A — Folder SQL (MVP, lowest UNO risk)

**User story:** “I have `actuals_2024.csv` and `actuals_2025.csv` beside my `.ods`. Run SQL to compare them.”

**Host:**

- Resolve `scoped_dir` from active document (`get_document_directory` in [`plugin/doc/document_research.py`](../../plugin/doc/document_research.py)); untitled doc → Work folder fallback (same as document research).
- Pass `scoped_dir`, `sql`, and optional `files` allowlist (host-normalized basenames) in worker request — **never** let LLM supply raw absolute paths unchecked.

**Venv** (`plugin/scripting/venv/duckdb_sql.py` or similar):

- Trusted functions: `query_folder_sql(scoped_dir, sql, files=...)`.
- Open `duckdb.connect()` (in-memory).
- Register only files validated to live under `scoped_dir` (reject `..`, absolute escapes).
- **Read-only SQL policy:** block `COPY`, `ATTACH`, export-to-disk statements (allowlist or parse guard).

**Surfaces:**

- Run Python Script → **[SQL] query_folder** template.
- Optional: `analyze_data`-style tool `run_sql` under `specialized_domain="analysis"` or new `"sql"` domain.
- Settings → Python: **Analytics / SQL Libraries** probe (`import duckdb`).

**Deliverable:** Shipped feature with docs + tests; no live active-sheet range wiring yet.

**Phase A+ (same release or fast follow):** Sibling **XLSX/XLS/ODS** in `scoped_dir` — host opens via LO import, reads sheet (default: first sheet or caller-specified), registers as a named table in the same worker request as CSV DuckDB reads. Close hidden docs with [`close_document_research_document`](../../plugin/doc/document_research.py) after wire pack.

---

### Phase B — Single Calc range as one table

**User story:** “SQL this sheet’s table in `A1:F500` — group by region, sum revenue.”

**Flow:**

1. Tool or template accepts `data_range` (existing pattern from [`analyze_data`](analysis-tools.md)).
2. Host: `read_range` → strip to values via [`calc_addin_data`](../../plugin/calc/calc_addin_data.py) → `host_pack_data`.
3. Worker: `child_unpack_data` → `coerce_to_dataframe(..., headers=True)` → `con.register("data", df)`.
4. Run user/LLM SQL; assign `result` for egress.

**Parameters (mirror analysis):**

- `sql` (required)
- `data_range` or pre-packed `data`
- `headers`, `header_row`, `task_hint`

**Surfaces:** `[SQL] query_sheet` template; analysis sub-agent tool; advanced `=PY()` users (SQL string + `data` arg).

**Limits:** Reuse `python_max_data_cells` ([`config_limits.py`](../../plugin/scripting/config_limits.py)); fail with clear error when range too large.

---

### Phase C — Multi-table catalog (joins) — **Landed**

**User story:** “Join `Sales!A1:F500` to `Costs!A1:D200` and to `ledger.parquet` in this folder.”

**Worker request shape (sketch):**

```json
{
  "tables": {
    "sales": {"sheet": "Sales", "headers": true},
    "costs": {"named_range": "CostData", "headers": true}
  },
  "files": {
    "ledger": "ledger.parquet"
  },
  "scoped_dir": "/path/to/project",
  "sql": "SELECT s.region, SUM(s.amount) - SUM(c.cost) FROM sales s JOIN costs c ON ..."
}
```

Frozen A1 (`{"range": "Sales.A1:F500"}`) remains valid. Prefer `sheet` / `named_range` so the catalog does not drift — see [Table source identity](#table-source-identity).

**Host responsibilities:**

- For each `tables` entry: sheet-qualified range parse (existing Calc tools), `read_range`, pack into request payload as named wire blobs.
- For each `files` entry: resolve under `scoped_dir`. **Tabular office files** (`.ods`, `.xlsx`, `.xls`): LO open + range read → wire blob (same as `tables`). **Flat files** (`.csv`, `.parquet`, `.json`): pass validated path for DuckDB `read_*` in worker.

**Worker:** Unpack each LO-sourced table → coerce → register all names → load flat files via DuckDB → one `sql()` → result.

**Tricky bits (called out for senior devs):**

- Column name sanitization for SQL (spaces, LO error tokens).
- Type coercion consistency ([`coerce.py`](../../plugin/scripting/venv/coerce.py) already handles `#N/A`, currency strings).
- Multi-sheet UNO reads on main thread (analysis sub-agent pattern).
- LLM-generated SQL injection: prefer host-supplied table **names** only; validate SQL is read-only.

**Implementation note:** Host (tool) resolves all UNO-dependent data (ranges + office files) into `preloaded` + `flat_files`; worker registers by name and executes. `tables` + `files` (as dict) now supported in the tool. Legacy paths preserved.

### Table source identity {#table-source-identity}

The catalog stores **identity**, not expanded A1. Host resolves bounds **at read time** so a later insert/append does not require rewriting the catalog.

| Spec | Meaning | Grows when |
|------|---------|------------|
| `{sheet: "Sales_Analytics"}` | Used range of that sheet on the active workbook | Rows/cols are added inside the used area |
| `{named_range: "SalesData"}` | Calc **named range** or **database range** (current referred / data-area bounds) | The name’s definition expands (Calc updates named refs on insert; or the user edits the name) |
| `{range: "Sales.A1:F500"}` | Frozen absolute A1 (also `Sheet.A1:F500`) | Never — leftover from Phase C; still accepted |
| `{file: "budget.xlsx", sheet: "Sales"}` or `{file: "budget.xlsx#Sales"}` | Same sheet used-range identity on a **sibling** workbook | Same as `{sheet}` on that file |
| `files={"sales": "budget.xlsx#Sales"}` | Same sibling sheet used-range; **dict key is the SQL table name** | Same as `{sheet}` |

Bare string `tables={"sales": "Sales.A1:F500"}` is still `{range}`. `data_range` still becomes table `data` with frozen A1 — prefer `tables={data: {sheet}}` or `{named_range}` for a stable id.

**Do not** store the resolved `C5:J40` back into the catalog. Re-query with the same `{sheet}` / `{named_range}` after the sheet grows.

```json
{
  "tables": {
    "sales": {"sheet": "Sales_Analytics"},
    "costs": {"named_range": "CostData"}
  },
  "files": {
    "income": "zip_income.csv",
    "budget": "budget.xlsx#Actuals"
  },
  "sql": "SELECT s.region, SUM(s.amount) FROM sales s GROUP BY 1"
}
```

Exactly one of `sheet`, `named_range`, or `range` per `tables` entry (a sibling `file` alone means first-sheet used range). Missing sheet / name / empty used-range fail loud.

---

### Phase D — Shared-kernel session cache {#phase-d--shared-kernel-session-cache}

**Landed.** Shared-kernel `=PY()` mode ([session modes](../enabling_numpy_in_libreoffice.md#session-modes-and-recalc-semantics)) keeps **one in-memory DuckDB connection** and its registered tables per workbook until **Reset Python Session** (or `invalidate_session_tables()`). Isolated mode and chat `query_folder_sql` stay per-request (`duckdb.connect()` + close), same as Phases A–C.

Cached connections are `GuardedDuckDBConnection` wrappers: `.execute` / `.sql` use the same COPY/escape firewall as `query_folder_sql`. Prefer `run_sql` / `session_duckdb()` from `=PY()`. Raw `import duckdb` still bypasses the guard. Do **not** use `duckdb.connect(read_only=True)` on `:memory:` (engine refuses).

**API (venv worker):**

| Helper | Role |
|--------|------|
| `session_duckdb()` | Return the workbook connection when a persistable session is active (`calc:` / `rps:` / `notebook:`); otherwise a fresh connection. Injected into `=PY()` / RPS namespaces. Also `from writeragent.scripting.duckdb_sql import session_duckdb`. |
| `query_folder_sql(..., session_id=)` | Reuses that connection when `session_id` is persistable **or** the current sandbox session is. Re-registers any `preloaded` / `flat_files` on this call (refresh). Tables omitted from this call stay as last registered. |
| `run_sql(sql, con=None)` | Same honesty dict + firewall. With no `con`, uses the persistable session when one is active. |
| `invalidate_session_tables(names=None)` | Drop named tables, or close the whole catalog when *names* is omitted. |
| `reset_session_duckdb(session_id)` | Close the cached connection. **Reset Python Session** calls this with the workbook id. |

`run_folder_sql` / chat still pass the routing id `writeragent:sql`. That prefix is **not** persistable, so analysis-tool SQL cannot leak one catalog across documents.

#### Staleness vs Calc recalc

Registered tables are **snapshots**, like other shared-kernel globals. F9 / partial recalc does **not** drop the connection.

| Event | What happens to the catalog |
|-------|-----------------------------|
| Cell / helper **re-registers** a name (`data` / `preloaded` / `con.register`) | That table is replaced with the new snapshot. Calc DAG + passing the range as `data` is how a sheet edit refreshes SQL. |
| Cell only **queries** (`SELECT … FROM sales`) | Sees the last-registered snapshot, even if the sheet changed. |
| Sibling file re-read on a later `query_folder_sql` that includes that file | Re-registered from disk. Omitted files stay as last snapshot. |
| **Reset Python Session** | Connection closed; all tables gone. Next `session_duckdb()` opens a new catalog. |
| `invalidate_session_tables(["sales"])` | Drops that name only. |
| Isolated `=PY()` / chat tool | No cache. Each call is a new connection. |

Authoring: pass upstream ranges as `data` so Calc dirties the cell that re-registers; keep one-off `CREATE`/`register` in the init script or a setup cell. Do not assume row-major order.

---

## User exposure matrix

| Surface | Phase | Notes |
|---------|-------|-------|
| **Run Python Script → SQL Helpers** | A, B, C | `[SQL] query_folder_sql` / `query_sheet_sql`; supports tables + files |
| **Analysis sub-agent** | A, B, C | `query_folder_sql` with `tables` / `files` / `data_range` |
| **Chat / delegate** | B, C | “Join Sales range to costs.csv and ledger.parquet” |
| **`=PY()` / `=PYTHON()`** | B+, D | `session_duckdb()` in shared kernel (guarded + 200-row honesty). Raw `import duckdb` is authorized but unguarded. Isolated stays per-eval. |
| **MCP** | B+ | Optional later via existing tool registry |

**PM note:** Phase A is releasable on its own — valuable for users who export CSV from Calc or receive data drops beside ODS files.

---

## Security and sandbox

| Risk | Mitigation |
|------|------------|
| Filesystem escape via SQL paths | Host passes `scoped_dir`; worker resolves and rejects paths outside prefix |
| Network exfil | Venv sandbox already blocks `requests`/`urllib`; DuckDB `httpfs` extension not whitelisted |
| Write/attach side effects | Read-only SQL guard: deny `COPY`, `EXPORT`, `ATTACH`, `INSTALL`, `LOAD` (word-boundary, comments/strings stripped). In-memory `CREATE VIEW` / `CREATE TABLE` / `INSERT` stay allowed. Path/URI escapes (`../`, absolute `/…`, `~/`, drive letters, `http(s)/s3/file/ftp`) fail with `READONLY_VIOLATION`. Do not use `duckdb.connect(read_only=True)` on `:memory:`. |
| Huge memory | Ingress: `python_max_data_cells` fail-loud on preloaded grids (table name + cell count). Egress: `MAX_TABLE_ROWS=200` with `truncated`, `total_rows`, `row_cap`, `warning` / `flags` / `message`, and `tables[].truncated` so chat, RPS, and tools cannot mistake a partial grid for a complete result. |
| Secrets in env | Existing `scrub_subprocess_env` in [`sandbox.py`](../../plugin/scripting/sandbox.py) |

Add `duckdb` / `duckdb.*` to [`VENV_AUTHORIZED_IMPORTS`](../../plugin/scripting/sandbox.py) when shipping; update [`import_policy.py`](../../plugin/scripting/import_policy.py) prompts to mention SQL helpers vs raw pandas.

---

## Implementation checklist (engineering)

| Item | Location / pattern |
|------|-------------------|
| Trusted venv module | `plugin/scripting/venv/duckdb_sql.py` (mirror [`plugin/scripting/venv/analysis.py`](../../plugin/scripting/venv/analysis.py)) — supports preloaded + flat_files |
| Host facade / client | `plugin/scripting/client.py` (`run_folder_sql`) + `plugin/calc/duckdb_tools.py` |
| Sibling spreadsheet open | Reuse [`open_document_for_read`](../../plugin/doc/document_research.py) + `CellInspector` (main thread); close hidden models after read — A+ |
| Calc tool | `plugin/calc/duckdb_tools.py` (`QueryFolderSqlTool` on `ToolCalcAnalysisBase`) — `tables`, `files` (dict), `data_range` |
| Run Python Script templates | `plugin/scripting/duckdb_sql.py` (host) + document_scripts (SQL Helpers) |
| Settings probe | Extend venv self-check groups in [`venv_worker.py`](../../plugin/scripting/venv_worker.py) — A0 |
| Tests | `tests/scripting/test_duckdb_sql.py`, `tests/calc/test_duckdb_tools.py` |
| UNO tests | Optional `tests/uno/` for end-to-end |
| Docs | This plan + updates in [../enabling_numpy_in_libreoffice.md](../enabling_numpy_in_libreoffice.md) and [analysis-tools.md](analysis-tools.md) |

**Dependency:** `duckdb` in user venv only (document in Settings guide); not in `pyproject.toml` extension runtime.

**pyarrow:** Optional later for zero-copy Arrow registration from split_grid ndarrays; not required for Phase A/B (pandas `register` is sufficient).

---

## Testing strategy

1. **Unit (pytest):** Path validation; folder fixtures with 2 CSVs + JOIN; single grid through `coerce_to_dataframe` → SQL → expected aggregates.
2. **Integration:** Mock worker request with wire envelopes from [`tests/scripting/`](../../tests/scripting/) payload fixtures.
3. **Manual:** Saved ODS + sibling CSVs and **sibling XLSX** (LO import path); analysis sub-agent “compare Q4 actuals file to sheet”; large range boundary at `python_max_data_cells`.
4. **UNO:** Hidden open `.xlsx` → read range → SQL join to CSV — fidelity vs Excel desktop spot-check.
4. **`make test`** before release per [AGENTS.md](../../AGENTS.md).

---

## Success metrics (PM — lightweight, local-first)

No cloud telemetry required. Suggested signals:

- GitHub issues / forum mentions mentioning SQL, CSV folder, join across files.
- `enable_agent_log` / debug log counts for `run_sql` / SQL helper names (if instrumented).
- Qualitative release-note feedback after auto-update nudges.

---

## Open questions

| # | Question | Owner |
|---|----------|-------|
| 1 | Separate `sql` specialized domain vs helpers under `analysis`? | PM + API |
| 2 | Allow LLM-authored SQL verbatim vs template-only (file list from host)? | Security |
| 3 | ~~XLSX via DuckDB vs host~~ | **Resolved:** LO import + UNO read (this doc § Decision). |
| 4 | ~~ODS cache on disk?~~ | **Resolved / shipped:** per-folder `writeragent_ods_cache/` with mtime+size invalidation (§ ODS cache directory). |
| 5 | Auto-export sheet snapshot to temp Parquet for huge ranges (Phase C perf) | Eng, defer |
| 6 | Relationship to deferred **pyarrow** / Parquet export from Calc | Roadmap |

---

## Related docs

| Topic | Doc |
|-------|-----|
| Venv worker, `=PY()`, sandbox | [../enabling_numpy_in_libreoffice.md](../enabling_numpy_in_libreoffice.md) |
| Analysis helpers (pattern to mirror) | [analysis-tools.md](analysis-tools.md), [analysis-sub-agent.md](analysis-sub-agent.md) |
| Wire format | [../scripting/numpy-serialization.md](../scripting/numpy-serialization.md) |
| Folder / sibling files | [../chat/multi-document-dev-plan.md](../chat/multi-document-dev-plan.md) |
| Blank vs NaN (ingress quality for SQL inputs) | [py-data-shapes.md](py-data-shapes.md#empty-cells-vs-nan) |
| Python-in-Calc UX backlog | [enabling_numpy §7 Calc UX backlog](../enabling_numpy_in_libreoffice.md#calc-ux-backlog) |

---

## Changelog

| Date | Change |
|------|--------|
| 2026-06-18 | Initial plan from architecture research (Calc bridge, phased delivery, security). |
| 2026-06-18 | **Decision:** sibling XLSX/XLS via LO Calc import → UNO range read (not DuckDB `read_xlsx`). |
| 2026-06-18 | Phase A start: sandbox whitelist, Settings probe (Data Eng), trusted `query_folder_sql` + path guard, host facade + templates, document picker, basic Calc tool. Tests + docs updated. |
| 2026-06-18 | A+ polish: sibling .xlsx/.xls/.ods via `open_document_for_read` + `CellInspector` + preloaded grids; sheet#hint syntax; size limits; clean stem + orig basename registration + aliases; improved reader + tests. |
| 2026-06-18 | Phase B: data_range support in tool for active sheet (registers as 'data' table, respects headers). Unified preloaded handling (structured for headers), template for query_sheet_sql, descriptions. |
| 2026-06-18 | Phase C: multi-table 'tables' param (named ranges on active), files as dict for named flat+office. Worker supports flat_files dict for direct named registration (no chdir). Host prepares preloaded + flat_files. Tool + client updated. |
| 2026-09-05 | Pretty demo: same ODS as the =PY() showcase gets a SQL_DuckDB sheet (SQL in cells) + ZIP on sales + sibling ACS `zip_income.csv`. Tests run `query_folder_sql` against those assets. |
| 2026-09-05 | Sibling office ingress: used-range (not `A1:AK2000` / `A1:AZ5000`); fail loud on open / `#SheetName` / empty range. ODS mtime cache still pending. |
| 2026-09-05 | SQL_DuckDB live RESULTS `=PY()` formulas quote SQL with Python single quotes inside the formula `"…"` payload. Triple-double quotes closed the formula string early (Calc Err:508). |
| 2026-09-05 | SQL_DuckDB RESULTS are formula-safe: SQL lives only in cells; the `=PY()` payload is a short runner that reads `data[1]` (SQL cell/range) plus `data[0]` (sheet range). |
| 2026-09-05 | SQL_DuckDB sheet-only RESULTS leave 15 empty rows × 5 cols under the formula so a Region×Category spill (header+12) does not `#SPILL!` into the next section title. The live RESULTS formula cell is unmerged (no ODS `span_cols` / XLSX A:H) so spill targets are real cells. |
| 2026-09-05 | Stable table identity: `{sheet}` (used range), `{named_range}` (named or database range), sibling `file.xlsx#Sheet` / `files={name: "file.xlsx#Sheet"}`. Frozen `{range: A1}` kept. Catalog does not store expanded A1. |
| 2026-09-05 | Phase A+ ODS cache: sibling `.xlsx`/`.xls` → `writeragent_ods_cache/` (hash of abs path + mtime + size); open cached ODS on hit; native `.ods` and the live workbook skip cache. Setting `calc.ods_cache_enabled` (default true). |
| 2026-09-05 | Phase D: shared-kernel `session_duckdb()` + `query_folder_sql` table cache until Reset Python Session; Isolated / `writeragent:sql` stay per-request. Staleness: re-register on `data`/`preloaded`, else last snapshot. |
| 2026-09-05 | Honesty + firewall polish: 200-row cap is visible (`warning`/`flags`/`message`/`tables`); deny-list is disk/network only (VIEW/register stay); `run_sql` / guarded `session_duckdb()` for `=PY()`; RPS SQL uses tabular egress. |
| 2026-09-05 | Folder SQL flat binders: Parquet + JSON/JSONL/NDJSON register reliably via `flat_files` / `files` dict; unsupported and missing types return `UNSUPPORTED_FILE_TYPE` / `MISSING_FILE`. Sibling XLSX/ODS stay on the LO import path. |
