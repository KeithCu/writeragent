# Writer Python sidebar — ideas

Status: **ideas only** (no implementation). The Writer Python deck is a native sidebar (not chat). Calc hides its `=PY()` chrome here, so Writer currently shows little more than venv/session status plus Reset/Settings.

Goal: make the deck useful for **Run Python Script** without a second editor or a new helper registry.

Related: [`../scripting/librepy-split.md`](../scripting/librepy-split.md) (LibrePy Python sidebar), [`../scripting/numpy-domains.md`](../scripting/numpy-domains.md) (domain helpers), `plugin/librepy/python_sidebar.py`, `plugin/scripting/document_scripts.py`, `plugin/scripting/python_runner.py`.

LibrePy constraints still apply: sidebar header/hamburger must not import `plugin.main`, `llm_client`, embeddings, or MCP. Do not fall back to a Calc document from a Writer frame.

---

## Current Writer surface

Header: Settings, Run Python Script, Insert LaTeX, hamburger.

Visible body: runtime status, Reset, Settings.

Hidden (Calc-only): `=PY()` cell list, diagnostics filter/list/detail, Refresh / Edit cell / Run script / Edit init.

Run Python Script already merges **document scripts**, **user scripts**, and **domain helper templates** in the Monaco/native picker (`build_xdl_script_picker_state`). The Writer sidebar does not show that merge.

---

## Proposed core: one list + Run

**One listbox.** Three *sources*, not three lists:

| Source | What | Already stored as |
|--------|------|-------------------|
| Document | Named scripts on this `.odt` (`WriterAgentDocumentPythonScripts`) | `SCRIPT_ORIGIN_DOCUMENT` |
| User | Global named scripts in config | `SCRIPT_ORIGIN_USER` |
| Helpers | Shipped templates (vision, math, units, viz, quant, text analytics) | picker origins / `PICKER_WIRING` |

Reuse the existing picker merge and display prefixes (`Units: convert_quantity`, etc.). Filter with the same `supports_*` predicates as Run Python Script so Calc-only sections (SQL, optimize, forecast, sheet analysis) stay out.

Suggested sort: document scripts, then user scripts, then helpers (or last-run first). Prefixes already disambiguate; no section headers required if the list stays short.

### Actions

- **Run** — `execute_and_insert_result` on that item’s code (same path as the dialog). Double-click can be Run.
- **Edit…** — open the existing Run Python Script / Monaco window on that item. **No inline Monaco in the deck.**

Insert still uses the Writer HTML / Math / plot egress the runner already has.

---

## Parameters (units, topic count, OCR engine, …)

Helpers are templates whose knobs live in the machine header:

`# writeragent:<tag> helper=… params={…}`

plus the `run_*` call. Selection already fills implicit args (`text`, `data`, `image`). Sticky knobs are the rest (`from`/`to` on units, `n_topics`, vision `engine`).

**Do not** add a generic JSON/param form in the AWT sidebar. Monaco (or the native multiline dialog) is the parameter UI.

| Selected item | Sidebar **Run** | **Edit** |
|---------------|-----------------|----------|
| Document or user script | Always run as stored (params already chosen). | Open editor on that script. |
| Helper with no extra knobs (readability, entities, OCR defaults) | Run now; bind selection / whole doc the same way RPS does. | Optional. |
| Helper with extra knobs (`convert_quantity` units, …) | Either run **shipped defaults**, or treat Run as **Edit** when defaults would surprise (e.g. `m/s` → `km/h`). | Open editor with the template. |

**Save as document script** (already in the picker) is how a user freezes units or topic count. After that, the item is a document script and one-click Run is correct.

Optional later, still not a form:

- Remember last-used params per helper in config (you already remember last script name).
- A *narrow* extra control only if one helper dominates (two fields for units `from`/`to`). Not a schema-driven inspector.

Selection is the other parameter. A one-line status under the list is enough: “Will use: selection, 1,204 chars” / “3 images” / “no selection → whole document.” Text analytics, vision, and units already resolve those inputs on Run.

---

## Later (not required for a useful first slice)

These were brainstormed as extra panes; they should **not** become extra lists if the script list is the product.

- **Last-run log** — stdout / traceback / insert preview. Calc already has a bounded diagnostics store for `=PY()`; Writer runs currently vanish when the dialog closes. Could be a detail box *under* the same list, not a second list.
- **Session peek** — names in the venv after last run; makes Reset less mysterious.
- **Venv health** — reuse `venv_diagnostics` (spaCy / matplotlib present). Status line, not a list.
- **Writer INIT** — document-open snippet (Calc already has Edit Init).
- **MRU** — last scripts can sort to the top of the *same* list.
- **Egress snippets** — insert HTML table, Math, heading tree: ship as extra helper templates in that list.

---

## Out of scope

- Second Monaco in the sidebar.
- Chat, embeddings, MCP on this deck.
- Writer Navigator / heading outline.
- Showing Calc `=PY()` cells or another open spreadsheet.
- A fourth domain/helper registry (`PICKER_WIRING` is the list).

---

## First slice (if built)

1. Populate one list from `build_xdl_script_picker_state` (Writer-filtered).
2. **Run** and **Edit** wired to existing runner / dialog.
3. Optional one-line “what will Run use?” status.
4. Keep header + Reset + Settings.

Params stay in script text. Custom units (etc.) become document scripts after Edit + Save.

## Later slice

Last-run detail under the list; last-used helper params; INIT only if users miss it.
