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
# Sidebar Layout Fix: Dynamic Width + Bottom-Anchored Buttons

## Problem

The chat sidebar panel has fixed width and height. The layout should:
1. Have a **minimum width of 180px** but expand to fill the sidebar width
2. **Buttons** (Send/Stop/Clear) anchored at the **bottom**
3. **Status control** above the buttons
4. **Chat window** (response area) fills remaining space at top
5. Work in a **DPI-independent** way

## Research Findings

### How the Sidebar Allocates Panel Height (from [DeckLayouter.cxx](file:///home/keithcu/Desktop/libreoffice/sfx2/source/sidebar/DeckLayouter.cxx))

The sidebar layout engine uses `LayoutSize(Minimum, Maximum, Preferred)` to decide panel heights:

1. **Collects** [getHeightForWidth(width)](file:///home/keithcu/Desktop/Python/localwriter/localwriter2/plugin/modules/chatbot/panel_factory.py#73-86) from each panel → gets `LayoutSize`
2. **Sums** all `Minimum` heights and all `Preferred` heights
3. If preferred fits → uses preferred as base, distributes extra height proportionally
4. If only minimum fits → uses minimum as base, distributes extra height proportionally
5. **Panels with `Maximum = -1`** (unbounded) get **all remaining height** distributed to them
6. Validation: `0 ≤ Minimum ≤ Preferred ≤ Maximum` (logs warning if violated)

### Current Code Bug

```python
# Current (WRONG) — violates IDL constraints
return uno.createUnoStruct("com.sun.star.ui.LayoutSize", 280, -1, 280)
# Minimum=280, Maximum=-1, Preferred=280
# Problem: Maximum(-1 = unbounded) should be ≥ Preferred, but -1 is treated specially
# More importantly: this CAPS the panel to 280px preferred height
```

### What Needs to Change

**Width** is already handled — the sidebar framework always gives the panel its full width via [getHeightForWidth(width)](file:///home/keithcu/Desktop/Python/localwriter/localwriter2/plugin/modules/chatbot/panel_factory.py#73-86) and `setPosSize`. [getMinimalWidth()](file:///home/keithcu/Desktop/Python/localwriter/localwriter2/plugin/modules/chatbot/panel_factory.py#87-89) already returns 180. The XDL width (168 Map AppFont) is just the initial template size — the sidebar overwrites it.

**Height** is the real problem. The fix has two parts:

1. **Tell the sidebar to give us all available height** via correct `LayoutSize`
2. **Reposition controls dynamically** when the panel is resized using `XWindowListener`

### Approach: XDL + XWindowListener (Hybrid)

Keep the XDL file for control definitions (maintains all existing controls including checkboxes, comboboxes, etc.) but add an `XWindowListener` to reposition controls dynamically when the panel resizes. This is simpler than localwriter2's fully-programmatic approach and preserves all existing UI controls.

> [!IMPORTANT]
> localwriter2 went **fully programmatic** (no XDL), but that dropped most controls (checkboxes, model selectors, image controls). Keeping the XDL + adding dynamic layout preserves all existing functionality with minimal code changes.

## Proposed Changes

### Panel Layout

#### [MODIFY] [ChatPanelDialog.xdl](file:///home/keithcu/Desktop/Python/localwriter/extension/LocalWriterDialogs/ChatPanelDialog.xdl)

Make the XDL just define controls with initial "reasonable" positions. The [_relayout](file:///home/keithcu/Desktop/Python/localwriter/localwriter2/plugin/modules/chatbot/panel_factory.py#623-710) will override them at runtime. Minor tweaks:
- Increase `dlg:width` to `"200"` (Map AppFont) so the initial template is wider — doesn't matter much since the sidebar overrides, but avoids controls being clipped during the brief moment before [_relayout](file:///home/keithcu/Desktop/Python/localwriter/localwriter2/plugin/modules/chatbot/panel_factory.py#623-710) fires.

#### [MODIFY] [panel_factory.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/chatbot/panel_factory.py)

Three changes:

**1. Fix [getHeightForWidth](file:///home/keithcu/Desktop/Python/localwriter/localwriter2/plugin/modules/chatbot/panel_factory.py#73-86) to request unbounded height:**
```python
def getHeightForWidth(self, width):
    if self.parent_window and self.PanelWindow and width > 0:
        parent_rect = self.parent_window.getPosSize()
        h = parent_rect.Height if parent_rect.Height > 0 else 400
        self.PanelWindow.setPosSize(0, 0, width, h, 15)
    # Minimum=100, Maximum=-1 (unbounded), Preferred=400
    # The sidebar will give us ALL remaining height since Maximum=-1
    return uno.createUnoStruct("com.sun.star.ui.LayoutSize", 100, -1, 400)
```

**2. Add `_PanelResizeListener` (XWindowListener):**

A class that implements `XWindowListener` with a [_relayout(self, w, h)](file:///home/keithcu/Desktop/Python/localwriter/localwriter2/plugin/modules/chatbot/panel_factory.py#623-710) method. The layout strategy is **bottom-up packing in pixels**:

```
┌─────────────────────┐ ← y=0 + margin
│   response (chat)   │  ← fills all remaining space
│                     │
│                     │
├─────────────────────┤
│  model_label        │  ← fixed height row
│  model_selector     │  ← fixed height row  
│  image controls...  │  ← optional, fixed height rows
│  checkboxes row     │  ← fixed height row
├─────────────────────┤
│  status             │  ← fixed height ~14px
├─────────────────────┤
│  query_label        │  ← fixed height ~14px
│  query (input)      │  ← fixed height ~40px
├─────────────────────┤
│ [Send] [Stop] [Clear]│ ← fixed height ~20px, at bottom
└─────────────────────┘ ← y = total height - margin
```

**DPI independence approach** (same as localwriter2): Compute a `scale_factor` from the panel's actual pixel height. A typical 1080p sidebar panel area is ~600-800px, so `scale = max(1.0, h / 800.0)`. All fixed sizes (margins, button height, gap) are multiplied by this factor.

**3. Wire the listener after [_wireControls](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/chatbot/panel_factory.py#225-543):**

```python
# At end of _wireControls:
ctrl_map = {
    "response": response_ctrl,
    "response_label": root_window.getControl("response_label"),
    "query_label": get_optional("query_label"),
    "query": query_ctrl,
    "send": send_btn, "stop": stop_btn, "clear": clear_btn,
    "status": status_ctrl,
    "model_label": model_label, "model_selector": model_selector,
    "image_model_selector": image_model_selector,
    "direct_image_check": direct_image_check,
    "web_search_check": web_search_check,
    "aspect_ratio_selector": aspect_ratio_selector,
    "base_size_input": base_size_input,
    "base_size_label": base_size_label,
}
resize_listener = _PanelResizeListener(ctrl_map)
root_window.addWindowListener(resize_listener)
resize_listener._relayout(root_window)  # initial layout
```

The [_relayout](file:///home/keithcu/Desktop/Python/localwriter/localwriter2/plugin/modules/chatbot/panel_factory.py#623-710) method works in pure pixels (since `setPosSize` on UNO controls uses pixels in this context). It:
1. Gets `w, h` from the window's `getPosSize()`
2. Computes bottom-section sizes (buttons, query, status, model selector, checkboxes)
3. Places the response control to fill everything above the bottom section
4. Places bottom controls from the bottom up
5. Handles hidden controls (image UI) by skipping them (check `isVisible()` or a flag)

## Verification Plan

> [!IMPORTANT]
> This is UNO sidebar code in a LibreOffice extension — there's no way to unit test the layout logic outside of LibreOffice. Verification must be manual.

### Manual Verification

After making changes and rebuilding/reinstalling the extension:

1. **Open LibreOffice Writer** → open the LocalWriter sidebar deck
2. **Verify layout order**: Response area at top → model selector/checkboxes in middle → status → query input → Send/Stop/Clear buttons at bottom
3. **Resize sidebar width**: Drag the sidebar border. Controls should stretch horizontally, buttons should remain evenly spaced
4. **Resize window height**: Maximize/restore the LibreOffice window. The response area should grow/shrink while bottom controls stay anchored
5. **DPI check**: If possible, test on a HiDPI display or change display scaling. Controls should scale proportionally
6. **Toggle "Use Image model"**: The image controls should appear/disappear and the layout should adjust (no overlapping)
7. **Check minimum width**: The sidebar should not allow shrinking below ~180px

> [!NOTE]
> Since you want to review this with other AIs before implementing, the plan is deliberately kept at the strategy level. The actual pixel values and exact control ordering will need tuning during implementation.
