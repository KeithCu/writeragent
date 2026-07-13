# Python compute & scientific helpers — LibreOffice core extension split

This document describes what WriterAgent would need to ship if LibreOffice pulled the **Python venv compute bridge** and its **scientific productivity surfaces** out of the main WriterAgent extension into a **stable core extension**, while WriterAgent keeps the fast-moving AI features (chat, tools, MCP, grammar, embeddings, and so on).

It answers two questions:

1. Which **`plugin/framework/`** modules must ship (entire files, even when only one function is used)?
2. What is the **complete file list** for the feature bundle (same whole-file rule)?

For user-facing behavior (`=PYTHON()` / `=PY()`, Run Python Script, domain helpers, Monaco, OCR, TeX), see [Enabling NumPy & Python in LibreOffice](enabling_numpy_in_libreoffice.md). This doc is about **packaging and dependencies**, not tutorials.

For a **prototype OXT** that works standalone and coexists with WriterAgent, see [Prototype extension](#prototype-extension-standalone-core--writeragent-overlay).

---

## Scope boundary

Aligned with [enabling_numpy_in_libreoffice.md](enabling_numpy_in_libreoffice.md) and linked topic docs.

| In core extension | WriterAgent extension only |
|-------------------|---------------------------|
| `=PYTHON()` / `=PY()` Calc add-in + warm venv worker | `=PROMPT()`, chat sidebar, MCP, grammar |
| **Run Python Script…**, document scripts, init script, **Reset Python Session** | Chat tools (`run_venv_python_script`, `analyze_data`, `extract_text_from_image`, …) |
| **Monaco** (Edit Python in Cell…, Run Python Script editor) | Analysis Sub-Agent, tool loop, LLM client |
| **NumPy domain trusted helpers** (Analysis, Viz, Symbolic, Units, Forecast, Optimize, Quant, Text Analytics menu) | Embeddings (`plugin/embeddings/`, folder FTS, hybrid search) |
| **Vision/OCR** (Run Python Script Vision Helpers + settings) | DuckDB SQL (`domain=sql`, spreadsheet SQL helpers) |
| **TeX/Math** (Insert LaTeX Math + Writer HTML math in Run Python Script egress) | Jupyter notebook import (`import_ipynb`) |
| Settings → Python + venv self-check | Calc spreadsheet → Python import (`convert_spreadsheet_to_python`) |

**Confirmed:** Core ships **menu + formula** surfaces only. Chat tool wrappers stay in WriterAgent even when they call the same trusted compute modules.

**Explicit exclusions** (do not register menus, trusted domains, or probe groups for these in a core OXT):

- Embeddings — `plugin/embeddings/`, `WORKER_POOL_EMBEDDINGS`, `embeddings_*` / `embedding` / `langdetect` trusted domains
- DuckDB — `plugin/scripting/venv/duckdb_sql.py`, `SCRIPT_ORIGIN_SQL`, `domain=sql`
- Jupyter — `plugin/notebook/`, `scripting.import_ipynb`
- Spreadsheet import — `plugin/calc/spreadsheet_import/` (proposed), `calc.convert_spreadsheet_to_python`
- Calc-parity `xl` helpers — **not in LibrePy today**; see [Calc-parity `xl` helpers (deferred)](#calc-parity-xl-helpers-deferred-from-librepy) below.

#### Calc-parity `xl` helpers (deferred from LibrePy) {#calc-parity-xl-helpers-deferred-from-librepy}

WriterAgent ships [`plugin/scripting/calc_functions.py`](../plugin/scripting/calc_functions.py) and [`plugin/scripting/venv/calc_functions_*.py`](../plugin/scripting/venv/) — **259** Calc/Excel formula parity helpers auto-imported as **`xl`** in the venv (e.g. `xl.sumif(...)`, `xl.xlookup(...)`). They exist so spreadsheet import can emit compact Python instead of pasting inline `def` blocks per workbook ([calc-spreadsheet-to-python-import.md](calc-spreadsheet-to-python-import.md)). This is **not** the same as Microsoft Python in Excel’s `xl()` range bridge ([comparison](calc-spreadsheet-to-python-import.md#microsoft-xl-vs-writeragent-xl-different-apis-same-name)).

**LibrePy (core OXT) excludes them for now** (~134 KB source; filtered in [`scripts/librepy_bundle_paths.py`](../scripts/librepy_bundle_paths.py) via `LIBREPY_CALC_FUNCTIONS_EXCLUDES`). Rationale:

- **Minimal core first** — Layers 0–6 should prove `=PY()`, Run Python Script, domain helpers, Monaco, Vision, and TeX before adding spreadsheet-conversion surface area.
- **Spreadsheet import is WriterAgent-only** — menu, translator, and chat tool are not in the core bundle; nothing in LibrePy menus calls `xl.*` today.
- **Coverage not fully exercised in core QA** — parity tests live in WriterAgent (`tests/scripting/test_calc_functions.py`); bundling without a core conversion workflow would ship dead weight for most installs.

**What LibrePy still ships:** [`calc_functions_common.py`](../plugin/scripting/calc_functions_common.py) — host-side name frozensets for Analysis, Viz, Forecast, etc. (no NumPy on the LO host).

**Runtime without the library:** `inject_auto_imports` skips `xl` when the module is absent; `=PY()` and Run Python Script work with `np`/`pd` and domain helpers. Only `xl.*` in user scripts or converted formulas would fail.

**Likely re-include later** when spreadsheet conversion moves into core and parity is validated — remove `LIBREPY_CALC_FUNCTIONS_EXCLUDES` from the bundle filter; no refactor required. Power users may also want `xl.*` in `=PY()` cells even before the conversion menu ships; that is a reasonable follow-on once tests and docs catch up.

---

## Why split?

| Concern | Core extension | WriterAgent extension |
|--------|----------------|----------------------|
| Change rate | Low: Calc add-in, subprocess protocol, trusted helpers, Monaco | High: models, tools, UI, MCP |
| Stability | Suitable to ship with LibreOffice core | Third-party / frequent releases |
| Scope | `=PY()`, scientific menus, Monaco, OCR, TeX | Chat, `=PROMPT()`, embeddings, duckdb, jupyter, grammar |

WriterAgent registers **`=PYTHON()`** / **`=PY()`** and **`=PROMPT()`** as **separate UNO components** ([`python_addin.py`](../plugin/calc/python/addin.py), [`prompt_addin.py`](../plugin/calc/prompt_addin.py)). A core split should ship only the Python add-in and **not** register a second competing `PYTHON` implementation.

---

## Feature bundles (layers)

The full core OXT is the **union of all layers** below. Each layer adds whole files; later layers depend on earlier ones.

```mermaid
flowchart TB
  L0["Layer 0: =PYTHON core"]
  L1["Layer 1: Trusted RPC + venv compute"]
  L2["Layer 2: Run Python Script + egress"]
  L3["Layer 3: NumPy domain helpers"]
  L4["Layer 4: Monaco editor"]
  L5["Layer 5: Vision/OCR"]
  L6["Layer 6: TeX/Math import"]
  L0 --> L1 --> L2 --> L3
  L2 --> L4
  L3 --> L5
  L2 --> L6
```

| Layer | Enables | Detail section |
|-------|---------|----------------|
| **0** | `=PYTHON()` / `=PY()` formula | [Layer 0](#layer-0--python-calc-add-in) |
| **1** | Trusted helper RPC (required for layers 3–5) | [Layer 1](#layer-1--trusted-rpc) |
| **2** | Run Python Script…, Reset Session, document scripts | [Layer 2](#layer-2--run-python-script) |
| **3** | Analysis, Viz, Symbolic, Units, Forecast, Optimize, Quant, Text Analytics | [Appendix E](#appendix-e--numpy-domain-trusted-helpers) |
| **4** | Edit Python in Cell…, Monaco Run/Save | [Appendix F](#appendix-f--monaco-editor) |
| **5** | Vision Helpers, Vision OCR Settings | [Appendix G](#appendix-g--visionocr) |
| **6** | Insert LaTeX Math…, HTML math in Writer insert | [Appendix D](#appendix-d--texmath-import) |

---

## Architecture (host vs venv)

LibreOffice’s embedded Python must **not** import NumPy/pandas from arbitrary user installs (ABI mismatch → crash). User Python runs in a **separate venv interpreter** over length-prefixed Pickle5 frames. Trusted helpers use the same warm child via `action: "run_trusted_action"` (no AST sandbox inside reviewed modules).

```mermaid
flowchart TB
  subgraph loHost [LibreOffice host process]
    PY["=PY(code, data?)"]
    RPS[Run Python Script menu]
    PF[python/function.py]
    PR[python_runner.py]
    TR[trusted_rpc.py]
    VW[venv_worker.py]
    CFG[framework.config]
  end
  subgraph child [User venv subprocess]
    WH[venv/worker_harness.py]
    TD[venv/trusted_dispatch.py]
    VS[venv/venv_sandbox.py]
    LPE[local_python_executor]
    DOM[venv/analysis viz symbolic ...]
  end
  PY --> PF --> VW
  RPS --> PR --> VW
  PR --> TR --> VW
  VW --> CFG
  VW -.->|stdin/stdout Pickle5| WH
  WH --> VS --> LPE
  WH --> TD --> DOM
```

**Recalc constraint:** `=PYTHON()` runs **synchronously** during Calc recalc. The implementation deliberately avoids UI event pumping on this path (see comments in [`function.py`](../plugin/calc/python/function.py)) so the formula engine is not re-entered.

**Config keys** (from [`plugin/scripting/module.yaml`](../plugin/scripting/module.yaml)):

| Key | Role |
|-----|------|
| `scripting.python_venv_path` | User venv directory; empty → `sys.executable` (LO embedded Python, stdlib-only unless extras installed there) |
| `scripting.python_session_mode` | `isolated` (default) or `shared` (workbook namespace for `=PY()` cells) |
| `scripting.python_exec_timeout` | Wall-clock seconds per user script run (default 10, clamp 1–600) |
| `scripting.python_auto_spill` | Auto-spill list/DataFrame returns from single-cell `=PY()` |

Stored in **`writeragent.json`** today ([`plugin/framework/config.py`](../plugin/framework/config.py)). A core extension would choose whether to reuse that file, use a dedicated JSON name, or bind LO Tools → Options.

IPC detail: [numpy-serialization.md](numpy-serialization.md).

---

## Two import closures (important)

Python loads **whole modules**. If a file is imported, the entire file ships in the OXT, even if only one function is called.

### 1. Runtime closure — `=PYTHON()` / `=PY()` only (Layer 0)

Call chain:

`PythonFunction.python()` → `execute_python_addin` → `calc_addin_data` / `payload_codec` → `run_code_in_user_venv` → `PythonWorkerManager` → `venv/worker_harness` → `venv/venv_sandbox` → `LocalPythonExecutor`

Error display: `format_error_for_display` → `framework.errors` / `framework.client.errors` / `i18n`.

Config: `get_config_str("scripting.python_venv_path")`, session mode, timeout, auto-spill.

This closure **does not** call the LLM, chat panel, or MCP.

### 2. As-shipped module closure — WriterAgent (split complete)

- **`=PY()`**: register [`python_addin.py`](../plugin/calc/python/addin.py) only → loads [`function.py`](../plugin/calc/python/function.py) (no `LlmClient`).
- **`=PROMPT()`**: register [`prompt_addin.py`](../plugin/calc/prompt_addin.py) → loads [`prompt_function.py`](../plugin/calc/prompt_function.py) (LLM stack).

A core OXT must **not** register `prompt_addin.py` / `prompt_function.py`. See [Recommended refactors](#recommended-refactors-for-libreoffice-maintainers).

---

## Layer 0 — `=PYTHON()` Calc add-in

### Framework files (whole files)

| File | Why it ships |
|------|----------------|
| [`plugin/framework/config.py`](../plugin/framework/config.py) | Config read/write; `WriterAgentConfig` + `MODULES` |
| [`plugin/framework/constants.py`](../plugin/framework/constants.py) | `get_plugin_dir`, `AUTO_IMPORTS`, worker pool ids |
| [`plugin/framework/errors.py`](../plugin/framework/errors.py) | `format_error_payload`, `ConfigError`, `safe_call` |
| [`plugin/framework/json_utils.py`](../plugin/framework/json_utils.py) | `safe_json_loads` (via `client/errors.py`) |
| [`plugin/framework/i18n.py`](../plugin/framework/i18n.py) | `_()` for translated errors |
| [`plugin/framework/event_bus.py`](../plugin/framework/event_bus.py) | `global_event_bus` — imported by `config.py` |
| [`plugin/framework/service.py`](../plugin/framework/service.py) | `ServiceBase` — imported by `event_bus.py` |
| [`plugin/framework/url_utils.py`](../plugin/framework/url_utils.py) | Endpoint normalization — imported by `config.py` |
| [`plugin/framework/thread_guard.py`](../plugin/framework/thread_guard.py) | `background` — imported by `venv_worker.py` |
| [`plugin/framework/client/errors.py`](../plugin/framework/client/errors.py) | `format_error_for_display` in `python()` handler |
| [`plugin/framework/client/__init__.py`](../plugin/framework/client/__init__.py) | Package |
| [`plugin/framework/__init__.py`](../plugin/framework/__init__.py) | Package |
| [`plugin/_manifest.py`](../plugin/_manifest.py) | **Generated** (`make manifest`). `config_limits.py` reads `MODULES` for schema defaults |

**Not required for Layer 0** unless a higher layer pulls them in: `llm_client.py`, `async_stream.py`, `tool.py`, `default_models.py`, `uno_context.py`, `worker_pool.py`, `appearance.py`, …

### Calc

| File | Role |
|------|------|
| [`plugin/calc/python/addin.py`](../plugin/calc/python/addin.py) | UNO add-in: `python()` |
| [`plugin/calc/python/function.py`](../plugin/calc/python/function.py) | `execute_python_addin`, matrix session, spill, `finalize_python_return` |
| [`plugin/calc/addin_common.py`](../plugin/calc/addin_common.py) | Shared add-in helpers |
| [`plugin/calc/calc_addin_data.py`](../plugin/calc/calc_addin_data.py) | Range → `data`, size limits, wire packing; `_resolve_python_data` for Run Python Script / trusted helpers |
| [`plugin/calc/__init__.py`](../plugin/calc/__init__.py) | Package (`CalcError`) |
| [`plugin/calc/prompt_addin.py`](../plugin/calc/prompt_addin.py) | **WriterAgent only** — do not register in core |
| [`plugin/calc/prompt_function.py`](../plugin/calc/prompt_function.py) | **WriterAgent only** |

### Scripting / worker (host)

| File | Role |
|------|------|
| [`plugin/scripting/venv_worker.py`](../plugin/scripting/venv_worker.py) | `run_code_in_user_venv`, `PythonWorkerManager`, warm worker, venv resolution |
| [`plugin/scripting/ipc.py`](../plugin/scripting/ipc.py) | Pickle5 frame read/write |
| [`plugin/scripting/payload_codec.py`](../plugin/scripting/payload_codec.py) | `split_grid` wire codec (host + child) |
| [`plugin/scripting/sandbox.py`](../plugin/scripting/sandbox.py) | `VENV_AUTHORIZED_IMPORTS`, `scrub_subprocess_env`, `resolve_venv_python` |
| [`plugin/scripting/config_limits.py`](../plugin/scripting/config_limits.py) | Timeout defaults/min/max, warm/long-trusted budgets |
| [`plugin/scripting/session_manager.py`](../plugin/scripting/session_manager.py) | Shared-kernel workbook sessions, reset |
| [`plugin/scripting/module.yaml`](../plugin/scripting/module.yaml) | Config schema for Python settings |
| [`plugin/scripting/__init__.py`](../plugin/scripting/__init__.py) | Package |

### Venv child (`plugin/scripting/venv/`)

| File | Role |
|------|------|
| [`plugin/scripting/venv/worker_harness.py`](../plugin/scripting/venv/worker_harness.py) | Child stdin/stdout loop, trusted-action handler |
| [`plugin/scripting/venv/venv_sandbox.py`](../plugin/scripting/venv/venv_sandbox.py) | `LocalPythonExecutor`, user-code sandbox |
| [`plugin/scripting/venv/coerce.py`](../plugin/scripting/venv/coerce.py) | Grid → DataFrame coercion |
| [`plugin/scripting/venv/__init__.py`](../plugin/scripting/venv/__init__.py) | Package |

### Vendored AST sandbox (smolagents subset)

| File | Role |
|------|------|
| [`plugin/contrib/smolagents/local_python_executor.py`](../plugin/contrib/smolagents/local_python_executor.py) | Restricted executor |
| [`plugin/contrib/smolagents/tools.py`](../plugin/contrib/smolagents/tools.py) | `Tool` type required by executor |
| [`plugin/contrib/smolagents/utils.py`](../plugin/contrib/smolagents/utils.py) | `BASE_BUILTIN_MODULES`, helpers |
| [`plugin/contrib/smolagents/agent_types.py`](../plugin/contrib/smolagents/agent_types.py) | Imported by `tools.py` |
| [`plugin/contrib/smolagents/tool_validation.py`](../plugin/contrib/smolagents/tool_validation.py) | Imported by `tools.py` |
| [`plugin/contrib/smolagents/_function_type_hints_utils.py`](../plugin/contrib/smolagents/_function_type_hints_utils.py) | Imported by `tools.py` |
| [`plugin/contrib/smolagents/__init__.py`](../plugin/contrib/smolagents/__init__.py) | Package |

**LibrePy bundle:** [`scripts/build_librepy_oxt.py`](../scripts/build_librepy_oxt.py) replaces `smolagents/__init__.py` with a slim stub (no `agents` import) so the venv worker can load `local_python_executor` without shipping chat-only smolagents modules.
| [`plugin/contrib/__init__.py`](../plugin/contrib/__init__.py) | Package |

### Package root

| File | Role |
|------|------|
| [`plugin/__init__.py`](../plugin/__init__.py) | Package — must use `pkgutil.extend_path` when core and WriterAgent ship different `plugin/` subtrees in separate OXTs ([Prototype extension §2](#2-split-plugin-across-two-oxts-extend_path)) |

---

## Layer 1 — Trusted RPC

Required for NumPy domain helpers, Vision OCR, and any `run_trusted_worker_action` path. Builds on Layer 0.

| File | Role |
|------|------|
| [`plugin/scripting/client.py`](../plugin/scripting/client.py) | `run_*` host stubs, long-trusted timeout list |
| [`plugin/scripting/trusted_rpc.py`](../plugin/scripting/trusted_rpc.py) | `run_trusted_worker_action` |
| [`plugin/scripting/trusted_action_registry.py`](../plugin/scripting/trusted_action_registry.py) | Domain → venv dispatcher wiring |
| [`plugin/scripting/venv/trusted_dispatch.py`](../plugin/scripting/venv/trusted_dispatch.py) | Routes `run_trusted_action` to domain `run_*` |
| [`plugin/scripting/venv/worker_heartbeat.py`](../plugin/scripting/venv/worker_heartbeat.py) | Heartbeat frames (long jobs) |
| [`plugin/scripting/_lazy_venv.py`](../plugin/scripting/_lazy_venv.py) | Lazy `plugin.scripting.*` → `plugin.scripting.venv.*` |
| [`plugin/scripting/helper_domain.py`](../plugin/scripting/helper_domain.py) | Shared helper-domain glue |
| [`plugin/scripting/domain_registry.py`](../plugin/scripting/domain_registry.py) | RPS fast-path, picker origins, post-venv routing |
| [`plugin/scripting/calc_functions_common.py`](../plugin/scripting/calc_functions_common.py) | Helper name frozensets (no numpy on host) |
| [`plugin/scripting/import_policy.py`](../plugin/scripting/import_policy.py) | LLM import-policy text generation |
| [`plugin/scripting/venv_diagnostics.py`](../plugin/scripting/venv_diagnostics.py) | Settings → Python **Test** self-check |

**Core-build note:** Filter [`trusted_action_registry.py`](../plugin/scripting/trusted_action_registry.py) to numpy + vision domains only (`units`, `symbolic`, `math`, `viz`, `analysis`, `forecast`, `optimize`, `quant`, `text`, `vision`). Omit `embeddings_*`, `sql`, `embedding`, `langdetect`, `languagetool`, `vale`, `harper`. The whole [`trusted_dispatch.py`](../plugin/scripting/venv/trusted_dispatch.py) file still ships unless maintainers split it; excluded domains become dead code.

**Trusted RPC flow:**

```mermaid
flowchart LR
  host[Host facade e.g. viz.py]
  client[client.py run_trusted_*]
  rpc[trusted_rpc.py]
  vw[venv_worker.py]
  wh[worker_harness.py]
  td[trusted_dispatch.py]
  compute[venv/analysis.py etc]
  host --> client --> rpc --> vw --> wh --> td --> compute
```

---

## Layer 2 — Run Python Script

WriterAgent exposes **Run Python Script…** ([`extension/Addons.xcu`](../extension/Addons.xcu) → `scripting.run_python_dialog`), **Reset Python Session** (`scripting.reset_python_session`), and **Text Analytics…** (`textanalytics.open_dialog`). Wired from [`plugin/main.py`](../plugin/main.py) or an equivalent core bootstrap job.

Entry: [`run_python_dialog()`](../plugin/scripting/python_runner.py). Reuses [`run_code_in_user_venv`](../plugin/scripting/venv_worker.py). After success, writes into the active document (Writer or Calc). Draw/Impress shows an info message only.

```mermaid
flowchart LR
  menu[Run Python Script menu]
  dlg[Monaco or native dialog]
  venv[run_code_in_user_venv]
  w[Writer: format_result_for_writer]
  insW[insert_content_at_position HTML]
  c[Calc: insert_result_into_calc]
  insC[write_formula_range from selection]
  menu --> dlg --> venv
  venv --> w --> insW
  venv --> c --> insC
```

### Behavior by app

| App | Insertion | Result shaping |
|-----|-----------|----------------|
| **Writer** | HTML at **selection** via [`insert_content_at_position`](../plugin/writer/format.py) | [`format_result_for_writer`](../plugin/scripting/python_runner.py) — lists → tables, dicts → sections |
| **Calc** | Values from **active selection** via [`insert_result_into_calc`](../plugin/scripting/python_runner.py) | Dict title/summary + tables; 1D/2D lists via `write_formula_range` |
| **Draw/Impress** | None (message box) | — |

Config keys: `last_python_script_writer`, `last_python_script_calc`, `last_python_script_draw`.

### Additional files (Layer 2)

| File | Role |
|------|------|
| [`plugin/scripting/python_runner.py`](../plugin/scripting/python_runner.py) | Dialog, run, domain fast-paths, Writer/Calc branch |
| [`plugin/scripting/python_runner_ui.py`](../plugin/scripting/python_runner_ui.py) | Native XDL fallback when Monaco unavailable |
| [`plugin/scripting/document_scripts.py`](../plugin/scripting/document_scripts.py) | Document-attached scripts, init script storage |
| [`plugin/chatbot/dialogs.py`](../plugin/chatbot/dialogs.py) | `add_dialog_*`, `msgbox` |
| [`plugin/framework/uno_context.py`](../plugin/framework/uno_context.py) | `get_ctx`, `get_desktop` |
| [`plugin/framework/worker_pool.py`](../plugin/framework/worker_pool.py) | Via `dialogs.py` |
| [`plugin/framework/appearance.py`](../plugin/framework/appearance.py) | LO light/dark → Monaco theme |
| [`plugin/doc/document_helpers.py`](../plugin/doc/document_helpers.py) | `is_writer` / `is_calc` / `is_draw` — **pulls** `calc.bridge` + `calc.analyzer` at import |
| [`plugin/doc/visual_helpers.py`](../plugin/doc/visual_helpers.py) | Graphic export for Vision egress |
| [`plugin/writer/format.py`](../plugin/writer/format.py) | HTML insert, mixed math segments |
| [`plugin/writer/ops.py`](../plugin/writer/ops.py) | Selection/cursor helpers |
| [`plugin/writer/review_authors.py`](../plugin/writer/review_authors.py) | Via `format.py` |
| [`plugin/writer/xhtml_style_postprocess.py`](../plugin/writer/xhtml_style_postprocess.py) | HTML post-process |
| [`plugin/calc/bridge.py`](../plugin/calc/bridge.py) | Active sheet / document access |
| [`plugin/calc/address_utils.py`](../plugin/calc/address_utils.py) | `index_to_column` for anchor cell |
| [`plugin/calc/manipulator.py`](../plugin/calc/manipulator.py) | `write_formula_range` |
| [`plugin/calc/tabular_egress.py`](../plugin/calc/tabular_egress.py) | Tabular helper results → sheet |
| [`plugin/calc/rich_html.py`](../plugin/calc/rich_html.py) | Rich HTML cell insert (Vision Calc egress) |
| [`plugin/main.py`](../plugin/main.py) | Action handlers — **or** [`plugin/main_core.py`](../plugin/main_core.py) for core-only bootstrap (see [Prototype extension](#prototype-extension-standalone-core--writeragent-overlay)) |

**Refactor note:** `plugin/doc/doc_type.py` with `is_writer`/`is_calc` only would avoid loading `calc.analyzer` for Writer menus.

Optional gettext: filtered catalogs via `make compile-translations-core` (part of `make build-core`) — [`scripts/build_librepy_locales.py`](../scripts/build_librepy_locales.py) extracts strings from the LibrePy file closure only and bundles slim `.mo` files from `build/generated/locales/`, not the full WriterAgent `locales/` tree.

---

## Extension packaging (OXT / registry)

### Calc add-in

| Artifact | Notes |
|----------|--------|
| [`extension/idl/XPythonFunction.idl`](../extension/idl/XPythonFunction.idl) | `python(in string code, in any data)` |
| [`extension/idl/XPromptFunction.idl`](../extension/idl/XPromptFunction.idl) | **WriterAgent only** — `prompt()` |
| [`extension/XPythonFunction.rdb`](../extension/XPythonFunction.rdb), [`XPromptFunction.rdb`](../extension/XPromptFunction.rdb) | Built from IDL — [`scripts/rebuild_xprompt_rdb.sh`](../scripts/rebuild_xprompt_rdb.sh) |
| [`extension/registry/.../CalcAddIns.xcu`](../extension/registry/org/openoffice/Office/CalcAddIns.xcu) | Core: `python` / `PY` node only; no `prompt` |
| [`extension/META-INF/manifest.xml`](../extension/META-INF/manifest.xml) | Filtered UNO entries + Python tree |
| `description.xml` | New extension identifier if not WriterAgent |

**Service:** `com.sun.star.sheet.AddIn`

### Core menus ([`extension/Addons.xcu`](../extension/Addons.xcu))

| Action | Core | WriterAgent only |
|--------|------|------------------|
| `scripting.run_python_dialog` | Yes | |
| `scripting.edit_python_cell` | Yes (Layer 4) | |
| `scripting.reset_python_session` | Yes | |
| `writer.insert_latex_dialog` | Yes (Layer 6) | |
| `vision.open_settings` | Yes (Layer 5) | |
| `textanalytics.open_dialog` | Yes (Layer 3) | |
| `scripting.import_ipynb` | | Yes |
| `calc.convert_spreadsheet_to_python` | | Yes |
| `embeddings.search_dialog` | | Yes |
| Chat / review accelerators | | Yes |

### Generated dialogs (`make manifest`)

From [`scripting/module.yaml`](../plugin/scripting/module.yaml) and [`vision/module.yaml`](../plugin/vision/module.yaml):

- Settings → Python tab (`SettingsDialog.xdl` pages)
- `PythonTestProgressDialog.xdl` (venv Test)
- `PythonScriptDialog.xdl` (native Run Python fallback)
- `LatexInputDialog.xdl` (native LaTeX fallback)
- `VisionSettingsDialog.xdl`
- `MsgBoxWithCopyDialog.xdl`, `ErrorReportDialog.xdl` (as used by dialogs)

Manifest reference: [`scripts/manifest_registry.py`](../scripts/manifest_registry.py).

### Third-party payloads

| Path | Layer | Notes |
|------|-------|-------|
| **`vendor/latex2mathml/`** | 6 | `make vendor` from [`requirements-vendor.txt`](../requirements-vendor.txt); on `sys.path` at bootstrap |
| **`vendor/json_repair/`** | 0–2 | Config read + [`json_utils.py`](../plugin/framework/json_utils.py) robust JSON parse |
| **User venv `rocher`** | 4 | Monaco UI assets — **not** in OXT |
| **User venv scientific stack** | 0–5 | numpy, docling, pywebview, etc. — user-maintained |

**LibrePy vendor subset:** [`build_librepy_oxt.py`](../scripts/build_librepy_oxt.py) copies only **`json_repair`** and **`latex2mathml`** from `vendor/` into `plugin/lib/` ([`LIBREPY_VENDOR_PACKAGES`](../scripts/librepy_bundle_paths.py)). WriterAgent-only vendored packages are omitted from the core OXT:

| Package | WriterAgent use | LibrePy |
|---------|-----------------|---------|
| `snowballstemmer` | Grammar stemming, web-research fluff words | **Excluded** |
| `websockets` | CDP browser tools (`plugin/contrib/cdp/`) | **Excluded** |
| `defusedxml` | Embeddings locale XML (`plugin/embeddings/`) | **Excluded** |

Full `make vendor` still installs all entries in [`requirements-vendor.txt`](../requirements-vendor.txt) for WriterAgent builds.

---

## Build and configuration

| Step | Detail |
|------|--------|
| Manifest | `make manifest` → [`plugin/_manifest.py`](../plugin/_manifest.py) from `module.yaml` files. Core: at least `scripting` + `vision`; **no** `embeddings` |
| Bundle | Same OXT pipeline as WriterAgent, filtered to layer file lists; vendor copy uses [`LIBREPY_VENDOR_PACKAGES`](../scripts/librepy_bundle_paths.py) (`json_repair`, `latex2mathml` only) |
| Config path | Linux: `~/.config/libreoffice/{4,24}/user/writeragent.json` (see AGENTS.md Config) |

---

## WriterAgent-only surfaces

These share the venv worker or trusted RPC with core but **must not ship** in a core OXT (or require the full WriterAgent chat/LLM stack).

### Chat tools (Calc)

| Surface | Entry | Uses venv? | Inserts? |
|---------|--------|------------|---------|
| **Chat `run_venv_python_script`** | [`plugin/calc/python/venv.py`](../plugin/calc/python/venv.py) | Yes | No — JSON to agent |
| **Chat `execute_python_script`** | [`plugin/calc/python/executor.py`](../plugin/calc/python/executor.py) | No (embedded LO Python) | Optional `target_range` |

Additional files: `plugin/calc/base.py`, `plugin/calc/inspector.py`, `plugin/framework/tool.py`, full `plugin/chatbot/*` tool loop.

**Tests:** [`tests/calc/python/test_venv.py`](../tests/calc/python/test_venv.py), [`tests/calc/python/test_executor.py`](../tests/calc/python/test_executor.py)

### Chat tools (domain helpers)

| Tool | Module | Core equivalent |
|------|--------|-----------------|
| `analyze_data` | [`plugin/calc/analysis.py`](../plugin/calc/analysis.py) | Run Python Script → Analysis Helpers |
| `plot_data` | [`plugin/calc/viz.py`](../plugin/calc/viz.py) | Run Python Script → Viz Helpers |
| `forecast_data` | [`plugin/calc/forecast.py`](../plugin/calc/forecast.py) | Run Python Script → Forecast |
| `optimize_data` | [`plugin/calc/optimize.py`](../plugin/calc/optimize.py) | Run Python Script → Optimize |
| `symbolic_math` | [`plugin/calc/symbolic_math.py`](../plugin/calc/symbolic_math.py) | Run Python Script → Math Helpers |
| `extract_text_from_image` | [`plugin/vision/vision_tools.py`](../plugin/vision/vision_tools.py) | Run Python Script → Vision Helpers |

### Writer chat (venv, no menu insert)

[`run_venv_python_script`](../plugin/calc/python/venv.py) on Writer ignores `data_range`; the model inserts via Writer HTML tools, not `python_runner`. Menu-driven Writer insert: [Layer 2](#layer-2--run-python-script).

### Other WriterAgent-only trees

| Area | Examples |
|------|----------|
| LLM / chat | `plugin/framework/client/llm_client.py`, `plugin/chatbot/*`, `=PROMPT()` |
| Embeddings | `plugin/embeddings/`, `WORKER_POOL_EMBEDDINGS` |
| DuckDB | `plugin/scripting/duckdb_sql.py`, SQL picker origin |
| Jupyter | `plugin/notebook/`, `import_ipynb` |
| Spreadsheet import | `calc.convert_spreadsheet_to_python` |
| Grammar | `languagetool`, `vale`, `harper` venv modules |
| MCP / grammar UI | `plugin/mcp/`, `plugin/writer/locale/` |
| Analysis Sub-Agent | [analysis-sub-agent.md](analysis-sub-agent.md) |
| Sidebar audio mic | [`plugin/chatbot/audio_recorder.py`](../plugin/chatbot/audio_recorder.py) — Settings may probe `sounddevice`; capture UI is chat |

```mermaid
flowchart TB
  subgraph corePaths [Core extension]
    PY["=PY()"]
    menu[Run Python Script]
    helpers[Trusted domain helpers]
  end
  subgraph writerAgent [WriterAgent only]
    chat[Chat tool loop]
    toolV[run_venv_python_script]
    toolA[analyze_data etc]
  end
  PY --> helpers
  menu --> helpers
  chat --> toolV --> helpers
  chat --> toolA --> helpers
```

---

## Recommended refactors for LibreOffice maintainers

1. **Split the add-in** — `python_addin.py` with zero imports from `llm_client` or `async_stream`.
2. **Slim configuration** — `python_config.py` with Python keys only instead of full `WriterAgentConfig`.
3. **Narrow IDL** — [`XPythonFunction.idl`](../extension/idl/XPythonFunction.idl) only in core RDB.
4. **Separate extension id** — avoid `org.extension.writeragent.*` if WriterAgent remains distinct.
5. **WriterAgent integration** — remove duplicate `=PY()` registration; declare dependency on core OXT ([Prototype extension](#prototype-extension-standalone-core--writeragent-overlay)).
6. **Slim `trusted_action_registry.py`** — core flavor: numpy + vision domains only.
7. **Slim `venv_diagnostics.py`** — drop Embeddings Libraries probe; optional Audio probe (sidebar is WriterAgent).
8. **Slim `domain_registry.py`** — remove `SCRIPT_ORIGIN_SQL` and embeddings origins.
9. **`doc_type.py`** — replace heavy `document_helpers.py` imports for menu guards.
10. **Split `writer/images/images.py`** — Vision graphic export vs LLM image generation.
11. **Filtered `Addons.xcu` / `main.py`** — core uses `main_core.py`; WriterAgent drops python menu handlers.
12. **`pkgutil.extend_path` in `plugin/__init__.py`** — required for dual-OXT `plugin/` split.
13. **Optional sandbox slimming** — minimal `Tool` stub instead of full smolagents `tools.py` chain.

---

## Coexistence options

| Option | Summary |
|--------|---------|
| **A — Core only** | Core OXT alone: `=PY()`, scientific menus, no chat. Valid install; WriterAgent not required. |
| **B — Core + WriterAgent (recommended)** | Core owns `=PY()` and the scripting stack; WriterAgent is AI-only overlay (`=PROMPT()`, chat, MCP). See [Prototype extension](#prototype-extension-standalone-core--writeragent-overlay). |
| **C — Duplicate (avoid)** | Both extensions register `=PY()` / `PYTHON` or both ship full `plugin/scripting/` → add-in conflict and/or import shadowing |

**Chosen model:** **B** — core is **standalone** (must work with only the core OXT installed). WriterAgent **assumes core is installed** and does **not** register `=PY()` or duplicate scientific menus.

---

## Prototype extension (standalone core + WriterAgent overlay)

How to ship a **prototype core OXT** in this repo that works **by itself** (including `=PY()` / `=PYTHON()`), while WriterAgent becomes an optional AI layer that imports the core Python stack instead of duplicating it.

### Target architecture

```mermaid
flowchart TB
  subgraph coreOXT [Core OXT standalone]
    PY["=PY / =PYTHON"]
    menus[Run Python Script Monaco Vision TeX]
    worker[venv_worker trusted helpers]
    cfg[writeragent.json scripting keys]
  end
  subgraph waOXT [WriterAgent OXT optional]
    chat[Sidebar chat tools]
    prompt["=PROMPT()"]
    mcp[MCP grammar embeddings]
  end
  coreOXT --> PY
  waOXT -->|import plugin.scripting.*| coreOXT
  waOXT -->|no python add-in| coreOXT
```

| Install set | Expected |
|-------------|----------|
| **Core only** | `=PY()` works; Run Python Script, Monaco, domains, Vision, LaTeX; Settings → Python; **no** chat sidebar |
| **Core + WriterAgent** | Same single `=PY()` add-in; chat tools call `plugin.scripting.venv_worker` from core; `=PROMPT()` from WriterAgent only |
| **WriterAgent only** (after strip) | **Unsupported** — no `=PY()`, `import plugin.scripting` fails without core |

### 1. New extension identity (core owns `=PY()`)

Do **not** reuse `org.extension.writeragent` for the core OXT — `unopkg` must be able to install both side by side. Example prototype id: **`org.extension.librepy`** (rename for upstream as needed).

| Artifact | WriterAgent today | Core prototype |
|----------|-------------------|----------------|
| Extension id | `org.extension.writeragent` | `org.extension.librepy` |
| `description.xml` | [`extension/description.xml.tpl`](../extension/description.xml.tpl) | `extension-core/description.xml` (new identifier + display name) |
| UNO add-in impl | `org.extension.writeragent.PythonFunction` | **`org.extension.writeragent.PythonFunction`** (alias — same namespace as WriterAgent so `ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.*` formulas are portable; extension id stays `org.extension.librepy`) |
| IDL / RDB | `org.extension.writeragent.PythonFunction.XPythonFunction` | Same IDL module path as WriterAgent ([`extension-core/idl/XPythonFunction.idl`](../extension-core/idl/XPythonFunction.idl)); rebuild via `make rdb-core` |
| CalcAddIns node | `org.extension.writeragent.PythonFunction` | **`org.extension.writeragent.PythonFunction`** — keep function names **`py`** and **`python`** (users still type `=PY()`) |
| Protocol handler | `org.extension.writeragent:*` | `org.extension.librepy:*` |
| Menu URLs | `org.extension.writeragent:scripting.run_python_dialog` | `org.extension.librepy:scripting.run_python_dialog` |
| Startup job | `org.extension.writeragent.Main` | `org.extension.librepy.Main` |
| Menubar node | `org.extension.writeragent.menubar` | `org.extension.librepy.menubar` |

Update hardcoded strings in [`addin_librepy.py`](../plugin/calc/python/addin_librepy.py) (`implementationName`, IDL import) — registers as `org.extension.writeragent.PythonFunction` while menus/protocol use `org.extension.librepy`. [`scripts/manifest_registry.py`](../scripts/manifest_registry.py) uses `_PROTOCOL = "org.extension.writeragent"` — core build needs a parallel constant or template substitution.

Core registers **`=PY()` / `=PYTHON()`** only. Do **not** register `prompt_addin` / `=PROMPT()` in core.

### 2. Split `plugin/` across two OXTs (`extend_path`)

Both extensions use the top-level package name **`plugin`**. LibreOffice prepends each extension root to `sys.path`. Without a namespace package, whichever extension wins on `sys.path` owns **all** of `plugin.*` — the other extension’s subpackages may be invisible.

**Required in both OXTs** — update [`plugin/__init__.py`](../plugin/__init__.py):

```python
from pkgutil import extend_path
__path__ = extend_path(__path__, __name__)
```

Then:

| OXT | Ships under `plugin/` (examples) |
|-----|----------------------------------|
| **Core** | `scripting/`, `calc/python/{addin,function,editor,...}`, `framework/` (config subset), `writer/format`, `vision/`, `main_core.py`, … — [Layers 0–6](#feature-bundles-layers) |
| **WriterAgent** | `chatbot/`, `mcp/`, `calc/prompt_*`, `calc/base.py`, `calc/python/venv.py` (chat tool), `calc/analysis.py` (chat tools), `framework/prompts.py`, `embeddings/`, `main.py`, `framework/client/llm_*`, … — [WriterAgent-only](#writeragent-only-surfaces) |

Chat `run_venv_python_script` imports `plugin.scripting.venv_worker` — resolved from **core’s** extension root. WriterAgent must **not** bundle duplicate `plugin/scripting/` in its OXT after the strip.

### 3. WriterAgent strip list (assume core installed)

Remove from WriterAgent when core is the Python owner:

**Manifest / UNO**

- [`plugin/calc/python/addin.py`](../plugin/calc/python/addin.py)
- [`extension/XPythonFunction.rdb`](../extension/XPythonFunction.rdb) (keep [`XPromptFunction.rdb`](../extension/XPromptFunction.rdb) for `=PROMPT()`)

**Registry**

- Entire `org.extension.writeragent.PythonFunction` node in [`CalcAddIns.xcu`](../extension/registry/org/openoffice/Office/CalcAddIns.xcu) — leave only `PromptFunction` / `prompt`.

**Menus** — remove from WriterAgent [`Addons.xcu`](../extension/Addons.xcu) (core owns these):

- `scripting.run_python_dialog`
- `scripting.edit_python_cell`
- `scripting.reset_python_session`
- `writer.insert_latex_dialog`
- `vision.open_settings`
- `textanalytics.open_dialog`

**Bootstrap** — [`plugin/main.py`](../plugin/main.py): drop handlers for the above; keep chat, LLM settings, MCP, grammar, embeddings, review toolbar, etc.

**OXT bundle** — do not package core layer files ([Summary inventory](#summary-inventory-full-core-checklist)) in WriterAgent; only ship AI-specific trees.

**Extension dependency** — add to WriterAgent [`description.xml.tpl`](../extension/description.xml.tpl):

```xml
<dependencies>
  <l:LibreOffice-minimal-version d:name="LibreOffice 24.8" value="24.8"/>
  <OpenOffice.org-extension name="org.extension.librepy" optional="false"/>
</dependencies>
```

Optional runtime guard: if core is missing, log a clear error and disable chat Python tools (not required if you always assume core is installed).

### 4. Slim core bootstrap

Core should not load chat, MCP, or grammar. Add **`plugin/main_core.py`** (or filter [`main.py`](../plugin/main.py) in the core build) that:

1. Puts `vendor/` on `sys.path` (for `latex2mathml`)
2. Calls `init_config(ctx)`
3. Registers only core actions: `run_python_dialog`, `edit_python_cell`, `reset_python_session`, `insert_latex_dialog`, `vision.open_settings`, `textanalytics.open_dialog`, Settings → Python

Register as `org.extension.librepy.Main` in a core-only [`Jobs.xcu`](../extension/Jobs.xcu) and [`META-INF/manifest.xml`](../extension/META-INF/manifest.xml).

### 5. Config and session (shared)

**Recommended:** keep **`writeragent.json`** for both extensions ([`CONFIG_FILENAME`](../plugin/framework/config.py)) so venv path, session mode, and timeouts are shared. Core owns **Python Settings** ([`plugin/librepy/settings.py`](../plugin/librepy/settings.py): Python tab only; General/Image tabs hidden); WriterAgent Settings focus on LLM / AI keys.

Shared Settings helpers (both OXTs): [`plugin/scripting/venv_probe_ui.py`](../plugin/scripting/venv_probe_ui.py) (venv Test / download progress modal) and [`plugin/chatbot/settings_fields.py`](../plugin/chatbot/settings_fields.py) (module.yaml field-spec build/apply). LibrePy keeps Python-only chrome and Cython-only download local; list new shared files in [`scripts/librepy_bundle_paths.py`](../scripts/librepy_bundle_paths.py).

WriterAgent-only `module.yaml` settings keys can carry **`librepy_exclude: true`** (e.g. `scripting.ppt_master_data_path`). LibrePy manifest generation (`make manifest-core` / `generate_manifest.py --skip-writeragent-extension`) omits those keys from `_manifest_librepy.py` and from generated `SettingsDialog.xdl` page 3 controls.

One venv + one shared-kernel session for `=PY()` and chat `run_venv_python_script` — desirable when both are installed.

Alternative for isolation: `librepy.json` + slim `python_config.py` in core only (more work; only if you need separate config files).

### 6. Repo layout and build targets

Suggested layout without forking all of `plugin/`:

```
extension-core/                    # parallel to extension/
  description.xml                    # org.extension.librepy
  META-INF/manifest.xml              # layers 0–6 UNO entries only
  Jobs.xcu                           # org.extension.librepy.Main
  ProtocolHandler.xcu                # org.extension.librepy:*
  Addons.xcu                         # core menus only
  registry/CalcAddIns.xcu            # PythonFunction only (no PROMPT)
  idl/XPythonFunction.idl            # librepy namespace

plugin/
  main_core.py                       # slim bootstrap (new)
  __init__.py                        # pkgutil.extend_path (both OXTs)

Makefile (suggested targets)
  bundle-core / deploy-core            # unopkg org.extension.librepy
  bundle / deploy                      # WriterAgent slim OXT (no PYTHON overlap)
```

**LibrePy menu Context:** In [`extension-core/Addons.xcu`](../extension-core/Addons.xcu), every submenu item must set an explicit `Context` property. Do not rely on “empty Context = all applications” when the same submenu mixes Writer-only and Calc-only entries — LibreOffice may hide shared items (Settings, Run Python Script, Reset Python Session) in Calc. Shared items use the full menubar context string (Writer, Calc, Draw, Impress, Web, Global); doc-specific items set `TextDocument` or `SpreadsheetDocument` only. Regression test: [`tests/scripts/test_librepy_addons_xcu.py`](../tests/scripts/test_librepy_addons_xcu.py).

- **`make manifest`** for core: include `scripting` + `vision` `module.yaml` only; exclude `embeddings`.
- **`make bundle-core`**: copy/filter files from [Layers 0–6](#feature-bundles-layers); substitute `org.extension.librepy` in XCU/IDL/addin.
- **`make deploy-core`**: `unopkg remove/add org.extension.librepy` (parallel to existing `org.extension.writeragent` deploy).

### 7. Implementation order (lowest risk)

1. Add `pkgutil.extend_path` to `plugin/__init__.py`.
2. Add `extension-core/` skeleton + `org.extension.librepy` identifiers + `main_core.py`.
3. `bundle-core` / `deploy-core`; verify **core alone** (`=PY()`, menus, Settings → Python Test).
4. Slim WriterAgent manifest (no `python_addin`, no python menus, no duplicate `scripting/` in OXT).
5. Add extension dependency in WriterAgent `description.xml`.
6. Verify **core + WriterAgent**: one `=PY()` add-in, chat Python tools work via core worker.

### 8. What not to do

- **Do not** install both OXTs with both registering `py`/`python` in CalcAddIns — Calc shows duplicate add-ins; behavior is undefined. LibrePy already registers `org.extension.writeragent.PythonFunction`; WriterAgent must skip its own PythonFunction when LibrePy is present.
- **Do not** ship two full copies of `plugin/scripting/` without `extend_path` — import shadowing is nondeterministic.
- **Do not** use `org.extension.writeragent` as the core extension id — conflicts with existing WriterAgent `unopkg` identity and protocol namespace.

---

## Tests (existing coverage)

| Test file | Covers |
|-----------|--------|
| [`tests/calc/test_prompt_function_uno.py`](../tests/calc/test_prompt_function_uno.py) | Add-in metadata, `python()` (mocked venv) |
| [`tests/calc/test_prompt_function_matrix_uno.py`](../tests/calc/test_prompt_function_matrix_uno.py) | Matrix / spill / `finalize_python_return` |
| [`tests/calc/test_calc_addin_data.py`](../tests/calc/test_calc_addin_data.py) | Range → data shaping |
| [`tests/scripting/test_venv_worker.py`](../tests/scripting/test_venv_worker.py) | `run_code_in_user_venv`, warm worker |
| [`tests/scripting/test_payload_codec.py`](../tests/scripting/test_payload_codec.py) | `split_grid` codec |
| [`tests/scripting/test_config_limits.py`](../tests/scripting/test_config_limits.py) | Timeout / budget schema |
| [`tests/scripting/test_venv_probe_progress.py`](../tests/scripting/test_venv_probe_progress.py) | Venv Test UI progress |
| [`tests/scripting/test_venv_diagnostics.py`](../tests/scripting/test_venv_diagnostics.py) | Self-check probe groups |
| [`tests/scripting/test_worker_harness_trusted_action.py`](../tests/scripting/test_worker_harness_trusted_action.py) | Trusted RPC dispatch |
| [`tests/scripting/test_python_runner_config.py`](../tests/scripting/test_python_runner_config.py) | `last_python_script_*` keys |
| [`tests/scripting/test_python_runner_formatting.py`](../tests/scripting/test_python_runner_formatting.py) | `format_result_for_writer` |
| [`tests/scripting/test_python_runner_analysis.py`](../tests/scripting/test_python_runner_analysis.py) | Analysis fast-path |
| [`tests/scripting/test_python_runner_viz.py`](../tests/scripting/test_python_runner_viz.py) | Viz fast-path |
| [`tests/scripting/test_python_runner_vision.py`](../tests/scripting/test_python_runner_vision.py) | Vision fast-path |
| [`tests/scripting/test_python_runner_monaco.py`](../tests/scripting/test_python_runner_monaco.py) | Monaco Run Python path |
| [`tests/scripting/test_analysis.py`](../tests/scripting/test_analysis.py) | Analysis templates / trusted stubs |
| [`tests/scripting/test_analysis_client.py`](../tests/scripting/test_analysis_client.py) | `client.py` analysis RPC |
| [`tests/scripting/test_viz_templates.py`](../tests/scripting/test_viz_templates.py) | Viz templates |
| [`tests/scripting/test_editor_host.py`](../tests/scripting/test_editor_host.py) | Monaco host spawn / IPC |
| [`tests/scripting/test_document_scripts.py`](../tests/scripting/test_document_scripts.py) | Document-attached scripts |
| [`tests/calc/python/test_editor_save_modes.py`](../tests/calc/python/test_editor_save_modes.py) | Edit Python in Cell save modes |
| [`tests/vision/test_vision_runner.py`](../tests/vision/test_vision_runner.py) | Vision runner |
| [`tests/vision/test_vision_availability.py`](../tests/vision/test_vision_availability.py) | Vision stack gating |
| [`tests/calc/test_vision_egress.py`](../tests/calc/test_vision_egress.py) | Calc Vision HTML insert |
| [`tests/calc/test_vision_structure_egress.py`](../tests/calc/test_vision_structure_egress.py) | Structure egress |
| [`tests/writer/math/test_latex_dialog_uno.py`](../tests/writer/math/test_latex_dialog_uno.py) | Insert LaTeX Math dialog |
| [`tests/writer/math/test_math_mml_convert.py`](../tests/writer/math/test_math_mml_convert.py) | LaTeX/MathML → StarMath |
| [`tests/writer/math/test_math_preservation.py`](../tests/writer/math/test_math_preservation.py) | HTML/math integration |
| [`tests/framework/test_appearance.py`](../tests/framework/test_appearance.py) | Theme sync for Monaco |

**Manual QA fixture:** [`tests/fixtures/numpy_domains_demo.ods`](../tests/fixtures/numpy_domains_demo.ods) — all domain helpers on one workbook ([`numpy_domains_demo.README.md`](../tests/fixtures/numpy_domains_demo.README.md)).

---

## Licensing

| Component | License |
|-----------|---------|
| WriterAgent (KeithCu / John Balis modifications) | GPL-3.0+ |
| Vendored `plugin/contrib/smolagents/` (Hugging Face) | Apache-2.0 — retain notices in core OXT |
| Vendored `vendor/latex2mathml/` | Per upstream license in vendor tree |
| In-process Calc sandbox lineage | Apache-2.0 note in [`executor.py`](../plugin/calc/python/executor.py) (**WriterAgent only**; core uses venv path) |

---

## Summary inventory (full core checklist)

Deduplicated union of **Layers 0–6**. Counts are approximate (~100 `plugin/` paths); verify with import closure on a filtered bundle.

### Layer 0 (~35 paths)

**Framework (13):** `config.py`, `constants.py`, `errors.py`, `json_utils.py`, `i18n.py`, `event_bus.py`, `service.py`, `url_utils.py`, `thread_guard.py`, `client/errors.py`, `client/__init__.py`, `framework/__init__.py`, `_manifest.py`

**Calc (5):** `python/addin.py`, `python/function.py`, `addin_common.py`, `calc_addin_data.py`, `calc/__init__.py`

**Scripting host (8):** `venv_worker.py`, `ipc.py`, `payload_codec.py`, `sandbox.py`, `config_limits.py`, `session_manager.py`, `module.yaml`, `scripting/__init__.py`

**Venv child (4):** `venv/worker_harness.py`, `venv/venv_sandbox.py`, `venv/coerce.py`, `venv/__init__.py`

**Contrib smolagents (8):** `local_python_executor.py`, `tools.py`, `utils.py`, `agent_types.py`, `tool_validation.py`, `_function_type_hints_utils.py`, `smolagents/__init__.py`, `contrib/__init__.py`

**Root:** `plugin/__init__.py`

### Layer 1 adds (~11 paths)

`client.py`, `trusted_rpc.py`, `trusted_action_registry.py`, `venv/trusted_dispatch.py`, `venv/worker_heartbeat.py`, `_lazy_venv.py`, `helper_domain.py`, `domain_registry.py`, `calc_functions_common.py`, `import_policy.py`, `venv_diagnostics.py`

### Layer 2 adds (~18 paths)

`python_runner.py`, `python_runner_ui.py`, `document_scripts.py`, `chatbot/dialogs.py`, `uno_context.py`, `worker_pool.py`, `appearance.py`, `doc/document_helpers.py`, `doc/visual_helpers.py`, `writer/format.py`, `writer/ops.py`, `writer/review_authors.py`, `writer/xhtml_style_postprocess.py`, `calc/bridge.py`, `calc/address_utils.py`, `calc/manipulator.py`, `calc/tabular_egress.py`, `calc/rich_html.py`, `main.py`

### Layer 3 adds (~25 paths)

**Host facades:** `analysis.py`, `viz.py`, `symbolic.py`, `units.py`, `forecast.py`, `optimize.py`, `quant.py`, `text_analytics.py`, `text_analytics_ui.py`

**Venv compute:** `venv/analysis.py`, `venv/viz.py`, `venv/symbolic.py`, `venv/units.py`, `venv/forecast.py`, `venv/optimize.py`, `venv/quant.py`, `venv/text_analytics.py`

**Calc runners/egress:** `calc/analysis_runner.py`, `calc/analysis_egress.py`, `calc/viz_auto_plot.py`, `calc/forecast_auto_plot.py`, `calc/quant_egress.py`, `calc/python/image_egress.py`, `calc/inspector.py`

**Writer images:** `writer/images/image_tools.py`, `writer/images/image_utils.py`, `writer/images/__init__.py` — note [`images.py`](../plugin/writer/images/images.py) pulls LLM image gen if imported

### Layer 4 adds (~8 paths)

`editor_host.py`, `editor_ipc.py`, `calc/python/editor.py`, `calc/python/formula_edit.py`, `calc/python/editor_context_menu.py`, `calc/python/workbook_lifecycle.py`, `venv/editor_main.py`

Dev reference only (not OXT): `contrib/scripting/assets/editor/*`

### Layer 5 adds (~14 paths)

`vision/__init__.py`, `vision/module.yaml`, `vision_common.py`, `vision_templates.py`, `vision_runner.py`, `vision_egress.py`, `vision_availability.py`, `vision/venv/vision.py`, `vision/venv/vision_docling.py`, `vision/venv/vision_paddle.py`, `vision/venv/vision_html_export.py`, `vision/venv/vision_layout_html.py`, `vision/venv/__init__.py`, `calc/vision_egress.py`, `chatbot/module_config_dialog.py`

Omit `vision_tools.py` for menu-only core (chat `extract_text_from_image`).

### Layer 6 adds (~4 paths, overlaps Layer 2)

`writer/math/latex_dialog.py`, `writer/math/math_mml_convert.py`, `writer/math/html_math_segment.py`, `writer/math/__init__.py`

### Extension artifacts

`idl/XPythonFunction.idl`, `XPythonFunction.rdb`, `registry/.../CalcAddIns.xcu`, `META-INF/manifest.xml`, `Addons.xcu`, `WriterAgentDialogs/*` (see [Extension packaging](#extension-packaging-oxt--registry)), `description.xml`, `vendor/latex2mathml/**`

### User venv packages (summary)

| Group | Packages |
|-------|----------|
| Scientific / EDA | numpy, pandas, scipy, scikit-learn, statsmodels, ydata-profiling, pandas-montecarlo |
| Viz | matplotlib, seaborn |
| Symbolic | sympy |
| Units | pint |
| Text | spacy, textdescriptives, … |
| Quant | yfinance, pandas_ta, … |
| Vision | docling, rapidocr-paddle, pillow, css-inline |
| Monaco | pywebview, rocher, PyQt6, PyQt6-WebEngine, qtpy |

See [numpy-domains.md](numpy-domains.md) and [image-recognition.md](image-recognition.md) for authoritative lists.

### Do not ship in core

`prompt_addin.py`, `prompt_function.py`, `plugin/framework/prompts.py`, `plugin/calc/base.py`, `plugin/embeddings/**`, `plugin/notebook/**`, `plugin/scripting/venv/duckdb_sql.py`, `plugin/calc/spreadsheet_import/**`, `plugin/scripting/calc_functions.py`, `plugin/scripting/venv/calc_functions*.py` ([deferred — see § Scope](#calc-parity-xl-helpers-deferred-from-librepy)), `plugin/calc/python/venv.py`, `plugin/calc/analysis.py` (chat tool), `plugin/vision/vision_tools.py` (if menu-only), `plugin/framework/client/llm_client.py`, `plugin/chatbot/panel.py`, grammar venv modules, full chat stack.

---

## Related documentation

- [Enabling NumPy & Python in LibreOffice](enabling_numpy_in_libreoffice.md) — user guide, architecture, `=PY()` behavior
- [NumPy domain helpers](numpy-domains.md) — Analysis, Viz, Symbolic, Units, Forecast, Optimize, Quant, Text
- [Venv subprocess IPC & serialization](numpy-serialization.md) — warm worker, protocol, wire formats
- [Monaco editor dev plan](python-monaco-editor-dev-plan.md) — IPC, phases 2B–2F
- [Image Recognition](image-recognition.md) — Vision/OCR design
- [Math / TeX import](math-tex.md) — LaTeX, MathML, StarMath pipeline
- [Calc integration](calc-integration.md) — broader Calc chat/tools (WriterAgent scope)

**Out of scope for core** (separate PM/dev docs): [embeddings.md](embeddings.md), [duckdb-calc-dev-plan.md](duckdb-calc-dev-plan.md), [jupyter-notebook-import.md](jupyter-notebook-import.md), [calc-spreadsheet-to-python-import.md](calc-spreadsheet-to-python-import.md)

---

## Appendix D — TeX/Math import

WriterAgent exposes **Insert LaTeX Math…** (`writer.insert_latex_dialog`) and embeds math in **Run Python Script** Writer HTML via [`format.py`](../plugin/writer/format.py) + [`html_math_segment.py`](../plugin/writer/math/html_math_segment.py).

### Menu path (Writer formula object)

Writer-only. Conversion runs **in-process** in LibreOffice’s embedded Python plus a **hidden LibreOffice Math** document load — **no venv**.

```mermaid
flowchart LR
  menu[Insert LaTeX Math menu]
  dlg[Monaco or XDL dialog]
  l2m[latex2mathml in vendor/]
  mml[convert_mathml_to_starmath]
  ins[insert_writer_math_formula]
  menu --> dlg --> l2m --> mml --> ins
```

| Step | Implementation |
|------|----------------|
| Dialog | [`latex_dialog.py`](../plugin/writer/math/latex_dialog.py) — Monaco `mode: latex` or native XDL |
| Persist UI | `last_latex_input`, `last_latex_display_block` in config |
| LaTeX → MathML | Vendored **`latex2mathml`** on `sys.path` |
| MathML → StarMath | [`math_mml_convert.py`](../plugin/writer/math/math_mml_convert.py) — hidden Math doc |
| Insert | `insert_writer_math_formula` — `TextEmbeddedObject` with Math CLSID |

### HTML math path (Layer 2 Writer insert)

| File | Role |
|------|------|
| [`plugin/writer/format.py`](../plugin/writer/format.py) | `_insert_mixed_html_and_math_at_cursor`, `insert_content_at_position` |
| [`plugin/writer/math/html_math_segment.py`](../plugin/writer/math/html_math_segment.py) | `$…$`, `$$…$$`, `\(...\)`, `\[...\]`, MathML segments |
| [`plugin/writer/math/math_mml_convert.py`](../plugin/writer/math/math_mml_convert.py) | LaTeX/MathML → StarMath (shared with menu) |

### Third-party

| Path | Role |
|------|------|
| **`vendor/latex2mathml/`** | OXT-bundled via `make vendor` |
| **LibreOffice Math** | Required at runtime for conversion |

### Not in core

| Path | Why |
|------|-----|
| `plugin/draw/math_insert.py` | Draw/Impress chat tool (WriterAgent) |
| Venv worker / smolagents | Menu path does not use subprocess |

Deeper design: [math-tex.md](math-tex.md).

---

## Appendix E — NumPy domain trusted helpers

Shipped domains per [numpy-domains.md](numpy-domains.md): **Analysis**, **Visualization**, **Symbolic Math**, **Units**, **Forecasting**, **Optimization**, **Quant**, **Text Analytics**.

### Integration surfaces (core)

| Domain | Trusted RPC `domain` | Menu / RPS | Chat tool (WriterAgent) |
|--------|---------------------|------------|---------------------------|
| Analysis | `analysis` | Run Python Script → Analysis Helpers | `analyze_data` |
| Viz | `viz` | Run Python Script → Viz Helpers | `plot_data` |
| Symbolic | `symbolic` / `math` | Run Python Script → Math Helpers | `symbolic_math` |
| Units | `units` | Run Python Script → Units Helpers | — |
| Forecast | `forecast` | Run Python Script → Forecast (header fast-path) | `forecast_data` |
| Optimize | `optimize` | Run Python Script → Optimize (header fast-path) | `optimize_data` |
| Quant | `quant` | Run Python Script → Quant Helpers | — |
| Text | `text` | **Tools → Text Analytics…** | — |

### Host facades (`plugin/scripting/`)

| File | Role |
|------|------|
| [`analysis.py`](../plugin/scripting/analysis.py) | Templates, `run_trusted_analysis` orchestration |
| [`viz.py`](../plugin/scripting/viz.py) | Viz templates, Writer/Calc egress |
| [`symbolic.py`](../plugin/scripting/symbolic.py) | SymPy helpers, math insert egress |
| [`units.py`](../plugin/scripting/units.py) | Pint unit conversion |
| [`forecast.py`](../plugin/scripting/forecast.py) | Time-series helpers |
| [`optimize.py`](../plugin/scripting/optimize.py) | scipy.optimize helpers |
| [`quant.py`](../plugin/scripting/quant.py) | Quantitative finance helpers |
| [`text_analytics.py`](../plugin/scripting/text_analytics.py) | spaCy / textdescriptives orchestration |
| [`text_analytics_ui.py`](../plugin/scripting/text_analytics_ui.py) | Text Analytics menu dialog |

### Venv compute (`plugin/scripting/venv/`)

| File | Role |
|------|------|
| [`analysis.py`](../plugin/scripting/venv/analysis.py) | `run_analysis` — full numpy/pandas/scipy stack |
| [`viz.py`](../plugin/scripting/venv/viz.py) | `run_viz` — matplotlib/seaborn |
| [`symbolic.py`](../plugin/scripting/venv/symbolic.py) | `run_symbolic` — SymPy |
| [`units.py`](../plugin/scripting/venv/units.py) | `run_units` — Pint |
| [`forecast.py`](../plugin/scripting/venv/forecast.py) | `run_forecast` — statsmodels |
| [`optimize.py`](../plugin/scripting/venv/optimize.py) | `run_optimize` |
| [`quant.py`](../plugin/scripting/venv/quant.py) | `run_quant` |
| [`text_analytics.py`](../plugin/scripting/venv/text_analytics.py) | `run_text_analytics` |

Requires [Layer 1](#layer-1--trusted-rpc) + [`domain_registry.py`](../plugin/scripting/domain_registry.py) for RPS picker templates and fast-path headers (`writeragent:forecast`, etc.).

### Calc runners / egress (not chat tool classes)

| File | Role |
|------|------|
| [`calc/analysis_runner.py`](../plugin/calc/analysis_runner.py) | Range resolve + trusted analysis invoke |
| [`calc/analysis_egress.py`](../plugin/calc/analysis_egress.py) | Analysis results → sheet |
| [`calc/viz_auto_plot.py`](../plugin/calc/viz_auto_plot.py) | Auto-chart after viz |
| [`calc/forecast_auto_plot.py`](../plugin/calc/forecast_auto_plot.py) | Auto-chart after forecast |
| [`calc/quant_egress.py`](../plugin/calc/quant_egress.py) | Quant results → sheet |
| [`calc/python/image_egress.py`](../plugin/calc/python/image_egress.py) | Matplotlib figure → sheet image |
| [`calc/inspector.py`](../plugin/calc/inspector.py) | `read_range` for data-bound helpers |

### Writer egress

| File | Role |
|------|------|
| [`writer/images/image_tools.py`](../plugin/writer/images/image_tools.py) | Insert plot images at cursor |
| [`writer/images/image_utils.py`](../plugin/writer/images/image_utils.py) | Graphic byte export helpers |
| [`writer/math/math_mml_convert.py`](../plugin/writer/math/math_mml_convert.py) | Symbolic math → Writer formula objects |

**Whole-file caveat:** [`writer/images/images.py`](../plugin/writer/images/images.py) imports LLM image-generation code; prefer `image_tools.py` only or split before core bundle.

### Do not ship for domain helpers alone

| Path | Why |
|------|-----|
| `plugin/calc/analysis.py`, `viz.py`, `forecast.py`, `optimize.py`, `symbolic_math.py` | Chat `ToolBase` wrappers — WriterAgent |
| `plugin/scripting/venv/duckdb_sql.py` | DuckDB — excluded |
| `plugin/embeddings/**` | Embeddings — excluded |

---

## Appendix F — Monaco editor

Monaco-based code editor (pywebview child in the user venv) for Calc formulas and ad-hoc scripts. Detail: [python-monaco-editor-dev-plan.md](python-monaco-editor-dev-plan.md).

| Feature | Entry |
|---------|--------|
| **Edit Python in Cell…** | [`calc/python/editor.py`](../plugin/calc/python/editor.py) |
| **Run Python Script…** Monaco | [`python_runner.py`](../plugin/scripting/python_runner.py) + [`editor_host.py`](../plugin/scripting/editor_host.py) |
| **Document scripts** | [`document_scripts.py`](../plugin/scripting/document_scripts.py) |
| **Init script editor** | Monaco `scripts_manager.js` IPC + `session_manager.py` (no separate `init_script_editor.py`) |
| **Theme sync** | [`appearance.py`](../plugin/framework/appearance.py) |

```mermaid
flowchart LR
  host[editor_host.py in LO]
  pipe[editor_ipc.py Pickle5]
  child[venv/editor_main.py pywebview]
  rocher[rocher Monaco assets in venv]
  host --> pipe --> child --> rocher
```

### Files (Layer 4)

| File | Role |
|------|------|
| [`plugin/scripting/editor_host.py`](../plugin/scripting/editor_host.py) | Spawn pywebview child, session, theme |
| [`plugin/scripting/editor_ipc.py`](../plugin/scripting/editor_ipc.py) | Pipe framing, errors |
| [`plugin/calc/python/editor.py`](../plugin/calc/python/editor.py) | Edit Python in Cell… |
| [`plugin/calc/python/formula_edit.py`](../plugin/calc/python/formula_edit.py) | Parse/rebuild `=PY()` formulas |
| [`plugin/calc/python/editor_context_menu.py`](../plugin/calc/python/editor_context_menu.py) | Cell context menu |
| [`plugin/calc/python/workbook_lifecycle.py`](../plugin/calc/python/workbook_lifecycle.py) | Init script session reset |
| [`plugin/scripting/venv/editor_main.py`](../plugin/scripting/venv/editor_main.py) | Child process entry (runs in user venv) |

Requires Layer 2 (`appearance.py`, `document_scripts.py`, `python_runner.py`) and Layer 0 worker for **Run** from Monaco.

### Not in OXT

| Asset | Location |
|-------|----------|
| Monaco `vs/`, shell HTML/JS/CSS | User venv **`rocher`** (via `pip install rocher`) |
| Dev reference copies | `plugin/contrib/scripting/assets/editor/*` |

### User venv packages

```bash
uv pip install pywebview rocher PyQt6 PyQt6-WebEngine qtpy
```

Optional: `jedi` (completion stub in `editor_main.py`).

**Edit Python in Cell…** does not fall back to embedded LO Python — fix the venv if Monaco fails to open.

---

## Appendix G — Vision/OCR

Local OCR and document layout via trusted Vision helpers. Manual path first per [image-recognition.md](image-recognition.md). Core ships **Run Python Script → Vision Helpers** + **Vision OCR Settings** — not chat `extract_text_from_image`.

```mermaid
flowchart LR
  rps[Run Python Script Vision Helpers]
  runner[vision_runner.py]
  rpc[trusted RPC domain vision]
  venv[vision/venv/vision.py]
  egressW[writer/format.py HTML insert]
  egressC[calc/vision_egress.py]
  rps --> runner --> rpc --> venv
  venv --> egressW
  venv --> egressC
```

### Host (`plugin/vision/`)

| File | Role |
|------|------|
| [`vision_common.py`](../plugin/vision/vision_common.py) | Shared types/helpers |
| [`vision_templates.py`](../plugin/vision/vision_templates.py) | RPS script templates |
| [`vision_runner.py`](../plugin/vision/vision_runner.py) | Host orchestration, egress |
| [`vision_egress.py`](../plugin/vision/vision_egress.py) | Writer HTML insert routing |
| [`vision_availability.py`](../plugin/vision/vision_availability.py) | Stack gating |
| [`module.yaml`](../plugin/vision/module.yaml) | Settings schema → `VisionSettingsDialog` |

Omit [`vision_tools.py`](../plugin/vision/vision_tools.py) for menu-only core (LLM tool wrapper).

### Venv (`plugin/vision/venv/`)

| File | Role |
|------|------|
| [`vision.py`](../plugin/vision/venv/vision.py) | `run_vision` dispatcher |
| [`vision_docling.py`](../plugin/vision/venv/vision_docling.py) | Docling OCR/layout |
| [`vision_paddle.py`](../plugin/vision/venv/vision_paddle.py) | PaddleOCR fallback |
| [`vision_html_export.py`](../plugin/vision/venv/vision_html_export.py) | HTML prep for LO import |
| [`vision_layout_html.py`](../plugin/vision/venv/vision_layout_html.py) | Structure HTML export |

Registered in [`trusted_action_registry.py`](../plugin/scripting/trusted_action_registry.py) as domain `vision` → [`trusted_dispatch.py`](../plugin/scripting/venv/trusted_dispatch.py).

### Calc / Writer egress (Layer 2 overlap)

| File | Role |
|------|------|
| [`calc/vision_egress.py`](../plugin/calc/vision_egress.py) | Calc HTML / structured grid insert |
| [`calc/rich_html.py`](../plugin/calc/rich_html.py) | `insert_cell_html_rich` |
| [`writer/format.py`](../plugin/writer/format.py) | Writer HTML at cursor; `run_writer_mutation_with_optional_review` wraps insert in `EditReviewSession` when WriterAgent’s `edit_review` module is co-installed (LibrePy alone applies directly) |
| [`doc/visual_helpers.py`](../plugin/doc/visual_helpers.py) | Export graphic to bytes |

### Settings / UI

| Resource | Role |
|----------|------|
| Menu `vision.open_settings` | [`chatbot/module_config_dialog.py`](../plugin/chatbot/module_config_dialog.py) |
| `VisionSettingsDialog.xdl` | Generated from `vision/module.yaml` |
| Settings → Python **Test** | Vision Libraries probe in `venv_diagnostics.py` (omit Embeddings group in core build) |

### User venv packages

```bash
uv pip install docling rapidocr-paddle numpy pillow css-inline
# optional fallback: paddleocr paddlepaddle
```

Models are **not** bundled in the OXT; user venv installs them.

### WriterAgent-only

| Path | Role |
|------|------|
| [`vision_tools.py`](../plugin/vision/vision_tools.py) | `extract_text_from_image` LLM tool |
| `plugin/framework/tool.py` | Tool registration |
| Draw/Impress page-positioned OCR | Deferred — [image-recognition.md](image-recognition.md) Phase 1b.2 |
