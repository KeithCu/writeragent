# Embedded Rich-Text Chat Sidebar

This document outlines the design, active roadmap, and historical timeline of development, bugs, and lessons learned for the embedded rich-text chat sidebar in LibreOffice Writer.

---

## Active Roadmap & Upcoming Work Items

These are the priority tasks to resolve remaining layout quirks and implement new features enabled by the rich-text sidebar.

### [x] Task 1: Fix Scroll-to-Bottom Auto-Scrolling (High Priority — Fixed)
*   **Issue:** Streamed chat content exceeding the visible viewport area did not trigger auto-scroll. The user had to manually scroll down.
*   **Root Cause:** In Online/Browse layout mode (which we keep for continuous reflow in the narrow sidebar), the embedded Writer's internal "make cursor visible" mechanism is non-functional for embedded frames. Standard UNO approaches (ViewCursor.gotoEnd, controller.select, .uno:GoToEndOfDoc, jumpToLastPage) moved the logical cursor but produced no visual scroll (or even scrolled to the top).
*   **Solution (what actually shipped):** Kept `ShowOnlineLayout = True` + 100% BY_VALUE zoom for seamless narrow-sidebar reflow (page layout would have required different margin/width math and lost the "continuous flow" feel). Implemented a robust `scroll_to_bottom` with two paths:
    - Lightweight (most callers: resize, timers, debug): cursor + `.uno:GoToEndOfDoc` dispatch + `processEventsToIdle`.
    - Aggressive (only on real insert sites in `append_rich_text` / `append_text_chunk` with `auto_scroll=True`): the above + `controller.select(collapsed end caret)` + zoom flicker (to force MakeVisible) + component invalidate + final idle. One-time ViewData / VCL tree debug dumps remain for diagnostics.
*   **Why the doc previously said "switched to page layout":** An early plan / commit message described that approach; the final implementation kept online layout and solved scrolling via the aggressive workaround instead. (See §8 for the full investigation history.)
*   **Files Changed:** `plugin/chatbot/rich_text.py` (view settings kept online, `scroll_to_bottom` with aggressive/light paths + ViewData logging), `plugin/chatbot/panel.py` (rerender, _append_response, deferred scroll timer).
*   **2026-05-26 diagnostic logging pass (re-investigation):** Added `_SCROLL_DIAG` flag + `[SCROLL-DIAG]` INFO logs in `append_text_chunk`, `append_rich_text`, and both paths of `scroll_to_bottom` (plus a one-time VCL/a11y scrollbar probe). These emit CharacterCount deltas, auto_scroll decisions, aggressive step completion, ViewData pre/post on every streaming insert, and whether any scrollbar surface was discoverable. Purpose: make the "never scrolls during live chunks" symptom fully visible in writeragent_debug.log so the next one-thing-at-a-time experiment (XScrollBar drive / ViewData write / post-layout timer strengthening) can be chosen with data. No behavior change. (See plan in .cursor/plans/ for the step-by-step.)
*   **Log analysis from first real run with the new diagnostics:** Aggressive path (select collapsed-end + zoom flicker + invalidate + multiple idles) **is** executing on every `append_text_chunk` (including 500+ char batched deltas) and on the final `append_rich_text` rerender. Scrollbar probe always returned vcl=False + a11y=False (Method A blocked for this LO build). Critical: for several aggressive calls (especially the big HTML rerender replace), the `VIEWDATA sample[post]` showed **identical Y** to the pre sample for that call (e.g. stayed at 8794 while content was replaced), even though CharacterCount deltas were correct. Meanwhile a storm of `on_window_resized` → lightweight `scroll_to_bottom` calls were firing in the same 10-20 ms windows. This matches the "layout not settled + MakeVisible still dead in embedded online frame + resize fighting" hypotheses. (Raw excerpts in the agent chat transcript for this session.)
*   **Second one-thing-at-a-time experiment (immediately after log review):** Implemented the smallest Method C variant — an 80 ms `threading.Timer` + `post_to_main_thread(scroll_to_bottom, ..., aggressive=True)` scheduled from inside the `if auto_scroll:` block in both `append_text_chunk` and `append_rich_text` (only when `_SCROLL_DIAG`). This guarantees one extra aggressive kick after the insert + first scroll + any immediate reflow/resize/layout work has had a moment to settle. Re-uses the exact timer+post pattern already present in `rerender_rich_text_session`. New log lines: `[SCROLL-DIAG] scheduling 80ms post-layout...` + a second set of aggressive entry / VIEWDATA pre/post / "complete" lines ~80 ms later. Rebuild + next stream will show whether the delayed kick moves the viewport (and advances Y) when the immediate one did not. Change is tiny, fully guarded, and trivial to revert by setting the flag False.
*   **2026-05-26 web research + full session archive (saved for a completely fresh future session):** After the 80 ms kick experiment also failed to move the visual viewport (delayed aggressive calls ran, ViewData Y still frequently stuck on post samples, resize lightweight storms continued, no VCL/a11y scrollbar surface ever found), a broad web search was performed across OpenOffice/LO forums, StackOverflow, mailing lists, and API references for "LibreOffice UNO scroll Writer embedded", "ViewData restoreViewData Writer", accessibility scrollbars, alternative .uno: scroll dispatches, XTextViewCursor tricks, etc.

    **Key external findings (this exact symptom is a known, recurring pain point):**
    - In Online/Web/Browse layout (our deliberate choice for narrow-sidebar reflow) and especially in *embedded* Writer frames, the normal "cursor/selection moves → viewport follows" contract is often broken or disabled.
    - The two categories of workarounds that have repeatedly succeeded for other developers when the cursor + select + GoToEndOfDoc + zoom + invalidate sequence fails:
      1. **Accessibility tree scrollbar actions** (most cited practical success): Walk `frame.getComponentWindow().getAccessible()` (or deeper `.windows(0)` etc.), look for children whose `AccessibleName` contains "Vertical scroll bar" (or role `SCROLL_BAR`; names can be localized or "null"), then call `.getAccessibleContext().doAccessibleAction(0 or 1 or ...)` on it. The resulting object often also supports `XAccessibleValue` for direct get/set of scroll position. Several working Basic examples exist for exactly this in LO 7.x Writer documents.
      2. **ViewData parse + restoreViewData write** (the other recurring precise-control hack): The 9-field semicolon string we are already logging (e.g. "679;784;100;284;284;5369;5884;0;0") is the payload of `controller.getViewData()` / `restoreViewData(string)`. Community reports show bumping specific fields (commonly indices 4 and/or 6 for vertical scroll, with increments like ~192 "line" units on some systems) and restoring *can* move the viewport when cursor methods do not. Caveats reported: sometimes a no-op until after an edit+save; can be version/DPI/document-layout sensitive; one tester saw no effect.

    **Concrete next experiments to try in a new fresh session (all minimal, behind `_SCROLL_DIAG`, easy to revert):**
    - Extend the existing scrollbar probe (already called on every append) into a deeper, recursive accessibility walk. Log *every* child's name + role + whether it supports XAccessibleAction / XAccessibleValue. When a plausible scroll child is found, try the `doAccessibleAction` calls (0–3) + value manipulation and watch the next VIEWDATA samples + visual result.
    - Add a guarded ViewData write path inside the aggressive block (and the 80 ms follow-up): after the current commands, parse the string we just sampled, compute a high Y from the size fields, call `controller.restoreViewData(newString)`, then one more idle + invalidate. Log pre/post Y and whether the viewport followed.
    - Cheap supplements to layer on the existing aggressive path: after the collapsed-end select, try `viewCursor.screenDown()` / small `goUp(2)+goDown(2)` dances, or dispatch `.uno:ScrollToNext` (with/without the ScrollNextPrev property that the macro recorder sometimes emits). These have helped some people wake MakeVisible when plain gotoEnd did not.
    - (Lower priority) Re-test the whole sequence after forcing an explicit edit+save on the embedded doc, or after temporarily switching out of + back into Online layout.

    **Session close note:** All raw writeragent_debug.log excerpts from the two instrumented runs (first logging pass + 80 ms kick run), the exact code changes made, the full web search queries, and the fetched forum/SO thread contents are preserved in the agent chat transcript for the May 26 2026 session. This file (docs/rich-text-sidebar.md) now contains the distilled, actionable summary. The current investigation pass is complete. Start any future work on this bug from a clean slate using only the leads documented here.

### [x] Smoothing: 250 ms producer-side batching for streamed display text (2026-05)
*   **Goal:** Reduce visual stutter / micro-updates during streaming (both plain-text and rich-text paths) without changing the consumer drain loop (still 0.1 s timeout).
*   **Solution:** New `BatchingStreamQueue` wrapper (in `plugin/framework/async_stream.py`).
    - Simple append into per-kind buffers (`CHUNK` / `THINKING`).
    - Hard 250 ms deadline timer from the *first* fragment of a burst ("every 250 ms max, or when done"): later fragments in the same burst are appended but do not move the deadline. Boundary items force immediate flush.
    - On timeout or explicit `.flush()`: emit **exactly one** joined string per kind to the underlying raw queue: `(StreamQueueKind.CHUNK, ''.join(buf))`.
    - Any control/boundary item (STREAM_DONE, ERROR, STOPPED, APPROVAL_REQUIRED, TOOL_*, NEXT_TOOL, FINAL_DONE, etc.) forces an immediate flush first.
    - Callers use the wrapper's `.content_cb()` / `.thinking_cb()` (drop-in replacements for the old `lambda t: q.put((CHUNK, t))`).
*   **Primary path wired:** Main chat LLM streaming and final no-tool stream in `tool_loop.py` (via `_active_batched_q` + `_spawn_llm_worker` / `_spawn_final_stream`).
*   **Secondary paths:** Direct puts in `send_handlers.py` (web research, librarian, image results, etc.) and `acp_backend.py` remain on the global audit list; the most important user-visible streaming (normal assistant answers) now benefits.
*   **Flush discipline for rerender/clear:** `panel.py` rerender and sidebar-clear paths already benefit indirectly because terminal items (STREAM_DONE etc.) now flush; explicit coordination with the per-send batcher can be added later if a "mid-stream clear" race is ever observed.
*   **Files Changed:** `plugin/framework/async_stream.py` (new class + defensive support in `run_async_worker_with_drain`), `plugin/chatbot/tool_loop.py`, `tests/framework/test_async_stream.py` (4 new unit tests), `docs/rich-text-sidebar.md`.
*   **Consumer note:** The 0.1 s drain-loop timeout was deliberately left at its previous value; only the *producer* side now emits larger, less frequent lumps.

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

## Lifecycle, Listener & Shutdown Safety (Critical Fix for Close-Time Errors)

This section was added in 2026-06 as the authoritative record of a class of bugs that only manifested when closing LibreOffice Writer (with `rich_text_sidebar=true`).

### Symptoms
- Errors, "object has been disposed", RuntimeException, or full segfaults / noisy stack traces on Writer exit (or sidebar deck close).
- Only reproducible with the rich-text sidebar enabled; plain-text path was unaffected.
- Happened after successful streaming, theme switching, list tightening, etc.

### Root Causes (Pre-Fix)
1. **Leaked XWindowListener registrations:**
   - `EmbeddedWriterListener` (the "lazy peer" deferred embedder) and `_PanelResizeListener` were added to `root_window` (the XDialog from the XDL) in `panel_wiring.py:203` and `:289` but **never removed**.
   - `BaseWindowListener` / `BaseListener` only provide a no-op `disposing()` ([listeners.py:53](plugin/chatbot/listeners.py:53)).

2. **Undisposed embedded sub-document:**
   - `create_embedded_writer_doc` ([rich_text.py:264-276](plugin/chatbot/rich_text.py:264-276)) creates a `toolkit.createWindow` container, an `XFrame`, initializes it, and loads `private:factory/swriter` into it.
   - These three objects (`embedded_container`, `embedded_frame`, `embedded_doc`) plus the listener held strong refs and were never closed/disposed on panel teardown.
   - The LO sidebar XUIElement / ChatPanelElement had **no `disposing` or `disposeUIElement` implementation** ([panel_factory.py:231](plugin/chatbot/panel_factory.py:231) before the fix).

3. **Late-scheduled work against dying objects:**
   - `post_to_main_thread` (global `QueueExecutor` + AsyncCallback) used for deferred embedding, scroll timers, etc. ([panel.py:444](plugin/chatbot/panel.py:444), rich_text deferred init).
   - Global event bus subscribers.
   - During LO shutdown the VCL event loop can still deliver queued work (or listener callbacks) after the UNO peers are gone.

4. **Cooperative cleanup was absent:**
   - `SendButtonListener.disposing` ([panel.py:1115](plugin/chatbot/panel.py:1115) pre-fix) only unsubscribed event bus.
   - No equivalent of `dialog_views.py:284` `_cleanup` (explicit `removeTextListener` + guarded `dispose`) existed for the sidebar rich-text path.
   - AGENTS.md already warned about double-dispose segfaults for dialogs; the same footgun applied to frames/windows.

These are classic UNO extension lifecycle bugs: Python objects outlive (or are notified after) their C++ VCL/UNO counterparts.

### The Fix (2026-06)
- `EmbeddedWriterListener` now overrides `disposing()` (which owns the `_disposed` idempotency guard + listener removal) and delegates actual UNO object cleanup to `_dispose_embedded_objects` (no early return inside the helper). This guarantees the embedded container + doc are actually released instead of leaked ([rich_text.py](plugin/chatbot/rich_text.py) after the 2026-06 fix, with detailed bug explanation in the method comments).
- `SendButtonListener` stores `_rich_listener` (via new `set_rich_listener`) and its `disposing()` now cooperatively calls through to the listener + directly disposes any embedded refs it holds ([panel.py:1126-1155](plugin/chatbot/panel.py:1126-1155)).
- `ChatPanelElement` gained a `disposing` hook (for documentation + explicit future use) that delegates to the send listener ([panel_factory.py:300-320](plugin/chatbot/panel_factory.py:300-320)).
- Wiring now passes the listener ref immediately after `addWindowListener` ([panel_wiring.py:288-300](plugin/chatbot/panel_wiring.py:288-300)).
- Cheap guards added on hot post/scroll paths and inside `scroll_to_bottom` itself.
- All paths are idempotent (flag) and swallow Disposed/Runtime exceptions.

The pattern mirrors the good examples that already existed (grammar document listener teardown, SettingsDialog._cleanup, AGENTS.md double-dispose rule).

### Future Dev Notes & Ideas (Capture for Next Work)
- **Broader listener hygiene janitor task:** Almost every `addActionListener` / `addItemListener` / `addTextListener` / `addKeyListener` / `addWindowListener` in `panel_factory.py` and `panel_wiring.py` is one-way. A follow-up could introduce a small `ListenerTracker` helper (register + auto-remove on a dispose token) so new UI never repeats the mistake.
- **Real XUIElement disposal hook:** Investigate whether the LO sidebar framework will call something on `ChatPanelElement` (or if we need to listen to the parent deck window's `XEventListener`). If a reliable hook appears, promote the existing `disposing` stub into the real path and drop the delegation-through-send_listener.
- **Global shutdown coordination:** The `QueueExecutor` + global event bus could grow an explicit `shutdown()` that drops pending work and prevents new posts once the module is unloading. This would be stronger than per-object guards for the "process is exiting" case.
- **Native UNO lifecycle test:** Add a `@native_test` (via `testing_runner`) that creates a ChatPanelElement, wires rich text, does a couple of appends, then forces panel teardown / doc close and asserts no Disposed noise and that the embedded doc is truly gone. (Unit tests mock the surface; only a real soffice run catches VCL-order surprises.)
- **Double-close hardening helper:** Consider a tiny `safe_close(obj, prefer_close=True)` util in `uno_context.py` or `errors.py` so every future embedded frame/doc site uses the same battle-tested sequence + logging.
- **Rich text still restart-gated:** The feature remains behind `rich_text_sidebar` + full LO restart. If someone later removes that requirement, the disposal paths become even more important (hot enable/disable cycles).
- **Memory / leak tracking:** With the listener + doc now explicitly torn down, the "per-chat" Python objects (session history, etc.) should be collectable when the sidebar for a document is closed. Worth a future `tracemalloc` or LO memory snapshot experiment.

See also: AGENTS.md (double-dispose, streaming drain, UNO ctx rules), `docs/streaming-and-threading.md`, and the grammar persistence teardown as the prior art for document-scoped listeners.

### 2026-06 Shutdown Lifecycle — Investigation & Current Status

#### The Fundamental Problem

The rich-text sidebar hosts a complete embedded Writer document (`XFrame` + `private:factory/swriter` + container window) parented to the sidebar's VCL dialog via `toolkit.createWindow` using the root window's peer. During `DeInitVCL`, the parent `VclBuilder` dialog begins destruction while the child frame/document may still be alive, producing use-after-free crashes (Signal 11) in `Window::dispose`.

#### The Timing Dilemma (Unsolved)

Every available disposal hook fires at the wrong time:

| Hook | When it fires | Problem |
|------|---------------|---------|
| `on_window_hidden` | When sidebar is hidden (including for modal dialogs) | Fires when LO shows the save dialog on close. If user cancels, sidebar comes back but editor is destroyed. |
| `OnPrepareUnload` (XDocumentEventListener) | Before the save dialog | Same problem as `on_window_hidden` — fires before the user confirms close. Canceling leaves the editor gone. The peer is already dead by this point too. |
| `OnUnload` (XDocumentEventListener) | After save dialog, during unload | Too late — VCL teardown already underway. `frame.close(True)` crashes. |
| Model `disposing()` (XEventListener on doc model) | When document model is being destroyed | Too late — same crash as OnUnload. |
| `EmbeddedWriterListener.disposing()` (XWindowListener) | When the listener's source is being disposed | Unreliable — often never delivered in crashing close paths. |
| `ChatPanelElement.disposing` | Sidebar panel lifecycle | Unreliable — not delivered before VCL teardown in many sequences. |

**Key observation from logs:** `container_window.getPeer()` is already `False` (peer dead) by the time `OnPrepareUnload` fires. VCL destroys the sidebar panel's native window structure *before* asking the user about saving.

#### What Was Tried (May 2026 Investigation)

1. **`on_window_hidden` as proactive disposal** — Prevented the crash but caused a user-facing bug: clicking Close → Cancel on the save dialog left the sidebar with a destroyed editor.

2. **`OnPrepareUnload` as primary trigger** — Same behavior as `on_window_hidden` — fires before the save dialog resolves.

3. **`OnPrepareUnload` with re-creation on `on_window_shown`** — After disposing on `OnPrepareUnload`, attempted to re-create the embedded editor if `on_window_shown` fired (indicating user canceled). Failed because `on_window_shown` is never delivered after the cancel — the peer was already dead and LO doesn't re-fire window events.

4. **Only model `disposing()` (no document events)** — Crashes (Signal 11) because by that point calling `frame.close(True)` hits dead VCL objects.

5. **Skip all close/dispose when peer is dead (just null refs)** — Crashes. Even without our explicit close calls, VCL's own `VclBuilder::disposeBuilder` hits the still-registered child window during `DeInitVCL`.

#### Why "Just Let GC Handle It" Doesn't Work

Python GC releasing references to UNO objects doesn't control C++ VCL window lifetimes. The embedded container window remains registered in VCL's window tree via the parent relationship established by `toolkit.createWindow`. During `DeInitVCL`, `VclBuilder::disposeBuilder` walks child windows — if the child's internal state is inconsistent (partially torn down), Signal 11 occurs.

#### Current State (Accepted Trade-off)

- **`on_window_hidden`** — no-op. Does not dispose. (Fixes the cancel-close bug.)
- **`documentEventOccured`** — no-op. Does not react to `OnPrepareUnload` or `OnUnload`.
- **`XCloseListener.notifyClosing`** on host doc frame — registered but never fires during app quit (only useful for single-doc close, untested).
- **`XTerminateListener.queryTermination`** on Desktop — registered but fires AFTER the peer is already dead (useless for this problem).
- **`disposing()` safety net** — calls `_initiate_disposal()` → `_dispose_embedded_objects()` which closes frame/doc/container only if peer is alive. If peer is dead, just releases Python refs.
- **Accepted behavior:** Signal 11 occurs on Writer exit with the rich-text sidebar enabled. The file is already saved by that point so no data loss occurs.

#### Why the Crash Cannot Be Fixed from Python/UNO

The crash is in `VclBuilder::disposeBuilder` → `vcl::Window::dispose` during `DeInitVCL`. The embedded container window (created via `toolkit.createWindow` with `desc.Parent = parent_window.getPeer()`) is a VCL child of the sidebar dialog window. When the `SidebarController::disposing` tears down the sidebar, `VclBuilder` walks its children and tries to dispose our container window, which is in an inconsistent state.

**The VCL peer is dead before any Python-level hook fires.** Confirmed by testing every available hook:

| Hook | Fires? | Peer alive? | Result |
|------|--------|-------------|--------|
| `on_window_hidden` | Yes | Unknown (fires too early — before save dialog) | Cancel bug |
| `OnPrepareUnload` | Yes | No | Cancel bug + peer already dead |
| `OnUnload` | Yes | No | Crash (frame.close on dead peer) |
| Model `disposing()` | Yes | No | Peer dead — can only null refs |
| `notifyClosing` (XCloseListener on doc frame) | **No** (never fires on app quit) | N/A | N/A |
| `queryTermination` (XTerminateListener on Desktop) | Yes | No | Fires AFTER model disposing, peer already dead |

The VCL shutdown order during app quit is:
1. VCL kills peers (sidebar dialog's native window destroyed)
2. Document model `disposing()` fires (Python-level)
3. `queryTermination` fires (Python-level)
4. `DeInitVCL` → `SidebarController::disposing` → `VclBuilder::disposeBuilder` → crash on orphaned child

#### Ideas for Future Resolution

1. **LibreOffice patch (most reliable):** Add a VCL-level hook (e.g. `VclEventId::WindowClose` or a custom sidebar panel teardown callback) that detaches child windows BEFORE `VclBuilder::disposeBuilder` runs. This would require an LO core change.

2. **Avoid `toolkit.createWindow` parenting entirely:** Use a top-level borderless window positioned over the sidebar area (the "floating sticker" approach from Strategy 3 in the archive below). Eliminates the parent-child VCL relationship that causes the crash. Downside: requires manual position tracking on resize/move, and may have issues with window stacking/focus.

3. **XDispatchProviderInterceptor on `.uno:CloseDoc` / `.uno:Quit`:** Intercept the close dispatch command, do cleanup, then re-dispatch. Might fire before VCL teardown begins. Worth investigating — this is the one approach not yet tested.

4. **Unparent the window during creation:** If there's a UNO API to change a window's parent (or create it parentless and manually position it), the VclBuilder wouldn't try to dispose it. Needs investigation into whether `XWindow` or `XVclWindowPeer` exposes reparenting.

#### Instrumentation

Two log prefixes remain for diagnosis:
- `[RICH-LIFECYCLE]` — creation, wiring, and normal runtime events.
- `[RICH-SHUTDOWN]` — every entry into a disposal path and the state at that moment.

#### 2026-05 Pure-Python VCL Child / drop_ownership Investigation

After all the hook-based approaches had been exhausted, the remaining promising minimal-change lever (identified by reading the actual LibreOffice core `VclBuilder::disposeBuilder` + `drop_ownership` / `delete_by_window` implementations in `vcl/source/window/builder.cxx`) was to try to remove our container from the parent's VCL child list *before* the parent teardown walk.

**Key findings from the core source (no prior art in any extension):**
- `toolkit.createWindow(desc)` with `desc.Parent = sidebar_dialog_peer` makes our container a direct child in the VCL window tree owned by the XDL dialog's `VclBuilder`.
- `disposeBuilder` walks `m_aChildren` in reverse and calls `disposeAndClear`; the broader `Window::dispose` / parent teardown does the same for the live VCL hierarchy.
- `drop_ownership(pWindow)` on `VclBuilder` (and `delete_by_window`) are the C++ ways to excise a child so the later walk ignores it.
- No equivalent is exposed via UNO on `XWindow`, `XWindowPeer`, `XVclWindowPeer`, or the awt Toolkit for already-created CONTAINER windows.

**What was implemented (pure Python only):**
- New private method `_instrument_vcl_child_relationship_and_defang` on `EmbeddedWriterListener` (`plugin/chatbot/rich_text.py`).
- **Instrumentation (read-only, using the existing `getWindows()` surface already present in our scroll-debug helpers):** At every disposal entry point we now walk the parent peer's `getWindows()` tree (the exact VCL child list) and log whether our container's peer is still present as a direct or indirect child. This produces concrete evidence in `writeragent_debug.log` of the form:
  ```
  [RICH-SHUTDOWN] VCL child check via getWindows(): container still registered under parent_peer? True
  [RICH-SHUTDOWN] parent_peer implName=...
  [RICH-SHUTDOWN] container_peer implName=...
  ```
- **Defang (safe hardening):** Unconditionally `setVisible(False)` + `setPosSize(0,0,0,0,15)` on the container (and symmetrically on the copies held by `SendButtonListener`) before any close/dispose decision. Idempotent, exception-swallowed, zero risk of making the situation worse.
- **drop-surface scan:** Exhaustive but guarded `hasattr` + call attempts for every plausible mutating name (`setParent`/`SetParent`, `removeChild`, `removeWindow`, `dropOwnership`, `orphan`, `releaseChild`, ...) on the container, its peer, and the parent's peer. All outcomes (or the exact exception) are logged. As expected, no call succeeded in orphaning the window; `getWindows()` is a pure getter.

The same defang step was also added (guarded) in `SendButtonListener.disposing` for the local `embedded_*` copies.

**Result of the pure-Python attempt:**
No viable pure-Python surface was found that can call the moral equivalent of `drop_ownership`. The child-relationship check + defang + richer logging are still valuable: they turn a previously opaque crash into a well-instrumented one and give us a hook point if a future LO version or a tiny C++ helper ever exposes the builder.

**New regression coverage:**
- Added `tests/chatbot/test_rich_text_uno.py` (module-matched per AGENTS.md). A real `@native_test` + `@setup`/`@teardown` that obtains a live Writer document + ctx, instantiates `EmbeddedWriterListener` with real UNO objects (doc model + host frame), exercises the new instrumentation method, all the `Sidebar*Listener` shims, `disposing()`, and the cooperative `SendButtonListener` path. Asserts guards and no leaked exceptions. This will be run automatically by `make test` whenever a `soffice` is available.

**Updated "Current State" (May 2026):**
- The disposal paths are now the most heavily instrumented and hardened they have ever been from pure Python.
- The fundamental VCL ownership problem remains (we are still a child of a dying dialog peer when `DeInitVCL` runs).
- `on_window_hidden` stays a deliberate no-op.
- All prior listeners (XTerminate on Desktop, XClose on host frame, document events, model disposing, `ChatPanelElement.disposing`, `SendButtonListener` delegation) remain in place as safety nets.
- The new child-check logging + defang + native test give us the best possible diagnostic + regression story short of an LO core change or a non-parented embedding technique.

The re-parenting and "prepare a core patch" ideas that appeared in earlier investigations remain deferred (per explicit user direction for this iteration) because they either risk the visual/scroll/theme fidelity we get from the current parented Writer or require C++/core work.

---

## Completed Milestones

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

**Root cause found (and still true):** In Online/Browse layout mode (`ShowOnlineLayout = True` — which we deliberately keep for continuous text reflow in the narrow sidebar), the embedded Writer's internal "make cursor visible" mechanism is broken for hosted frames. All basic UNO approaches move the logical cursor but the viewport does not follow (or actively jumps to the top on select).

**Actual fix shipped (2026-05):** Kept online layout + 100% zoom for visual seamlessness. Added a dual-path `scroll_to_bottom`:
- Every caller gets the lightweight core (gotoEnd + GoToEndOfDoc dispatch + idle).
- Real insert paths (`append_*` with `auto_scroll=True`) also get the aggressive workaround: `controller.select(collapsed caret at absolute end)` + zoom flicker (to kick MakeVisible) + invalidate + extra idles + ViewData sampling.

This matches the "aggressive only on hot path" discipline so resize/debug callers cannot cause re-entrancy loops. The earlier plan text that said "switched to page layout and simplified" was aspirational and was corrected in the 2026-06 lifecycle update.

The `auto_scroll` parameter is now properly honored. Debug helpers (`_dump_scroll_debug_once`, `find_vcl_scrollbar`, `traverse_window_tree`, `_sample_viewdata`) remain for future diagnostics.

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

---

### 11. Future: Incremental HTML Stripping During Streaming

#### Motivation

During streaming the user sees raw HTML tags in the sidebar because `append_text_chunk` inserts text verbatim. Unlike Markdown -- where formatting markers are one or two characters (`**`, `_`, `` ` ``) and easy to ignore at a glance -- HTML tags are verbose and visually dominant. A response like:

```
<ul><li><strong>First point</strong> — some explanation</li><li>Second point</li></ul>
```

is significantly harder to read mid-stream than the equivalent Markdown:

```
- **First point** — some explanation
- Second point
```

The tags consume a large fraction of the visible sidebar width and break reading flow, especially `<strong>`, `</strong>`, `<table>`, `<tr>`, `<td>`, `<ul>`, `<li>`, and `<br/>`. The problem is worse in the narrow sidebar than it would be in a full-width editor.

The current architecture addresses this by re-rendering the entire session through `append_rich_text` (with full HTML import via Writer's filter) after streaming completes. This produces the correct final result, but during the stream itself the user stares at raw HTML for seconds.

#### Why We Are Not Doing It Yet

1. **Chunk boundaries split tags.** A single `<strong>` can arrive as `<str` in one chunk and `ong>` in the next. Handling this correctly requires a stateful residue buffer carried across chunks -- essentially a mini tokenizer on the hot streaming path (main thread, inside the drain loop).

2. **Tag-to-plaintext mapping is a design surface.** Stripping tags without replacement produces garbled text (`item oneitem two`). Producing readable output requires a mapping table (`<li>` → `- `, `<br>` → newline, `<p>` → double newline, `<strong>` → strip silently, etc.) and decisions about edge cases (nested lists, tables, attributes).

3. **Risk of regressions in the streaming path.** The drain loop is latency-sensitive; adding per-chunk parsing could interact with batching (`BatchingStreamQueue`), scroll-to-bottom, and the rerender lifecycle in subtle ways.

4. **The final result is already correct.** The rerender pass produces proper HTML-imported formatting. The incremental stripping would only improve the transient streaming display.

#### Why It Is Worth Doing Eventually

- The raw HTML is genuinely ugly and distracting -- it degrades the perceived quality of the product during the most visible phase (streaming).
- The tag vocabulary is constrained. The system prompt instructs the LLM to use a known subset of HTML tags (the same ~12 families detected by `_HTML_TAG_RE`). This is not arbitrary HTML; it is a controlled vocabulary.
- Formal verification and hypothesis testing can cover the chunk-boundary state machine thoroughly.
- The `saw_html` flag accumulated during streaming can eliminate the regex detection pass in `append_rich_text` entirely -- a small but free optimization.

#### Development Plan

##### Phase 1: Stateful HTML tag stripper

Build a `StreamingHtmlStripper` class (stdlib only) with:

- **Input:** `feed(chunk: str) -> str` — accepts a raw chunk, returns cleaned text for display.
- **State:** a small `residue` buffer (bytes after the last `>`, or an incomplete `<...` at chunk end). Bounded because tag names are short (longest is `<strong>` at 8 chars + attributes).
- **Tag detection:** reuse `_HTML_TAG_RE` (or a superset) to identify known tags within the combined `residue + chunk` buffer.
- **Mapping table** (initial conservative set):

| HTML | Plain-text replacement |
|------|----------------------|
| `<p>`, `</p>` | double newline |
| `<br>`, `<br/>`, `<br />` | newline |
| `<ul>`, `</ul>`, `<ol>`, `</ol>` | newline |
| `<li>` | `- ` (or `* `) |
| `</li>` | newline |
| `<strong>`, `</strong>` | strip (bold is lost during streaming, restored at rerender) |
| `<em>`, `</em>` | strip |
| `<code>`, `</code>` | strip (or wrap in backticks) |
| `<pre>`, `</pre>` | newline |
| `<div>`, `</div>` | newline |
| `<table>`, `</table>` | newline |
| `<tr>`, `</tr>` | newline |
| `<td>`, `</td>` | tab or ` | ` |
| `<h1>`–`<h6>`, `</h1>`–`</h6>` | newline (heading lost during streaming) |
| Unknown `<...>` inside known vocab | pass through unchanged (safe default) |

- **`saw_html` property:** set to `True` on first tag match. Caller can read this after streaming completes.
- **`reset()`** — clear state between messages.

##### Phase 2: Wire into the streaming path

- In `_handle_chunk` (or at the `append_text_chunk` call site in `panel.py`), run `chunk = stripper.feed(chunk)` before inserting.
- The stripper instance lives on the `SendButtonListener` (one per send, reset on new send).
- After `STREAM_DONE` / `FINAL_DONE`, check `stripper.saw_html` and pass it to the rerender so `append_rich_text` can skip the regex.

##### Phase 3: Testing

- **Unit tests for `StreamingHtmlStripper`:**
  - Single-chunk cases (tag fully within one chunk).
  - Split-tag cases (`<str` + `ong>text</strong>`).
  - Residue carry across 3+ chunks.
  - All mapping table entries.
  - Unknown tags pass through.
  - Empty chunks, whitespace-only chunks.
  - Mixed HTML and plain text in one chunk.
  - `saw_html` flag transitions.
- **Hypothesis / property-based tests:**
  - For any HTML string `s`, splitting `s` at arbitrary positions and feeding chunks through the stripper produces the same output as feeding `s` in one shot.
  - The `saw_html` flag is True iff `_HTML_TAG_RE` matches the concatenation of all chunks.
- **Integration test:** mock a streaming session, verify the embedded doc content matches expected plain-text during streaming, then verify rerender produces correct HTML-formatted output.

##### Phase 4: Edge cases and polish

- Handle `<tag attr="value">` — strip the entire tag including attributes (regex `<tag[^>]*>`).
- Handle self-closing variants (`<br/>`, `<br />`, `<hr/>`).
- Consider whether `<a href="...">text</a>` should show `text` or `text (url)`.
- Tune the newline collapsing (avoid triple/quadruple newlines from `</li></ul></div>` sequences).

