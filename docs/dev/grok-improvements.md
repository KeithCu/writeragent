### Plan for Code Improvements

Based on my review of the codebase, here are suggestions to reduce complexity, improve robustness, and simplify the system while keeping necessary logging. The code is functional but has areas of excessive intricacy (e.g., multiple logging mechanisms, frequent UI dispatching, and monolithic files). Prioritized high-impact changes first.

#### 1. **Consolidate and Simplify Logging (High Priority)**
   - **Issue**: Too many log sinks (`log_to_file` to `~/log.txt`, `debug_log` to chat logs, `agent_log` to NDJSON). This increases I/O overhead, maintenance, and file proliferation. The watchdog thread adds unnecessary complexity for a console message ("Hung: ...").
   - **Improvements**:
     - Merge `log_to_file` (API logs) and `debug_log` (chat logs) into a single debug logger that writes to a single file (e.g., `localwriter_debug.log` in user config). Keep `agent_log` if hypothesis tracking is essential, but make it optional/configurable.
     - Remove duplicate paths (e.g., fallback to `/tmp` is redundant if user config works).
     - Remove the watchdog thread (`start_watchdog_thread`, `update_activity_state`) as it's fragile (activity states are hard to maintain) and only logs "hanged" states without recovery. Replace with simpler timeout logging in the main loop.
     - Logging code is scattered; centralize into a single logging module with context-aware logging (e.g., prefix with "API", "Chat", etc.). Reduce granular logs in loops (e.g., don't log every SSE line unless debugging).
     - Estimated Impact: Reduces code by 50+ lines, 3-4 redundant functions, and improves performance by cutting I/O.

#### 2. **Reduce UI Dispatch Frequency (High Priority)**
   - **Issue**: `toolkit.processEventsToIdle()` was called aggressively: every 0.05s when no queue items, after every batch, and per-chunk if `PROCESS_EVENTS_DURING_STREAM=True`. This could cause hangs or slowdowns.
   - **Improvements** (implemented): `processEventsToIdle()` is now called only after processing a full batch of queue items or on drain-loop timeout. Per-chunk calls in `core/api.py` are disabled (`PROCESS_EVENTS_DURING_STREAM=False`). Queue timeout increased to 0.1s to reduce poll frequency.
     - Test for hangs; if needed, add a global flag to disable/callback-based dispatching.
     - Estimated Impact: Fewer redraws, smoother streaming, simpler threading logic.

#### 3. **Refactor Large Files into Modules (Medium Priority)**
   - **Issue**: `main.py` (500+ lines), `chat_panel.py` (700+ lines), `api.py` (500+ lines) are monolithic. This hinders maintenance and understanding.
   - **Improvements**:
     - Split `main.py`: Move settings/input dialogs to `core/dialogs.py`. Move Writer/Calc edit logic to `core/edit_actions.py`.
     - Split `chat_panel.py`: Extract `ChatSession` and listeners to separate files (e.g., `core/chat_session.py`).
     - Split `api.py`: Move request builders to `core/request_builder.py`. Extract streaming logic to `core/streaming.py`.
     - Ensure imports use relative paths consistently.
     - Estimated Impact: Easier navigation, better testing, reduced merge conflicts.

#### 4. **Enhance Error Handling and Robustness (Medium Priority)**
   - **Issue**: Errors are often swallowed or logged but not handled gracefully (e.g., import failures in `_do_send` just set status without fallback). UI updates during streaming can fail (e.g., UNO calls from threads).
   - **Improvements**:
     - Add more try-except around critical points (e.g., document access in `chat_panel.py`). Provide fallbacks (e.g., if tool import fails, switch to simple stream).
     - Standardize error callbacks: Ensure all async functions call `on_error_fn` on any thread issues, not just API errors.
     - Validate config items (e.g., null-check model strings before requests).
     - Add a global exception hook for unhandled errors.
     - Estimated Impact: Fewer crashes, better user feedback.

#### 5. **Simplify Tool-Calling and Streaming Logic (Medium-High Priority)**
   - **Issue**: The tool-calling loop is complex (max rounds, multiple branches, delta accumulation in `streaming_deltas.py`). Streaming deduplication (thinking/chunk buffering) adds nesting.
   - **Improvements**:
     - Reduce `MAX_TOOL_ROUNDS` or make it configurable; add early exit if no progress.
     - Simplify buffering: Handle thinking/chunk merging in a single pass rather than separate buffers.
     - Make `accumulate_delta` optional or inline if seldom used.
     - Unified stream entry: Ensure all paths (tools, simple) use the same async helper (`run_stream_completion_async`), reducing duplication.
     - Estimated Impact: Halves tool-calling code, easier debugging.

#### 6. **Configuration Presets and UI Grouping (Low-Medium Priority)**
   - **Issue**: Settings dialog has 12 fields, no presets (as noted in AGENTS.md).
   - **Improvements**:
     - Add a dropdown for presets (load/save JSON configs). Group fields into tabs/categories (e.g., "API", "Chat").
     - Cache loaded presets to avoid re-reading files.
     - Estimated Impact: Improves usability, addressed "want to do next".

#### 7. **Add Unit Tests and Documentation (Ongoing)**
   - **Issue**: Limited tests (only `tests/` directory mentioned).
   - **Improvements**: Add mock tests for API requests, streaming. Document key functions with docstrings (many lack them).
   - Estimated Impact: Prevents regressions, aids maintenance.

#### 8. **Performance and Memory Optimizations (Low Priority)**
   - Cap document context lengths strictly. Avoid large queues (flush buffers more often).
   - Profile for memory leaks in long chat sessions.

#### Overall Assessment
- **Strengths**: Streaming architecture is solid (Python queue + main-thread drain). Dialog system works well. Core logic is separated into `core/`.
- **Weaknesses**: Over-logging, UI dispatching, monoliths. Code has AI-generated redundancy (e.g., repeated try-excepts).
- **Next Steps**: Start with logging/consolidated UI dispatch for quick wins. If switching to Agent mode, begin refactoring large files.

This plan focuses on simplicity and robustness without removing functionality. Let me know if you'd like code snippets or deeper dives into specific files!