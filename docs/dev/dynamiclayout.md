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
