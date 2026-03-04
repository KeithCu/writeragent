# Dynamic Sidebar Panel Layout

## Overview

The chat sidebar panel uses a **hybrid XDL + runtime relayout** approach for dynamic sizing. Control definitions (types, labels, initial positions) live in `ChatPanelDialog.xdl`. At runtime, `_PanelResizeListener` (an `XWindowListener`) repositions controls whenever the sidebar resizes, stretching the response area to fill available space while anchoring all other controls to the bottom.

## How It Works

### 1. LayoutSize: Telling the Sidebar We Want All Available Height

The `XSidebarPanel.getHeightForWidth()` method returns a `LayoutSize(Minimum, Maximum, Preferred)` struct. The sidebar's `DeckLayouter` (in `sfx2/source/sidebar/DeckLayouter.cxx`) uses this to distribute height among panels:

- Panels with `Maximum = -1` (unbounded) receive **all remaining height** after fixed-size panels are satisfied
- The distribution algorithm (`DistributeHeights`) proportionally divides extra space, with unbounded panels absorbing everything left over

We return `LayoutSize(100, -1, 400)`:
- **Minimum = 100**: panel won't shrink below 100px
- **Maximum = -1**: unbounded — we'll take all available height
- **Preferred = 400**: reasonable default if the sidebar has exactly enough space

### 2. XDL as the Source of Truth for Control Sizes

All control heights, widths, gaps, and relative positions are defined in `ChatPanelDialog.xdl` using Map AppFont units (DPI-independent). The XDL defines:

- Response area (multiline textfield, read-only, with vscroll)
- Status bar
- Query label and input
- Send / Stop / Clear buttons
- Checkboxes (Use Image model, Web research)
- Model selector combobox
- Image model controls (overlapping with model selector, toggled by checkbox)

When `ContainerWindowProvider.createContainerWindow()` loads the XDL, it converts Map AppFont units to pixels and creates all controls with their initial positions. These pixel values become the baseline for dynamic layout.

### 3. _PanelResizeListener: The Runtime Layout Engine

On the first `windowResized` event, `_capture_initial()` snapshots every control's pixel position and size as loaded from the XDL. It also records the initial window dimensions and the response area's bottom edge.

On each subsequent resize, `_relayout()` does three things:

**a) Bottom-anchor all controls below the response:**
```python
new_y = h - (ih - oy)  # same offset from bottom as in the XDL
```
This preserves the XDL's spacing between buttons, checkboxes, model selectors, etc., while keeping them pinned to the bottom edge of the panel regardless of how tall it gets.

**b) Stretch the response area to fill the gap:**
```python
new_rh = max(30, top_of_bottom - gap - ry)
```
The response area keeps its top position and grows its height to fill everything above the bottom-anchored controls.

**c) Scale widths proportionally:**
```python
new_w = int(ow * width_ratio)
```
All controls stretch horizontally by the ratio of new panel width to initial XDL width.

### Visual Layout

```
┌─────────────────────────┐
│  Chat (Testing):        │  ← response_label (fixed at top)
├─────────────────────────┤
│                         │
│   Response / Chat       │  ← FILLS ALL AVAILABLE SPACE
│   (scrollable)          │
│                         │
│                         │
├─────────────────────────┤ ─┐
│  status: Ready          │  │
│  Ask / instruct:        │  │
│  ┌───────────────────┐  │  │
│  │ query input       │  │  │  Bottom-anchored section
│  └───────────────────┘  │  │  (sizes from XDL, pinned
│  ☐ Use Image  ☐ Web     │  │   to bottom edge)
│  AI Model:              │  │
│  [model_selector    ▾]  │  │
│  [Send] [Stop] [Clear]  │  │
└─────────────────────────┘ ─┘
```

## Why This Is Better Than localwriter2's Approach

### localwriter2: Fully Programmatic Layout

localwriter2 abandoned the XDL entirely and builds all controls programmatically via `panel_layout.py` (`create_panel_window` + `add_control`). Its `_ChatResize._relayout()` computes every dimension from scratch:

```python
# localwriter2: everything is hardcoded pixel math
scale_factor = max(1.0, h / 1000.0)
m = int(8 * scale_factor)
btn_h = int(28 * scale_factor)
query_h = int(70 * scale_factor)
label_h = int(22 * scale_factor)
gap = int(10 * scale_factor)
```

**Problems with this approach:**

1. **Lost controls**: The programmatic panel only creates 6 controls (response, query_label, query, send, stop, clear). All the additional UI — model selector, image model selector, checkboxes, aspect ratio, base size, status bar — is missing. Adding them back means writing extensive `add_control()` boilerplate for each one.

2. **Fragile DPI scaling**: The `scale_factor = max(1.0, h / 1000.0)` heuristic guesses DPI from the window height. This breaks when the window is simply small (e.g. laptop) vs. actually low-DPI, or when the user resizes the sidebar to be narrow and short.

3. **Maintenance burden**: Every UI change requires updating two places — the Python layout code and any documentation about control positions. There's no visual editor and no declarative format. The 90-line `_relayout` method (with its `if self._pos == "top"` / `else` branches) is hard to reason about.

4. **No Map AppFont**: Programmatic controls use pixel positioning via `setPosSize()`. Map AppFont units (which LibreOffice's XDL system converts to pixels accounting for system font metrics and DPI) are not available, so the DPI independence is approximate at best.

### localwriter: XDL + Runtime Adjustment

Our approach keeps the XDL as the single source of truth for control layout:

1. **All controls defined in XDL**: The dialog editor (or hand-edited XML) defines every control with Map AppFont units. Adding a new control means adding one XML element — no Python code changes needed for positioning.

2. **True DPI independence**: Map AppFont units are converted to pixels by LibreOffice's rendering engine, which knows the actual system DPI, font metrics, and toolkit backend (GTK, Qt, Win32). This is more accurate than any heuristic.

3. **Minimal runtime code**: `_PanelResizeListener` is ~50 lines. It captures the XDL-loaded positions once and then applies a simple rule: bottom-anchor everything below the response, stretch the response to fill. No per-control pixel math.

4. **Separation of concerns**: Layout design in XDL (declarative, editable). Dynamic behavior in Python (capture + transform). Adding controls doesn't require touching the resize logic.

## Key Files

| File | Role |
|------|------|
| `LocalWriterDialogs/ChatPanelDialog.xdl` | Control definitions and initial layout (Map AppFont) |
| `plugin/modules/chatbot/panel_factory.py` | `_PanelResizeListener`, `ChatToolPanel.getHeightForWidth()` |
| `registry/.../Sidebar.xcu` | Deck and panel registration with LibreOffice |

## LibreOffice Sidebar Layout Reference

The sidebar layout engine lives in `sfx2/source/sidebar/DeckLayouter.cxx`:

- **`LayoutSize` struct** (IDL: `com/sun/star/ui/LayoutSize.idl`): Fields are `Minimum`, `Maximum`, `Preferred` — this order matters when calling `uno.createUnoStruct`.
- **`DistributeHeights`**: Extra height goes proportionally to panels based on weight. Panels with `Maximum = -1` absorb all remaining height after capped panels are satisfied.
- **`getMinimalWidth()`**: Returns the minimum sidebar width in pixels. We return 180.
- **`getHeightForWidth(width)`**: Called by the layouter to ask panels their size preferences. Our implementation also calls `setPosSize` on the panel window to match the sidebar width.
