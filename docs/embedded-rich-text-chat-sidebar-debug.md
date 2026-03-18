# Embedded Rich-Text Chat Sidebar (PR #91) — LibreOffice Embedding Debug Notes

This document records the current state of the rich-text chat sidebar implementation from PR #91:

- [PR #91: rich text chat sidebar using embedded Writer document](https://github.com/KeithCu/writeragent/pull/91)

## What we expected

The sidebar response area is replaced with an *embedded* (hidden) Writer document so we can render rich formatting (colored role prefixes like **You:** and **Assistant:**).

Implementation entry points:

- `plugin/modules/chatbot/rich_text.py` → `create_embedded_writer_doc(...)` + `append_rich_text(...)`
- `plugin/modules/chatbot/panel_wiring.py` → `_wireControls(...)` hides the plain response control only if the embedded doc succeeds.

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

- `plugin/modules/chatbot/rich_text.py`
- `plugin/modules/chatbot/panel_wiring.py`

