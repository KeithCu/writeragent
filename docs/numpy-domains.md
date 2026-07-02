# NumPy Domains — Trusted Scientific Helpers

Back to [Enabling NumPy & Python in LibreOffice](enabling_numpy_in_libreoffice.md) (core venv bridge, `=PYTHON()`, architecture, sandbox).

WriterAgent builds **domain-specific trusted helpers** on top of the warm venv subprocess: fixed host stubs call reviewed modules under `plugin/scripting/` (and related packages) with the full scientific stack — no AST sandbox inside those modules. This document covers Analysis, Visualization, Symbolic Math, Units, Text Analytics, and planned domains (Forecasting, Optimization, Geospatial, Audio).

**Related:** [Analysis Sub-Agent](analysis-sub-agent.md) · [Image Recognition](image-recognition.md) (Vision) · [Embeddings](embeddings.md) · [DuckDB Calc](duckdb-calc-dev-plan.md) · [SageMath (deferred)](sagemath-integration-dev-plan.md) · [Venv IPC & serialization](numpy-serialization.md)

---

## Table of contents

1. [Venv packages by domain](#venv-packages-by-domain)
2. [Scientific domain roadmap (trusted helpers)](#scientific-domain-roadmap-trusted-helpers)
   - [Domain helper pattern](#domain-helper-pattern-analysis--vision-canonical)
   - [Visualization & Plotting](#visualization)
   - [Time Series & Forecasting](#forecasting)
   - [Symbolic Mathematics](#symbolic-math)
   - [Data Engineering / Units (Pint)](#data-engineering-units)
   - [Text / Document Analytics](#text-analytics)
   - [Optimization & OR](#optimization)
   - [Geospatial](#geospatial)
   - [Audio / Signal Processing](#audio-signal)
   - [Implementation phasing](#implementation-phasing-cross-domain)

---

## Venv packages by domain {#venv-packages-by-domain}

Trusted helpers require packages in the user venv (`scripting.python_venv_path`). Settings → Python **Test** reports Present/Missing per group.

### Required venv packages (trusted analysis helpers)

The 14 Calc **Analysis Helpers** in [`plugin/scripting/analysis.py`](../plugin/scripting/analysis.py) require a fixed scientific stack in the user venv. Settings → Python **Test** reports these under **Data Analysis / EDA Libraries** and prints an install line when any are missing.

```bash
uv pip install numpy pandas scipy scikit-learn statsmodels ydata-profiling pandas-montecarlo
```

| Package | Used by |
|---------|---------|
| `numpy`, `pandas` | All helpers (coercion, tables, aggregates) |
| `scipy` | `detect_outliers` (IQR, z-score) |
| `scikit-learn` | `detect_outliers` (`isolation_forest`), `cluster_numeric` |
| [ydata-profiling](https://github.com/ydataai/ydata-profiling) (`data_profiling`) | `describe_data` |
| `statsmodels` | `run_regression` |
| [pandas-montecarlo](https://github.com/ranaroussi/pandas-montecarlo) | `monte_carlo` |

Helpers that need a missing package return `MISSING_PACKAGE` with the install line above — there is no in-code fallback to alternate libraries. See [Analysis Sub-Agent](analysis-sub-agent.md).

### Demo workbook (all NumPy domains)

Manual QA fixture covering Analysis, Viz, Math, Quant, Optimize, Units, and Goal Seek/Solver: [`tests/fixtures/numpy_domains_demo.ods`](../tests/fixtures/numpy_domains_demo.ods) ([`numpy_domains_demo.README.md`](../tests/fixtures/numpy_domains_demo.README.md)). Native ODS preserves uppercase `=PYTHON()` (LibreOffice lowercases custom add-ins when importing XLSX). One sheet per domain with sample data, `=PYTHON()` scalar checks where applicable, Run Python Script picker hints, and chat prompts for tools that expose a Calc chat surface. Regenerate from repo root:

```bash
python scripts/generate_numpy_domains_demo_spreadsheet.py
```

Case definitions: [`tests/calc/numpy_domains_demo_cases.py`](../tests/calc/numpy_domains_demo_cases.py).

### Planned domain package groups {#planned-domain-package-groups}

Future trusted-helper domains (Forecasting, Text Analytics, Optimization, Geospatial, Audio) will each declare required venv packages and a Settings → Python **Test** group when implemented. **Shipped today:**

| Domain | Settings → Python **Test** group | Entry doc |
|--------|----------------------------------|-----------|
| **Vision** | **Vision Libraries** (`docling`, `rapidocr`, `paddleocr`, `paddle`, `ultralytics`, optional `skimage`) | [image-recognition.md](image-recognition.md) |
| **Embeddings** | **Embeddings Libraries** (`envwrap`, `sentence_transformers`, `sqlite_vec`, `langgraph`, `langchain_core`, `langchain_text_splitters`) | [embeddings.md](embeddings.md#embeddings-venv-packages) |
| **Visualization** | **Visualization Libraries** (`matplotlib`, `seaborn`) | [Visualization § Phase A–C](#visualization) |
| **Symbolic Math (SymPy)** | **Computer Algebra** (`sympy`) | [Symbolic Math §3](#symbolic-math) |

SageMath remains a future optional extension — [sagemath-integration-dev-plan.md](sagemath-integration-dev-plan.md).

---

## Scientific domain roadmap (trusted helpers) {#scientific-domain-roadmap-trusted-helpers}

The sections below are **roadmaps and reference** for scientific capabilities. **Shipped domains:** **Analysis** ([analysis-sub-agent.md](analysis-sub-agent.md)), **Vision** ([image-recognition.md](image-recognition.md)), **Visualization** ([§1](#visualization)), and **Symbolic Math (SymPy)** ([§3](#symbolic-math)). DuckDB SQL helpers (up to Phase C: multi-table catalog with named ranges + folder files) are implemented under the same trusted + Run Python Script + analysis-domain pattern; see [duckdb-calc-dev-plan.md](duckdb-calc-dev-plan.md). Remaining domains (Forecasting, Text Analytics, Optimization, Geospatial, Audio) follow the same pattern: trusted modules under `plugin/scripting/`, fixed venv stubs, host extract → IPC → compact results → document egress, plus optional Run Python Script templates and specialized sub-agent exposure.

### Domain helper pattern (Analysis + Vision canonical)

Shipped domains prove the stack. New domains should mirror them—not invent parallel plumbing.

| Layer | Analysis | Vision | Viz | Symbolic (SymPy) | Units (Pint) | Planned |
|-------|----------|--------|-----|------------------|--------------|---------|
| Trusted module | [`analysis.py`](../plugin/scripting/analysis.py) | [`vision.py`](../plugin/vision/venv/vision.py) | [`viz.py`](../plugin/scripting/viz.py) | [`symbolic.py`](../plugin/scripting/symbolic.py) | [`units.py`](../plugin/scripting/units.py) | `forecast.py`, `text_analytics.py`, … |
| Templates | `# writeragent:analysis` | `# writeragent:vision` | `# writeragent:viz` | `# writeragent:math` | `# writeragent:units` | `# writeragent:forecast`, … |
| Host client | [`client.py`](../plugin/scripting/client.py) `run_analysis` | [`client.py`](../plugin/scripting/client.py) `run_vision` | [`client.py`](../plugin/scripting/client.py) `run_viz` | [`client.py`](../plugin/scripting/client.py) `run_symbolic` | [`client.py`](../plugin/scripting/client.py) `run_units` | Same RPC shape |
| Runner / egress | [`analysis_runner.py`](../plugin/calc/analysis_runner.py), [`analysis_egress.py`](../plugin/calc/analysis_egress.py) | [`vision_runner.py`](../plugin/vision/vision_runner.py), [`vision_egress.py`](../plugin/vision/vision_egress.py) | egress in [`viz.py`](../plugin/scripting/viz.py) | egress in [`symbolic.py`](../plugin/scripting/symbolic.py) | egress in [`units.py`](../plugin/scripting/units.py) | Per domain |
| Run Python Script | `_analysis_script_section` | `_vision_script_section` | `_viz_script_section` | `_math_script_section` | `_units_script_section` | [`document_scripts.py`](../plugin/scripting/document_scripts.py) |
| Fast path order | — | 1st | 2nd | 3rd | 4th (units) | [`python_runner.py`](../plugin/scripting/python_runner.py): vision → viz → math → **units** → quant → … |
| Settings Test | **Data Analysis / EDA** | **Vision Libraries** | **Visualization Libraries** | **Computer Algebra** | **Data Engineering Libraries** | Per domain when shipped |
| LLM surface | Calc `domain="analysis"` — [`analyze_data`](../plugin/calc/analysis.py), [`plot_data`](../plugin/calc/viz.py) | `analyze_image` deferred | `plot_data` (analysis); raw matplotlib via `run_venv_python_script` | `domain="python"` — [`symbolic_math`](../plugin/calc/symbolic_math.py) | Run Python Script **Units Helpers** only | Extend analysis or add domains |

```mermaid
flowchart TD
  user[User_or_LLM] --> picker[RunPythonScript_or_delegate]
  picker --> host[Host_UNO_extract]
  host --> stub[Fixed_venv_stub]
  stub --> trusted[Trusted_module_full_stack]
  trusted --> result[Compact_JSON_or_image]
  result --> egress[Calc_Writer_egress]
```

**Dual access model:** Prefer high-level `run_*({helper, params}, data, context)` (or domain-specific inputs like vision's `image`). Keep `run_venv_python_script` / `=PYTHON()` as the escape hatch for novel work. Return `MISSING_PACKAGE` when required venv packages are absent; optional pure-Python or ASCII fallbacks per domain.

**Data handoff:** Reuse [`calc_addin_data.py`](../plugin/calc/calc_addin_data.py) and [`payload_codec`](../plugin/scripting/payload_codec.py) split-grid. For LLM/sub-agent paths, pass **`data_range`** (late binding) rather than full grids in chat context — see [Analysis Sub-Agent — Data Handoff](analysis-sub-agent.md#data-handoff--context-limits-out-of-band-data).

**Visualization note:** Phase A uses the venv worker and `__wa_payload__: "image"` envelope for raw matplotlib (no trusted module required). **Phases B–C shipped:** Run Python Script image egress and trusted Viz helpers (`viz.py`, `[Viz]` templates, `plot_data`, analysis auto-plot).

### New Domain Proposals

We are actively expanding the set of supported scientific libraries. These packages are not part of the standard LLM sandbox and must be accessed via trusted extension modules.

| Domain | Packages | Implementation |
|--------|----------|----------------|
| **Data Engineering** | `pint`, `pyarrow` | Trusted module `plugin/scripting/units.py` or `io.py` |
| **NLP** | `langdetect` (grammar Local + embeddings plain-text locale), `spacy` (future) | Venv `langdetect` via [`langdetect_rpc.py`](../plugin/embeddings/venv/langdetect_rpc.py); future `plugin/scripting/nlp.py` for spacy |
| **Bayesian Opt** | `scikit-optimize` | Trusted module `plugin/scripting/optimization.py` |

The implementation should follow the [Domain helper pattern](#domain-helper-pattern-analysis--vision-canonical) using the established RPC stub architecture.

### Prioritization

| Priority | Domain | Status today | First target |
|----------|--------|--------------|--------------|
| 0 | **Analysis** (numeric EDA, regression, clustering, …) | **Shipped** — [analysis-sub-agent.md](analysis-sub-agent.md); Viz auto-plot via [`viz_auto_plot.py`](../plugin/calc/viz_auto_plot.py) | Extend with Forecast hooks |
| 1 | **Visualization & Plotting** | **Shipped** (Phase A–C) | `plot_data`, `[Viz] quick_plot` |
| 2 | **Time Series & Forecasting** | Partial building blocks in analysis | `forecast_time_series` |
| 3 | **Symbolic Mathematics** | **Shipped** (SymPy only; Sage deferred) | `symbolic_math`, `[Math] solve_equation` |
| 4 | **Text / Document Analytics** | spaCy features shipped; `topics` (NMF) added | readability, entities, key_phrases, **topics** (section-aware) |
| 5 | **Optimization & OR** | Partial (scipy, `monte_carlo`) | `optimize_portfolio` |
| 6 | **Geospatial** | Not started | `[Geo] map_data` |
| 7 | **Audio / Signal Processing** | Recording shipped; no librosa analysis | Spectrogram via Viz egress |
| 8 | **Data Engineering** | **Shipped (Pint)** — [`units.py`](../plugin/scripting/units.py), `[Units]` templates; `pyarrow` IO deferred | `convert_quantity`, `parse_quantity` |
| 9 | **NLP** | **Partial** — `langdetect` in embeddings venv (grammar Local + plain-text locale) | `spacy` entity extraction |
| 10 | **Bayesian Opt** | Not started | `skopt` |

---

### 1. Visualization & Plotting {#visualization}

**Status:** **Phase A–C shipped** (raw matplotlib pipeline, Run Python Script image egress, trusted Viz helpers).

**Goal:** Turn analysis results into publication-quality charts inside LibreOffice—Calc sheet graphics or Writer inline images—without requiring the LLM to write matplotlib every time. Highest immediate ROI for demos and shareable workflows.

**Why:** Users respond to visuals. "I generated a professional chart from my spreadsheet in two clicks" is a strong adoption story. Pairs naturally with the analysis sub-agent (auto-plot regression, clusters, Monte Carlo distributions).

#### Phase A — Raw matplotlib pipeline (shipped)

No `viz.py` yet. Matplotlib figures from user/LLM code are captured in the venv and inserted via the existing image envelope.

| Component | Module | Behavior |
|-----------|--------|----------|
| Figure → bytes | [`venv_sandbox.py`](../plugin/scripting/venv_sandbox.py) | `_figure_to_image_payload()` (SVG default); `_capture_open_figures_payload()` merges multiple open figures vertically; `serialize_result()` for returned `Figure`; `Agg` backend; figure cleanup |
| Wire format | [`payload_codec.py`](../plugin/scripting/payload_codec.py), [`image_payload.py`](../plugin/scripting/image_payload.py) | `PAYLOAD_IMAGE`, `is_image_payload()`; shared temp-file helper |
| Calc `=PYTHON()` | [`python_function.py`](../plugin/calc/python/function.py), [`python_image_egress.py`](../plugin/calc/python/image_egress.py) | `insert_image_result_on_sheet()` → `GraphicObjectShape` anchored to active cell |
| Chat / LLM | [`venv_python.py`](../plugin/calc/python/venv.py) | **Calc:** auto-insert on active sheet + `image_path`. **Writer/Draw:** `image_path` → `insert_image` |
| Writer notebook | [`notebook_runner.py`](../plugin/notebook/notebook_runner.py) | Inline image insert (SVG + PNG) on notebook cell run |
| LLM prompts | [`import_policy.py`](../plugin/scripting/import_policy.py) | App-specific `format_matplotlib_plot_hint()` (Calc / Writer / Draw); not in global import policy |
| LLM sandbox | [`sandbox.py`](../plugin/scripting/sandbox.py) | `matplotlib`, `seaborn` whitelisted |
| Settings Test | [`venv_worker.py`](../plugin/scripting/venv_worker.py) | `matplotlib` under **Scientific Libraries**; **Visualization Libraries** group (`matplotlib`, `seaborn`) when Viz helpers are used |
| Tests | [`test_matplotlib_output.py`](../tests/scripting/test_matplotlib_output.py), [`test_python_function.py`](../tests/calc/python/test_function.py), [`test_venv_python_image.py`](../tests/calc/python/test_venv_image.py), [`test_python_runner_viz.py`](../tests/scripting/test_python_runner_viz.py), [`test_viz.py`](../tests/scripting/test_viz.py), [`test_plot_data.py`](../tests/calc/test_plot_data.py) | Codec, sandbox e2e, multi-figure merge, Calc chat insert, RPS fast path, trusted helpers |

**Works today:**

```python
# =PYTHON() — implicit plt.show() or explicit Figure return
import matplotlib.pyplot as plt
plt.plot([1, 2, 3])
```

```text
# Calc chat — one step (plot inserts on active sheet; image_path still returned)
run_venv_python_script(code="… plt.plot(…) …")

# Writer / Draw chat — two steps
1. run_venv_python_script(code="… plt.plot(…) …")
2. insert_image(image_path=<returned path>)
```

**Native LO charts** ([`charts.py`](../plugin/calc/charts.py) — `UpsertChart`, `ListCharts`, …) are a **separate** UNO chart path, not matplotlib. The LLM can already create native Calc/Writer charts from structured data; Viz helpers complement that with statistical plotting (seaborn, heatmaps, distribution plots).

**Known limitations:** No UNO e2e test for full `=PYTHON()` plot insertion (geometry unit-tested with mocks). Multiple open figures are merged into one vertical stack (PNG). Optional polish: [python-in-excel-dev-plan.md Phase 3](python-in-excel-dev-plan.md#phase-3-monaco--calc-editor-ux-in-progress).

#### Phase B — Run Python Script + Writer image egress (shipped)

[`python_runner.py`](../plugin/scripting/python_runner.py) `execute_and_insert_result()` checks `is_viz_result()` / `is_image_payload()` after venv execution and inserts plots via [`viz_egress.py`](../plugin/scripting/viz_egress.py) (Calc → [`insert_image_result_on_sheet`](../plugin/calc/python/image_egress.py); Writer → [`insert_image_at_locator`](../plugin/writer/images/image_tools.py)). Viz header fast path mirrors Analysis/Vision. Tests: [`test_python_runner_viz.py`](../tests/scripting/test_python_runner_viz.py).

#### Phase C — Trusted Viz helpers (shipped)

[`viz.py`](../plugin/scripting/viz.py), [`viz_templates.py`](../plugin/scripting/viz_templates.py), [`viz_client.py`](../plugin/framework/client/viz_client.py), [`viz_runner.py`](../plugin/scripting/viz_runner.py), [`viz_egress.py`](../plugin/scripting/viz_egress.py), `_viz_script_section` in [`document_scripts.py`](../plugin/scripting/document_scripts.py), fast path in `python_runner.py`, [`plot_data`](../plugin/calc/viz.py) analysis-domain tool, and `analyze_data` auto-plot via [`viz_auto_plot.py`](../plugin/calc/viz_auto_plot.py).

| Helper | Purpose | Notes |
|--------|---------|-------|
| `plot_data` | Auto chart from numeric grid + `spec` | Chart-type recommendation, title/legend metadata |
| `correlation_heatmap` | Heatmap | Builds on `correlation_matrix` analysis output |
| `time_series_plot` | Date-indexed line plot | Shared with Forecast domain |
| `quick_plot` | Default Run Python Script template | Phase B egress for insert |

**Run Python Script templates:** **Viz Helpers →** `[Viz] quick_plot`, `[Viz] correlation_heatmap`, `[Viz] time_series`.

**Result contract (draft):** `{status, helper, image: {format, data}, title, legend, chart_type, writer_cleanup_hints}` — image bytes use the same `__wa_payload__: "image"` envelope as Phase A.

**Analysis sub-agent:** After `run_regression`, `cluster_numeric`, `monte_carlo`, or `correlation_matrix`, `analyze_data` can auto-call a matching viz helper when `auto_plot=true` or the task hint mentions charts (see [`viz_auto_plot.py`](../plugin/calc/viz_auto_plot.py)).

**Packages:** `matplotlib` (required); `seaborn` (recommended). Settings → Python **Visualization Libraries** group lists both.

**Fallback:** ASCII mini-charts or compact text tables when matplotlib is missing (`MISSING_PACKAGE`).

**Phase 2+ (deferred):** `create_interactive_chart` — static multi-view export or embedded HTML/JS if LibreOffice egress supports it.

---

### 2. Time Series & Forecasting {#forecasting}

**Status:** **Not shipped** as dedicated helpers. **Partial:** analysis building blocks exist.

**Goal:** Forecast, decompose, and flag anomalies on date-indexed Calc data—natural fit for spreadsheets (finance, ops, sales).

**Why:** Strong Calc synergy; pairs with Visualization for confidence-band plots.

**Already in codebase:**

| Piece | Location |
|-------|----------|
| Period-over-period change | [`compare_periods`](../plugin/scripting/analysis.py) in analysis helpers |
| Outlier detection | [`detect_outliers`](../plugin/scripting/analysis.py) — base for time-series anomalies |
| OLS / statsmodels | [`run_regression`](../plugin/scripting/analysis.py); `statsmodels` in analysis venv install line |
| Range → pandas | [`calc_addin_data.py`](../plugin/calc/calc_addin_data.py), [`analysis_coerce.py`](../plugin/scripting/analysis_coerce.py) |

**Proposed helpers:**

| Helper | Purpose | Key params |
|--------|---------|------------|
| `forecast_time_series` | Forward predictions + intervals | `periods=12`, `model="auto"` (ARIMA/Holt-Winters) |
| `decompose_time_series` | Trend / seasonal / residual | `date_col`, `value_col` |
| `anomaly_detection_time_series` | Series-aware outliers | Extends `detect_outliers` with temporal context |

**Module layout:** `plugin/scripting/forecast.py` (or extend `analysis.py` with forecast helpers in the same `run_analysis` dispatcher—prefer separate module if package deps differ).

**Packages:** `statsmodels` (required, already in analysis stack); optional `prophet` (heavy — optional Test group, `MISSING_PACKAGE` if absent).

**Run Python Script:** **Forecast Helpers →** `[Forecast] forecast_series`, `[Forecast] decompose`.

**Output:** Predictions table (analysis egress pattern) + optional Viz Phase C plot for bands.

**Sub-agent:** Extend `domain="analysis"` — same delegation as EDA/regression.

**Fallback:** Simple moving-average projection in pandas when statsmodels forecasting APIs unavailable.

---

### 3. Symbolic Mathematics & Equation Solving {#symbolic-math}

**Status:** **Shipped (SymPy).** Trusted helpers via [`symbolic.py`](../plugin/scripting/symbolic.py), Run Python Script **Math Helpers**, and `symbolic_math` chat tool (`domain="python"`). SageMath remains a future optional extension — see [sagemath-integration-dev-plan.md](sagemath-integration-dev-plan.md).

**Goal:** Solve, simplify, integrate, and differentiate equations; round-trip LaTeX ↔ LibreOffice Math objects; bridge Writer, Calc `=PYTHON()`, and Vision OCR of handwritten equations.

**Why:** Appeals to students, engineers, researchers; synergizes with Docling/Vision → sympy → Writer Math OLE.

**Shipped helpers:**

| Helper | Purpose |
|--------|---------|
| `solve_equation` | Symbolic solve for variables |
| `symbolic_simplify` / `integrate` / `differentiate` | Core SymPy wrappers |
| `latex_to_math_object` | Validate/normalize LaTeX for Writer Math insert |

**Integration:**

| Piece | Location |
|-------|----------|
| Trusted module | [`symbolic.py`](../plugin/scripting/symbolic.py), [`symbolic_client.py`](../plugin/framework/client/symbolic_client.py) |
| Run Python Script | `# writeragent:math` templates in [`symbolic_templates.py`](../plugin/scripting/symbolic_templates.py), **Math Helpers** in [`document_scripts.py`](../plugin/scripting/document_scripts.py) |
| Writer Math insert | [`symbolic_egress.py`](../plugin/scripting/symbolic_egress.py) → [`math_mml_convert.py`](../plugin/writer/math/math_mml_convert.py) |
| Chat tool | [`symbolic_math`](../plugin/calc/symbolic_math.py) (`domain="python"`) |

**Packages:** `sympy` (required). Settings → Python **Computer Algebra** group lists sympy.

**Run Python Script templates:** **Math Helpers →** `[Math] solve_equation`, `[Math] symbolic_simplify`, `[Math] integrate`.

**Tests:** [`test_symbolic.py`](../tests/scripting/test_symbolic.py), [`test_symbolic_templates.py`](../tests/scripting/test_symbolic_templates.py), [`test_python_runner_symbolic.py`](../tests/scripting/test_python_runner_symbolic.py), [`test_symbolic_tool.py`](../tests/scripting/test_symbolic_tool.py).

**Out of scope (deferred):** SageMath backend, `sage` sandbox whitelist — [sagemath-integration-dev-plan.md](sagemath-integration-dev-plan.md).

---

### 3b. Data Engineering / Units (Pint) {#data-engineering-units}

**Status:** **Shipped (Pint).** Trusted helpers via [`units.py`](../plugin/scripting/units.py) and Run Python Script **Units Helpers**. `pyarrow` / Arrow IO remains deferred.

**Goal:** Convert, parse, format, and dimensionally-check physical quantities inside LibreOffice without requiring the LLM to manage `UnitRegistry()` singletons or serialization.

**Why:** Unit normalization composes with analysis and vision workflows (OCR tables, lab data, engineering spreadsheets). Pint covers compound units (`m/s` → `km/h`) beyond Calc's built-in `CONVERT()` symbol set.

**Shipped helpers:**

| Helper | Purpose |
|--------|---------|
| `convert_quantity` | Convert a value between units |
| `parse_quantity` | Parse a quantity string |
| `format_quantity` | Format magnitude + units for display |
| `check_dimensionality` | Test dimensional compatibility |

**Integration:**

| Piece | Location |
|-------|----------|
| Trusted module | [`units.py`](../plugin/scripting/units.py), [`client.py`](../plugin/scripting/client.py) `run_units` |
| Run Python Script | `# writeragent:units` templates, **Units Helpers** in [`document_scripts.py`](../plugin/scripting/document_scripts.py) |
| Writer / Calc egress | `insert_units_result_into_doc` in [`units.py`](../plugin/scripting/units.py) |

**Packages:** `pint` (required). Settings → Python **Data Engineering Libraries** group lists pint.

**Run Python Script templates:** **Units Helpers →** `[Units] convert_quantity`, `[Units] parse_quantity`, `[Units] check_dimensionality`.

**Calc egress:** By default, `convert_quantity` and `parse_quantity` write a **single formatted cell** at the selection anchor (e.g. `36 km/h`). Writer inserts the same formatted string as plain text. For a debug/report layout, pass `output_style: "detailed"` in template params — this writes a key-value grid (formatted value on the first row, then magnitude/units or compatibility fields). `check_dimensionality` defaults to `detailed`.

```text
# formatted (default for convert/parse) — anchor cell:
36 km/h

# detailed — starting at anchor cell:
36 km/h
Magnitude | 36
Units     | kilometer / hour
```

**Tests:** [`test_units.py`](../tests/scripting/test_units.py), [`test_units_templates.py`](../tests/scripting/test_units_templates.py), [`test_python_runner_units.py`](../tests/scripting/test_python_runner_units.py).

**Out of scope (deferred):** `xl.convert()` Calc-parity wrapper, `pyarrow` / `plugin/scripting/io.py`.

---

### 4. Text / Document Analytics {#text-analytics}

**Status:** High-quality spaCy implementation (multilingual via textdescriptives + spaCy pipelines) plus `topics` (NMF) using scikit-learn. Exposed via modeless dialog (Writer), Run Python Script templates (Writer), direct imports, and Settings Python self-check. No stdlib fallback.

**Goal:** Readability, topic structure, key phrases, sentiment by section, and cross-document comparison for reports and long-form Writer content.

**Why:** Strengthens core Writer use case; overlaps with professional writers, legal, academic users.

**Input sources (host):** Document text (whole or selection) is extracted on the host and sent to the venv worker.

**High-quality spaCy features (multilingual):**

- Readability + descriptive stats via `textdescriptives`
- Entity extraction (NER)
- Key phrases (noun chunks)
- Linguistic profile

**Sentiment by section** (lexicon-based, works on sections extracted from headings)

**Fancier: Topics**

`topics` helper (added in 2026) performs lightweight topic modeling with TF-IDF + NMF from scikit-learn. It is especially useful on whole documents because the host extracts logical sections (using the heading tree) and passes a list of section texts. The result includes:

- Top terms per topic
- (When sections are provided) dominant topic + strength per section

This gives writers an at-a-glance "map" of the major themes and where they appear — exactly the "topic structure" goal.

**Sentiment (by section)**

`sentiment` helper uses `transformers` + a strong multilingual model (default: `cardiffnlp/twitter-xlm-roberta-base-sentiment`, an XLM-RoBERTa model with good cross-lingual performance). When run on the whole document it uses heading-based section extraction and returns both overall sentiment and per-section results. This delivers the "sentiment by section" goal for reports and long-form Writer content across 34 locales.

The old spacytextblob implementation has been removed (it provided only limited multilingual support).

Install hint (CPU wheels recommended for broad compatibility):

    uv pip install transformers torch --index-url https://download.pytorch.org/whl/cpu

Override the model (or engine in future) via the JSON setting `text_analytics_sentiment_model` (see config).

Results are inserted as compact tables and usable from scripts.

**Module:** `plugin/scripting/text_analytics.py` (real spaCy + textdescriptives implementation; runs inside the user venv).

**UI (minimal):** WriterAgent → **Text Analytics...** opens a modeless dialog with buttons for Readability (doc/sel), Entities, Key Phrases, **Topics**, Check Venv, and "Insert report here". All work is done with real spaCy pipelines (or sklearn for topics) in your configured Python venv (Settings → Python).

**Advanced scripting:** Hand-written Run Python Script code may use the header `# writeragent:text helper=...` and call `from writeragent.scripting.text_analytics import run_text_analytics`; results insert as a compact HTML table after the caret/selection.

**Settings → Python Test:** Reports a "Text / NLP Libraries" group (spacy, textdescriptives, transformers). For topics also install scikit-learn. Install hint: `uv pip install spacy textdescriptives transformers torch --index-url https://download.pytorch.org/whl/cpu && python -m spacy download xx_sent_ud_sm`.

**Requirements in the venv:** `spacy` + `textdescriptives` + at least one model for the spaCy features. `transformers` + `torch` (CPU) for the `sentiment` helper (multilingual XLM-RoBERTa default). `scikit-learn` for the `topics` helper. The document's `CharLocale` (if present) is passed to prefer a better model for spaCy.

**Direct use:** From any `run_venv_python_script` or Run Python Script you can `from writeragent.scripting.text_analytics import analyze_text, run_text_analytics`.

**No LLM tool surface** for this helper yet; access via the dialog or by writing/running scripts. The LLM can still reach it via the `python` domain / `run_venv_python_script` or by invoking the trusted helper directly.

---

### 5. Optimization & Operations Research {#optimization}

**Status:** **Partial.** `scipy` optimization shipped; [`monte_carlo`](../plugin/scripting/analysis.py) shipped. *Note: `pulp` and `ortools` integration is deferred to a later phase; current helpers rely on `scipy.optimize`.*

**Goal:** Linear programming, scheduling, portfolio optimization inside Calc—appeals to analysts, supply chain, finance.

**Proposed helpers:**

| `optimize_portfolio` | Mean-variance or constraint-based | `scipy.optimize`, numpy |
| `linear_programming` | LP from spec dict | `scipy.optimize.linprog` (pulp deferred) |
| `solve_scheduling_problem` | Assignment / small IP | `scipy.optimize.linear_sum_assignment` (ortools/pulp deferred) |

**Run Python Script:** **Optimize Helpers →** `[Optimize] portfolio`, `[Optimize] linear_program`.

**Tie-in:** Stochastic optimization with existing `monte_carlo` helper.

**Sub-agent:** Extend `domain="analysis"`.

**Packages:** `scipy` (required).

---

### 6. Geospatial {#geospatial}

**Status:** **Not started** (niche; lower priority unless demand appears).

**Goal:** Static map image + attribute table from location columns in Calc.

**Proposed helper:** `map_data(data_range, …)` → image envelope (same as Viz Phase A/C) + summary table.

**Packages (all optional):** `geopandas`, `folium`, `shapely` — ship only if users request; `MISSING_PACKAGE` otherwise.

**Run Python Script:** **Geo Helpers →** `[Geo] map_data`.

**Egress:** Viz image path + analysis-style table insert.

---

### 7. Audio / Signal Processing {#audio-signal}

**Status:** **Partial.** Voice recording shipped ([audio-architecture.md](audio-architecture.md)); no venv analysis helpers.

**Goal:** Analyze imported audio (including recordings saved from the chat panel): spectrograms, basic features, optional transcription post-processing.

**Synergy:** Recording produces WAV in user workflow; analysis runs in **venv** (librosa), not in embedded LO Python (recording uses vendored `sounddevice` without numpy).

**Proposed helpers:**

| Helper | Purpose |
|--------|---------|
| `analyze_audio` | Duration, RMS, tempo, key features |
| `spectrogram_plot` | Image via Viz envelope |

**Run Python Script:** **Audio Helpers →** `[Audio] analyze`, `[Audio] spectrogram`.

**Packages:** `librosa` (optional Test group); matplotlib for plots.

**Sub-agent:** Writer main or specialized; optional link to STT pipeline in [audio-architecture.md](audio-architecture.md).

---

### Implementation phasing (cross-domain)

| Phase | Scope | Domains |
|-------|--------|---------|
| **0** | Trusted module + 1–2 helpers + Run Python Script section + unit tests | Viz C, Forecast, or Text (one at a time) |
| **0b** | Glue without full trusted module | **Viz Phase B** — `is_image_payload` in Run Python Script |
| **1** | Sub-agent / `analyze_data`-style tools + delegation prompts | Analysis extensions (Viz auto-plot, forecast) |
| **2** | Egress polish, optional caches, Writer cleanup hints | All |

Keep each domain lean: reuse `payload_codec`, split-grid, document-attached scripts + Monaco, and Settings Test reporting—the same surfaces that make Analysis and Vision usable without an LLM.

Shared-kernel **Calc semantics** (reset, recalc, idempotent cells): [enabling_numpy_in_libreoffice.md §6 — Session modes](enabling_numpy_in_libreoffice.md#session-modes-and-recalc-semantics). Worker lifecycle and code hot cache: [numpy-serialization.md — Warm worker](numpy-serialization.md#warm-worker-lifecycle). Trusted-code pattern (generic): [enabling_numpy_in_libreoffice.md §5](enabling_numpy_in_libreoffice.md#trusted-extension-code-in-the-venv).
