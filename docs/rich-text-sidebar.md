# Embedded Rich-Text Chat Sidebar

This document outlines the design, active roadmap, and historical timeline of development, bugs, and lessons learned for the embedded rich-text chat sidebar in LibreOffice Writer.

---

## Active Roadmap & Upcoming Work Items

These are the priority tasks to resolve remaining layout quirks and implement new features enabled by the rich-text sidebar.

### [x] Task 1: Fix Scroll-to-Bottom Auto-Scrolling (High Priority — Fixed)
*   **Issue:** Streamed chat content exceeding the visible viewport area did not trigger auto-scroll. The user had to manually scroll down.
*   **Root Cause:** In Online/Browse layout mode, the embedded Writer's internal "make cursor visible" mechanism is completely non-functional for embedded frames. No UNO approach (ViewCursor, select, dispatch, page jumps, VCL scrollbar) could scroll the viewport.
*   **Solution:** Switched from `ShowOnlineLayout = True` to normal page layout with `ZoomType = PAGE_WIDTH`. In page mode, Writer's internal `SwEditWin::MakeVisible()` fires correctly when the view cursor moves across page boundaries, and `.uno:GoToEndOfDoc` triggers proper viewport scrolling. Page styled with zero margins and width matching the sidebar container so the visual result is seamless.
*   **Files Changed:** `plugin/chatbot/rich_text.py` (`create_embedded_writer_doc` view settings, `scroll_to_bottom` simplified), `plugin/chatbot/panel.py` (`_get_scrollbar`, `_should_auto_scroll`, `_append_response` logging).

### [x] Task 2: Fix Bullets / List Spacing & Horizontal Indentation
*   **Issue:** List bullets and numbered items took up far too much horizontal space, leaving text squished in the narrow sidebar.
*   **Solution:** Post-process `NumberingRules` on newly inserted paragraphs only. See section 10 below for full details.

### [ ] Task 3: Paragraph Padding & Margin Cleanup
*   **Issue:** Standard paragraphs still look slightly "pushed in" or have excess default margins/padding.
*   **Goal:** Modify the "Default" or "Standard" Paragraph Style in the embedded document template to set `ParaLeftMargin = 0`, `ParaRightMargin = 0`, `ParaFirstLineIndent = 0`, and adjust `ParaTopMargin`/`ParaBottomMargin` for tight vertical flow.

### [ ] Task 4: Resolve "Gray Bar" Centering Borders
*   **Issue:** A thick gray bar sometimes remains on the left or right of the sidebar.
*   **Goal:** Remove the visual artifact where the virtual page doesn't perfectly fill the VCL canvas.
*   **Avenues of Investigation:**
    *   Use `ConfigurationProvider` to temporarily override `org.openoffice.Office.Common/Appearance/ApplicationBackground` during the chat session.
    *   Explore VCL-level background color overrides directly on the container window.

### [ ] Task 5: High-DPI Dynamic Reflow Tuning
*   **Issue:** The hardcoded `26.458` pixel-to-1/100mm conversion factor might cause layout discrepancies on high-DPI/Retina screens.
*   **Goal:** Query the actual device context or DPI from `XDevice` to adjust the conversion factor dynamically.

### [ ] Task 6: Richer Simulated Syntax Highlighting
*   **Goal:** Extend regex parsing in `append_rich_text` to support:
    *   Inline code snippets surrounded by single backticks (` `code` `).
    *   Robust handling of nested triple-backticks.
    *   Applying a dedicated "CodeBlock" character style (monospace font, distinct background) in the embedded template.

### [x] Task 7: Color/Theme Customization & Configuration
*   **Goal:** Make assistant and user chat text colors dynamically theme-aware (matching system dark/light modes) instead of hardcoding them in `rich_text.py`.

---

## Completed Milestones

### 1. Embedded Writer Document Implementation
*   Successfully replaced the plain `dlg:textfield` response area with a hosted, embedded (hidden) Writer document.
*   **Files touched:**
    - `plugin/chatbot/rich_text.py` → `create_embedded_writer_doc(...)` + `append_rich_text(...)`
    - `plugin/chatbot/panel_wiring.py` → `_wireControls(...)` hides plain response control when embedding succeeds.

### 2. The "Lazy Peer" Deferred Initialization
*   **Problem:** LibreOffice 25.x lazy-realizes window peers, causing `win.getPeer()` to return `False` during initial UI wiring. Attempting `XFrame::initialize()` without a peer caused recursive layout loops and crashes.
*   **Solution:** Defer initialization using the `XWindowListener` pattern on `EmbeddedWriterListener`, waiting for `windowShown`.
*   **Async Break:** Used `post_to_main_thread` inside `windowShown` to break the synchronous recursion loop that crashed LO 25.x.
*   **Guard:** A global `_EMBEDDING_STARTED` set prevents multiple concurrent embedding attempts.

### 3. UI Polish & "Nuclear" Sidebar Formatting
*   **Web View:** `vs.IsOnlineLayout = True` to enable a continuous scrollable flow.
*   **Ruler Kill:** `vs.ShowRulers = False`, `vs.ShowHoriRuler = False`, `vs.ShowVertRuler = False`.
*   **Shadow Kill:** `vs.ShowShadows = False` (Crucial for removing gray page borders).
*   **Boundary Kill:** `vs.ShowTextBoundaries = False`.
*   **Scaling:** `vs.ZoomType = 1` (Page Width) combined with dynamic page width.
*   **Dynamic Width:** Calculating `style.Width` based on placeholder pixel width (`px * 26.458` to 1/100mm) ensures the text reflows correctly into the sidebar.
*   **Background:** `style.BackColor = 0xFFFFFF` (White) [Obsoleted by Dynamic Theme Matching].

### 4. Text Color & Contrast Softening
*   Changed `ASSISTANT_COLOR` from harsh pure black (`0x000000`) to a modern, softer Deep Slate Gray (`0x1E293B`) for the white background.

### 5. Dynamic VCL Theme Matching (Dark / Light Mode)
*   **Dynamic Theme Sensing:** Implemented `get_theme_colors(doc)` which extracts the native sidebar container window's VCL `StyleSettings` to automatically sense whether LibreOffice is running in dark mode or light mode (based on the relative luminance of `StyleSettings.FieldColor`).
*   **Dark Mode Palette:** When dark mode is active, the background is set to the system `FieldColor`, the User role prefix is rendered in soft light blue (`0x60A5FA`), and the Assistant text is rendered in a soft off-white (`0xE2E8F0`).
*   **Light Mode Contrast:** In light mode, the background is dynamically set to a beautifully darkened dialog box contrast color (exactly 6% darker than `StyleSettings.DialogColor`, e.g. `0xE0E1E2`), preventing harsh white glare while matching the surrounding chrome's hue.
*   **Global Configuration Color Alignment:** Updates `/org.openoffice.Office.UI/ColorScheme/.../DocColor` globally to match the dynamic background, ensuring perfect canvas border blending.

---

## Archive: Historical Investigations, Lessons Learned & Timeline

This section preserves in full all historical records, findings, and technical strategies from the initial development phase of the rich-text sidebar.

### 1. What We Expected

The sidebar response area is replaced with an *embedded* (hidden) Writer document so we can render rich formatting (colored role prefixes like **You:** and **Assistant:**).

### 2. What Failed (Core Error)

On LibreOffice 25.x, embedded document hosting fails during `XFrame::initialize()` with:
- `XFrameImpl::initialize() called without a valid container window reference.`

### 3. Observed Root Cause (From Logs)

The main problem is consistent across all descriptor/descriptor-parent combinations we tried:
- `toolkit.createWindow(desc)` returns a window object where `win.getPeer()` is *always falsy* (`hasPeer=False`).
- When we attempt `frame.initialize(win)` without a realized GUI peer, LibreOffice enters a re-entrant sidebar creation loop (recursion-like behavior), repeatedly calling the tool panel’s UI wiring.

Example log pattern (most informative lines):
- `created win_ok=True hasPeer=False (...)`
- `no container window candidates succeeded (no usable GUI peer)`
- and (earlier) `initializing Frame ...` followed by repeated `getRealInterface called ===` / `_wireControls entered` sequences.

### 4. Timeline of Initial Attempts (And Outcomes)

#### A) Fix container descriptor service name
*   **Change:** `WindowDescriptor.WindowServiceName = "container"` → `""`.
*   **Outcome:** the container window still produced `hasPeer=False`; `frame.initialize(win)` continued to fail.

#### B) Improve peer selection + add debug logging
*   **Changes:**
    - Prefer `placeholder_control.getPeer()` when available; otherwise use `parent_window.getPeer()`.
    - Add logging for placeholder position, parent/placeholder peer existence, and container window creation status.
*   **Outcome:** peer selection existed, but toolkit-created containers still ended with `hasPeer=False`.

#### C) Work around missing UNO constant imports
*   **Action:** Attempted to import `com.sun.star.awt.WindowAttribute` constants for `desc.WindowAttributes`.
*   **Outcome:** ImportError (`No module named com ...` / unknown constants). We reverted to numeric flags: `desc.WindowAttributes = 2 | 4`. This fixed module import failures, but did not resolve the `hasPeer=False` issue.

#### D) Guard against recursion / repeated creation
*   **Action:** Added a re-entrancy guard in `create_embedded_writer_doc()` to prevent spawning many embedded docs if LibreOffice re-enters UI wiring: `_CREATION_GUARDS` keyed by `id(placeholder_control)`.
*   **Outcome:** removed document-spawn explosions, but did not fix the underlying peer realization problem.

#### E) Ensure VCL realizes the window peer
*   **Action:** Added `toolkit.processEventsToIdle()` (when available) after `win.setVisible(True)`, then re-check `hasPeer`.
*   **Outcome:** still `hasPeer=False` after idle processing.

#### F) Expand the matrix of window descriptor combinations
*   **Changes:** We expanded the search to try different combinations of:
    - `WindowDescriptor.Type`: `CONTAINER`, `TOP`
    - `WindowDescriptor.WindowServiceName` candidates: `""`, `"control"`, `"dockingwindow"`, `"container"`, `"framewindow"`
    - parent source: placeholder peer vs root panel peer
*   **Outcome:** every attempt still reported `created win_ok=True hasPeer=False`. Final line observed: `no container window candidates succeeded (no usable GUI peer)`.

---

### 5. Why the Peer Issue Blocked Rich Text

Rich formatting depends on writing to the embedded Writer document via:
- `doc.getText()` / `XTextCursor` in `append_rich_text()`

If the embedded doc cannot be hosted, role-color/bold formatting cannot be applied. The implementation therefore fell back to leaving the plain `dlg:textfield` response visible.

---

### 6. Proposed Technical Strategies (May 2026)

#### Strategy 1: The "Lazy Peer" Solution (Fixing hasPeer=False)
The root cause of the `hasPeer=False` failure is VCL's lazy window realization. The sidebar container exists as a UNO object but lacks a physical window handle (peer) at the moment of initialization.
*   **Strategy:** Deferred Initialization.
*   **Implementation:**
    1.  In `_wireControls`, do **not** call `create_embedded_writer_doc` immediately.
    2.  Register an **`XWindowListener`** on the sidebar's container window or the placeholder control's parent.
    3.  Implement the **`windowShown`** event handler.
    4.  Inside `windowShown`, verify if the peer now exists (`win.getPeer() is not None`).
    5.  Trigger the `XFrame::initialize(win)` and `loadComponentFromURL` logic only at this point.
    6.  **Guard:** Ensure this only runs once via a flag (e.g., `self._embedding_initialized`).

#### Strategy 2: "Simulated" Syntax Highlighting for Chat
Since the sidebar is for read-only history, full Monaco-level tokenization is unnecessary and resource-intensive.
*   **Strategy:** Character Style Mapping.
*   **Implementation:** 
    1.  Create a "CodeBlock" Character Style in the embedded Writer template (Monospace font, subtle background color).
    2.  When parsing AI responses, identify markdown code blocks (` ` `python ... ` ` `).
    3.  Insert the text into the embedded doc and apply the "CodeBlock" style to that specific text range using `XTextRange`.

#### Strategy 3: The "Floating Sticker" Backup (Wayland/Sandbox Fallback)
If cross-process embedding remains blocked by Wayland security or Sandbox isolation, use a tethered window approach.
*   **Strategy:** Borderless Subprocess Overlay.
*   **Implementation:**
    1.  Launch the Rich Text UI in a separate borderless `pywebview` subprocess.
    2.  In LibreOffice, calculate the absolute screen coordinates of the sidebar area (`XWindow.getPosSize()` combined with frame coordinates).
    3.  Send these coordinates to the subprocess via the stdin pipe.
    4.  The subprocess window moves/resizes itself to perfectly "stick" to the sidebar area.
    5.  Listen for LibreOffice window move/resize events to update the "sticker" position.

---

### 7. Detailed Post-MVP Observations & Deep-Dive Quirks

#### The "Gray Bar" Mystery
Despite setting `BackColor = 0xFFFFFF` and `IsOnlineLayout = True`, a dark gray border persists. This is likely the "Application Background" (the area outside the paper).
*   **Hypothesis:** In `OnlineLayout`, Writer may still center the "virtual page" and show the background if the window is wider than the page.
*   **Resolution Avenues:** Use the `ConfigurationProvider` to temporarily override `org.openoffice.Office.Common/Appearance/ApplicationBackground` just for the sidebar session, or find a VCL-level property on the `container_window` to set its background color.

#### Window Resizing Precision
The `on_window_resized` listener is in place but may have a slight lag or precision issue with the pixel-to-1/100mm conversion (`26.458` factor).
*   **Resolution Avenues:** Query the system DPI via `XDevice` to get a more accurate conversion factor than the 96 DPI constant.

#### Paragraph Formatting & Margins
The text still looks "pushed in."
*   **Resolution Avenues:** Explicitly modify the "Standard" or "Default" Paragraph Style to set `ParaLeftMargin`, `ParaRightMargin`, `ParaFirstLineIndent`, and `ParaTopMargin` all to 0.

#### Simulated Syntax Highlighting Limitations
The current regex-based approach in `append_rich_text` works for basic blocks.
*   **Resolution Avenues:** Add support for inline backticks (` `code` `) and better handling of nested triple-backticks.

---

### 8. Scroll-to-Bottom Detailed Investigation (May 25, 2026)

#### Problem
When the embedded Writer sidebar receives streamed chat content that exceeds the visible area, the view does not auto-scroll to show the latest text. The user can see content is inserted (if they manually scroll), but the viewport stays at the top.

#### Approaches Tested (All Failed to Visually Scroll)

| Approach | Behavior | Why It Failed / Details |
|----------|----------|-------------------------|
| `view_cursor.gotoEnd(False)` | Cursor moves logically; no visual scroll | Viewport does not follow the cursor automatically. |
| `view_cursor.jumpToLastPage()` + `jumpToEndOfPage()` | Returns success; no visual scroll | Online/Web layout has no real "pages," making page-bound jumps a no-op. |
| `.uno:GoToEndOfDoc` dispatch | Dispatch succeeds (State=1); no visual scroll | Moves the cursor inside the document logically but the viewport remains static. |
| `controller.select(end_cursor)` | **Scrolls to TOP** — actively harmful | LibreOffice attempts to make the start of selection visible, resulting in viewport jumping to the top of the chat history. |
| `frame.activate()` | Combined with select, causes scroll-to-top | Resets/refreshes focus which defaults the view back to the document start. |
| Accessibility scrollbar (`find_vertical_scrollbar`) | Returns "no scrollbar found" | The embedded Writer view does not expose a scrollbar via `AccessibleRole.SCROLL_BAR` in its accessible tree. |
| `post_to_main_thread(scroll_to_bottom, doc)` | Callback never executes during streaming | The callback was nested inside the queue execution flow and was not drained in time. Fixed by direct synchronous invocation. |

#### Key Findings

1.  **Online/Web Layout disables page-based scrolling.** With `ShowOnlineLayout = True`, the document is a single continuous page. `jumpToLastPage()` is a no-op, `.uno:GoToEndOfDoc` moves the cursor but the viewport doesn't follow.
2.  **`controller.select()` scrolls to the TOP.** This is the opposite of what's needed — likely it "shows the beginning of the selection."
3.  **`frame.activate()` resets the view.** Combined with select, it forces the view to the document start.
4.  **The embedded Writer's view does NOT auto-follow the cursor.** In a normal standalone Writer window, moving the view cursor causes the viewport to follow. In an embedded Writer (parented to a toolpanel container window), this "follow cursor" behavior is disabled or broken.
5.  **No accessibility scrollbar exists.** `find_vertical_scrollbar(frame)` traverses the accessible tree from `frame.getComponentWindow().getAccessible()` but finds zero `SCROLL_BAR` role children. The embedded Writer may not have a visible scrollbar at all — content simply renders beyond the visible area.
6.  **`post_to_main_thread` from within `queue_executor.post` never fires.** The nested post to the main thread queue doesn't get drained during the streaming loop. Fixed by calling `scroll_to_bottom(doc)` directly.

#### Current Status (Updated May 25, 2026)

**Root cause found:** In Online/Browse layout mode (`ShowOnlineLayout = True`), the embedded Writer's internal "make cursor visible" mechanism is broken. All UNO approaches (ViewCursor.gotoEnd, controller.select, .uno:GoToEndOfDoc, jumpToLastPage) move the logical cursor but the viewport remains static. The VCL peer tree exposes no scrollbar objects (all `impl=?`, no accessible tree).

**Fix:** Switched to normal **page layout mode** (`ShowOnlineLayout = False`) with `ZoomType = PAGE_WIDTH`. In page mode, Writer's internal MakeVisible fires correctly when the cursor moves to content on a different page. Combined with zero margins and page width matching the sidebar container, the visual result is nearly identical to online layout but scrolling works.

`scroll_to_bottom` is now streamlined to:
1. `view_cursor.gotoEnd(False)` — positions cursor at document end.
2. `.uno:GoToEndOfDoc` dispatch — triggers Writer's viewport scroll to follow cursor.
3. `processEventsToIdle()` — flushes VCL repaint synchronously.

The `auto_scroll` parameter is now properly honored by `append_rich_text` (previously ignored). Debug introspection helpers (`_dump_scroll_debug_once`, `find_vcl_scrollbar`, `traverse_window_tree`) remain for future diagnostics.

---

### 9. Dynamic VCL Theme Matching Detailed Investigation (May 25, 2026)

#### Problem
In light or dark modes, the hardcoded white background (`0xFFFFFF`) of the embedded Writer document did not automatically match the user's LibreOffice application colors. In dark mode, this resulted in unreadable white-on-white text (due to automatic text coloring resolving to white) or invisible charcoal assistant roles. In light mode, the pure white was aesthetically harsh and lacked a soft, premium contrast against the surrounding sidebar chrome.

#### Approaches Investigated & Lessons Learned

| Approach / Source | What We Tried | Findings & Why It Was Dropped / Modified |
|-------------------|---------------|-----------------------------------------|
| **1. Configuration Access** | Queried the `/org.openoffice.Office.UI/ColorScheme` registry properties. | **Failed to resolve actual colors:** Default color schemes (like `COLOR_SCHEME_LIBREOFFICE_AUTOMATIC`, `COLOR_SCHEME_LIBREOFFICE_DARK`, and `COLOR_SCHEME_LIBREOFFICE_LIGHT`) return `None`/`void` properties in PyUNO unless explicitly customized by the user. LibreOffice resolves these dynamically at runtime inside its C++ rendering pipeline based on the OS theme, making them inaccessible via the standard `ConfigurationProvider`. |
| **2. Dynamic VCL StyleSettings** | Explored parent window VCL styling properties. | **Successful discovery:** Uncovered that VCL container windows (supporting the `com.sun.star.awt.XStyleSettingsSupplier` interface) expose a live `StyleSettings` object containing resolved system-wide colors (e.g. `FieldColor`, `FieldTextColor`, `DialogColor`, `DialogTextColor`). These properties update in real-time when the user switches their OS or LibreOffice theme. |
| **3. Relative Luminance Detection** | Auto-detected dark mode using `FieldColor` (background color for text fields). | **Luminance formula:** Calculated relative luminance of `FieldColor` using standard coefficients: `Luminance = 0.2126 * R + 0.7152 * G + 0.0722 * B`. If `Luminance < 128`, it dynamically identifies the dark theme. |
| **4. Softer Light Contrast** | Darkened the background color to `#B8B8B8` (medium gray) in light mode. | **Aesthetic check:** Medium gray `#B8B8B8` was too harsh/dark, creating a stark visual boundary that stood out excessively from the surrounding sidebar chrome. |
| **5. Dynamic Proportional Darkening** | Darkened `DialogColor` dynamically by exactly 6% in light mode. | **Perfect premium contrast:** Proportionally multiplied the red, green, and blue components of `StyleSettings.DialogColor` (typically `0xEFF0F1`) by `0.94` (producing `0xE0E1E2`). This creates a soft, premium contrast that perfectly aligns with the surrounding dialog frame's hue. |

#### Implementation & dynamic colors

1.  **get_theme_colors(doc):** Resolves colors dynamically by traversing the document window's VCL `StyleSettings` hierarchy.
    - **Dark Mode**:
      - Background color: `StyleSettings.FieldColor` (native system dark color)
      - User role prefix: `0x60A5FA` (soft Tailwind sky-blue)
      - Assistant body text: `0xE2E8F0` (soft light slate gray)
    - **Light Mode**:
      - Background color: Proproportionally darkened `StyleSettings.DialogColor` by 6% (typically `0xE0E1E2`)
      - User role prefix: `0x2A6099` (premium indigo blue)
      - Assistant body text: `0x1E293B` (premium slate gray)
2.  **Embedded Canvas Blending:** Overwrites `/org.openoffice.Office.UI/ColorScheme/.../DocColor` with the dynamic background color. This ensures the canvas borders around the virtual page blend seamlessly, resolving the "gray bar centering borders" mystery.

---

### 10. List Spacing / NumberingRules Tightening (May 25, 2026)

#### Problem

The HTML filter (`HTML (StarWriter)`) imports `<ul><li>` content as Writer list paragraphs with `NumberingIsNumber=True`. The default `LeftMargin` in NumberingRules is ~1251 (12.5mm) for level 0 and ~2501 for level 1 -- far too wide for the narrow sidebar, wasting most of the horizontal text area on indentation.

#### Key Findings

1. **The indentation lives in `NumberingRules`, not `ParaLeftMargin`.** Despite the paragraphs being list items, `ParaLeftMargin` is 0. All indent information is in the `NumberingRules` XIndexAccess (per-level property sequences with `LeftMargin` and `FirstLineOffset` keys).

2. **`body_range.createEnumeration()` on a cursor created from a captured `TextRange` position only returns 1 paragraph** after `insertDocumentFromURL`. The position reference becomes stale because the HTML import restructures paragraphs. The fix: use `doc.CharacterCount` before insertion, then create a fresh cursor with `goRight(pre_len, False)` + `gotoEnd(True)` to span exactly the new content.

3. **`FirstLineOffset` controls bullet-to-text spacing.** This value (typically around -625) should NOT be modified -- it provides the natural breathing room between the bullet glyph and the text. Only `LeftMargin` (which controls total indent from the left edge) needs tightening.

4. **Deduplication by `(ListId, level)`.** Paragraphs sharing the same list and level share NumberingRules. Modifying once via `uno.invoke(rules, "replaceByIndex", ...)` + reassigning `para.NumberingRules = rules` updates all paragraphs in that list at that level.

#### Implementation

`_tighten_list_indent(body_range)` in `plugin/chatbot/rich_text.py`:
- Enumerates paragraphs in the body_range (only newly inserted content).
- Skips non-list paragraphs (`NumberingIsNumber=False`).
- For each unique `(ListId, level)`, reads the existing `FirstLineOffset` and sets `LeftMargin = abs(FirstLineOffset) + 115 + level * 225`. This positions the bullet ~1.15mm from the left edge at level 0, preserving the original bullet-to-text gap.
- Uses reflective `uno.invoke(rules, "replaceByIndex", ...)` because Python UNO proxies don't expose a direct `replaceByIndex`.

#### What Could Be Done Next

- **Nested list level spacing tuning:** The `level * 225` increment could be made configurable or adjusted based on sidebar width.
- **Ordered lists (numbered):** Same mechanism applies but verify that `IndentAt` / `FirstLineIndent` behave identically for numbered lists.
- **Streaming path:** Currently only `append_rich_text` (the HTML import path) triggers tightening. The streaming path (`append_text_chunk`) inserts plain text with no list formatting, so it doesn't need it.

