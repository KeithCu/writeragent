---
name: DuckDB Calc Integration
overview: Add DuckDB as a horizontal analytics layer in the user venv — SQL over folder files (CSV, Parquet, XLSX) and over live Calc ranges materialized via the existing split_grid → DataFrame bridge. Phased delivery; no venv↔LO tool RPC required for MVP.
todos:
  - id: phase-a-spec
    content: "Phase A: Spec + sandbox whitelist + Settings probe group + path policy for scoped_dir"
    status: pending
  - id: phase-a-venv
    content: "Phase A: Trusted venv module plugin/scripting/venv/duckdb_sql.py (folder read-only SQL)"
    status: pending
  - id: phase-a-host
    content: "Phase A: Host tool + Run Python Script [SQL] folder templates; scoped_dir; sibling XLSX/ODS via LO import + read_range (not DuckDB read_xlsx)"
    status: pending
  - id: phase-a-plus-ods-cache
    content: "Phase A+: writeragent_ods_cache/ beside folder; mtime invalidation for xlsx/xls → cached ods; open cache on hit"
    status: pending
  - id: phase-a-tests
    content: "Phase A: pytest for path guard + folder SQL round-trip (temp dir fixtures)"
    status: pending
  - id: phase-b-sheet
    content: "Phase B: Single Calc range → coerce_to_dataframe → duckdb.register → SQL → result egress"
    status: pending
  - id: phase-c-multi
    content: "Phase C: Multi-table catalog (named ranges + optional folder files in one SQL request)"
    status: pending
  - id: phase-d-cache
    content: "Phase D (optional): Shared-kernel DuckDB session / table cache across =PY() cells"
    status: pending
isProject: false
---

# DuckDB for Calc & Folder Analytics — PM / Senior Dev Plan

Back to [Enabling NumPy & Python in LibreOffice](enabling_numpy_in_libreoffice.md).

**Status:** Phase A foundations landed (whitelist + probe + trusted query_folder_sql + [SQL] Run Python Script templates + QueryFolderSqlTool). Incremental steps per execution plan continue.

**Audience:** Product, senior engineers, and future implementers. This doc captures why DuckDB fits WriterAgent, what users get, and how to build on existing Calc↔venv infrastructure without a new architectural pillar.

---

## Executive summary

**Product goal:** Let Calc and chat users run **SQL locally** against (1) spreadsheet files in the same folder as their document and (2) live Calc ranges — without loading entire workbooks into memory with pandas, without cloud analytics, and without teaching the LLM forty lines of groupby code for every question.

**Technical goal:** Add [DuckDB](https://duckdb.org/) as a **venv-only** dependency (like Docling, sentence-transformers, scipy). DuckDB never talks to UNO. LibreOffice reads sheet data on the host; the existing **split_grid wire** and **`coerce_to_dataframe`** path produce pandas tables; DuckDB registers them and runs SQL; compact results return via the same **`result`** egress as analysis helpers.

**Why now:** Analysis helpers (`describe_data`, `run_regression`, …) cover curated single-table workflows. Users with **folders of CSV/XLSX exports** and **multi-range joins** need a horizontal layer. DuckDB is the standard embedded answer (“SQLite for analytics”), local-first, one `pip install`, strong LLM familiarity with SQL.

**Explicit non-goals (MVP):** Replace `corpus.db` / embeddings SQLite; replace pandas for single-range `=PY()`; venv↔LO tool RPC; write-back via SQL `INSERT` (results egress through existing `write_formula_range` / tool paths only).

### Decision: sibling XLSX via LibreOffice import (not DuckDB `read_xlsx`)

**Policy:** For Excel files (`.xlsx`, `.xls`) in the scoped folder, use **LibreOffice’s native Calc import filter** — not DuckDB’s spreadsheet extension and not zip/XML shortcuts ([`embeddings_ooxml_extract.py`](../plugin/embeddings/venv/embeddings_ooxml_extract.py) is for text FTS only).

**Rationale:** LO’s import produces **high-fidelity** evaluated sheet semantics (types, dates, locale, merged cells, used range) aligned with what users see when they open the file in Calc. DuckDB `read_xlsx` and lightweight parsers are acceptable for quick analytics elsewhere; WriterAgent already depends on UNO for live Calc and should **one-path** sibling spreadsheets through the same bridge.

**Mechanism (host, main thread):**

1. Hidden read-only open via [`open_document_for_read`](../plugin/doc/document_research.py) (`loadComponentFromURL` + `Hidden` + `ReadOnly`) — same pattern as document research.
2. Read target sheet / used range with `CellInspector.read_range` → `host_pack_data` → worker `coerce_to_dataframe` → `duckdb.register(table_name, df)`.
3. **ODS disk cache (recommended for XLSX/XLS):** see [§ ODS cache directory](#ods-cache-directory) below.

CSV / Parquet / JSON remain **direct DuckDB file reads** in the venv (no UNO).

### ODS cache directory {#ods-cache-directory}

**Question:** Should WriterAgent maintain an `ods_cache` (or `writeragent_ods_cache/`) beside the document folder and reuse converted ODS files instead of re-importing XLSX every time?

**Recommendation: yes, with mtime invalidation — but not in Phase A (CSV-only).** Add when sibling XLSX ingress ships (Phase A+).

| Approach | Pros | Cons |
|----------|------|------|
| **Re-import XLSX every request** | Simplest; always fresh | LO open + filter cost; painful for repeated SQL / large files |
| **In-memory only (session)** | Fast within one worker request / chat turn | Lost on restart; no cross-session reuse |
| **Per-folder ODS cache on disk** | Amortizes LO conversion; opens native `.ods` on hit; matches embeddings cache mental model | Invalidation logic; disk use; must handle stale entries |

**Proposed layout** (mirror [`writeragent_embeddings/`](embeddings.md)):

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

**Settings (optional):** `duckdb.ods_cache_enabled` (default `true`), `duckdb.ods_cache_max_mb` prune LRU.

**Why not skip cache:** SQL workflows often re-query the same sibling Excel file many times (chat iterations, `=PY()` recalc, analysis sub-agent). LO’s XLSX filter is the fidelity win; cache makes that win **affordable**.

**MVP shortcut:** Phase A+ can ship **mtime-checked cache** only (no LRU) — enough for v1.

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

- **Reuses shipped bridge:** `read_range` → `host_pack_data` → `child_unpack_data` → `coerce_to_dataframe` ([`plugin/calc/inspector.py`](../plugin/calc/inspector.py), [`plugin/scripting/payload_codec.py`](../plugin/scripting/payload_codec.py), [`plugin/scripting/venv/coerce.py`](../plugin/scripting/venv/coerce.py)).
- **Same execution shell as Analysis/Viz:** Warm venv worker, trusted module, no LLM-submitted arbitrary imports beyond whitelist.
- **No ABI risk:** DuckDB runs in the child interpreter only.
- **Build-on mountain:** Future Parquet export ([pyarrow](enabling_numpy_in_libreoffice.md), deferred) makes DuckDB faster; optional shared-kernel cache (Phase D) builds on session modes.

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
│  • duckdb.connect(); register(name, df)  ← Calc + LO-imported XLSX │
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
| **Sibling `.xlsx` / `.xls`** | **LO Calc import** → UNO `read_range` → wire → `register()` | High fidelity; reuse [`open_document_for_read`](../plugin/doc/document_research.py). **Not** DuckDB `read_xlsx`. |
| **Sibling `.ods`** | Same UNO path as XLSX | Native format; hidden open + range read |
| **Live active workbook range** | UNO → wire → DataFrame → `register()` | Phase B; **evaluated values** from `getDataArray()` |
| **Unsaved active workbook** | Wire path only | No on-disk file |
| **PDF** | Out of scope | Separate document pipeline (not this plan) |

Folder discovery aligns with [document research](multi-document-dev-plan.md) (`get_document_directory`, same parent as active saved doc). **CSV/XLSX in folder are not today indexed for embeddings text search** ([embeddings.md](embeddings.md)); DuckDB addresses the **numeric/tabular** side of the same folder.

---

## Phased implementation

### Phase A — Folder SQL (MVP, lowest UNO risk)

**User story:** “I have `actuals_2024.csv` and `actuals_2025.csv` beside my `.ods`. Run SQL to compare them.”

**Host:**

- Resolve `scoped_dir` from active document (`get_document_directory` in [`plugin/doc/document_research.py`](../plugin/doc/document_research.py)); untitled doc → Work folder fallback (same as document research).
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

**Phase A+ (same release or fast follow):** Sibling **XLSX/XLS/ODS** in `scoped_dir` — host opens via LO import, reads sheet (default: first sheet or caller-specified), registers as a named table in the same worker request as CSV DuckDB reads. Close hidden docs with [`close_document_research_document`](../plugin/doc/document_research.py) after wire pack.

---

### Phase B — Single Calc range as one table

**User story:** “SQL this sheet’s table in `A1:F500` — group by region, sum revenue.”

**Flow:**

1. Tool or template accepts `data_range` (existing pattern from [`analyze_data`](calc-analysis-tools.md)).
2. Host: `read_range` → strip to values via [`calc_addin_data`](../plugin/calc/calc_addin_data.py) → `host_pack_data`.
3. Worker: `child_unpack_data` → `coerce_to_dataframe(..., headers=True)` → `con.register("data", df)`.
4. Run user/LLM SQL; assign `result` for egress.

**Parameters (mirror analysis):**

- `sql` (required)
- `data_range` or pre-packed `data`
- `headers`, `header_row`, `task_hint`

**Surfaces:** `[SQL] query_sheet` template; analysis sub-agent tool; advanced `=PY()` users (SQL string + `data` arg).

**Limits:** Reuse `python_max_data_cells` ([`config_limits.py`](../plugin/scripting/config_limits.py)); fail with clear error when range too large.

---

### Phase C — Multi-table catalog (joins)

**User story:** “Join `Sales!A1:F500` to `Costs!A1:D200` and to `ledger.parquet` in this folder.”

**Worker request shape (sketch):**

```json
{
  "tables": {
    "sales": {"range": "Sales.A1:F500", "headers": true},
    "costs": {"range": "Costs.A1:D200", "headers": true}
  },
  "files": {
    "ledger": "ledger.parquet"
  },
  "scoped_dir": "/path/to/project",
  "sql": "SELECT s.region, SUM(s.amount) - SUM(c.cost) FROM sales s JOIN costs c ON ..."
}
```

**Host responsibilities:**

- For each `tables` entry: sheet-qualified range parse (existing Calc tools), `read_range`, pack into request payload as named wire blobs.
- For each `files` entry: resolve under `scoped_dir`. **Tabular office files** (`.ods`, `.xlsx`, `.xls`): LO open + range read → wire blob (same as `tables`). **Flat files** (`.csv`, `.parquet`, `.json`): pass validated path for DuckDB `read_*` in worker.

**Worker:** Unpack each LO-sourced table → coerce → register all names → load flat files via DuckDB → one `sql()` → result.

**Tricky bits (called out for senior devs):**

- Column name sanitization for SQL (spaces, LO error tokens).
- Type coercion consistency ([`coerce.py`](../plugin/scripting/venv/coerce.py) already handles `#N/A`, currency strings).
- Multi-sheet UNO reads on main thread (analysis sub-agent pattern).
- LLM-generated SQL injection: prefer host-supplied table **names** only; validate SQL is read-only.

---

### Phase D — Optional session cache (defer)

Shared-kernel `=PY()` mode ([session modes](enabling_numpy_in_libreoffice.md#session-modes-and-recalc-semantics)) could keep one DuckDB connection and registered tables across cells until Reset Python Session. High complexity (staleness vs Calc recalc); only after Phases A–C prove usage.

---

## User exposure matrix

| Surface | Phase | Notes |
|---------|-------|-------|
| **Run Python Script → SQL Helpers** | A, B | Same UX as Analysis/Viz/Units |
| **Analysis sub-agent** | A, B, C | `run_sql` or extend `analyze_data` with `helper=sql_*` |
| **Chat / delegate** | B, C | “Join sheet to CSV in folder” |
| **`=PY()` / `=PYTHON()`** | B | Single-table; multi-table awkward without catalog API |
| **MCP** | B+ | Optional later via existing tool registry |

**PM note:** Phase A is releasable on its own — valuable for users who export CSV from Calc or receive data drops beside ODS files.

---

## Security and sandbox

| Risk | Mitigation |
|------|------------|
| Filesystem escape via SQL paths | Host passes `scoped_dir`; worker resolves and rejects paths outside prefix |
| Network exfil | Venv sandbox already blocks `requests`/`urllib`; DuckDB `httpfs` extension not whitelisted |
| Write/attach side effects | Read-only SQL guard (deny `COPY`, `ATTACH`, `INSTALL`, `LOAD` untrusted) |
| Huge memory | Cell/file size caps; optional row limits on `sql()` result before egress |
| Secrets in env | Existing `scrub_subprocess_env` in [`sandbox.py`](../plugin/scripting/sandbox.py) |

Add `duckdb` / `duckdb.*` to [`VENV_AUTHORIZED_IMPORTS`](../plugin/scripting/sandbox.py) when shipping; update [`import_policy.py`](../plugin/scripting/import_policy.py) prompts to mention SQL helpers vs raw pandas.

---

## Implementation checklist (engineering)

| Item | Location / pattern |
|------|-------------------|
| Trusted venv module | `plugin/scripting/venv/duckdb_sql.py` (mirror [`plugin/scripting/venv/analysis.py`](../plugin/scripting/venv/analysis.py)) |
| Host facade | `plugin/scripting/duckdb_client.py` or extend analysis client pattern |
| Sibling spreadsheet open | Reuse [`open_document_for_read`](../plugin/doc/document_research.py) + `CellInspector` (main thread); close hidden models after read |
| Calc tool | `plugin/calc/duckdb_tools.py` — `RunSqlTool`, `ToolCalcAnalysisBase` or new base |
| Run Python Script templates | `plugin/scripting/` template registry (SQL Helpers section) |
| Settings probe | Extend venv self-check groups in [`venv_worker.py`](../plugin/scripting/venv_worker.py) |
| Tests | `tests/scripting/test_duckdb_sql.py` — path guard, folder SQL, mock grid → SQL (no LO for A/B unit tests) |
| UNO tests | Optional `tests/uno/` for end-to-end range → SQL → writeback |
| Docs | This plan + section in [enabling_numpy_in_libreoffice.md](enabling_numpy_in_libreoffice.md); link from [calc-analysis-tools.md](calc-analysis-tools.md) |

**Dependency:** `duckdb` in user venv only (document in Settings guide); not in `pyproject.toml` extension runtime.

**pyarrow:** Optional later for zero-copy Arrow registration from split_grid ndarrays; not required for Phase A/B (pandas `register` is sufficient).

---

## Testing strategy

1. **Unit (pytest):** Path validation; folder fixtures with 2 CSVs + JOIN; single grid through `coerce_to_dataframe` → SQL → expected aggregates.
2. **Integration:** Mock worker request with wire envelopes from [`tests/scripting/`](../tests/scripting/) payload fixtures.
3. **Manual:** Saved ODS + sibling CSVs and **sibling XLSX** (LO import path); analysis sub-agent “compare Q4 actuals file to sheet”; large range boundary at `python_max_data_cells`.
4. **UNO:** Hidden open `.xlsx` → read range → SQL join to CSV — fidelity vs Excel desktop spot-check.
4. **`make test`** before release per [AGENTS.md](../AGENTS.md).

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
| 4 | ~~ODS cache on disk?~~ | **Resolved:** per-folder `writeragent_ods_cache/` with mtime invalidation (§ ODS cache directory). Defer until XLSX ingress. |
| 5 | Auto-export sheet snapshot to temp Parquet for huge ranges (Phase C perf) | Eng, defer |
| 6 | Relationship to deferred **pyarrow** / Parquet export from Calc | Roadmap |

---

## Related docs

| Topic | Doc |
|-------|-----|
| Venv worker, `=PY()`, sandbox | [enabling_numpy_in_libreoffice.md](enabling_numpy_in_libreoffice.md) |
| Analysis helpers (pattern to mirror) | [calc-analysis-tools.md](calc-analysis-tools.md), [analysis-sub-agent.md](analysis-sub-agent.md) |
| Wire format | [numpy-serialization.md](numpy-serialization.md) |
| Folder / sibling files | [multi-document-dev-plan.md](multi-document-dev-plan.md) |
| Blank vs NaN (ingress quality for SQL inputs) | [calc-blanks-vs-nans.md](calc-blanks-vs-nans.md) |
| Python-in-Calc UX backlog | [python-in-excel-dev-plan.md](python-in-excel-dev-plan.md) |

---

## Changelog

| Date | Change |
|------|--------|
| 2026-06-18 | Initial plan from architecture research (Calc bridge, phased delivery, security). |
| 2026-06-18 | **Decision:** sibling XLSX/XLS via LO Calc import → UNO range read (not DuckDB `read_xlsx`). |
| 2026-06-18 | Phase A start: sandbox whitelist, Settings probe (Data Eng), trusted `query_folder_sql` + path guard, host facade + templates, document picker, basic Calc tool. Tests + docs updated. |