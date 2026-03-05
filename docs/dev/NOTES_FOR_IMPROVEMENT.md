# Notes for Improvement & Cleanup

During the review of the codebase to update `AGENTS.md` to reflect the `localwriter` -> `localwriter2` refactor, a few areas for potential improvement or structural cleanup of the Python code were identified:

1. **The `plugin/modules/chatbot/panel.py` File:**
   This file is quite large (~1100 lines) and seems to handle a lot of UI logic, event listening, stream draining, and session state. Consider breaking this down further:
   - Extract the streaming/draining logic into a separate file (e.g., `plugin/modules/chatbot/streaming.py`).
   - Extract the `SendButtonListener`, `StopButtonListener`, etc., into a `listeners.py` file.
   - Keep `panel.py` focused purely on the `ChatSession` state and UI component wiring.

2. **`plugin/modules/chatbot/panel_factory.py`:**
   Also quite large (~670 lines). Similar to `panel.py`, the `ChatPanelFactory` could be split to handle XDL creation separately from the UNO `XSidebarPanel` interface implementation.

3. **Options Handling:**
   `plugin/options_handler.py` is nearly 900 lines long. With the new `ConfigSchema` and `ServiceRegistry` in `plugin/framework/`, it might be possible to make the settings dialog more declarative or break out the tab logic (e.g., Chat Settings vs Image Settings) into separate handler classes instead of one giant file.

4. **Tool Registration:**
   The `ToolRegistry` is great, but currently, some modules have very large tool files (e.g., `plugin/modules/ai/tools.py` at ~280 lines, `plugin/modules/writer/ops.py` at ~124 lines, `plugin/modules/writer/content.py` at ~1000 lines).
   - `plugin/modules/writer/content.py` could be split by concern (e.g., one file for markdown generation, one for content application, one for structural updates).

5. **Web Cache DB:**
   The SQLite cache for web research is currently created in the user config dir directly from the tool code. It might be better to have a central `CacheService` in `plugin/modules/core/services/` that handles file paths and locks, which the web search tool simply consumes via dependency injection.
