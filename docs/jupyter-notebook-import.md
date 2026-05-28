# Jupyter Notebook Import (`.ipynb`)

Back to the [core NumPy and Python guide](enabling_numpy_in_libreoffice.md).

WriterAgent can **read** Jupyter notebooks (nbformat v4) and **import** them into an open LibreOffice Writer document. Imported code cells are editable **form TextFields**; you can **run** them with the in-document **▶** button against a **shared Python kernel** per document (`notebook:…` session — same venv worker as WriterAgent scripting, not a Jupyter server).

For the full interactive roadmap (Run All, Stop, export, …), see [notebook-interactive-dev-plan.md](notebook-interactive-dev-plan.md).

## Table of contents

1. [Shipped vs deferred](#shipped-vs-deferred)
2. [How to use](#how-to-use)
3. [Run a code cell](#run-a-code-cell)
4. [Document layout](#document-layout-per-notebook-cell)
5. [Limits and stats](#limits-and-stats)
6. [Page-count bug (draw page, fixed 2026-05)](#page-count-bug-draw-page-fixed-2026-05)
7. [Debugging import and run](#debugging-import-and-run)
8. [Image loading and UI threading](#image-loading-and-ui-threading)
9. [Developer reference](#developer-reference)
10. [Deferred roadmap](#deferred-roadmap)
11. [Implementation status](#implementation-status)

---

## Jupyter notebook import (`.ipynb`) <!-- anchor: jupyter-notebook-import-ipynb -->

### Shipped vs deferred

| Shipped (2026-05) | Deferred |
|-------------------|----------|
| Vendored **nbformat v4** read — [`plugin/contrib/nbformat/`](../plugin/contrib/nbformat/): `read_ipynb(path)`, `reads(json_string)` → `NotebookNode` with `rejoin_lines` | **nbformat v3** upgrade |
| Menu: **Tools → Import Jupyter Notebook…** — [`import_dialog.py`](../plugin/notebook/import_dialog.py) | Full CommonMark/HTML for all markdown (HTML-tagged cells only today) |
| Import engine — [`writer_importer.py`](../plugin/notebook/writer_importer.py): headings, body text, in-flow code fields, images; **`zxx` locale** at import start (spellcheck off for imported body) | Run All / Stop ([dev plan](notebook-interactive-dev-plan.md) Phase 2) |
| **Notebook registry (Phase 0)** — [`cell_registry.py`](../plugin/notebook/cell_registry.py): `WriterAgentNotebookJson`, stable `cell_id`, output bookmarks `nb_out_*`, `WriterAgentNotebookSourcePath` | Export back to `.ipynb` (Phase 5) |
| **Run code cell (Phase 1)** — in-flow ▶ **push** button + [`notebook_controls.py`](../plugin/notebook/notebook_controls.py) + [`notebook_runner.py`](../plugin/notebook/notebook_runner.py); shared `notebook:…` kernel; UI drain on every run | Cell CRUD, sidebar (Phases 3–4) |
| Control lookup — [`form_lookup.py`](../plugin/notebook/form_lookup.py) indexes `ControlShape` models on the document draw page (required for wiring ▶ buttons) | Batched background image decode |
| **Reset Python Session** — clears `notebook:…` kernel for Writer docs with a registry ([`session_manager.py`](../plugin/scripting/session_manager.py)) | `notebook.enable_interactive` / Settings UI keys |
| Output images: `image/png`, `image/jpeg` in `display_data` / `execute_result` | JSON schema validation (`fastjsonschema`), `traitlets`, `jupyter_core` |
| Tests: [`tests/contrib/test_nbformat_read.py`](../tests/contrib/test_nbformat_read.py), [`tests/notebook/`](../tests/notebook/) | UNO smoke test for run cell (optional) |

**Why vendored nbformat, not PyPI:** Same pattern as [`local_python_executor.py`](../plugin/contrib/smolagents/local_python_executor.py) — no extra deps in the OXT. Do not `pip install nbformat` into LibreOffice.

**v3 note:** `reads()` raises `NBFormatError` if `nbformat` ≠ 4. To support v3 `.ipynb`, port upstream `v4/convert.py` into `plugin/contrib/nbformat/` and call `upgrade()` before `rejoin_lines`.

---

### How to use

1. Open a **Writer** document (empty or existing — import appends at the end).
2. **Tools → Import Jupyter Notebook…**
3. Pick a `.ipynb` file.
4. Wait for the completion dialog (cells / code fields / image counts). Large notebooks run on the **main thread**; the UI may pause — see [Debugging](#debugging-import-and-run).
5. Click **▶** beside a code cell to run it (see [Run a code cell](#run-a-code-cell)).

After `make deploy`, **restart LibreOffice** so the extension and menu handlers load. Re-import after upgrading WriterAgent if ▶ buttons do nothing (old builds may lack listener wiring).

---

### Run a code cell

| Action | How |
|--------|-----|
| **Run one cell** | Click the in-flow **▶** push button immediately before the code `TextField`. |
| **Shared variables** | All code cells in the same Writer document share one `notebook:…` Python namespace (like a Jupyter kernel). Run cell 0 (`x = 1`), then cell 1 (`print(x)`) → `1`. |
| **Execution count** | After a successful run, the gutter updates to `[In [n]]` (and the cell heading line when present). |
| **Reset kernel** | **WriterAgent → Reset Python Session** when the document has an imported notebook registry. Clears variables; re-run cells from the top if needed. |
| **Errors** | Tracebacks and stdout appear under the cell’s **Output** section (Preformatted Text). Empty code shows a msgbox. |
| **Sandbox** | Code runs in your configured user venv ([`venv_worker.py`](../plugin/scripting/venv_worker.py)), subject to the same AST safety rules as other WriterAgent scripting (e.g. dunder methods may be blocked by design). |

**Not supported yet:** Run All, Stop mid-batch, export to `.ipynb`, add/delete cells in the UI.

**Known limitation:** Re-running a cell may **not fully clear** the previous output paragraph until `clear_cell_output` is fixed in [`notebook_runner.py`](../plugin/notebook/notebook_runner.py) (`deleteContents` is not a valid Writer API — see [dev plan](notebook-interactive-dev-plan.md)).

**How ▶ wiring works (developers):** Form **URL** buttons do **not** reach the extension protocol handler. The importer creates **PUSH** `CommandButton` controls; after import, [`notebook_controls.py`](../plugin/notebook/notebook_controls.py) attaches `XActionListener` on the control **view** via `XControlAccess` (PyUNO must use `uno.getTypeByName`). Protocol dispatch `notebook.run_cell.{hex}` in [`main.py`](../plugin/main.py) remains available for future menu/URL entry points.

---

### Document layout (per notebook cell)

For each cell in order, the importer appends to the **document body**:

| Cell type | Structure in Writer |
|-----------|-------------------|
| **All cells** | **Heading 2** — `Cell N: Markdown` / `Cell N: Code` / `Cell N: Raw` |
| **code (gutter)** | **`WriterAgent Notebook In`** — `[In [k]]` or `[In [ ]]` (updates after ▶ run) |
| **markdown** | HTML-tagged source → [`insert_html_at_cursor`](../plugin/writer/ops.py); else **Text Body** plain text |
| **code (body)** | Gutter line + title → in-flow **▶** (`nb_run_{cell_id hex}`) → **TextField** (`nb_cell_{index}_code`) → **Heading 4** “Output” (bookmark `nb_out_{hex}`) → **Preformatted Text** / images |
| **raw** | **Text Body** — raw cell source |

**Code fields (in-flow):** `TextField` inside `ControlShape`, **`AS_CHARACTER`**, `insertTextContent` at document end — same pattern as [`CreateFormControl`](../plugin/writer/specialized/forms.py). Models are also registered on the document **draw page** (used to find controls for ▶ wiring and for `read_code_from_field`).

**Spellcheck:** At import start, document and paragraph styles used by the notebook are set to locale **`zxx`** (no linguistic content) so code and markdown are not spell-checked. Form field contents may still show squiggles depending on LO version.

**Text outputs:** Stream, error tracebacks (ANSI stripped), and `text/plain` from outputs use **Preformatted Text** (one paragraph per block).

**Paragraph styles:** Built-in names are resolved case-insensitively. **`WriterAgent Notebook In`** is auto-created for the `[In [n]]` gutter.

**Images:** `image/png` and `image/jpeg` in outputs are embedded via [`insert_image_at_locator`](../plugin/writer/images/image_tools.py) in the Output section.

---

### Limits and stats

| Limit | Value |
|-------|--------|
| Text per block (source or output) | 50 000 chars — suffix `[… truncated for import …]` |
| Outputs per code cell | 200 (extra outputs dropped with WARNING log) |
| Image base64 decode | 8 MB per image |
| Progress logging | INFO every 10 cells |

`import_ipynb_to_writer` returns stats: `cells`, `markdown`, `code`, `raw`, `shapes` (code fields + ▶ buttons), `images`, `outputs`, and legacy `controls` (= `shapes`).

---

### Page-count bug (draw page, fixed 2026-05)

An early importer placed code `ControlShape`s on the Writer **draw page** via `XDrawPage.add()` **without** `AnchorType`. That nested controls in the first heading and inflated page count.

**Fix:** Code fields and images use **in-flow** `insertTextContent` at document end. Draw page still holds shape references for form APIs; body flow is correct.

---

### Debugging import and run

Import and ▶ runs log to **`writeragent_debug.log`** next to `writeragent.json` (e.g. `~/.config/libreoffice/4/user/`). Use `"log_level": "DEBUG"` for wiring detail.

```bash
tail -f ~/.config/libreoffice/4/user/writeragent_debug.log
```

| Log line | Meaning |
|----------|---------|
| `notebook import start` / `complete` | Import began / finished |
| `notebook import read_ipynb cells=N` | JSON parse finished |
| `notebook import progress cell=30/120` | Still importing (INFO every 10 cells) |
| `notebook controls: indexed N form control model(s)` | Draw-page control index built |
| `notebook controls: wired M/K run button(s)` | ▶ listeners attached (`M` should equal code cell count) |
| `wired 0/K … missing_model=… no_view=…` | Wiring failed — redeploy extension and re-import |
| `notebook run cell index=… field=nb_cell_…` | ▶ click ran a cell |
| `failed to clear output for cell` | Output replace bug (run still executes) |
| `run_venv_code` / `Task … completed` | Python worker finished |

[`flush_ui_idle`](../plugin/notebook/writer_importer.py) calls `processEventsToIdle()` after import, after wiring, and after each run.

---

### Image loading and UI threading

There is **no** background UNO image import API. All `insertTextContent` / `GraphicURL` work runs on the **main thread**.

**Future optimization:** Decode base64 off-thread; queue inserts on main thread every *k* images.

---

## Developer reference

### Module map

```
plugin/
├── notebook/
│   ├── cell_registry.py       # WriterAgentNotebookJson, bookmarks, cell_id UUID
│   ├── form_lookup.py         # Find form models (draw page + text fallback)
│   ├── import_dialog.py       # File picker, post-import wire + msgbox
│   ├── notebook_controls.py   # ▶ PUSH buttons, XActionListener wiring
│   ├── notebook_runner.py     # run_cell, venv execute, output refresh
│   └── writer_importer.py     # Import loop, zxx locale, flush_ui_idle
└── contrib/nbformat/          # Vendored .ipynb reader (v4 read only)
```

Entry points: [`main.py`](../plugin/main.py) (`notebook.run_cell.*` dispatch, bootstrap wiring), [`session_manager.py`](../plugin/scripting/session_manager.py) (`notebook_session_id`).

Vendored nbformat: [`plugin/contrib/nbformat/README.md`](../plugin/contrib/nbformat/README.md).

---

## Deferred roadmap

**Phased plan:** [notebook-interactive-dev-plan.md](notebook-interactive-dev-plan.md).

| Item | Phase |
|------|--------|
| Fix output clear on re-run | Phase 1 polish |
| Run All / Run from here / Stop | 2 |
| Add / delete / reorder cells; re-import merge | 3 |
| Notebook sidebar (cell list, clear outputs) | 4 |
| Export `.ipynb` | 5 |
| In-kernel UNO via host tool proxy | 6 (deferred) |
| nbformat v3, full markdown, background import | Backlog |

---

## Implementation status

### Shipped (2026-05-28)

| Component | Status |
|-----------|--------|
| Vendored **nbformat v4** reader | [`plugin/contrib/nbformat/`](../plugin/contrib/nbformat/) |
| **Writer `.ipynb` import** | [`writer_importer.py`](../plugin/notebook/writer_importer.py), [`import_dialog.py`](../plugin/notebook/import_dialog.py) |
| **Notebook registry (Phase 0)** | [`cell_registry.py`](../plugin/notebook/cell_registry.py); `notebook:…` session; **Reset Python Session** |
| **Run single cell (Phase 1)** | [`notebook_runner.py`](../plugin/notebook/notebook_runner.py), [`notebook_controls.py`](../plugin/notebook/notebook_controls.py), [`form_lookup.py`](../plugin/notebook/form_lookup.py) |
| Tests | [`tests/notebook/`](../tests/notebook/) (registry, importer, form lookup, controls, runner, session) |

### Not shipped

Run All, Stop, cell CRUD, sidebar, export `.ipynb`, nbformat v3, full CommonMark markdown, background image import, optional `notebook.*` config keys.
