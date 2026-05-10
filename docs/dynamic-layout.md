# Dynamic Sidebar Panel Layout (WriterAgent Chat)

## Overview

The chat sidebar uses a **hybrid XDL + runtime relayout** approach. Control definitions (types, labels, initial positions) live in `extension/WriterAgentDialogs/ChatPanelDialog.xdl`. At runtime, `_PanelResizeListener` (an `XWindowListener` in `plugin/chatbot/panel_resize.py`) repositions and resizes controls when the panel size changes: the **response** area grows vertically to fill space above a bottom-anchored cluster; **fluid** controls stretch horizontally to a fixed right margin; other controls stay fixed width and left-anchored.

Wiring happens in `plugin/chatbot/panel_wiring.py`. `ChatToolPanel.getHeightForWidth()` in `plugin/chatbot/panel_factory.py` negotiates width/height with LibreOffice’s sidebar deck layouter and must stay consistent with `_relayout()`’s width logic.

---

## How LibreOffice Uses `LayoutSize`

`XSidebarPanel.getHeightForWidth(width)` returns `LayoutSize(Minimum, Maximum, Preferred)`. The sidebar’s `DeckLayouter` (`sfx2/source/sidebar/DeckLayouter.cxx`) uses this to distribute height:

- Panels with **Maximum = -1** (unbounded) receive **remaining height** after fixed panels are satisfied.
- Field order for `uno.createUnoStruct("com.sun.star.ui.LayoutSize", ...)` is **Minimum, Maximum, Preferred** (IDL order matters).

WriterAgent returns `LayoutSize(100, -1, 400)`: minimum width hint 100, unbounded max height, preferred height 400.

---

## XDL as Baseline

`ChatPanelDialog.xdl` defines Map AppFont positions/sizes. After `ContainerWindowProvider.createContainerWindow()` loads the dialog, pixel geometry is the baseline for `_capture_initial()`. The root dialog width (e.g. 180 Map AppFont units) is aligned with `getMinimalWidth()` so the first paint matches the declared minimum sidebar width.

---

## Runtime layout: `_PanelResizeListener`

### Snapshot (`_capture_initial`)

On first `_relayout`, the listener records each control’s `(x, y, width, height)`, the window size, the response field’s bottom edge, and the vertical span of the “bottom cluster” (everything below the response). Control widths in the snapshot are **clamped up** to `_MIN_WIDTHS` when GTK/VCL briefly reports ultra-narrow widths (~10px), so later math does not lock in a broken baseline.

### Two-pass layout (`_relayout`)

1. **Width sync (root window)**  
   The panel’s **root** window width is synchronized with a **target** derived from the sidebar **parent** window and the last **deck** width hint (see [Parent vs deck width](#parent-vs-deck-width-divergence) below). This avoids the root staying wider than the visible column (or vice versa) when UNO reports inconsistent sizes.

2. **Non-response controls**  
   - **Fluid** (`response` is handled in pass 2): `query`, `status`, `model_selector`, `image_model_selector`, `aspect_ratio_selector`. Each gets `new_w = max(10, w - ox - fixed_margin)` (fill to a small right margin), then minimum widths from `_MIN_WIDTHS` are applied **without exceeding** available horizontal space (`avail`), so combos never force a width larger than the panel.  
   - **Fixed**: buttons, labels, checkboxes keep snapshot width (with minimum floors for non-fluid controls).

3. **Bottom anchoring**  
   Controls at or below the response’s original bottom edge are shifted vertically so the cluster sits near the window bottom while preserving intra-cluster spacing and not overlapping the response (see `bottom_top_new` / `gap_below_response`).

4. **Response area**  
   Second pass sets height from the top of the response to just above the bottom cluster, and width using the same right-margin rule as other fluid controls.

**Note:** Early design docs described **proportional horizontal scaling** (`new_w = ow * width_ratio`). The implementation intentionally switched to **fill-to-margin + minimum floors** to avoid feedback loops between intrinsic control sizing and panel width (especially on GTK).

---

## `ChatToolPanel.getHeightForWidth(width)`

Called by the deck layouter with a **width hint** (`deck_w`). The implementation:

1. Reads **parent** window size (`parent_w`, `parent_h`) from `ChatToolPanel.parent_window`.
2. Stores **`_last_deck_w = deck_w`** for `_relayout` to use on the next pass.
3. Computes **`eff_w`** using the same rule as `_relayout`’s parent sync ([divergence](#parent-vs-deck-width-divergence)).
4. **`setPosSize(0, 0, eff_w, h)`** on `PanelWindow` so the panel root matches the chosen column width.
5. Calls **`resize_listener.relayout_now(PanelWindow)`** because **`windowResized` does not always fire** when the layouter changes size programmatically.
6. Returns `LayoutSize(100, -1, 400)`.

---

## Parent vs deck width (“divergence”)

On GTK/VCL (notably LibreOffice 24), **two different width numbers** show up in logs:

- **`deck_w`**: width passed into `getHeightForWidth` (deck’s idea of the column).
- **`parent_w`**: width of the sidebar content parent (`xParentWindow`).

Often they track together when the user drags the sidebar splitter. Sometimes **`parent_w` grows with “intrinsic” layout** (e.g. long text, combo preferred size) even when **`deck_w` stays modest** — logs showed huge `parent_w` vs ~deck width. Conversely, locking the panel to `min(parent, deck)` **all the time** prevented fluid controls from **filling** a legitimately wide sidebar.

**Current rule** (must stay in sync in `panel_factory.py` and `panel_resize.py`; constant `_DIVERGENCE_PX = 80`):

- If **`parent_w <= deck_w + 80`**: treat parent and deck as **aligned** → use **`eff_w = parent_w`** (fill the visible column; fluid controls stretch).
- If **`parent_w > deck_w + 80`**: treat parent as **inflated** vs real allocation → use **`eff_w = min(parent_w, deck_w)`** to clamp runaway width.

`_relayout` repeats the same logic when syncing the root window width using `_deck_w_getter()` → `ChatToolPanel._last_deck_w`.

---

## Evolution: what we tried

| Approach | Intent | Outcome |
|----------|--------|---------|
| **Simple `setPosSize(width, h)` in `getHeightForWidth`** | Match deck width | Worked for basic cases; **combo dropdown** still clipped; typing could **widen** the panel on GTK. |
| **`_MIN_WIDTHS` + fill-to-margin for fluid controls** | Stop ~10px-wide controls; keep dropdown affordance visible | **Helped**; model comboboxes use ≥120px floor where space allows. |
| **`relayout_now` from `getHeightForWidth`** | Relayout when `windowResized` misses | **Necessary**; without it, sizes lag after layouter updates. |
| **Fixed Send button width** (`_measure_send_button_max_width` + `QueryTextListener.set_fixed_send_width`) | **Record / Send / Stop Rec** label changes resized the button and caused **~22px width steps** and feedback loops | **Worked**; measure max label width once after wiring, re-apply after each label change. |
| **`_column_width_cap`** (`min(parent, deck)`, grow only when parent≈deck) | Stop runaway width | **Stopped creep** but **blocked stretching** when the user widened the sidebar — fluid fields no longer filled the column. **Replaced** by divergence rule. |
| **Relayout after toggling “Use Image model”** | Visibility swap changes vertical stack | **Needed** so the visible model row gets correct widths. |
| **Wiring split: `panel_wiring.py`** | Smaller `panel_factory.py` | Organizational; behavior unchanged. |

Debugging relied on `writeragent_debug.log` lines: `getHeightForWidth deck_hint=...`, `_relayout: sync root ...`, `_relayout fluid widths=...`, `_capture_initial ctrl_widths=...`.

---

## Key files (WriterAgent)

| File | Role |
|------|------|
| `extension/WriterAgentDialogs/ChatPanelDialog.xdl` | Control definitions; baseline Map AppFont layout |
| `plugin/chatbot/panel_resize.py` | `_PanelResizeListener`, `_MIN_WIDTHS`, `_relayout` / `relayout_now` |
| `plugin/chatbot/panel_factory.py` | `ChatToolPanel`, `getHeightForWidth`, `getMinimalWidth`, image-mode relayout hook |
| `plugin/chatbot/panel_wiring.py` | `_wireControls`, resize listener construction, Send width measurement |
| `plugin/chatbot/panel.py` | `QueryTextListener` (Send label + fixed width) |
| `registry/.../Sidebar.xcu` | Deck / panel registration |

---

## Comparison: fully programmatic layout (e.g. writeragent2-style)

Some projects drop XDL and place every control with raw pixel math. That can work for a **minimal** toolbar, but it tends to drop controls (model selectors, checkboxes, image rows) or duplicate a lot of boilerplate. WriterAgent keeps **XDL as the declarative source** and a **single resize listener** for dynamic height and horizontal fill.

---

## Future work and simplification ideas

1. **Single source for `_DIVERGENCE_PX`**  
   Today the same literal `80` exists in `panel_factory.py` and `panel_resize.py`. A shared module-level constant (or small `layout_constants.py`) would prevent drift.

2. **Tune or auto-tune divergence**  
   `80` is empirical. If a theme consistently mis-reports parent vs deck, consider a slightly larger threshold or a short comment linking to LO/GTK versions where it was chosen.

3. **Reduce special cases**  
   If LibreOffice ever exposes a single authoritative “sidebar content width,” much of the parent/deck logic could be deleted. Until then, the divergence heuristic is a workaround for toolkit behavior.

4. **Declarative fluid list**  
   `fluid_controls` and `_MIN_WIDTHS` could be driven from one table (name → `{fluid, min_w}`) to avoid naming drift when XDL gains new fields.

5. **Tests**  
   UNO sidebar layout cannot be unit-tested outside LibreOffice. A **headless or screenshot harness** would be heavy; documenting manual checks (resize width/height, toggle image mode, Record/Send toggle, narrow sidebar) remains the practical approach.

6. **docs/dynamic-layout.md vs AGENTS.md**  
   Keep **AGENTS.md** as the short “what exists” pointer; this file is the **deep dive** for layout debugging and future refactors.

---

## Manual verification checklist

1. Open Writer (or Calc) → WriterAgent sidebar deck.  
2. **Resize sidebar width**: response, query, model combo should **stretch**; dropdown glyph should stay visible.  
3. **Resize window height**: response grows/shrinks; bottom cluster stays at bottom.  
4. **Type in query** (with recording enabled): **Send ↔ Record** should not widen the panel stepwise.  
5. **Toggle “Use Image model”**: no permanent overlap; relayout correct.  
6. **Narrow sidebar**: fluid widths should **not** exceed panel (no clipped-off combo button).  
7. Compare debug log `parent` vs `deck_hint` when anomalies appear.

---

## LibreOffice references

- `sfx2/source/sidebar/DeckLayouter.cxx` — height distribution, `getHeightForWidth` usage  
- `com.sun.star.ui.LayoutSize` — struct field order  
- XDL / Map AppFont: DevGuide graphical UIs, `xmlscript` DTD
