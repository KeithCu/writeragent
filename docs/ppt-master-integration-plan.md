# Integration Plan: PPT-Master in WriterAgent (Adapter Layer)

This document describes how [ppt-master](https://github.com/hugohe3/ppt-master) integrates with WriterAgent: a **UNO adapter layer**, **sidebar PPT-Master mode** (Impress/Draw only), and **upstream assets from a cloned skill tree** (not vendored in the OXT).

## Status (implementation summary)

| Decision | Choice |
|----------|--------|
| Upstream `svg_to_pptx` | **Not** copied into `plugin/contrib/` — loaded from skill tree `scripts/svg_to_pptx/` |
| WriterAgent-only code | Adapters + forked `SKILL.md` under [`plugin/contrib/ppt_master/`](../plugin/contrib/ppt_master/) |
| Host / UNO | [`plugin/ppt_master/`](../plugin/ppt_master/) (client, paths, tools, adapters) |
| Sidebar UX | Venv-hosted smol sub-agent via [`plugin/ppt_master/venv/`](../plugin/ppt_master/venv/) + [`plugin/chatbot/ppt_master.py`](../plugin/chatbot/ppt_master.py) — hidden from main chat |
| Dev reference clone | Optional repo root `ppt-master/` (not shipped) |

**Removed during cleanup (no longer in tree):**

- `plugin/contrib/ppt_master/bundled/svg_to_pptx/` — byte-identical upstream copy (~18 files); deleted in favor of external skill tree
- `plugin/contrib/ppt_master/backends/` — unused protocol stubs
- `plugin/ppt_master/diagnostics.py` — install hint moved to `paths.PPT_MASTER_INSTALL_CMD`
- `plugin/contrib/ppt_master/svg_convert.py`, `shape_ops.py`, `svg_preprocess.py`, `plugin/ppt_master/adapter/uno_apply.py`, `uno_svg_import.py`, `uno_svg_deck.py` — removed; replaced by PPTX build + LO PPTX import

**Added for venv-hosted sub-agent (current sidebar design):**

- [`plugin/ppt_master/venv/`](../plugin/ppt_master/venv/) — smol loop, script/file tools, host RPC client
- [`plugin/contrib/ppt_master/skill/SKILL.md`](../plugin/contrib/ppt_master/skill/SKILL.md) + [`skill_paths.py`](../plugin/contrib/ppt_master/skill_paths.py) — WriterAgent orchestration fork (sidebar chat confirmations; no browser UI)

## Overview

ppt-master is an agentic workflow (SKILL.md + project artifacts + SVG → native shapes). WriterAgent:

1. Ships **adapter modules** under [`plugin/contrib/ppt_master/`](../plugin/contrib/ppt_master/) — see [`README.md`](../plugin/contrib/ppt_master/README.md)
2. Loads **unmodified upstream scripts and assets** from the configured skill tree (`PPT_MASTER_DATA_ROOT`); orchestration doc is the **WriterAgent fork** in contrib (see Packaging)
3. Hosts **UNO adapters** under [`plugin/ppt_master/`](../plugin/ppt_master/)
4. Exposes **PPT-Master** in the sidebar mode dropdown for **Impress and Draw only**
5. Runs a **venv-hosted smol sub-agent** when that mode is selected — full SKILL workflow (scripts + project files) in the user venv; host provides LLM HTTP and UNO export tools only

## Packaging

Upstream [ppt-master](https://github.com/hugohe3/ppt-master) is a **skill/workflow repo**, not a pip package (no `pyproject.toml`). Install by cloning and pointing Settings at the skill directory:

```bash
git clone https://github.com/hugohe3/ppt-master.git
```

Then **Settings → Python** → **PPT-Master data path** → `.../ppt-master` (clone root) or `.../ppt-master/skills/ppt-master` (inner skill dir). The resolved directory must contain `SKILL.md`, `templates/`, and `scripts/svg_to_pptx/`.

**Dev without manual path:** clone upstream beside the repo as `ppt-master/`; `paths._dev_clone_data_root()` finds `ppt-master/skills/ppt-master` automatically.

| Layer | In OXT? | Location |
|-------|---------|----------|
| UNO adapter (`coords`, `upstream`, `config`, `skill_paths`) | Yes | `plugin/contrib/ppt_master/` |
| Forked orchestration `SKILL.md` | Yes | [`plugin/contrib/ppt_master/skill/SKILL.md`](../plugin/contrib/ppt_master/skill/SKILL.md) |
| Upstream `scripts/`, templates, references, workflows | **No** — user clone / path | Resolved to `PPT_MASTER_DATA_ROOT` |
| UNO apply, client, tools | Yes | `plugin/ppt_master/` |
| Venv sub-agent runner | Yes | `plugin/ppt_master/venv/` |
| Sidebar session (host bridge) | Yes | `plugin/chatbot/ppt_master.py` |

**SKILL split:** The venv agent loads the **WriterAgent fork** via [`skill_paths.resolve_writeragent_skill_md()`](../plugin/contrib/ppt_master/skill_paths.py). Routing files (`workflows/routing.md`, etc.) still come from the user data root. The fork drops Confirm UI browser and Step 6 live-preview server — sidebar chat + `export_presentation_project` instead.

## Sidebar sub-agent design

### Why LO-hosted smolagents does not work

WriterAgent’s **standard smol sub-agent pattern** (Brainstorming, Writing Plan, Librarian) runs [`ToolCallingAgent`](../plugin/contrib/smolagents/agents.py) **inside LibreOffice embedded Python** on a background worker thread, with HTTP via [`WriterAgentSmolModel`](../plugin/chatbot/smol_agent.py) and tools executed on the **main thread** when they touch UNO. That pattern is correct for document-centric workflows.

**ppt-master is not document-centric.** Upstream is a **skill/workflow package** ([`SKILL.md`](../ppt-master/skills/ppt-master/SKILL.md) + `scripts/` + project folders on disk). In Cursor or Claude Code, the agent runs where it has **bash, filesystem, and a normal Python venv** — not inside a UNO extension sandbox.

| What upstream `SKILL.md` expects | What LO-hosted smol had (v1) |
|----------------------------------|------------------------------|
| `python3 ${SKILL_DIR}/scripts/project_manager.py …` | No shell / script runner |
| `pdf_to_md`, `finalize_svg`, `svg_to_pptx`, `confirm_ui/server.py --daemon` | Only `svg_to_pptx` via a single host-orchestrated venv subprocess |
| Read/write project artifacts (`svg_output/`, `design_spec.md`, …) | No arbitrary file tools |
| Long-running local servers (confirm UI, SVG live editor) | Not possible from LO thread agent |
| `python-pptx` stack during build | **Forbidden** on LO host ([`upstream.py`](../plugin/contrib/ppt_master/upstream.py) policy) |

Injecting full `SKILL.md` into an LO smol agent would teach a workflow it **cannot execute** — the model would hallucinate script runs, fail on missing tools, or frustrate users. Merging ~20 Draw/Impress core tools (`add_slide`, `upsert_shape`, …) did not fix this: upstream does not build decks by hand-editing UNO shapes; it runs a **script pipeline** and exports PPTX.

**What LO smol *can* do for ppt-master (export-only path):**

- Call host UNO tools: `export_presentation_project`, `validate_ppt_master_project`, template-fill, native-enhance
- Trigger one-shot `svg_to_pptx` in the user venv when the host builds missing `exports/*.pptx`

That is enough for **“import an existing project folder”** but not for **“run the full ppt-master workflow in the sidebar.”**

### Chosen design: venv-hosted sub-agent + host RPC bridge

**Decision:** Run the smol sub-agent **inside the user venv worker** (same environment class as Cursor/Claude Code). LibreOffice host provides only:

1. **LLM HTTP** — `llm_request` RPC → [`LlmClient.request_with_tools`](../plugin/framework/client/llm_client.py); API keys stay in `writeragent.json`
2. **UNO tools** — `tool_call` RPC → main-thread [`ToolRegistry.execute`](../plugin/framework/tool.py) for export/validate/fill/enhance
3. **UI streaming** — `worker_event` frames → sidebar thinking/status

```text
Sidebar send → ppt_master_session (host)
  → execute_ppt_master_turn (venv_worker IPC)
    → runner.run_turn (venv): ToolCallingAgent + forked SKILL (contrib)
      → llm_request / tool_call / worker_event (stdout pipe)
        → host_rpc.dispatch_worker_response (LO main thread for UNO)
```

**Venv-local tools** ([`runner.py`](../plugin/ppt_master/venv/runner.py)):

| Tool | Role |
|------|------|
| `run_ppt_master_script` | Whitelisted subprocess under `data_root/scripts/` |
| `read_ppt_master_workflow_file` | On-demand reads from skill tree (`references/`, …) |
| `read_project_file` / `write_project_file` | Project artifact I/O with path traversal guards |
| `validate_ppt_master_project` | Host RPC (UNO-safe validation) |
| `export_presentation_project` | Host RPC (PPTX build + LO import) |
| `apply_ppt_master_template_fill` / `apply_ppt_master_native_enhance` | Host RPC |
| `reply_to_user` / `ppt_master_finished` | Session continue / HTML handoff |

**Session model:** Multi-turn chat reuses the warm venv worker; `session_id` is derived from the document URL ([`ppt_master_session_id`](../plugin/ppt_master/venv/host.py)). Conversation history is passed each turn; SKILL context is cached in the venv process per session.

**Model guidance:** Upstream recommends **Claude Opus/Sonnet** with large context (~1M) for best results; GPT/Gemini/Kimi work with a lower ceiling. Sidebar model selection is forwarded through `llm_request` RPC.

**Honest limits (venv):**

| SKILL feature | Feasibility |
|---------------|-------------|
| Script pipeline (`project_manager`, `pdf_to_md`, …) | **Yes** — core win |
| Hand-written SVG by LLM | **Yes** — `write_project_file` |
| `confirm_ui/server.py --daemon` | **No** — removed from WriterAgent fork; sidebar chat only |
| `svg_editor/server.py --live` | **No** — removed from WriterAgent fork; use `read_project_file` + `export_presentation_project` |

**Infrastructure reused:** existing venv worker IPC ([`venv_worker.py`](../plugin/scripting/venv_worker.py) `tool_call` loop), [`pptx_build.py`](../plugin/ppt_master/pptx_build.py) subprocess patterns, vendored smolagents on worker `PYTHONPATH`.

## Settings

On **Settings → Python** (bottom of tab):

| Control | Config key | Notes |
|---------|------------|-------|
| PPT-Master data path | `scripting.ppt_master_data_path` | Directory picker row (own line, below Python options) |
| Test | — | Probes `SKILL.md`, `templates/`, `scripts/svg_to_pptx/` via `data_root_status` |

Python venv path is required for the full sidebar workflow; install ppt-master deps into the user venv:

```bash
pip install -r /path/to/ppt-master/skills/ppt-master/requirements.txt
```

Export-only tools (`export_presentation_project`, etc.) still run on the LO host when invoked via RPC from the venv agent.

## Architecture

```mermaid
flowchart TB
  subgraph host [LibreOffice host]
    UI[Sidebar PPT-Master mode]
    Session[ppt_master_session]
    LLM_RPC[LLM RPC → LlmClient]
    ToolRPC[Tool RPC → main thread UNO]
    Drain[thinking/status UI]
  end
  subgraph venv [User venv worker]
    Runner[ppt_master_runner]
    Smol[ToolCallingAgent + forked SKILL]
    Scripts[subprocess scripts/*]
    Files[project read/write]
  end
  ForkSKILL[(contrib skill/SKILL.md)] --> Smol
  UI --> Session
  Session --> Runner
  Runner --> Smol
  Smol -->|llm_request| LLM_RPC
  Smol --> Scripts
  Smol --> Files
  Smol -->|export_presentation_project| ToolRPC
  Runner -->|worker_event| Drain
  Data[(user data root scripts templates workflows)] --> Scripts
  Data -.->|routing refs| Smol
  ToolRPC --> UNO[Impress/Draw document]
```

### Host ↔ venv IPC

| Frame `type` | Direction | Handler |
|--------------|-----------|---------|
| `llm_request` | venv → host → venv | [`host_rpc.handle_llm_request`](../plugin/ppt_master/venv/host_rpc.py) |
| `tool_call` | venv → host → venv | [`host_rpc.execute_tool_on_main_thread`](../plugin/ppt_master/venv/host_rpc.py) |
| `worker_event` | venv → host (no reply) | Sidebar thinking/status drain |
| final `status`/`result` | venv → host | End of `ppt_master_turn` in [`worker_harness.py`](../plugin/scripting/venv/worker_harness.py) |

Venv-side client: [`ipc.py`](../plugin/ppt_master/venv/ipc.py) (`rpc_llm`, `rpc_tool`, `emit_worker_event`). Host entry: [`host.py`](../plugin/ppt_master/venv/host.py) → [`PythonWorkerManager.execute_ppt_master_turn`](../plugin/scripting/venv_worker.py).

SKILL injection at session start: [`skill_context.py`](../plugin/ppt_master/venv/skill_context.py) loads the **bundled fork** via [`skill_paths.resolve_writeragent_skill_md()`](../plugin/contrib/ppt_master/skill_paths.py), plus `workflows/routing.md`, `workflows/index.md`, and `references/artifact-ownership.md` from the data root (capped), and a short LO-bridge paragraph.

### Data root resolution (`plugin/ppt_master/paths.py`)

1. `scripting.ppt_master_data_path` (Settings → Python; clone root or inner `skills/ppt-master`)
2. `PPT_MASTER_DATA_ROOT` env (set by `apply_data_root_env`)
3. User venv `site-packages` scan (optional fallback)
4. Dev clone `ppt-master/skills/ppt-master`

`data_root_status()` requires templates/references/SKILL.md **and** `scripts/` (with `svg_to_pptx/`).

### Upstream import policy (`plugin/contrib/ppt_master/upstream.py`)

- Load `pptx_discovery.py` **by file path** so `svg_to_pptx/__init__.py` is not executed on the LO host (that import chain requires `python-pptx`).
- Full `svg_to_pptx` stack runs in the **user venv** when ppt-master workflow scripts need PPTX output.

### Main export path (UNO)

`export_presentation_project` → [`uno_pptx_deck.export_project_to_doc`](../plugin/ppt_master/adapter/uno_pptx_deck.py) → [`uno_pptx_import.py`](../plugin/ppt_master/adapter/uno_pptx_import.py):

```text
svg_final/ or svg_output/  (required for auto-build)
  → find exports/*.pptx OR venv: svg_to_pptx.py -q
  → UNO loadComponentFromURL(pptx) hidden Impress doc
  → copy shapes per slide to active document (no SVG Break/postprocess)
  → optional mirror ODP: exports/{pptx_stem}.odp
```

**Why PPTX not SVG:** upstream `svg_to_pptx` already produces native DrawingML; LibreOffice’s PPTX filter preserves layout far better than the removed SVG `draw_svg_import` + Break path (~8–26% PDF diff on real decks).

### Import fidelity tooling (agents)

Compare **reference** vs **imported** output per slide using PDF as the interchange format:

| Step | Reference | Imported |
|------|-----------|----------|
| Source | Project `exports/*.pptx` (page N) | `import_pptx_slide_to_odp` → one-slide ODP |
| PDF | `soffice --headless --convert-to pdf` on PPTX | same on ODP |
| Compare | Rasterize both (`pdftoppm` or ImageMagick) → pixel diff + `diff.png` | |

**CLI:**

```bash
# Full project (writes <project>/.import_fidelity/report.json + SUMMARY.md)
python scripts/ppt_master_import_fidelity.py ppt-master/examples/ppt169_attention_is_all_you_need

# One slide while iterating
python scripts/ppt_master_import_fidelity.py path/to/project --slides 01_cover

# Structural only (shape/text counts; no pdftoppm)
python scripts/ppt_master_import_fidelity.py path/to/project --structural-only

# Stricter pass threshold (default diff_fraction 0.12)
python scripts/ppt_master_import_fidelity.py path/to/project --threshold 0.08
```

**Requires:** LibreOffice (`soffice`, UNO Python), **`pdftoppm`** (poppler) or ImageMagick for visual mode.

**Library:** [`plugin/ppt_master/fidelity.py`](../plugin/ppt_master/fidelity.py) — unit-tested in [`test_ppt_master_fidelity.py`](../tests/ppt_master/test_ppt_master_fidelity.py).

**Agent loop:**

1. Run fidelity script on a real project (e.g. `ppt169_attention_is_all_you_need`).
2. Open worst `slide_NN_*/diff.png` and side-by-side PDFs (`reference.pdf`, `imported.pdf`).
3. If diff is high, inspect LO PPTX import or upstream PPTX build — not SVG preprocess.
4. Re-run until `diff_fraction` ≤ threshold; check mirror `exports/*.odp` for manual spot-check.

**Manual export (full deck):**

```bash
# From repo root, UNO Python — or use export_presentation_project in Impress sidebar
python -c "
import officehelper, uno
from pathlib import Path
from plugin.ppt_master.adapter.uno_pptx_deck import export_project_to_doc
p = Path('ppt-master/examples/ppt169_attention_is_all_you_need')
ctx = officehelper.bootstrap()
d = ctx.ServiceManager.createInstanceWithContext('com.sun.star.frame.Desktop', ctx)
h = uno.createUnoStruct('com.sun.star.beans.PropertyValue', Name='Hidden', Value=True)
doc = d.loadComponentFromURL('private:factory/simpress', '_blank', 0, (h,))
export_project_to_doc(doc, p, ctx=ctx)
doc.storeToURL((p / 'exports' / 'deck.odp').resolve().as_uri(), ())
doc.close(True)
"
```

Reference PDF is LO’s render of the **PPTX slide**. Imported PDF is the same slide after **PPTX → ODP** import. Expect near-zero diff when LO’s PPTX filter matches PowerPoint layout.

## Routes

| Route | Implementation | Notes |
|-------|----------------|-------|
| Main export | `export_presentation_project` → `uno_pptx_import` | PPTX → ODP via LO (default) |
| template-fill | `apply_ppt_master_template_fill` → `uno_template_fill` | Incremental stub |
| native-enhance | `apply_ppt_master_native_enhance` → `uno_enhance` | `enhancement_plan.json` |
| beautify | venv `pptx_to_svg` + re-import | Not wired end-to-end yet |

## Key modules

| Module | Role |
|--------|------|
| [`plugin/chatbot/ppt_master.py`](../plugin/chatbot/ppt_master.py) | `ppt_master_session` — delegates to venv runner |
| [`plugin/contrib/ppt_master/skill/SKILL.md`](../plugin/contrib/ppt_master/skill/SKILL.md) | WriterAgent fork of upstream orchestration doc |
| [`plugin/contrib/ppt_master/skill_paths.py`](../plugin/contrib/ppt_master/skill_paths.py) | Resolve bundled SKILL vs data-root fallback |
| [`plugin/ppt_master/venv/runner.py`](../plugin/ppt_master/venv/runner.py) | Venv smol loop, script + file tools |
| [`plugin/ppt_master/venv/host_rpc.py`](../plugin/ppt_master/venv/host_rpc.py) | Host LLM + UNO tool RPC dispatch |
| [`plugin/ppt_master/venv/host.py`](../plugin/ppt_master/venv/host.py) | Host entry: `run_ppt_master_venv_turn`, session id |
| [`plugin/ppt_master/venv/ipc.py`](../plugin/ppt_master/venv/ipc.py) | Venv-side RPC client over worker stdin/stdout |
| [`plugin/ppt_master/venv/model.py`](../plugin/ppt_master/venv/model.py) | `HostRpcModel` — smol `Model` forwarding to `rpc_llm` |
| [`plugin/ppt_master/venv/skill_context.py`](../plugin/ppt_master/venv/skill_context.py) | Level-2 SKILL + routing load for system prompt |
| [`plugin/ppt_master/venv/path_ops.py`](../plugin/ppt_master/venv/path_ops.py) | Path guards + `run_script` subprocess helper |
| [`plugin/scripting/venv_worker.py`](../plugin/scripting/venv_worker.py) | `execute_ppt_master_turn`, extended IPC dispatch |
| [`plugin/chatbot/chat_sidebar_mode.py`](../plugin/chatbot/chat_sidebar_mode.py) | `CHAT_MODE_PPT_MASTER`, `sidebar_mode_flags_for_doc_type` |
| [`plugin/ppt_master/tools.py`](../plugin/ppt_master/tools.py) | Specialized tools (`ToolDrawPptMasterBase`) |
| [`plugin/ppt_master/pptx_build.py`](../plugin/ppt_master/pptx_build.py) | Find/build `exports/*.pptx` via user venv |
| [`plugin/ppt_master/adapter/uno_pptx_import.py`](../plugin/ppt_master/adapter/uno_pptx_import.py) | UNO PPTX load → copy slides to Impress |
| [`plugin/ppt_master/adapter/uno_pptx_deck.py`](../plugin/ppt_master/adapter/uno_pptx_deck.py) | Project orchestrator (build + import + mirror ODP) |
| [`plugin/ppt_master/adapter/uno_shape_postprocess.py`](../plugin/ppt_master/adapter/uno_shape_postprocess.py) | Shape clone helpers for slide copy |
| [`plugin/ppt_master/project_notes.py`](../plugin/ppt_master/project_notes.py) | Speaker notes discovery by slide index |
| [`plugin/ppt_master/fidelity.py`](../plugin/ppt_master/fidelity.py) | PDF/PNG diff: PPTX reference vs ODP import |
| [`scripts/ppt_master_import_fidelity.py`](../scripts/ppt_master_import_fidelity.py) | CLI fidelity loop for agents |
| [`plugin/framework/constants.py`](../plugin/framework/constants.py) | `IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS`, sub-agent instructions |

## Contrib merge policy

Only add files under `plugin/contrib/ppt_master/` when WriterAgent must **change** behavior. Do not re-vendor `svg_to_pptx/`. **Exception:** [`plugin/contrib/ppt_master/skill/SKILL.md`](../plugin/contrib/ppt_master/skill/SKILL.md) is a tracked fork of upstream orchestration only — scripts, templates, and references remain in the user's cloned data root. Upstream is **MIT** (Hugo He); WriterAgent adapters are **GPL-3.0-or-later** — see [`plugin/contrib/ppt_master/README.md`](../plugin/contrib/ppt_master/README.md) for the full MIT notice, upstream pin, and symbol map.

**Python files:** shipped adapters are WriterAgent-original; keep upstream attribution in README only (not in `.py` headers). When **vendoring** upstream lines, comment out replaced code with `'''` blocks in that file only (see [`plugin/contrib/nbformat/README.md`](../plugin/contrib/nbformat/README.md)).

---

## Roadmap

Backlog for PPT-Master integration work. **Priority order matters** — validate the main export path on real decks before secondary routes or large refactors.

### Agent quick start

1. Confirm dev setup: clone upstream beside repo as `ppt-master/` **or** set **Settings → Python → PPT-Master data path** to the clone root (`.../ppt-master`) or inner skill dir (`.../ppt-master/skills/ppt-master`). Run **Test** in Settings.
2. Read [`plugin/contrib/ppt_master/README.md`](../plugin/contrib/ppt_master/README.md) symbol map (WriterAgent module ↔ upstream equivalent).
3. Trace the happy path: `export_presentation_project` → [`client.py`](../plugin/ppt_master/client.py) → [`uno_pptx_deck.py`](../plugin/ppt_master/adapter/uno_pptx_deck.py) → [`uno_pptx_import.py`](../plugin/ppt_master/adapter/uno_pptx_import.py).
4. Run tests: `pytest tests/ppt_master/`; UNO: `python -m plugin.testing_runner test_ppt_master_pptx_import_uno`; fidelity: `python scripts/ppt_master_import_fidelity.py <project>`.
5. Pick the next roadmap item below; run fidelity script before/after changes.
6. Add tests in the matching `test_*` file per [AGENTS.md](../AGENTS.md).

### What is done (v1 baseline)

| Area | Status | Notes |
|------|--------|-------|
| Settings data path + Test probe | Shipped | [`paths.py`](../plugin/ppt_master/paths.py), [`test_ppt_master_data_test_listener.py`](../tests/chatbot/test_ppt_master_data_test_listener.py) |
| Sidebar PPT-Master mode (Impress/Draw) | Shipped | [`chat_sidebar_mode.py`](../plugin/chatbot/chat_sidebar_mode.py) |
| Smol sub-agent session | Shipped | Venv-hosted via [`plugin/ppt_master/venv/`](../plugin/ppt_master/venv/); forked SKILL + host LLM/UNO RPC |
| Specialized tools | Shipped | [`tools.py`](../plugin/ppt_master/tools.py) — export, validate, template-fill, native-enhance (host UNO) |
| Main PPTX → ODP export | Shipped | [`uno_pptx_deck.py`](../plugin/ppt_master/adapter/uno_pptx_deck.py) + [`pptx_build.py`](../plugin/ppt_master/pptx_build.py) |
| PPTX auto-build from SVG | Shipped | User venv runs upstream `svg_to_pptx.py` when `exports/*.pptx` missing |
| Shape copy on import | Shipped | [`uno_shape_postprocess.py`](../plugin/ppt_master/adapter/uno_shape_postprocess.py) — clone + text props |
| Impress page size on import | Shipped | Target slide set to 25400×14288 hmm in `uno_pptx_import._ensure_target_page` |
| Multi-slide UNO tests | Shipped | [`test_ppt_master_pptx_import_uno.py`](../tests/uno/test_ppt_master_pptx_import_uno.py) — 3-slide fixture |
| Import fidelity script | Shipped | PPTX vs ODP PDF diff per slide |
| Real-project smoke | Validated | `ppt169_attention_is_all_you_need` via existing `exports/*.pptx` |
| Speaker notes matching | Partial | [`project_notes.py`](../plugin/ppt_master/project_notes.py); PPTX import copies notes from source slides |

### What is not done (gaps)

| Area | Status | Impact |
|------|--------|--------|
| LO PPTX import fidelity | **Good** | PPTX→ODP via native filter; fidelity script compares PDF pages |
| Speaker notes from project `notes/` on build | **Partial** | Notes in PPTX export when upstream `svg_to_pptx` runs; import copies from PPTX slides |
| template-fill route | **Stub** | Creates slides; does not call `set_placeholder_text` |
| beautify route | **Not wired** | `pptx_to_svg` → SVG pipeline absent |
| Browser-grade SVG reference | **Optional** | Upstream [`visual_review.py`](../ppt-master/skills/ppt-master/scripts/visual_review.py) (Playwright); not wired to fidelity script |
| `make test` typecheck | **Known issue** | `SendHandlerHost._in_ppt_master_mode` unresolved in [`send_handlers.py`](../plugin/chatbot/send_handlers.py) |

### Architecture decision log (do not re-litigate without cause)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| LO-hosted smol for PPT-Master sidebar | **Rejected** | Upstream SKILL is script/filesystem-driven; LO agent cannot run it (see [Sidebar sub-agent design](#sidebar-sub-agent-design)) |
| Draw tool merge into PPT-Master session | **Removed** | Upstream builds via script pipeline + PPTX export, not UNO shape editing |
| `get_ppt_master_skill_path` tool | **Removed** | SKILL loads in venv at session start; `read_ppt_master_workflow_file` for on-demand reads |
| Upstream Python in OXT | **No** — external skill tree | Avoid `python-pptx` on LO host; keep OXT small |
| `svg_convert.py` / `uno_apply.py` / `uno_svg_import` / `svg_preprocess` | **Removed** | Replaced by PPTX build + LO PPTX import |
| `bundled/svg_to_pptx/` | **Removed** | Was byte-identical copy; use user clone |
| Upstream attribution | **README only** | Shipped `.py` files are WriterAgent-original ([`contrib README`](../plugin/contrib/ppt_master/README.md)) |
| Fork upstream `SKILL.md` only (orchestration) | **Shipped** | [`plugin/contrib/ppt_master/skill/SKILL.md`](../plugin/contrib/ppt_master/skill/SKILL.md); scripts/templates stay in user clone; diff tracked in git |
| Fork full upstream Python tree | **Rejected** | Reference-only for scripts; expand rewrites or host-safe delegation instead |

**WriterAgent export path:**

```text
SVG (project) → venv svg_to_pptx → exports/*.pptx → LO PPTX import → Impress document (+ mirror ODP)
```

### Fidelity (PPTX → ODP)

Run [`scripts/ppt_master_import_fidelity.py`](../scripts/ppt_master_import_fidelity.py). Compares LO PDF render of each PPTX slide vs the same slide after `import_pptx_slide_to_odp`. Expect low `diff_fraction` when LO’s PPTX filter matches the source deck.

Primary files: [`uno_pptx_import.py`](../plugin/ppt_master/adapter/uno_pptx_import.py), [`fidelity.py`](../plugin/ppt_master/fidelity.py), [`pptx_build.py`](../plugin/ppt_master/pptx_build.py).

### Prioritized backlog

#### P0 — Validate end-to-end on a real project

**Status:** Done for [`ppt169_attention_is_all_you_need`](../ppt-master/examples/ppt169_attention_is_all_you_need) (16 slides). Repeat for additional upstream examples as regressions.

**Goal:** Confirm the main pipeline works on artifacts from upstream's normal workflow, not just unit-test SVGs.

**PM acceptance criteria:**

- User with skill tree configured can export a project folder containing `svg_final/` into an open Impress doc via PPT-Master sidebar mode.
- Slide count matches SVG count; at least one shape visible per slide.
- Document failure modes (missing path, empty folder, wrong data root) with clear tool errors.

**Dev steps:**

1. Manual test checklist (see [Manual E2E checklist](#manual-e2e-checklist-qa--pm)).
2. Run fidelity script; capture `report.json` for the example project.
3. Fixtures: [`tests/fixtures/ppt_master_minimal/`](../tests/fixtures/ppt_master_minimal/) (3 slides); add realistic snippets from failing real SVGs as needed.

**Files:** [`uno_pptx_deck.py`](../plugin/ppt_master/adapter/uno_pptx_deck.py), [`client.py`](../plugin/ppt_master/client.py), [`fidelity.py`](../plugin/ppt_master/fidelity.py).

---

#### P1 — PPTX → ODP fidelity

**Goal:** Keep PDF `diff_fraction` low on real projects (PPTX reference vs imported ODP).

**Dev strategy:** Run fidelity script on `ppt169_attention_is_all_you_need`; triage in [`uno_pptx_import.py`](../plugin/ppt_master/adapter/uno_pptx_import.py) or upstream PPTX build if reference/import diverge.

**Tests:** [`test_ppt_master_fidelity.py`](../tests/ppt_master/test_ppt_master_fidelity.py), [`test_ppt_master_pptx_import_uno.py`](../tests/uno/test_ppt_master_pptx_import_uno.py), [`test_ppt_master_pptx_build.py`](../tests/ppt_master/test_ppt_master_pptx_build.py).

---

#### P2 — Speaker notes matching

**Status:** Partial — [`project_notes.py`](../plugin/ppt_master/project_notes.py) maps project `notes/`; PPTX import copies notes from source slides.

**Goal:** Notes from project `notes/` land on the correct slides when skill tree absent or naming edge cases fail.

**Remaining:**

1. Broader fallback patterns when skill tree absent.
2. Tests: fixture project with mixed note naming; assert notes on imported slides via UNO.

---

#### P3 — Agent UX: SKILL + workflow context

**Status:** Shipped — venv runner loads the **WriterAgent fork** ([`plugin/contrib/ppt_master/skill/SKILL.md`](../plugin/contrib/ppt_master/skill/SKILL.md) via [`skill_paths.py`](../plugin/contrib/ppt_master/skill_paths.py)) plus data-root routing at session start ([`skill_context.py`](../plugin/ppt_master/venv/skill_context.py)); venv tools `read_ppt_master_workflow_file`, `run_ppt_master_script`, `read_project_file`, `write_project_file`. Fork uses **sidebar chat** for Eight Confirmations (Confirm UI / live-preview browser removed).

**Model guidance (upstream FAQ):** Claude Opus/Sonnet with large context (~1M) gives best results; GPT/Gemini/Kimi work with a lower ceiling. Sidebar model selection is forwarded to the host via `llm_request` RPC (API keys stay in `writeragent.json`).

**Tests:** [`test_ppt_master_venv_runner.py`](../tests/ppt_master/test_ppt_master_venv_runner.py), [`test_venv_ppt_master_rpc.py`](../tests/scripting/test_venv_ppt_master_rpc.py).

---

#### P4 — Secondary routes (defer until P0–P1 acceptable)

| Route | Tool | Adapter | Work |
|-------|------|---------|------|
| template-fill | `apply_ppt_master_template_fill` | [`uno_template_fill.py`](../plugin/ppt_master/adapter/uno_template_fill.py) | Wire `set_placeholder_text` via DrawBridge / existing draw tools; parse `fill_plan.json` |
| native-enhance | `apply_ppt_master_native_enhance` | [`uno_enhance.py`](../plugin/ppt_master/adapter/uno_enhance.py) | Extend when using `enhancement_plan.json` in practice |
| beautify | (none) | — | Design: venv `pptx_to_svg` → existing SVG pipeline; not started |

**PM note:** Do not prioritize beautify/template-fill until main SVG export looks good on real decks.

---

#### P5 — Tests and CI hygiene

| Task | File(s) | Done when |
|------|---------|-----------|
| Multi-slide UNO export | [`test_ppt_master_pptx_import_uno.py`](../tests/uno/test_ppt_master_pptx_import_uno.py) | ✅ 3 slides, shape checks |
| Import fidelity CLI | [`scripts/ppt_master_import_fidelity.py`](../scripts/ppt_master_import_fidelity.py) | ✅ PDF diff + `report.json` |
| Fix `ty` on PPT-Master send path | [`send_handlers.py`](../plugin/chatbot/send_handlers.py) | Declare `_in_ppt_master_mode: bool` on host class; `make test` typecheck passes |
| Regression fixtures | `tests/fixtures/ppt_master_*` | Minimal shipped; add realistic SVGs from failing slides |
| Example project baseline | `ppt169_attention_is_all_you_need` | Cover ~0.086 @ 300 dpi; slide 2 ~0.094 @ 150 dpi; slide 6 ~0.094 @ 150 dpi (from ~0.258 pre-fix) |

Run: `pytest tests/ppt_master/`; full matrix: `make test`.

### What not to do (unless requirements change)

- **Run PPT-Master smol inside LO embedded Python with SKILL.md injection only** — teaches a workflow the host cannot execute; use venv runner instead.
- **Re-merge Draw core tools into PPT-Master session** — upstream does not use UNO shape editing for the main route.
- **Re-vendor `plugin/contrib/ppt_master/bundled/svg_to_pptx/`** — external skill tree is intentional.
- **Fork full upstream Python tree for attribution** — symbol map lives in contrib README; orchestration is the tracked [`skill/SKILL.md`](../plugin/contrib/ppt_master/skill/SKILL.md) fork only.
- **Import `svg_to_pptx` package on LO host** — triggers `python-pptx` dependency ([`upstream.py`](../plugin/contrib/ppt_master/upstream.py) policy).
- **Spread effort across beautify + template-fill + fidelity in parallel** — finish P0/P1 first.

### Manual E2E checklist (QA / PM)

```text
[ ] git clone https://github.com/hugohe3/ppt-master.git
[ ] Settings → Python → PPT-Master data path → .../ppt-master (clone root) or .../ppt-master/skills/ppt-master
[ ] Settings → Python → user venv configured
[ ] pip install -r .../ppt-master/skills/ppt-master/requirements.txt (in user venv)
[ ] Settings → Test → SKILL.md, templates/, scripts/svg_to_pptx/ all yes
[ ] Open LibreOffice Impress (make deploy impress)
[ ] Sidebar → mode PPT-Master
[ ] Describe topic OR point agent at existing project with svg_final/
[ ] Agent runs scripts in venv; export_presentation_project imports to deck (host RPC)
[ ] Optional: python scripts/ppt_master_import_fidelity.py <project> — review .import_fidelity/SUMMARY.md
[ ] Verify: slide count, shapes visible, notes if present, PDF page sizes match in report.json
[ ] Note failures: element type, SVG file name, diff.png screenshot
```

### Roadmap summary (one line)

**Run fidelity script on real projects → PPTX→ODP import quality → notes matching → secondary routes → CI.**

---

## Tests

| File | Coverage |
|------|----------|
| `tests/ppt_master/test_ppt_master_sidebar.py` | sidebar flags, tool tier exclusion, no draw-tool merge |
| `tests/ppt_master/test_ppt_master_venv_runner.py` | path guards, SKILL load, venv session delegation |
| `tests/scripting/test_venv_ppt_master_rpc.py` | `llm_request` / `tool_call` / `worker_event` RPC dispatch |
| `tests/ppt_master/test_ppt_master_paths.py` | config path, dev clone, upstream `pptx_discovery` |
| `tests/chatbot/test_ppt_master_data_test_listener.py` | Settings Test button probe |
| `tests/ppt_master/test_ppt_master_project.py` | Project fixture, collect_svg_files, notes |
| `tests/ppt_master/test_ppt_master_pptx_build.py` | PPTX discovery + venv build |
| `tests/ppt_master/test_ppt_master_fidelity.py` | PNG diff math, summary writer |
| `tests/uno/test_ppt_master_pptx_import_uno.py` | LO PPTX import, multi-slide fixture |

**Import fidelity (agents):** [`scripts/ppt_master_import_fidelity.py`](../scripts/ppt_master_import_fidelity.py) — see [Import fidelity tooling](#import-fidelity-tooling-agents) above.

Run: `pytest tests/ppt_master/`; UNO: `python -m plugin.testing_runner test_ppt_master_pptx_import_uno`; full matrix: `make test`.
