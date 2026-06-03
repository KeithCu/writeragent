# Dynamic Sidebar Panel Layout (WriterAgent Chat)

## Overview

The chat sidebar uses **XDL for control definitions** plus a **small deterministic relayout**. Control types, labels, and baseline positions live in [`extension/WriterAgentDialogs/ChatPanelDialog.xdl`](../extension/WriterAgentDialogs/ChatPanelDialog.xdl). At runtime, `_PanelResizeListener` in [`plugin/chatbot/panel_resize.py`](../plugin/chatbot/panel_resize.py) applies one rule:

- **Bottom band** (`status`, query row, buttons, toggles, model controls): keep XDL spacing, anchor the block near the panel bottom.
- **Chat history** (`response` / RichTextControl): fill all remaining vertical space above that band.

Wiring is in [`plugin/chatbot/panel_wiring.py`](../plugin/chatbot/panel_wiring.py). Width negotiation lives in [`ChatToolPanel.getHeightForWidth()`](../plugin/chatbot/panel_factory.py); height is owned by LibreOffice’s deck layouter.

---

## Layout model

Pure function: `compute_chat_panel_layout(width, height, snapshot)`.

1. **Snapshot once** after the dialog loads: each control’s `(x, y, width, height)` (Send/Stop/Clear widths are measured in wiring **before** the first relayout so label toggles do not fight the snapshot).
2. **Measure bottom band height** from the snapshot: `cluster_height = bottom_bottom - status_top` (all controls in `_BOTTOM_CLUSTER`).
3. **Anchor bottom band**: `bottom_top = height - bottom_margin - cluster_height`; shift every bottom control by the same delta from its XDL `y`.
4. **Size transcript**: `response.height = bottom_top - gap - response.y` with fixed XDL gap (2px) and minimum height (30px).
5. **Stretch width** for `response`, `query`, `status`, and model combos; clamp query/model rows with `_content_right_from_layout` / `_CONTENT_EDGE_CLAMP` (Clear button right, capped by panel width).

**XDL baseline:** Mutually exclusive rows (text `model_*` vs image `image_model_*` / aspect / base size) share the same Y positions so `_BOTTOM_CLUSTER` height is identical in both visibility modes.

Rich text does not participate in layout math. [`sync_rich_control_bounds`](../plugin/chatbot/rich_text_control.py) receives `last_response_rect` from `_PanelResizeListener` and applies inset bounds only. Query/model width caps are **not** recomputed in rich code.

---

## `ChatToolPanel.getHeightForWidth(width)`

Called by the deck layouter with a **width hint** (`deck_w`):

1. Computes **`eff_w`** from the deck hint (with the startup mis-hint clamp when the panel is docked narrow but the hint is frame-wide).
2. **`setPosSize(0, 0, eff_w, current_h)`** — updates width only; keeps the root height LibreOffice already allocated.
3. Calls **`resize_listener.relayout_now(PanelWindow)`** because `windowResized` does not always fire after programmatic resize.
4. Returns `LayoutSize(100, -1, 400)`.

---

## Rich text sidebar

When `rich_text_control_sidebar=True`:

1. Plain `response` is hidden after RichTextControl is ready.
2. `_PanelResizeListener.relayout_now()` runs once (after hide); it sizes the hidden placeholder and syncs `response_rich` from `last_response_rect` (single resize owner — `RichTextControlListener` does not handle `windowResized`).
3. `RichTextControlListener._sync_bounds()` may refresh peer paint after history render.

There is no deferred relayout chain, height clamp history, or gap re-capture from live GTK geometry.

---

## Key files

| File | Role |
|------|------|
| `extension/WriterAgentDialogs/ChatPanelDialog.xdl` | Baseline control geometry |
| `plugin/chatbot/panel_resize.py` | `compute_chat_panel_layout`, `_PanelResizeListener` |
| `plugin/chatbot/panel_factory.py` | `getHeightForWidth`, pre-negotiation width cap |
| `plugin/chatbot/panel_wiring.py` | Listener wiring, rich init, Send/Stop/Clear width measurement before first relayout |
| `plugin/chatbot/rich_text_control.py` | RichText bounds from `placeholder_rect` |
| `tests/chatbot/test_panel_resize.py` | Pure layout + listener integration tests |

---

## Manual verification

1. Open Writer → WriterAgent sidebar.
2. **Height**: transcript grows/shrinks; status/query/buttons stay visible at the bottom.
3. **Width**: response/query/combos stretch; no persistent H scrollbar when docked narrow.
4. **Rich text**: transcript fills space above status on startup without clicking.
5. Debug log: `[LAYOUT] response_rect … root=WxH` — one line per relayout, no height-clamp spam.

---

## LibreOffice references

- `sfx2/source/sidebar/DeckLayouter.cxx` — height distribution, `getHeightForWidth`
- `com.sun.star.ui.LayoutSize` — Minimum, Maximum, Preferred field order
