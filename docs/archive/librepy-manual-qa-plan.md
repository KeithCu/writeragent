# LibrePy-surface live QA plan (real user scenarios)

**Audience:** Cloud / checkout agents (and humans) exercising the **LibrePy feature set** in a real LibreOffice Calc/Writer/Draw session.

**Which OXT:** **Does not matter.** `=PY()`, the warm venv, Run Python Script, domain helpers, Monaco, Vision, and TeX are the **same code** in WriterAgent.oxt and LibrePy.oxt. Install whichever is already on the machine (`make deploy` or `make deploy-core`). Do **not** spend time swapping extensions or asserting which id is registered.

**What to test:** Only surfaces LibrePy ships — formulas, Python menus, Settings → Python, sidebar diagnostics, trusted domain helpers via **Run Python Script** (and `=PY()`). That is the product slice, not “must boot LibrePy.oxt”.

**Goal:** Prove that slice works the way a data-science spreadsheet user would use it — not formula-lexer trivia. Start with `=PY("1 + 1")` and walk the shipped layers.

**Out of scope for this pass (do later):**

- Syntax/runtime junk (`=PY("1 + E")`, empty code, nested quotes).
- WriterAgent-only features even if the WriterAgent OXT is installed: chat, `=PROMPT()`, `analyze_data` / `run_venv_python_script` / MCP, embeddings, DuckDB, spreadsheet → Python converter. Jupyter File → Open is in LibrePy.
- `calc.*` parity helpers (not in the LibrePy *feature* set; ignore if they happen to work under WriterAgent).
- Collabora Online / jail-safe C++ path ([../scripting/numpy-jailsafe.md](../scripting/numpy-jailsafe.md)).
- Geospatial, Audio analysis, SageMath, Prophet.

Related docs: [extension split](../scripting/librepy-split.md), [user guide](../enabling_numpy_in_libreoffice.md), [data shapes](../calc/py-data-shapes.md), [domains](../scripting/numpy-domains.md), [showcase](python-in-calc-showcase.md).

---

## How to farm this to agents

Each **work packet** below is independent after **P0** (venv + smoke). Assign one packet per agent. Agents must:

1. Run on a machine with **LibreOffice + either OXT + configured venv** (not pytest-only).
2. Fill the **result table** at the bottom of their packet: `id | pass/fail | actual | notes`.
3. Prefer **recalc in Calc** (`Ctrl+Shift+F9`) over inventing new formulas when a fixture already exists.
4. Not expand into edge cases unless a happy path is already broken.
5. Leave chat / `=PROMPT()` / `chat_prompt` cells in fixtures **untested** — those are WriterAgent-only even when the full OXT is installed.

**Pass rule:** Cell/script result matches the expected column (or visual: plot is on the sheet, not `#VALUE!`). Numeric tolerance: relative 1e-4 unless a formatted string is specified.

**Fail rule:** `#VALUE!`, `#NUM!` from a successful computation we expected to be a number, empty when a value is expected, LO crash, hang past timeout, or missing menu.

---

## P0 — Environment (every agent, once)

Do this before any packet.

| Step | Action | Pass |
|------|--------|------|
| P0.1 | LibreOffice with **WriterAgent or LibrePy** already installed. Restart if you just deployed. | `=PY` is in the function wizard; Python menus exist. Do not uninstall/swap OXTs |
| P0.2 | **Settings → Python**: set `scripting.python_venv_path` to a real venv | Path accepted |
| P0.3 | **Test** button | Scientific + Data Analysis groups **Present** for analysis packets; Viz / Computer Algebra / Units as needed |
| P0.4 | Session mode **Isolated** unless a packet says Shared | Default |
| P0.5 | Auto-spill **on** (default) | — |

Suggested venv (from the user guide):

```bash
uv pip install numpy pandas scipy scikit-learn statsmodels matplotlib seaborn sympy pint
# optional per packet: yfinance pandas_ta quantstats pyportfolioopt fg-data-profiling pandas-montecarlo
# optional Vision: docling rapidocr css_inline
# optional Text: spacy textdescriptives; python -m spacy download xx_sent_ud_sm
# optional Monaco: pywebview  (+ PyQt6 PyQt6-WebEngine qtpy on Linux)
```

**Existing automated coverage (do not re-implement in pytest):** `tests/calc/python/test_function.py`, `test_calc_addin_data.py`, `tests/scripting/test_*.py` for trusted helpers, `tests/calc/numpy_domains_demo_cases.py` (case catalog). **This plan is live LO**, which those mocks do not replace.

**Existing live fixtures (reuse):**

| File | Use |
|------|-----|
| New blank Calc | Packets A–C (formula authoring) |
| [`tests/fixtures/python_showcase_demo.xlsx`](../tests/fixtures/python_showcase_demo.xlsx) | Packet D (business dashboard). **Do not use** `python_showcase_demo.ods` — ODS generator is currently wrong |
| [`tests/fixtures/numpy_domains_demo.ods`](../tests/fixtures/numpy_domains_demo.ods) | Packet E (trusted helpers via `=PYTHON()`) |
| [numpy_domains_demo.README.md](../tests/fixtures/numpy_domains_demo.README.md) | How to recalc that ODS |

---

## Packet A — `=PY()` smoke (Layer 0)

**Why first:** If this fails, nothing else is worth debugging.

Open a **new** Calc spreadsheet. Semicolon vs comma: use your locale’s argument separator (`;` in many EU locales).

| id | Scenario | Formula / action | Expected |
|----|----------|------------------|----------|
| A1 | Hello world | `=PY("1 + 1")` | `2` |
| A2 | Alias | `=PYTHON("1 + 1")` | `2` |
| A3 | `result` assignment | `=PY("result = 3 ** 8")` | `6561` |
| A4 | Last-expression fallback | `=PY("3 ** 8")` | `6561` |
| A5 | Auto-imported NumPy | `=PY("float(np.mean([1, 2, 3, 4]))")` | `2.5` |
| A6 | Auto-imported math | `=PY("round(math.sqrt(2), 4)")` | `1.4142` |
| A7 | String return | `=PY("result = 'hello'")` | `hello` |
| A8 | List as text | `=PY("str([1, 2, 3])")` | `[1, 2, 3]` (single cell) |
| A9 | Recalc persists | Edit an unrelated cell, then **F9** | A1 still `2` |
| A10 | Hard recalc | **Ctrl+Shift+F9** | All PY cells still correct |

---

## Packet B — Ranges, pandas, spill (Layer 0 + data shapes)

Put sample data on **Sheet1**:

```
A1: Region    B1: Sales    C1: Units
A2: North     B2: 1200.5   C2: 10
A3: South     B3: 800      C3: 8
A4: North     B4: 1500     C4: 12
A5: East      B5: 400      C5: 5
```

| id | Scenario | Formula | Expected |
|----|----------|---------|----------|
| B1 | Column mean | `=PY("float(np.mean(data))"; B2:B5)` | `975.125` |
| B2 | Header table → pandas | `=PY("df = data.to_pandas(); float(df['Sales'].sum())"; A1:C5)` | `3900.5` |
| B3 | Filter in Python | `=PY("sum(r[1] for r in data[1:] if r[0]=='North')"; A1:C5)` | `2700.5` |
| B4 | Weighted idea (units × sales not needed) | `=PY("float(np.sum(np.asarray(data)))"; C2:C5)` | `35` |
| B5 | Multi-range | `=PY("float(np.mean(data[0])+np.mean(data[1]))"; B2:B5; C2:C5)` | mean(sales)+mean(units) ≈ `983.875` |
| B6 | Auto-spill list | Single cell `=PY("result = [10, 20, 30]")` | Origin + two cells below fill `10,20,30` |
| B7 | Auto-spill DataFrame | `=PY("data.to_pandas()"; A1:C5)` from a **free** cell (e.g. E1) | Header + 4 data rows spill |
| B8 | Spill blocked | Put text in the spill target, re-enter B6 | Origin shows `#SPILL!` |
| B9 | Date parse (opt-in) | Dates in `A1:B4` as ISO strings + `=PY("df=data.to_pandas(date_cols=True); str(df.dtypes.iloc[0])"; A1:B4)` | datetime-like dtype, not object crash |
| B10 | Dependents | `=PY("float(np.mean(data))"; B2:B5)` then change B2 | Mean updates on recalc |

**Do not** combine a data range **and** `ROW()-1` as a third argument (IDL is `(code, data)`; that is a known limitation, not this pass).

---

## Packet B Phase 2 — Ranges, pandas, spill (deepen)

**Prerequisite:** Phase 1 (B1–B10) already green. Do not re-run those rows.  
**Out of scope:** `xl()` / in-code range helpers (separate packet later).  
**Goal:** Spill geometry, dependents, multi-range shape, dates, blocked spill, recalc — still formula-only.  
**Setup:** New Calc book unless noted. Auto-spill on. Isolated mode. Locale argument separator as needed (`;` vs `,`).

Sample grid on **Sheet1** (same spirit as Phase 1):

```
A1: Region    B1: Sales    C1: Units
A2: North     B2: 1200.5   C2: 10
A3: South     B3: 800      C3: 8
A4: North     B4: 1500     C4: 12
A5: East      B5: 400      C5: 5
```

### B2.0 — Multi-range & shape

| id | Scenario | Formula / action | Expected |
|----|----------|------------------|----------|
| B2.0.1 | Two ranges order | `=PY("result = float(np.mean(data[0]) + np.mean(data[1]))"; B2:B5; C2:C5)` | ~`983.875` (sales mean + units mean) |
| B2.0.2 | Swapped args | Same code but ranges `C2:C5; B2:B5` | ~`983.875` still if code uses `data[0]`/`data[1]` correctly — or document that order follows formula args |
| B2.0.3 | Single column as 1D | `=PY("result = len(data)"; B2:B5)` | `4` (not nested length surprise) |
| B2.0.4 | Block stays 2D | `=PY("result = (len(data), len(data[0]))"; A2:C5)` | `(4, 3)` or equivalent row/col counts |

### B2.1 — pandas

| id | Scenario | Formula / action | Expected |
|----|----------|------------------|----------|
| B2.1.1 | Header table | `=PY("df=data.to_pandas(); float(df['Sales'].sum())"; A1:C5)` | `3900.5` |
| B2.1.2 | Filter | `=PY("df=data.to_pandas(); float(df.loc[df['Region']=='North','Sales'].sum())"; A1:C5)` | `2700.5` |
| B2.1.3 | Groupby | `=PY("df=data.to_pandas(); float(df.groupby('Region')['Sales'].sum().loc['North'])"; A1:C5)` | `2700.5` |
| B2.1.4 | date_cols | Sheet with ISO date strings in col A + values in B; `=PY("df=data.to_pandas(date_cols=True); str(df.dtypes.iloc[0])"; A1:B4)` | datetime-like dtype, not crash |

### B2.2 — Spill geometry

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| B2.2.1 | List vertical | Free cell: `=PY("result = [10, 20, 30]")` | Origin + two below: `10, 20, 30` |
| B2.2.2 | 2D list | `=PY("result = [[1,2],[3,4]]")` | 2×2 block from origin |
| B2.2.3 | DataFrame spill | Free cell E1: `=PY("data.to_pandas()"; A1:C5)` | Header + 4 data rows spill |
| B2.2.4 | Spill blocked | Put text in a spill target of B2.2.1; re-enter formula | Origin `#SPILL!` (or documented blocked marker) |
| B2.2.5 | Clear blocker | Clear the blocking cell; recalc | Spill fills again |
| B2.2.6 | Scalar no spill | `=PY("result = 42")` | Single cell only; neighbors untouched |

### B2.3 — Dependents & recalc

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| B2.3.1 | Input edit | `=PY("float(np.mean(data))"; B2:B5)` then change B2 | Mean updates on recalc |
| B2.3.2 | Downstream of PY | A10: PY mean of `B2:B5`; A11: `=A10*2` | A11 tracks A10 |
| B2.3.3 | F9 ×3 | Pure mean formula | Same value each time (idempotent) |
| B2.3.4 | Hard recalc | **Ctrl+Shift+F9** | Values stable; no hang |

### B2.4 — Return types (single cell / spill)

| id | Scenario | Formula | Expected |
|----|----------|---------|----------|
| B2.4.1 | String | `=PY("result = 'hello'")` | `hello` |
| B2.4.2 | Bool | `=PY("result = True")` | `TRUE` / equivalent |
| B2.4.3 | Empty-ish | `=PY("result = None")` | Blank or documented empty — no crash |
| B2.4.4 | List as text | `=PY("str([1,2,3])")` | `[1, 2, 3]` in one cell |

### B2.5 — Soft failures

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| B2.5.1 | Bad column name | `=PY("data.to_pandas()['Nope'].sum()"; A1:C5)` | Error in cell; UI alive |
| B2.5.2 | Empty range | `=PY("float(np.mean(data))"; Z1:Z3)` on empty cells | Error or NaN policy — no LO crash |
| B2.5.3 | Huge spill request | `=PY("result = list(range(5000))")` | Completes, truncates, or clear limit error — no freeze forever |

### Pass rules

- **Pass:** Numbers within 1e-4 rel; spill occupies expected block; `#SPILL!` when blocked; dependents update; no crash/hang.
- **Fail:** Wrong aggregate; spill onto wrong area without blocker logic; LO crash; permanent UI freeze.
- **Note:** Exact `#SPILL!` string vs other blocked marker; `None` display; multi-range `data[i]` order.

---

## Packet C — Shared kernel, init script, Reset (Layer 0 session)

Settings → Python → session mode **shared**. New workbook.

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| C1 | Init helpers | **Edit Initialization Script…**: `def double(x): return x * 2` Save. Cell: `=PY("double(21)")` | `42` |
| C2 | Isolated vs seed | Switch to **isolated**, same init, same formula | Still `42` (init seeds every cell) |
| C3 | Shared leak | Shared mode. A1: `=PY("x = 10")`. B1: `=PY("x + 1"; A1)` (**must pass A1 as data**) | `11` |
| C4 | DAG order | Put C3’s consumer **above** the producer on the sheet; still pass producer as `data` | Still `11` (order via `data`, not row-major) |
| C5 | Reset | **Reset Python Session** (or **Ctrl+Alt+Shift+F9**). Recalc B1 without re-running A1 first | `NameError` / error text for `x`, not stale `11` |
| C6 | Idempotent KPI | `=PY("result = float(np.sum(data))"; B2:B5)` press F9 three times | Same number each time (no growth) |

---

## Packet C Phase 2 — Shared kernel, init, reset (deepen)

**Prerequisite:** Phase 1 (C1–C6) already green. Do not re-run those rows.  
**Goal:** Lifecycle and state bugs Phase 1 did not stress — leftover names, reset boundaries, init changes, multi-cell DAGs, isolated vs shared side-by-side.  
**Settings:** Start each scenario from a new workbook unless noted. Auto-spill on.

### C2.0 — Baseline hygiene

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| C2.0.1 | Fresh shared book | Shared mode; no init; `=PY("result = 1")` | `1` |
| C2.0.2 | No cross-book leak | Book A shared: `=PY("x = 99")`. Book B shared: `=PY("x")` | Book B errors / undefined — not `99` |

### C2.1 — Leftover result / name hygiene (regression class of #388)

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| C2.1.1 | Stale result | Shared. A1: `=PY("result = 10")`. B1: `=PY("result = 20")`. C1: `=PY("result")` with no data dep on A/B | C1 is `20` or clear error — not silently `10` if B ran last; record actual contract |
| C2.1.2 | Consumer ignores prior result | A1: `=PY("result = 5")`. B1: `=PY("result = result + 1"; A1)` (A1 as data) | `6` (or documented: data inject vs name) |
| C2.1.3 | Name without data edge | A1: `=PY("k = 7")`. B1: `=PY("k + 1")` without passing A1 | Fail or stale — must not pretend DAG safety; prefer error or documented shared-read |
| C2.1.4 | After successful cell, unrelated formula | A1 sets `x = 1`. B1: `=PY("result = 100")` (no use of x). Recalc B only | `100`; A1 still ok |

### C2.2 — Reset boundaries

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| C2.2.1 | Reset clears names | Shared; A1: `=PY("x = 42")`; Reset Python Session; B1: `=PY("x")` | Error / undefined, not `42` |
| C2.2.2 | Reset clears result | After C2.1.1 pattern; Reset; recalc a cell that only reads `result` | Not previous result |
| C2.2.3 | Reset does not break init re-seed | Init defines `def double(x): return x*2`; Reset; `=PY("double(3)")` | `6` (init reapplied) |
| C2.2.4 | Ctrl+Alt+Shift+F9 | Same as Reset for shared state | Same as Reset |

### C2.3 — Init script evolution

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| C2.3.1 | Init then cell | Init: `FACTOR = 10`; cell `=PY("result = 3 * FACTOR")` | `30` |
| C2.3.2 | Change init | Change init to `FACTOR = 2`; Reset (or documented re-seed); same formula | `6` |
| C2.3.3 | Init error | Init: `raise RuntimeError("init fail")`; cell `=PY("1+1")` | Clear error path; no LO crash |
| C2.3.4 | Init only helpers | Init defines `double` only; cell does not call it: `=PY("result = 1")` | `1` |

### C2.4 — Multi-cell DAG (explicit data only)

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| C2.4.1 | Chain of three | A1: `=PY("result = 2")`. B1: `=PY("result = data + 3"; A1)`. C1: `=PY("result = data * 4"; B1)` | C1 → `20` |
| C2.4.2 | Consumer above producer | Put consumer row above producer; still pass producer as data | Still correct value (order via `data`, not sheet order) |
| C2.4.3 | Fan-out | A1 producer; B1 and C1 both depend on A1 via data | Both see same A1 value |
| C2.4.4 | Change input | After C2.4.1, change A1 code to `result = 5`; recalc chain | Downstream updates |

### C2.5 — Isolated vs shared side-by-side

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| C2.5.1 | Isolated no leak | Isolated; A1: `=PY("x = 1")`; B1: `=PY("x")` without data | B1 does not see `x` |
| C2.5.2 | Switch mode | Shared book with `x` set; switch to Isolated; new cell reads `x` without data | No shared `x` (or after Reset — record required step) |
| C2.5.3 | Init in both modes | Same init `double`; Isolated cell `=PY("double(4)")` | `8` (init seeds isolated too) |

### C2.6 — Idempotence & recalc storms

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| C2.6.1 | Pure function stable | `=PY("result = float(np.sum(data))"; B2:B5)`; F9 ×5 | Same number every time |
| C2.6.2 | Shared append hazard | Shared; `=PY("result = globals().setdefault('n', 0) or 0; n += 1; result = n")` or equivalent counter | Document behavior; prefer note if F9 grows (known footgun) — fail only if crash |
| C2.6.3 | Hard recalc | Ctrl+Shift+F9 on chain from C2.4.1 | Final values correct; no hang |

### C2.7 — Soft failures (no LO death)

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| C2.7.1 | NameError in shared | `=PY("not_defined + 1")` | Error text in cell / diagnostics; UI alive |
| C2.7.2 | Timeout under shared | Timeout 2s; `=PY("import time; time.sleep(10)")` | Timeout message; session still usable after for a simple `=PY("1+1")` |

### Pass rules

- **Pass:** Values match; Reset clears shared names; init re-seeds after Reset; explicit data chains work regardless of sheet order.
- **Fail:** Cross-book leak; Reset leaves `x`/`result`; LO crash; hang past timeout; isolated sees shared names.
- **Note (not auto-fail):** Whether bare `=PY("x")` reads shared names without a data edge — record the product rule; prefer explicit data in docs either way.

---

## Packet D — Showcase workbook (real dashboard)

Open [`python_showcase_demo.xlsx`](../tests/fixtures/python_showcase_demo.xlsx). **Ctrl+Shift+F9**. Check live KPI / metric cells, not the static labels.

**Use the `.xlsx` only.** The matching `.ods` from `generate_pretty_demo_spreadsheet.py` is currently buggy; do not report ODS mismatches as product failures. (XLSX import may show `=PYTHON()` / `=py()` casing — that is expected; recalc should still hit the add-in.)

Source of formulas: [python-in-calc-showcase.md](python-in-calc-showcase.md) and `scripts/generate_pretty_demo_spreadsheet.py`.

| id | Sheet | What to check | Expected (from docs / generator) |
|----|-------|---------------|----------------------------------|
| D1 | Overview | Total Revenue KPI | `$119,142.00` (or `119142`) |
| D2 | Overview | Avg Profit Margin | `28.4%` |
| D3 | Overview | Anomalies Flagged | `2 Detected` (or `2`) |
| D4 | Sales_Analytics | Enterprise revenue `=PY` | `81497.5` (matches filter on `Customer Type == Enterprise`) |
| D5 | Sales_Analytics | Top SKU by revenue | `FURN-3388` (non-empty SKU code) |
| D5a | Sales_Analytics | High-value threshold (mean plus 2 stdev) | `9711.89` |
| D5b | Sales_Analytics | High value orders above that threshold | `2` |
| D6 | Statistics_ML | Pearson r Ad Spend vs Revenue | ~`0.7978` |
| D7 | Statistics_ML | OLS slope | ~`5.07` |
| D8 | Statistics_ML | Top ROI channel | `Email Marketing` (one of Search Ads / Social Media / Email Marketing) |
| D9 | Forecasting | CAGR string | `46.3%` (percentage like `x.x%`) |
| D10 | Forecasting | Peak historical sales | `303.5` (max of volume column) |
| D11 | Optimization | Lowest-vol asset | `Treasury_Bonds` (one of the four names) |
| D12 | Engineering_Math | kW→hp, PSI→bar, °C→°F, km/h→m/s | `201.15`, `151.68`, `185.0`, `33.33` (sensible converted numbers) |
| D13 | Engineering_Math | derivative / erf cells | `7.5824`, `0.7468` (finite numbers, not errors) |
| D14 | Viz_Gallery | Four `=PY(plt…)` cells | Four GraphicObjectShape plots anchored near those cells |

If KPIs are `#VALUE!` but Packet A passed, suspect locale separators or add-in namespace; LibrePy must still resolve `ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION` ([`tests/scripts/test_librepy_calc_addin_namespace.py`](../tests/scripts/test_librepy_calc_addin_namespace.py)).

---

## Packet E — Domain demo ODS (trusted helpers via formula)

Open [`numpy_domains_demo.ods`](../tests/fixtures/numpy_domains_demo.ods). Follow [the README](../tests/fixtures/numpy_domains_demo.README.md): **Ctrl+Shift+F9**, compare `python_formula` vs `expected_scalar`.

**Skip** `chat_prompt` cells (WriterAgent). **Skip** `goal_seek_solver` chat block. **Quant** helpers that `requires_network` (`fetch_historical_data`): skip unless the agent has network + `yfinance`.

Cases are defined in [`tests/calc/numpy_domains_demo_cases.py`](../tests/calc/numpy_domains_demo_cases.py). Treat each `DomainDemoCase.id` as a row:

### E-analysis (14)

`describe_data`, `kpi_summary`, `detect_outliers`, `quick_stats`, `format_currency`, `format_percent`, `clean_and_prepare`, `pivot_aggregate`, `group_summary`, `compare_periods`, `correlation_matrix`, `run_regression`, `cluster_numeric`, `monte_carlo`

Needs: numpy pandas scipy sklearn statsmodels; `describe_data` also fg-data-profiling; `monte_carlo` also pandas-montecarlo. If a helper returns `MISSING_PACKAGE`, record that as **blocked**, not fail.

### E-forecast (3)

`forecast_time_series`, `decompose_time_series`, `anomaly_detection_time_series` (spike month should be flagged)

### E-viz (formula + visual)

`quick_plot`, `correlation_heatmap`, `time_series_plot` — `check_mode: visual`: image on sheet. Optional `matplotlib_multi_figure` block.

### E-math (4)

`solve_equation`, `symbolic_simplify`, `integrate`, `differentiate` — scalar matches `expected_scalar`.

### E-optimize (3)

`linear_programming`, `optimize_portfolio`, `solve_scheduling_problem`

### E-units (formula if present on sheet)

`convert_quantity`, `parse_quantity` — formatted cell like `36 km/h`.

### E-quant (optional)

`technical_analysis` on OHLCV grid (no network). `portfolio_tearsheet`, `efficient_frontier` if packages present. `fetch_historical_data` only with network.

---

## Packet F — Run Python Script menus (Layers 2–3)

LibrePy surface: **Tools / Python menus → Run Python Script…** (not chat). Use **domain helper picker**, not freeform unless noted.

For each row: select the **input range** on the demo ODS (or Packet B sample), open the named helper, **Run**, confirm table/image/text lands in the document.

| id | Menu section | Helper | Input | Pass |
|----|--------------|--------|-------|------|
| F1 | Analysis Helpers | `[Analysis] kpi_summary` | Sales grid | KPI table inserted |
| F2 | Analysis Helpers | `[Analysis] detect_outliers` | `OUTLIER_GRID` (100 is the outlier) | Flags 100 |
| F3 | Analysis Helpers | `[Analysis] run_regression` | x/y 1→2,2→4,… | Slope ~2 |
| F4 | Viz Helpers | `[Viz] quick_plot` | Sales grid | Chart image on sheet |
| F5 | Viz Helpers | `[Viz] correlation_heatmap` | 3-col numeric | Heatmap image |
| F6 | Forecast Helpers | `[Forecast] forecast_time_series` | 36-month grid | Forecast table (and optional plot) |
| F7 | Forecast Helpers | `[Forecast] anomaly_detection_time_series` | anomaly grid | Spike flagged |
| F8 | Math Helpers | `[Math] solve_equation` | template defaults | Solution text/table |
| F9 | Units Helpers | `[Units] convert_quantity` | `10, "m/s", "km/h"` | `36 km/h` at selection |
| F10 | Optimize Helpers | `[Optimize] linear_programming` | LP grid from demo | Feasible solution table |
| F11 | Calc undo | After F1, **Ctrl+Z** | Inserted table gone in one undo |
| F12 | Writer RPS | Open Writer, Units or Math helper | Formatted string / math-related insert, no crash |
| F13 | Text Analytics (opt) | `[Text] readability` on a Writer paragraph | Scores table if spaCy present |

Quant RPS (optional, same as E-quant).

---

## Packet G — Matplotlib from `=PY()` (expanded)

**Goal:** Prove plots from formula cells land correctly, stay stable on recalc, and respect sheet affinity.  
**Needs:** `matplotlib` in venv (`seaborn` optional).  
**Setup:** New Calc workbook unless noted. Auto-spill on. Session Isolated unless a row says Shared.

### G0 — Smoke

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| G0.1 | Minimal | `=PY("plt.plot([1,2,3])")` | Graphic on/near formula cell; cell not `#VALUE!` |
| G0.2 | Explicit result still works | `=PY("plt.plot([1,2,3]); result = 1")` | Plot and cell value `1` (or document current contract if plot-only wins) |
| G0.3 | Auto-import | No `import matplotlib.pyplot as plt` in code | Still plots (policy: `plt` preloaded) |

### G1 — Sheet affinity (regression for #385)

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| G1.1 | Formula not on active sheet | Create Sheet2; put `=PY("plt.plot([1,2,3])")` on Sheet2; leave Sheet1 active; recalc | Plot appears on Sheet2, not Sheet1 |
| G1.2 | Two sheets, two plots | Sheet1 `=PY("plt.plot([1,2])")`; Sheet2 `=PY("plt.plot([3,4,5])")`; hard recalc | One plot per sheet, correct sheet each |
| G1.3 | After sheet rename | Plot on “Data”; rename sheet to “Sales”; F9 | Plot still on that sheet (no orphan / wrong sheet) |

### G2 — Data + chart content

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| G2.1 | From range | Packet B–style sales A1:C5; `=PY("plt.plot([r[1] for r in data[1:]]); plt.title('Sales')"; A1:C5)` | Line chart; title visible or in image |
| G2.2 | pandas path | `=PY("df=data.to_pandas(); plt.plot(df['Sales']); plt.title('Sales')"; A1:C5)` | Chart from column |
| G2.3 | Bar chart | `=PY("plt.bar(['A','B','C'],[1,3,2]); plt.title('Bars')")` | Bar image, not empty axes only |
| G2.4 | Labels | `=PY("plt.plot([1,2,3]); plt.xlabel('x'); plt.ylabel('y'); plt.title('T')")` | Labeled chart image |

### G3 — Lifecycle / recalc

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| G3.1 | Double F9 | G0.1, F9, F9 | No crash; still a sensible graphic (note if duplicates accumulate) |
| G3.2 | Ctrl+Shift+F9 | Hard recalc whole book | Plots still present / refreshed; no hang |
| G3.3 | Edit code | Change `[1,2,3]` → `[1,2,3,4]`, confirm | Chart updates (or old replaced — note behavior) |
| G3.4 | Undo | After insert, **Ctrl+Z** | Graphic removed or formula undo consistent (document actual) |

### G4 — Multi-figure & size

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| G4.1 | Two figures one cell | `=PY("plt.figure(); plt.plot([1,2]); plt.figure(); plt.plot([3,4])")` | One stacked/merged image or documented multi-image behavior; no crash |
| G4.2 | Larger series | `=PY("plt.plot(list(range(200)))")` | Image appears in reasonable time; UI not frozen forever |

### G5 — Errors that must stay soft

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| G5.1 | Bad plot call | `=PY("plt.plot(None)")` | Readable error in cell or diagnostics; no LO crash |
| G5.2 | No display backend issues | Headless-style Agg path (default) | Still produces image bytes; no GUI backend popup |

### G6 — Optional (seaborn / shared)

| id | Scenario | Action | Expected |
|----|----------|--------|----------|
| G6.1 | seaborn | If present: `=PY("import seaborn as sns; sns.heatmap([[1,2],[3,4]])")` | Heatmap image; else blocked |
| G6.2 | Shared kernel | Shared mode; A1 sets data in Python; B1 plots using name and passes needed data if required by contract | Plot works or clear error — no silent wrong sheet |

### Pass / fail rules (same as plan)

- **Pass:** GraphicObject (or equivalent) on the formula’s sheet, near the cell; no crash/hang.
- **Fail:** Plot on wrong sheet, LO crash, permanent UI freeze, `#VALUE!` with no image when plot was expected.
- **Note (not fail):** Duplicate images on repeated F9 — record actual behavior for a later polish issue.

### Suggested agent instructions

- Prefer new blank Calc + small constructed ranges over the full showcase workbook.
- For G1, explicitly switch active sheet away from the formula sheet before recalc.
- Screenshot or note sheet name of the graphic’s anchor if the tool can see it.
- Do not open chat / `=PROMPT()` / converter menus.

---

## Packet H — Monaco / Edit Python in Cell (Layer 4)

Needs `pywebview` in the venv. If missing: **Run Python Script** should fall back to the native dialog (H-fallback). **Edit Python in Cell** should **not** silently use LO embedded Python.

| id | Scenario | Pass |
|----|----------|------|
| H1 | Select a `=PY` cell → **Edit Python in Cell…** (or **Ctrl+Alt+Shift+P**) | Monaco (or documented failure) with the code |
| H2 | Change `1 + 1` → `1 + 2`, Save | Cell formula updates and value is `3` |
| H3 | **Run Python Script…** with `result = 2 + 2` | Inserts `4` / table |
| H4 | Document-attached script: Save under **This Document**, close/reopen file | Script still in picker |
| H5 | LibrePy Python **sidebar** (Calc deck) | Lists PY cells; diagnostics show stdout if you `print()` in a cell (cell value still from `result`) |

---

## Packet I — Vision / OCR (Layer 5, optional)

Skip if Vision packages missing (record **blocked**).

| id | Scenario | Pass |
|----|----------|------|
| I1 | Insert a PNG of a simple table into Writer or Calc | Graphic selected |
| I2 | Run Python Script → **Vision Helpers** OCR | Text/table extracted into doc |
| I3 | Settings → Python **Vision Libraries** Test | OCR group Present |

---

## Packet J — TeX / Math (Layer 6)

| id | Scenario | Pass |
|----|----------|------|
| J1 | Writer: **Insert LaTeX Math…** with `E = mc^2` | Native Math object, not raw LaTeX dump |
| J2 | RPS Math `latex_to_math_object` if exposed | Valid Math insert |

---

## Packet K — Settings / worker health

| id | Scenario | Pass |
|----|----------|------|
| K1 | Empty venv path: `=PY("1+1")` | Works on embedded Python (stdlib) |
| K2 | Empty path: `=PY("float(np.mean([1,2]))")` | Clear missing-numpy / import error, **not** LO crash |
| K3 | Point at a good venv again | NumPy formula works without restart if documented; otherwise after restart |
| K4 | Timeout: set timeout to `1`, `=PY("import time; time.sleep(5)")` | Timeout message in cell, UI not frozen forever |
| K5 | LibrePy weekly update check does not throw at startup | Log clean enough |

---

## Packet L — Stay on the LibrePy *surface* (sandbox)

Not a packaging checklist. If WriterAgent is installed, chat / `=PROMPT()` / converter menus may exist — **do not open them** for this plan.

| id | Check | Pass |
|----|-------|------|
| L1 | `=PY("import os; os.getcwd()")` | Sandbox **blocks** `os` (error in cell, not a path) |

---

## Suggested farm-out batches

| Agent | Packets | Time-ish | Venv extras |
|-------|---------|----------|-------------|
| 1 | P0 + A + B | 20–40 min | numpy pandas |
| 2 | P0 + C | 15–25 min | numpy |
| 3 | P0 + D | 20–40 min | numpy pandas scipy matplotlib |
| 4 | P0 + E-analysis + E-forecast | 30–45 min | analysis + statsmodels stack |
| 5 | P0 + E-viz + G | 20 min | matplotlib seaborn |
| 6 | P0 + E-math + E-units + J | 20 min | sympy pint |
| 7 | P0 + E-optimize + F (subset) | 30 min | scipy |
| 8 | P0 + H + K + L | 25 min | pywebview |
| 9 | P0 + E-quant + F quant | optional | yfinance stack + network |
| 10 | P0 + I | optional | docling/paddle |

Agents 4–7 can share one running LO if they use **separate workbooks** and do not Reset Session on each other.

---

## Mapping to layers (split doc)

| Layer | Packets |
|-------|---------|
| 0 `=PY()` | A, B, C, D, G, K |
| 1 Trusted RPC | E (formula path), F |
| 2 Run Python Script | F, H3, H4 |
| 3 Domain helpers | E, F |
| 4 Monaco / sidebar | H |
| 5 Vision | I |
| 6 TeX | J |
| Sandbox (LibrePy surface, not packaging) | L |

---

## After this pass (not now)

- Lexer/quoting, `#SPILL!` geometry, jagged ranges, NaN vs blank tables ([../calc/py-data-shapes.md](../calc/py-data-shapes.md)).
- Matrix `ROW()-1` fast path UNO tests (`test_prompt_function_matrix_uno.py`).
- Excel `.xlsx` round-trip (`PythonExcelSamples/`) — WriterAgent converter, not LibrePy.
- Jail-safe Online (`compute_service/`, [../scripting/numpy-jailsafe.md](../scripting/numpy-jailsafe.md)).
- Turn passing live cases into UNO `@native_test` only where mocks already lie.

---

## Result template (copy per agent)

```
packet: A
librepy_version:
lo_version:
venv:
id	result	actual	notes
A1	pass	2
A2	...
```
