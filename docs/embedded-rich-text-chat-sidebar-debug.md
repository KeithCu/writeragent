# Embedded Rich-Text Chat Sidebar (PR #91) — LibreOffice Embedding Debug Notes

This document records the current state of the rich-text chat sidebar implementation from PR #91:

- [PR #91: rich text chat sidebar using embedded Writer document](https://github.com/KeithCu/writeragent/pull/91)

## What we expected

The sidebar response area is replaced with an *embedded* (hidden) Writer document so we can render rich formatting (colored role prefixes like **You:** and **Assistant:**).

Implementation entry points:

- `plugin/chatbot/rich_text.py` → `create_embedded_writer_doc(...)` + `append_rich_text(...)`
- `plugin/chatbot/panel_wiring.py` → `_wireControls(...)` hides the plain response control only if the embedded doc succeeds.

## What failed (core error)

On LibreOffice 25.x, embedded document hosting fails during `XFrame::initialize()` with:

- `XFrameImpl::initialize() called without a valid container window reference.`

## Observed root cause (from logs)

The main problem is consistent across all descriptor/descriptor-parent combinations we tried:

- `toolkit.createWindow(desc)` returns a window object where `win.getPeer()` is *always falsy* (`hasPeer=False`).
- When we attempt `frame.initialize(win)` without a realized GUI peer, LibreOffice enters a re-entrant sidebar creation loop (recursion-like behavior), repeatedly calling the tool panel’s UI wiring.

Example log pattern (most informative lines):

- `created win_ok=True hasPeer=False (...)`
- `no container window candidates succeeded (no usable GUI peer)`
- and (earlier) `initializing Frame ...` followed by repeated `getRealInterface called ===` / `_wireControls entered` sequences.

## Timeline of attempts (and outcomes)

### 1) Fix container descriptor service name

Change: `WindowDescriptor.WindowServiceName = "container"` → `""`.

Outcome: the container window still produced `hasPeer=False`; `frame.initialize(win)` continued to fail.

### 2) Improve peer selection + add debug logging

Changes:

- Prefer `placeholder_control.getPeer()` when available; otherwise use `parent_window.getPeer()`.
- Add logging for:
  - placeholder position
  - whether parent/placeholder peer exists
  - container window creation status

Outcome: peer selection existed, but toolkit-created containers still ended with `hasPeer=False`.

### 3) Work around missing UNO constant imports

Attempted to import `com.sun.star.awt.WindowAttribute` constants for `desc.WindowAttributes`.

Outcome: ImportError (`No module named com ...` / unknown constants). We reverted to numeric flags:

- `desc.WindowAttributes = 2 | 4`

This fixed module import failures, but did not resolve the `hasPeer=False` issue.

### 4) Guard against recursion / repeated creation

We added a re-entrancy guard in `create_embedded_writer_doc()` to prevent spawning many embedded docs if LibreOffice re-enters UI wiring:

- `_CREATION_GUARDS` keyed by `id(placeholder_control)`

Outcome: removed document-spawn explosions, but did not fix the underlying peer realization problem.

### 5) Ensure VCL realizes the window peer

Added `toolkit.processEventsToIdle()` (when available) after `win.setVisible(True)`, then re-check `hasPeer`.

Outcome: still `hasPeer=False` after idle processing.

### 6) Expand the matrix of window descriptor combinations

We expanded the search to try different combinations of:

- `WindowDescriptor.Type`: `CONTAINER`, `TOP`
- `WindowDescriptor.WindowServiceName` candidates: `""`, `"control"`, `"dockingwindow"`, `"container"`, `"framewindow"`
- parent source: placeholder peer vs root panel peer

Outcome: every attempt still reported:

- `created win_ok=True hasPeer=False`

Final line observed:

- `no container window candidates succeeded (no usable GUI peer)`

## Current status

With the current LibreOffice build (and the toolpanel context used by the sidebar), toolkit-created windows never get a realized VCL GUI peer (`win.getPeer()` remains falsy).

Because `XFrame::initialize()` requires a valid realized container window, embedding cannot proceed reliably. The implementation therefore falls back to leaving the plain `dlg:textfield` response visible.

## Why this blocks rich text

Rich formatting depends on writing to the embedded Writer document via:

- `doc.getText()` / `XTextCursor` in `append_rich_text()`

If the embedded doc cannot be hosted, role-color/bold formatting cannot be applied.

## Next things to try (recommended)

These are the most promising directions given the evidence:

1. **Try a different embedding strategy**
   - Instead of embedding via `toolkit.createWindow(desc)` + `XFrame::initialize(container_window)`, locate a pattern in LibreOffice tooling/examples for embedding an editor/document view in a toolpanel where the container peer is already realized.

2. **Bind to an already-realized peer**
   - If a realized peer for the response area can be obtained from the existing control rather than via toolkit window creation, initialize the frame using that realized peer.
   - (We previously experimented with fallback peer usage, but earlier iterations produced recursion/hangs.)

3. **Investigate toolpanel-specific constraints**
   - The toolpanel context may be preventing VCL from realizing peers for certain descriptor constructions.
   - Compare against how the `ContainerWindowProvider.createContainerWindow()` path behaves; possibly the embedded content needs to be attached to that existing container rather than creating a new one.

## Files touched during this investigation

- `plugin/chatbot/rich_text.py`
- `plugin/chatbot/panel_wiring.py`


## Proposed Technical Strategies (May 2026)

### 1. The "Lazy Peer" Solution (Fixing hasPeer=False)
The root cause of the `hasPeer=False` failure is VCL's lazy window realization. The sidebar container exists as a UNO object but lacks a physical window handle (peer) at the moment of initialization.
*   **Strategy:** Deferred Initialization.
*   **Implementation:**
    1.  In `_wireControls`, do **not** call `create_embedded_writer_doc` immediately.
    2.  Register an **`XWindowListener`** on the sidebar's container window or the placeholder control's parent.
    3.  Implement the **`windowShown`** event handler.
    4.  Inside `windowShown`, verify if the peer now exists (`win.getPeer() is not None`).
    5.  Trigger the `XFrame::initialize(win)` and `loadComponentFromURL` logic only at this point.
    6.  **Guard:** Ensure this only runs once via a flag (e.g., `self._embedding_initialized`).

### 2. "Simulated" Syntax Highlighting for Chat
Since the sidebar is for read-only history, full Monaco-level tokenization is unnecessary and resource-intensive.
*   **Strategy:** Character Style Mapping.
*   **Implementation:** 
    1.  Create a "CodeBlock" Character Style in the embedded Writer template (Monospace font, subtle background color).
    2.  When parsing AI responses, identify markdown code blocks (` ` `python ... ` ` `).
    3.  Insert the text into the embedded doc and apply the "CodeBlock" style to that specific text range using `XTextRange`.

### 3. The "Floating Sticker" Backup (Wayland/Sandbox Fallback)
If cross-process embedding remains blocked by Wayland security or Sandbox isolation, use a tethered window approach.
*   **Strategy:** Borderless Subprocess Overlay.
*   **Implementation:**
    1.  Launch the Rich Text UI in a separate borderless `pywebview` subprocess.
    2.  In LibreOffice, calculate the absolute screen coordinates of the sidebar area (`XWindow.getPosSize()` combined with frame coordinates).
    3.  Send these coordinates to the subprocess via the stdin pipe.
    4.  The subprocess window moves/resizes itself to perfectly "stick" to the sidebar area.
    5.  Listen for LibreOffice window move/resize events to update the "sticker" position.

---

## Update: Successful Implementation (May 2026)

We have successfully resolved the `hasPeer=False` blocker and established a working rich-text embedding. The code is currently disabled in `plugin/chatbot/panel_wiring.py` for safety before check-in.

### 1. The "Lazy Peer" Solution (Verified)
The solution was to defer initialization using the `XWindowListener` pattern:
*   **Listener:** `EmbeddedWriterListener` waits for `windowShown`.
*   **Async Break:** Uses `post_to_main_thread` to call `create_embedded_writer_doc`, preventing the synchronous recursion loop that crashed LO 25.x in previous attempts.
*   **Guard:** A global `_EMBEDDING_STARTED` set prevents multiple concurrent embedding attempts.

### 2. UI Polish "Nuclear" Settings (Verified)
To make Writer look like a chat sidebar, we discovered the following optimal UNO properties:
*   **Web View:** `vs.IsOnlineLayout = True`.
*   **Ruler Kill:** `vs.ShowRulers`, `vs.ShowHoriRuler`, `vs.ShowVertRuler = False`.
*   **Shadow Kill:** `vs.ShowShadows = False` (Crucial for removing gray page borders).
*   **Boundary Kill:** `vs.ShowTextBoundaries = False`.
*   **Scaling:** `vs.ZoomType = 1` (Page Width) works best when combined with dynamic page width.
*   **Dynamic Width:** Calculating `style.Width` based on placeholder pixel width (px * 26.458 to 1/100mm) ensures the text reflows correctly into the sidebar.
*   **Background:** `style.BackColor = 0xFFFFFF` (White).

### 3. Next Steps / Things to Try
While we made great progress, some UI artifacts remain:
*   **Persistent Gray Bar:** A thick gray bar sometimes remains on the left/right. This may be the **Application Background** color showing through if the `OnlineLayout` page doesn't perfectly fill the window.
*   **Reflow Tuning:** The pixel-to-1/100mm conversion might need adjustment for high-DPI displays.
*   **Paragraph Indents:** Set the "Standard" paragraph style indents to 0 to prevent the "pushed-in" text look.
*   **Code Block Refinement:** Improve the regex-based simulated syntax highlighting.

### Detailed Observations (Post-MVP UI Polish)

1.  **The "Gray Bar" Mystery:** Despite setting `BackColor = 0xFFFFFF` and `IsOnlineLayout = True`, a dark gray border persists. This is likely the "Application Background" (the area outside the paper). 
    *   **Hypothesis:** In `OnlineLayout`, Writer may still center the "virtual page" and show the background if the window is wider than the page.
    *   **Try:** Use the `ConfigurationProvider` to temporarily override `org.openoffice.Office.Common/Appearance/ApplicationBackground` just for the sidebar session, or find a VCL-level property on the `container_window` to set its background color.

2.  **Window Resizing:** The `on_window_resized` listener is in place but may have a slight lag or precision issue with the pixel-to-1/100mm conversion (`26.458` factor).
    *   **Try:** Query the system DPI via `XDevice` to get a more accurate conversion factor than the 96 DPI constant.

3.  **Paragraph Formatting:** The text still looks "pushed in."
    *   **Try:** Explicitly modify the "Standard" or "Default" Paragraph Style to set `ParaLeftMargin`, `ParaRightMargin`, `ParaFirstLineIndent`, and `ParaTopMargin` all to 0.

4.  **Simulated Syntax Highlighting:** The current regex-based approach in `append_rich_text` works for basic blocks.
    *   **Try:** Add support for inline backticks (` `code` `) and better handling of nested triple-backticks.
