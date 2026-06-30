# Integration Plan: PPT-Master in WriterAgent (Adapter Layer)

This document describes how [ppt-master](https://github.com/hugohe3/ppt-master) integrates with WriterAgent: a **UNO adapter layer**, **sidebar PPT-Master mode** (Impress/Draw only), and **upstream assets from a cloned skill tree** (not vendored in the OXT).

## Status (implementation summary)

| Decision | Choice |
|----------|--------|
| Upstream `svg_to_pptx` | **Not** copied into `plugin/contrib/` — loaded from skill tree `scripts/svg_to_pptx/` |
| WriterAgent-only code | Four modules under [`plugin/contrib/ppt_master/`](../plugin/contrib/ppt_master/) |
| Host / UNO | [`plugin/ppt_master/`](../plugin/ppt_master/) (client, paths, tools, adapters) |
| Sidebar UX | Smol sub-agent via [`plugin/chatbot/ppt_master.py`](../plugin/chatbot/ppt_master.py) — hidden from main chat (like Brainstorming) |
| Dev reference clone | Optional repo root `ppt-master/` (not shipped) |

**Removed during cleanup (no longer in tree):**

- `plugin/contrib/ppt_master/bundled/svg_to_pptx/` — byte-identical upstream copy (~18 files); deleted in favor of external skill tree
- `plugin/contrib/ppt_master/backends/` — unused protocol stubs
- `plugin/ppt_master/diagnostics.py` — install hint moved to `paths.PPT_MASTER_INSTALL_CMD`
- `plugin/ppt_master/venv/` — dead worker stub removed; export is host-side `draw_svg_import`
- `plugin/contrib/ppt_master/svg_convert.py`, `shape_ops.py`, `plugin/ppt_master/adapter/uno_apply.py` — removed; replaced by LO native SVG import

## Overview

ppt-master is an agentic workflow (SKILL.md + project artifacts + SVG → native shapes). WriterAgent:

1. Ships **adapter modules** under [`plugin/contrib/ppt_master/`](../plugin/contrib/ppt_master/) — see [`README.md`](../plugin/contrib/ppt_master/README.md)
2. Loads **unmodified upstream** Python and assets from the configured skill tree (`PPT_MASTER_DATA_ROOT`)
3. Hosts **UNO adapters** under [`plugin/ppt_master/`](../plugin/ppt_master/)
4. Exposes **PPT-Master** in the sidebar mode dropdown for **Impress and Draw only**
5. Runs a **smol sub-agent** when that mode is selected — tools use `specialized_domain="ppt-master"` and are excluded from the main agent / `delegate_to_specialized_draw_toolset`

## Packaging

Upstream [ppt-master](https://github.com/hugohe3/ppt-master) is a **skill/workflow repo**, not a pip package (no `pyproject.toml`). Install by cloning and pointing Settings at the skill directory:

```bash
git clone https://github.com/hugohe3/ppt-master.git
```

Then **Settings → Python** → **PPT-Master data path** → `.../ppt-master/skills/ppt-master` (must contain `SKILL.md`, `templates/`, `scripts/svg_to_pptx/`).

**Dev without manual path:** clone upstream beside the repo as `ppt-master/`; `paths._dev_clone_data_root()` finds `ppt-master/skills/ppt-master` automatically.

| Layer | In OXT? | Location |
|-------|---------|----------|
| UNO adapter (`coords`, `svg_preprocess`, `upstream`, `config`) | Yes | `plugin/contrib/ppt_master/` |
| Upstream `scripts/svg_to_pptx`, templates, references, `SKILL.md` | **No** — user clone / path | Resolved to `PPT_MASTER_DATA_ROOT` |
| UNO apply, client, tools | Yes | `plugin/ppt_master/` |
| Sidebar session | Yes | `plugin/chatbot/ppt_master.py` |

## Settings

On **Settings → Python** (bottom of tab):

| Control | Config key | Notes |
|---------|------------|-------|
| PPT-Master data path | `scripting.ppt_master_data_path` | Directory picker row (own line, below Python options) |
| Test | — | Probes `SKILL.md`, `templates/`, `scripts/svg_to_pptx/` via `data_root_status` |

Python venv path is separate; PPT-Master does **not** require a pip install of upstream.

## Architecture

```mermaid
flowchart TB
  UI[Sidebar PPT-Master mode] --> Session[ppt_master_session smol sub-agent]
  Session --> DrawTools[Existing draw/impress core tools]
  Session --> PmTools[ppt-master specialized tools]
  PmTools --> Pre[svg_preprocess]
  Pre --> HostSVG[draw_svg_import hidden doc]
  HostSVG --> Break[uno Break on GraphicObject]
  Break --> Copy[copy shapes + postprocess]
  Copy --> UNO[Impress/Draw document]
  Data[(skill tree: SKILL templates scripts/svg_to_pptx)] --> Upstream[upstream.py loads pptx_discovery]
  Upstream --> Session
  Data --> Session
```

### Data root resolution (`plugin/ppt_master/paths.py`)

1. `scripting.ppt_master_data_path` (Settings → Python)
2. `PPT_MASTER_DATA_ROOT` env (set by `apply_data_root_env`)
3. User venv `site-packages` scan (optional fallback)
4. Dev clone `ppt-master/skills/ppt-master`

`data_root_status()` requires templates/references/SKILL.md **and** `scripts/` (with `svg_to_pptx/`).

### Upstream import policy (`plugin/contrib/ppt_master/upstream.py`)

- Load `pptx_discovery.py` **by file path** so `svg_to_pptx/__init__.py` is not executed on the LO host (that import chain requires `python-pptx`).
- Full `svg_to_pptx` stack runs in the **user venv** when ppt-master workflow scripts need PPTX output.

### Main export path (UNO)

`export_presentation_project` → `uno_svg_deck.export_project_to_doc` → [`uno_svg_import.py`](../plugin/ppt_master/adapter/uno_svg_import.py):

```text
svg_final/*.svg
  → svg_preprocess (href + slide size in mm)
  → draw_svg_import (hidden Draw doc → one GraphicObjectShape)
  → .uno:Break (decompose into TextShape / paths / lines)
  → clone shapes to Impress slide (254×142.88 mm page)
  → uno_shape_postprocess (scale, fonts, text frame height)
```

**LibreOffice behavior:** `draw_svg_import` does **not** produce editable shapes directly — it embeds the SVG as one graphic. **Break** (same as UI: right-click → Break) is required to get native draw objects. There is no stock LO filter that skips this for arbitrary ppt-master SVGs.

### Import fidelity tooling (agents)

Compare **reference** vs **imported** output per slide using PDF as the interchange format:

| Step | Reference | Imported |
|------|-----------|----------|
| Source | Preprocessed SVG | `import_svg_to_slide` → one-slide ODP |
| PDF | `soffice --headless --convert-to pdf` | same |
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
3. Fix [`svg_preprocess.py`](../plugin/contrib/ppt_master/svg_preprocess.py), [`uno_shape_postprocess.py`](../plugin/ppt_master/adapter/uno_shape_postprocess.py), or [`uno_svg_import.py`](../plugin/ppt_master/adapter/uno_svg_import.py).
4. Re-run until `diff_fraction` ≤ threshold; export ODP for manual spot-check.

**Manual export (full deck):**

```bash
# From repo root, UNO Python — or use export_presentation_project in Impress sidebar
python -c "
import officehelper, uno
from pathlib import Path
from plugin.ppt_master.adapter.uno_svg_deck import export_project_to_doc
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

Reference PDF is LO’s render of the **preprocessed SVG** (single graphic — “perfect” baseline). Imported PDF is after **Break + copy + postprocess** (editable shapes). Remaining diff is real import fidelity gap, not canvas mismatch.

## Routes

| Route | Implementation | Notes |
|-------|----------------|-------|
| Main SVG pipeline | `export_presentation_project` → `uno_svg_import` | Default Impress/Draw path |
| template-fill | `apply_ppt_master_template_fill` → `uno_template_fill` | Incremental stub |
| native-enhance | `apply_ppt_master_native_enhance` → `uno_enhance` | `enhancement_plan.json` |
| beautify | venv `pptx_to_svg` + SVG pipeline | Not wired end-to-end yet |

## Key modules

| Module | Role |
|--------|------|
| [`plugin/chatbot/ppt_master.py`](../plugin/chatbot/ppt_master.py) | `ppt_master_session`, `collect_ppt_master_tools`, `ppt_master_finished` |
| [`plugin/chatbot/chat_sidebar_mode.py`](../plugin/chatbot/chat_sidebar_mode.py) | `CHAT_MODE_PPT_MASTER`, `sidebar_mode_flags_for_doc_type` |
| [`plugin/ppt_master/tools.py`](../plugin/ppt_master/tools.py) | Specialized tools (`ToolDrawPptMasterBase`) |
| [`plugin/contrib/ppt_master/svg_preprocess.py`](../plugin/contrib/ppt_master/svg_preprocess.py) | SVG normalization before LO import |
| [`plugin/ppt_master/adapter/uno_svg_import.py`](../plugin/ppt_master/adapter/uno_svg_import.py) | `draw_svg_import` → Break → copy shapes to Impress |
| [`plugin/ppt_master/adapter/uno_shape_postprocess.py`](../plugin/ppt_master/adapter/uno_shape_postprocess.py) | Clone shapes, font copy, text frame sizing |
| [`plugin/ppt_master/fidelity.py`](../plugin/ppt_master/fidelity.py) | PDF/PNG diff vs reference SVG render |
| [`scripts/ppt_master_import_fidelity.py`](../scripts/ppt_master_import_fidelity.py) | CLI fidelity loop for agents |
| [`plugin/framework/constants.py`](../plugin/framework/constants.py) | `IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS`, sub-agent instructions |

## Contrib merge policy

Only add files under `plugin/contrib/ppt_master/` when WriterAgent must **change** behavior. Do not re-vendor `svg_to_pptx/`. Upstream is **MIT** (Hugo He); WriterAgent adapters are **GPL-3.0-or-later** — see [`plugin/contrib/ppt_master/README.md`](../plugin/contrib/ppt_master/README.md) for the full MIT notice, upstream pin, and symbol map.

**Python files:** shipped adapters are WriterAgent-original; keep upstream attribution in README only (not in `.py` headers). When **vendoring** upstream lines, comment out replaced code with `'''` blocks in that file only (see [`plugin/contrib/nbformat/README.md`](../plugin/contrib/nbformat/README.md)).

---

## Roadmap

Backlog for PPT-Master integration work. **Priority order matters** — validate the main export path on real decks before secondary routes or large refactors.

### Agent quick start

1. Confirm dev setup: clone upstream beside repo as `ppt-master/` **or** set **Settings → Python → PPT-Master data path** to `.../ppt-master/skills/ppt-master`. Run **Test** in Settings.
2. Read [`plugin/contrib/ppt_master/README.md`](../plugin/contrib/ppt_master/README.md) symbol map (WriterAgent module ↔ upstream equivalent).
3. Trace the happy path: `export_presentation_project` → [`client.py`](../plugin/ppt_master/client.py) → [`uno_svg_deck.py`](../plugin/ppt_master/adapter/uno_svg_deck.py) → [`uno_svg_import.py`](../plugin/ppt_master/adapter/uno_svg_import.py).
4. Run tests: `pytest tests/ppt_master/`; UNO: `python -m plugin.testing_runner test_ppt_master_svg_import_uno`; fidelity: `python scripts/ppt_master_import_fidelity.py <project>`.
5. Pick the next roadmap item below; run fidelity script before/after changes.
6. Add tests in the matching `test_*` file per [AGENTS.md](../AGENTS.md).

### What is done (v1 baseline)

| Area | Status | Notes |
|------|--------|-------|
| Settings data path + Test probe | Shipped | [`paths.py`](../plugin/ppt_master/paths.py), [`test_ppt_master_data_test_listener.py`](../tests/chatbot/test_ppt_master_data_test_listener.py) |
| Sidebar PPT-Master mode (Impress/Draw) | Shipped | [`chat_sidebar_mode.py`](../plugin/chatbot/chat_sidebar_mode.py) |
| Smol sub-agent session | Shipped | [`ppt_master.py`](../plugin/chatbot/ppt_master.py), instructions in [`constants.py`](../plugin/framework/constants.py) `PPT_MASTER_SUB_AGENT_INSTRUCTIONS` |
| Specialized tools | Shipped | [`tools.py`](../plugin/ppt_master/tools.py) — export, validate, template-fill, native-enhance, skill path |
| Main SVG → UNO export | Shipped | `draw_svg_import` + **Break** via [`uno_svg_import.py`](../plugin/ppt_master/adapter/uno_svg_import.py) |
| SVG preprocess | Shipped | [`svg_preprocess.py`](../plugin/contrib/ppt_master/svg_preprocess.py) — image hrefs, **slide width/height in mm** (254×142.88 for 16:9) |
| Shape postprocess | Shipped | [`uno_shape_postprocess.py`](../plugin/ppt_master/adapter/uno_shape_postprocess.py) — see [postprocess fixes](#postprocess-fixes-jun-2025) |
| Impress page size on import | Shipped | Target slide set to 25400×14288 hmm in `_ensure_target_page` (was LO default 28000×15750) |
| Multi-slide UNO tests | Shipped | [`test_ppt_master_svg_import_uno.py`](../tests/uno/test_ppt_master_svg_import_uno.py) — 3-slide fixture |
| Import fidelity script | Shipped | [`scripts/ppt_master_import_fidelity.py`](../scripts/ppt_master_import_fidelity.py) + [`fidelity.py`](../plugin/ppt_master/fidelity.py) |
| Real-project smoke | Validated | `ppt169_attention_is_all_you_need` — 16 slides, ~959 shapes; cover slide PDF diff ~8.6% after page-size fix |
| Speaker notes matching | Partial | Upstream `find_notes_files` when skill tree configured; fallback `slide_NN.md` |

#### Postprocess fixes (Jun 2025)

Issues found exporting real decks; fixes live in [`uno_shape_postprocess.py`](../plugin/ppt_master/adapter/uno_shape_postprocess.py) and [`svg_preprocess.py`](../plugin/contrib/ppt_master/svg_preprocess.py):

| Problem | Cause | Fix |
|---------|-------|-----|
| One object per slide | `draw_svg_import` → single `GraphicObjectShape` | `.uno:Break` in [`uno_svg_import.py`](../plugin/ppt_master/adapter/uno_svg_import.py) |
| Wrong fonts / sizes | Plain `setString` reset char props; px slide dimensions overscaled page ~1.33× | Copy per-run `CharHeight`/`CharFontName`; preprocess sets **mm** not px |
| Text lines overlapping | `TextAutoGrowHeight` + `setString` ballooned frames; LO Break frames taller than one line | `TextAutoGrowHeight=False`, re-apply source size after text copy; tighten frame height to ~`CharHeight×1.05` |
| PDF/ODP canvas mismatch | New Impress doc default 280 mm wide | Set page 25400×14288 hmm before shape copy |

### What is not done (gaps)

| Area | Status | Impact |
|------|--------|--------|
| SVG fidelity vs upstream `drawingml_converter` | **Reduced** | LO Break + pre/post-process; gradients, diagram edges ~8–15% PDF diff on cover |
| Speaker notes matching | **Partial** | Upstream `find_notes_files` when skill tree present |
| SKILL.md auto-injection | **Manual** | Agent must call `get_ppt_master_skill_path`; workflow not pre-loaded |
| template-fill route | **Stub** | Creates slides; does not call `set_placeholder_text` |
| beautify route | **Not wired** | `pptx_to_svg` → SVG pipeline absent |
| Browser-grade SVG reference | **Optional** | Upstream [`visual_review.py`](../ppt-master/skills/ppt-master/scripts/visual_review.py) (Playwright); not wired to fidelity script |
| `make test` typecheck | **Known issue** | `SendHandlerHost._in_ppt_master_mode` unresolved in [`send_handlers.py`](../plugin/chatbot/send_handlers.py) |

### Architecture decision log (do not re-litigate without cause)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Upstream Python in OXT | **No** — external skill tree | Avoid `python-pptx` on LO host; keep OXT small |
| `svg_convert.py` / `uno_apply.py` | **Removed** | Replaced by LO `draw_svg_import` + Break; do not resurrect |
| `bundled/svg_to_pptx/` | **Removed** | Was byte-identical copy; use user clone |
| Upstream attribution | **README only** | Shipped `.py` files are WriterAgent-original ([`contrib README`](../plugin/contrib/ppt_master/README.md)) |
| Fork upstream for clarity | **Rejected for now** | Reference-only; expand rewrites or host-safe delegation instead |

**Two export paths (only first is WriterAgent's job today):**

```text
WriterAgent (Impress/Draw):  SVG → preprocess → draw_svg_import → Break → copy shapes → postprocess → UNO document
Upstream (PPTX):             SVG → drawingml_converter → DrawingML → python-pptx → .pptx
```

### Known fidelity gaps (LO import + pre/post-process)

Use this table when triaging visual bugs from real projects. Run [`scripts/ppt_master_import_fidelity.py`](../scripts/ppt_master_import_fidelity.py) first.

| SVG / feature | WriterAgent today | Mitigation |
|---------------|-------------------|------------|
| Editable shapes | Break after `draw_svg_import` | [`uno_svg_import._break_svg_graphic_objects`](../plugin/ppt_master/adapter/uno_svg_import.py) |
| Slide / PDF page size | Preprocess mm + `_ensure_target_page` | 254×142.88 mm; check `pdfinfo` in fidelity report |
| Paths, transforms, basic shapes | LO Break → native shapes | — |
| Text fonts | Per-run char prop copy on clone | [`uno_shape_postprocess._copy_shape_text`](../plugin/ppt_master/adapter/uno_shape_postprocess.py) |
| Text line spacing | Frame height + AutoGrow | `_fit_text_frame_height`, `TextAutoGrowHeight=False` |
| Gradients | LO approximates | Post-process fills if needed |
| Complex `tspan` text | LO import gaps | Post-process text props |
| `<image>` hrefs | Preprocess resolves paths | `svg_preprocess.py` |
| Diagram vs text diff | Break changes anti-aliasing | Expect higher diff on path-heavy regions; tune threshold |

**Measured (cover slide, `ppt169_attention_is_all_you_need`):** PDF page sizes matched → **diff_fraction ≈ 0.086** @ 300 dpi (left text ~0.09, right diagram ~0.16). Wrong Impress page size inflated diff to ~0.12.

Primary files to change for fidelity: [`svg_preprocess.py`](../plugin/contrib/ppt_master/svg_preprocess.py), [`uno_shape_postprocess.py`](../plugin/ppt_master/adapter/uno_shape_postprocess.py), [`uno_svg_import.py`](../plugin/ppt_master/adapter/uno_svg_import.py).

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

**Files:** [`uno_svg_deck.py`](../plugin/ppt_master/adapter/uno_svg_deck.py), [`client.py`](../plugin/ppt_master/client.py), [`fidelity.py`](../plugin/ppt_master/fidelity.py).

---

#### P1 — SVG → UNO fidelity (highest technical ROI)

**Goal:** Drive PDF `diff_fraction` down on real `svg_final/` decks using the fidelity loop.

**PM acceptance criteria:**

- Cover + 2 inner slides from `ppt169_attention_is_all_you_need` pass `--threshold 0.08` (stretch) or stay ≤ 0.12 (baseline).
- No text overlap on title stacks; shape count ≥ SVG `<text>` count per slide.

**Dev strategy (in order):**

1. **Measure:** `python scripts/ppt_master_import_fidelity.py <project> --slides …` → inspect `diff.png`, `pdfinfo` page-size lines in `report.json`.
2. **Preprocess:** href resolution, mm dimensions, future: `<use>`/defs, inline styles LO rejects.
3. **Import:** Break rounds, page size, temp-doc quirks in [`uno_svg_import.py`](../plugin/ppt_master/adapter/uno_svg_import.py).
4. **Postprocess:** fonts, frame height, scaling in [`uno_shape_postprocess.py`](../plugin/ppt_master/adapter/uno_shape_postprocess.py).
5. **Optional reference:** wire Playwright PNG from upstream `visual_review.py` as `--reference browser` (not implemented).

**Do not:** revive deleted `svg_convert.py` / `uno_apply.py` — LO native path is the architecture.

**Tests:** extend [`test_ppt_master_svg_preprocess.py`](../tests/ppt_master/test_ppt_master_svg_preprocess.py), [`test_ppt_master_fidelity.py`](../tests/ppt_master/test_ppt_master_fidelity.py), [`test_ppt_master_svg_import_uno.py`](../tests/uno/test_ppt_master_svg_import_uno.py).

---

#### P2 — Speaker notes matching

**Status:** Partial — [`uno_svg_deck._notes_for_slides`](../plugin/ppt_master/adapter/uno_svg_deck.py) calls upstream `find_notes_files` when data root is configured.

**Goal:** Notes from project `notes/` land on the correct slides when skill tree absent or naming edge cases fail.

**Remaining:**

1. Broader fallback patterns when skill tree absent.
2. Tests: fixture project with mixed note naming; assert notes on imported slides via UNO.

---

#### P3 — Agent UX: SKILL + workflow context

**Goal:** Sub-agent follows ppt-master workflow without user re-pasting SKILL.md.

**Today:** [`PPT_MASTER_SUB_AGENT_INSTRUCTIONS`](../plugin/framework/constants.py) tells agent to call `get_ppt_master_skill_path`; no automatic injection.

**Dev steps:**

1. On session start in [`ppt_master.py`](../plugin/chatbot/ppt_master.py) `_run_ppt_master_agent`, after `apply_data_root_env`:
   - Read first N KB of `SKILL.md` from data root (or summaries from `references/`).
   - Append to instructions block (cap token size).
2. Optionally add tool `read_ppt_master_workflow_file` (relative path under data root) for on-demand reads.
3. Document route boundaries (main SVG vs template-fill vs beautify) in injected text — mirror upstream `workflows/routing.md` summary.

**Tests:** mock data root with fake `SKILL.md`; assert instructions contain expected substring (unit test on helper, not full smol run).

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
| Multi-slide UNO export | [`test_ppt_master_svg_import_uno.py`](../tests/uno/test_ppt_master_svg_import_uno.py) | ✅ 3 slides, shape + text frame checks |
| Import fidelity CLI | [`scripts/ppt_master_import_fidelity.py`](../scripts/ppt_master_import_fidelity.py) | ✅ PDF diff + `report.json` |
| Fix `ty` on PPT-Master send path | [`send_handlers.py`](../plugin/chatbot/send_handlers.py) | Declare `_in_ppt_master_mode: bool` on host class; `make test` typecheck passes |
| Regression fixtures | `tests/fixtures/ppt_master_*` | Minimal shipped; add realistic SVGs from failing slides |
| Example project baseline | `ppt169_attention_is_all_you_need` | Fidelity metrics recorded in this doc |

Run: `pytest tests/ppt_master/`; full matrix: `make test`.

### What not to do (unless requirements change)

- **Re-vendor `plugin/contrib/ppt_master/bundled/svg_to_pptx/`** — external skill tree is intentional.
- **Fork upstream Python only for attribution** — symbol map lives in contrib README.
- **Import `svg_to_pptx` package on LO host** — triggers `python-pptx` dependency ([`upstream.py`](../plugin/contrib/ppt_master/upstream.py) policy).
- **Spread effort across beautify + template-fill + fidelity in parallel** — finish P0/P1 first.

### Manual E2E checklist (QA / PM)

```text
[ ] git clone https://github.com/hugohe3/ppt-master.git
[ ] Settings → Python → PPT-Master data path → .../ppt-master/skills/ppt-master
[ ] Settings → Test → SKILL.md, templates/, scripts/svg_to_pptx/ all yes
[ ] Open LibreOffice Impress (make deploy impress)
[ ] Sidebar → mode PPT-Master
[ ] Project with svg_final/ (from upstream workflow or examples/)
[ ] Agent or tool: export_presentation_project(project_path=...)
[ ] Optional: python scripts/ppt_master_import_fidelity.py <project> — review .import_fidelity/SUMMARY.md
[ ] Verify: slide count, shapes visible, notes if present, PDF page sizes match in report.json
[ ] Note failures: element type, SVG file name, diff.png screenshot
```

### Roadmap summary (one line)

**Run fidelity script on real projects → lower PDF diff via preprocess/import/postprocess → notes matching → SKILL context → secondary routes → CI.**

---

## Tests

| File | Coverage |
|------|----------|
| `tests/ppt_master/test_ppt_master_coords.py` | coords helpers |
| `tests/ppt_master/test_ppt_master_sidebar.py` | sidebar flags, tool tier exclusion |
| `tests/ppt_master/test_ppt_master_paths.py` | config path, dev clone, upstream `pptx_discovery` |
| `tests/chatbot/test_ppt_master_data_test_listener.py` | Settings Test button probe |
| `tests/ppt_master/test_ppt_master_project.py` | Project fixture, collect_svg_files, notes |
| `tests/ppt_master/test_ppt_master_svg_preprocess.py` | SVG preprocess (href, mm dimensions) |
| `tests/ppt_master/test_ppt_master_fidelity.py` | PNG diff math, SVG text counts, summary writer |
| `tests/uno/test_ppt_master_svg_import_uno.py` | LO import: Break, multi-slide, text frame height |

**Import fidelity (agents):** [`scripts/ppt_master_import_fidelity.py`](../scripts/ppt_master_import_fidelity.py) — see [Import fidelity tooling](#import-fidelity-tooling-agents) above.

Run: `pytest tests/ppt_master/`; UNO: `python -m plugin.testing_runner test_ppt_master_svg_import_uno`; full matrix: `make test`.
