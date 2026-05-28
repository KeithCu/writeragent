# Jupyter Notebook Import (`.ipynb`)

Back to the [core NumPy and Python guide](enabling_numpy_in_libreoffice.md).

WriterAgent can **read** Jupyter notebooks (nbformat v4) and **import** them into an open LibreOffice Writer document. This is separate from the venv NumPy bridge ([core guide](enabling_numpy_in_libreoffice.md)): imported code cells are editable **form TextFields** in the document; they are **not** executed against a Jupyter kernel unless the user runs Python elsewhere.

## Table of contents

1. [Shipped vs deferred](#shipped-vs-deferred)
2. [How to use](#how-to-use)
3. [Document layout](#document-layout-per-notebook-cell)
4. [Limits and stats](#limits-and-stats)
5. [Page-count bug (draw page, fixed 2026-05)](#page-count-bug-draw-page-fixed-2026-05)
6. [Debugging import](#debugging-import-slow-or-frozen-ui)
7. [Image loading and UI threading](#image-loading-and-ui-threading)
8. [Developer reference](#developer-reference)
9. [Deferred roadmap](#deferred-roadmap)
10. [Implementation status](#implementation-status)

---

## Jupyter notebook import (`.ipynb`) <!-- anchor: jupyter-notebook-import-ipynb -->

### Shipped vs deferred

| Shipped (2026-05) | Deferred |
|-------------------|----------|
| Vendored **nbformat v4** read — [`plugin/contrib/nbformat/`](../plugin/contrib/nbformat/): `read_ipynb(path)`, `reads(json_string)` → `NotebookNode` with `rejoin_lines` (multi-line `source`, stream `text`, MIME bundles) | **nbformat v3** upgrade ([`nbformat/v4/convert.py`](https://github.com/jupyter/nbformat/blob/main/nbformat/v4/convert.py) in upstream) |
| Menu: **Tools → Import Jupyter Notebook…** — [`import_dialog.py`](../plugin/notebook/import_dialog.py) (file picker, Writer-only check, stats msgbox) | Full CommonMark/HTML renderer for all markdown (only HTML-tagged cells today) |
| Import engine — [`writer_importer.py`](../plugin/notebook/writer_importer.py): per-cell headings, body text, in-flow code fields, embedded plot images | Run All / Stop (Phase 2 — [dev plan](notebook-interactive-dev-plan.md)) |
| **Notebook registry (Phase 0)** — [`cell_registry.py`](../plugin/notebook/cell_registry.py): `WriterAgentNotebookJson` on the document (code cells, stable `cell_id`, output bookmarks `nb_out_*`); `WriterAgentNotebookSourcePath`; **Reset Python Session** clears `notebook:…` kernel ([`session_manager.py`](../plugin/scripting/session_manager.py)) | Export back to `.ipynb` |
| **Run code cell (Phase 1)** — in-flow ▶ button per code cell ([`notebook_runner.py`](../plugin/notebook/notebook_runner.py)); shared `notebook:…` kernel; output refresh from bookmark; UI drain via [`run_blocking_in_thread`](../plugin/framework/async_stream.py) on every run | Cell CRUD, sidebar, `.ipynb` export |
| Output images: `image/png`, `image/jpeg` in `display_data` / `execute_result` | Batched background decode queue (see [Image loading](#image-loading-and-ui-threading)) |
| Tests: [`tests/contrib/test_nbformat_read.py`](../tests/contrib/test_nbformat_read.py), [`tests/notebook/`](../tests/notebook/), [`tests/writer/test_image_tools_safe_property.py`](../tests/writer/test_image_tools_safe_property.py) | JSON schema validation (`fastjsonschema`), `traitlets`, `jupyter_core` |

**Why vendored nbformat, not PyPI:** Same pattern as [`local_python_executor.py`](../plugin/contrib/smolagents/local_python_executor.py) — no extra deps in the OXT; LO embedded Python stays light. Do not `pip install nbformat` into LibreOffice.

**v3 note:** `reads()` raises `NBFormatError` if `nbformat` ≠ 4. To support v3 `.ipynb`, port upstream `v4/convert.py` (drop `traitlets` logging) into `plugin/contrib/nbformat/` and call `upgrade()` before `rejoin_lines`.

### How to use

1. Open a **Writer** document (empty or existing — import appends at the end).
2. **Tools → Import Jupyter Notebook…** (registered from [`plugin/notebook/`](../plugin/notebook/) via manifest).
3. Pick a `.ipynb` file.
4. Wait for the completion dialog (cells / code fields / image counts). Large notebooks run on the **main thread**; the UI may pause — see [Debugging](#debugging-import-slow-or-frozen-ui).

After `make deploy`, restart LibreOffice so the menu handler loads.

### Document layout (per notebook cell)

For each cell in order, the importer appends to the **document body**:

| Cell type | Structure in Writer |
|-----------|-------------------|
| **All cells** | **Heading 2** — `Cell N: Markdown` / `Cell N: Code` / `Cell N: Raw` |
| **code (gutter)** | **`WriterAgent Notebook In`** — left-aligned `[In [k]]` or `[In [ ]]` when not executed (separate line before the cell heading, Jupyter-style breadcrumb) |
| **markdown** | If source contains HTML tags (e.g. Colab badge `<a>…<img>…`): **HTML (StarWriter)** import via [`insert_html_at_cursor`](../plugin/writer/ops.py). Otherwise **Text Body** plain text (`# headings`, etc.) |
| **code (body)** | **`WriterAgent Notebook In`** line `[In [k]]` + cell title → in-flow **▶** (`CommandButton`, `notebook.run_cell.{hex}`) + **TextField** (`nb_cell_{index}_code`) → **Heading 4** “Output” (bookmark `nb_out_*`) → **Preformatted Text** / images (static on import; replaced when you click ▶) |
| **raw** | **Text Body** — raw cell source |

**Code fields (in-flow, not draw page):** Each code cell gets one `com.sun.star.form.component.TextField` inside a `ControlShape`, anchored **`AS_CHARACTER`**, inserted with `text.insertTextContent` at **document end** after the “Code” heading — same pattern as Writer [`CreateFormControl`](../plugin/writer/specialized/forms.py). Height scales with line count (capped); width ~140 mm (`_DEFAULT_WIDTH` in 1/100 mm).

**Text outputs:** Stream, error tracebacks (ANSI stripped), and `text/plain` from `execute_data` / `execute_result` go to **Preformatted Text**, **one paragraph per block** (internal newlines preserved — not one Writer paragraph per line).

**Paragraph styles:** Built-in English names (`Heading 2`, `Text Body`, `Preformatted Text`, …) are resolved against the document’s **ParagraphStyles** (case-insensitive). On each import, **`WriterAgent Notebook In`** is created once if missing (left gutter for `[In [n]]` only). Missing or localized templates fall back quietly (no traceback spam).

**Images:** For each `image/png` or `image/jpeg` in cell outputs, base64 is decoded to a temp file and inserted via [`insert_image_at_locator`](../plugin/writer/images/image_tools.py) (uses `PropertySetInfo` for `GraphicURL` — **never** `hasattr` on UNO properties, which raised `UnknownPropertyException`). Embedded only (not linked). Inserted in the **Output** section after any text output. PNG size from IHDR (width capped at 140 mm).

**HTML / remote images:** Colab badges use `https://` image URLs inside HTML. LibreOffice loads those at import time if network access works; offline import may show the link without the badge graphic.

### Limits and stats

| Limit | Value |
|-------|--------|
| Text per block (source or output) | 50 000 chars — suffix `[… truncated for import …]` |
| Outputs per code cell | 200 (extra outputs dropped with WARNING log) |
| Image base64 decode | 8 MB per image |
| Progress logging | INFO every 10 cells |

`import_ipynb_to_writer` returns stats: `cells`, `markdown`, `code`, `raw`, `shapes` (code fields), `images`, `outputs`, and legacy `controls` (= `shapes`). The completion dialog shows cells, code-field count, and image count.

### Page-count bug (draw page, fixed 2026-05)

An early importer placed code `ControlShape`s on the Writer **draw page** via `XDrawPage.add()` **without** setting `AnchorType`. Writer defaulted to **as-character** anchoring at the **current text cursor**, which was still inside the first cell **Heading 2** after the first markdown cell. Every subsequent code field was nested in that heading with `text:soft-page-break` between controls — e.g. **144 code cells → ~184 pages** (`meta:page-count` matched cell count).

**Fix:** Code fields and images use **in-flow** `insertTextContent` at document end (`gotoEnd` before each insert). Do **not** revive draw-page stacking for notebook code without `AnchorType=AT_PAGE` and page-aware placement ([`plugin/draw/shapes.py`](../plugin/draw/shapes.py) `_try_writer_anchor_shape_before_add`).

### Debugging import (slow or “frozen” UI)

Import runs on LibreOffice’s **MainThread** (menu handler → `import_ipynb_to_writer`). There is no background import job yet.

**Log file:** `writeragent_debug.log` next to `writeragent.json` (e.g. `~/.config/libreoffice/4/user/` or `.../24/user/`). Set `"log_level": "DEBUG"` in `writeragent.json` (or **INFO** for progress only).

```bash
tail -f ~/.config/libreoffice/4/user/writeragent_debug.log
```

| Log line | Meaning |
|----------|---------|
| `notebook import start` | Import began after file pick |
| `notebook import read_ipynb cells=N` | JSON parse finished |
| `notebook import progress cell=30/120` | Still running (INFO every 10 cells) |
| `notebook import add step=code_field` / `step=image` | Per-control UNO timing (DEBUG) |
| `notebook import slow UNO add` | Single insert ≥2 s (WARNING) |
| `notebook import complete` | Finished; completion msgbox follows |

[`flush_ui_idle`](../plugin/notebook/writer_importer.py) calls `processEventsToIdle()` after import and around the msgbox. No `lockControllers()` during bulk import.

### Image loading and UI threading

There is **no** documented LibreOffice UNO API for “load this image in the background” during Writer import. Relevant pieces:

| Layer | What LO offers | WriterAgent today |
|-------|----------------|-------------------|
| **GraphicProvider** (`queryGraphic`) | Synchronous load from URL ([LO Programming ch.8](https://flywire.github.io/lo-p/08-Graphic_Content.html)) | Setting `GraphicURL` on `TextGraphicObject` does the same work on the calling thread |
| **`.uno:InsertGraphic`** (`AsLink`) | Dispatch insert; good for user files ([`image_tools`](../plugin/writer/images/image_tools.py)) | Not used for notebook plots (temp files must embed) |
| **UNO threading** | Document APIs are **not** thread-safe ([`queue_executor.py`](../plugin/framework/queue_executor.py)) | All `insertTextContent` / `GraphicURL` on **main thread** |
| **UI pump** | `XToolkit.processEventsToIdle()` | After import; can be extended to every *k* images |

**Future optimization (not implemented):** Worker thread only for base64 decode + temp file writes; queue paths to main thread for `insertTextContent` + `processEventsToIdle` every *k* images. Acceptable for typical tutorial notebooks synchronously; very image-heavy imports may stutter.

---

## Developer reference

### Module map

```
plugin/
├── notebook/                     # Writer .ipynb import (menu + UNO importer)
│   ├── cell_registry.py          # Document registry + output bookmarks
│   ├── import_dialog.py          # File picker, completion msgbox
│   └── writer_importer.py        # Cell loop, body text, code fields, images
└── contrib/nbformat/             # Vendored .ipynb reader (nbformat v4 only)
    ├── reader.py
    ├── notebooknode.py
    └── v4/rwbase.py              # rejoin_lines / split_lines / strip_transient
```

Vendored nbformat details: [`plugin/contrib/nbformat/README.md`](../plugin/contrib/nbformat/README.md).

---

## Deferred roadmap

**Phased implementation plan (Run / Stop / shared kernel / export):** [notebook-interactive-dev-plan.md](notebook-interactive-dev-plan.md).

- **nbformat v3** — port upstream `v4/convert.py` for legacy `.ipynb` files.
- **Full markdown rendering** — CommonMark/HTML for all markdown cells (today: HTML-tagged cells only).
- **Run / re-execute** — buttons on imported code cells; execute in shared `notebook:…` kernel — see [notebook-interactive-dev-plan.md](notebook-interactive-dev-plan.md) Phases 1–2. (Phase 0 shipped: registry + session id + reset only.)
- **Export to `.ipynb`** — round-trip Writer document → notebook file — see dev plan Phase 5.
- **Background import** — batched image decode queue; main-thread UNO inserts with periodic `processEventsToIdle`.

---

## Implementation status

### Shipped (2026-05)

| Component | Status |
|-----------|--------|
| Vendored **nbformat v4** reader | [`plugin/contrib/nbformat/`](../plugin/contrib/nbformat/), [`tests/contrib/test_nbformat_read.py`](../tests/contrib/test_nbformat_read.py) |
| **Writer `.ipynb` import** | [`plugin/notebook/`](../plugin/notebook/) (`import_dialog.py`, `writer_importer.py`, `cell_registry.py`), [`tests/notebook/`](../tests/notebook/) |
| **Notebook document model (Phase 0)** | Registry in UserDefinedProperties; `notebook:…` session id; **WriterAgent → Reset Python Session** on imported notebooks |

### Not shipped / deferred

- Export to `.ipynb`, Run on imported cells (execute in `notebook:…` kernel), full HTML/CommonMark markdown rendering, nbformat v3 upgrade, JSON schema validation (`fastjsonschema`), batched background image decode.
