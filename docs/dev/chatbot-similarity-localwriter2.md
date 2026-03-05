# How similar is our chatbot code to localwriter2?

With `localwriter2/` in this repo, here is a direct comparison (for Phase 4 migration).

## Same (conceptually)

- **Path:** Both use `plugin/modules/chatbot/`; localwriter2 uses `panel_factory.py` for the UNO factory + panel wiring. Moving with `git mv` gives the same path.
- **UNO surface:** Both have `ChatPanelFactory`, `ChatPanelElement`, `ChatToolPanel`; same service name `org.extension.localwriter.ChatPanelFactory`.
- **Threading:** Both use the fixed pattern: Send runs on the main thread, worker thread only for network, queue + main-thread drain with `processEventsToIdle()` (ours: `run_stream_drain_loop` / `run_stream_completion_async`; his: `chat_event_stream` + pump).
- **Session:** Both have `ChatSession` (messages, `update_document_context`). His adds `maybe_compress()` (history compression) and `clear()`.

## Different (we keep ours for Phase 4)

| Aspect | Ours (`plugin/chat_panel.py`) | localwriter2 |
|--------|--------------------------------|--------------|
| **Panel UI** | **XDL**: ContainerWindowProvider + `ChatPanelDialog.xdl` | **Programmatic**: `panel_layout.create_panel_window()` + `add_control()` (no XDL) |
| **Layout** | **Split**: `panel_factory.py` (UNO + XDL wiring), `panel.py` (ChatSession, SendButtonListener, StopButtonListener, ClearButtonListener) | **Split**: `panel_factory.py` (UNO + wiring), `panel.py` (ChatSession, ChatToolAdapter, SendButtonListener), `handler.py` (REST API), `streaming.py` (chat_event_stream) |
| **Tools** | Inline: `execute_tool` / `execute_calc_tool` / `execute_draw_tool` from core; doc-type branch in `_do_send` | **ChatToolAdapter**: `tool_registry.get_openai_schemas(doc_type)`, `adapter.execute_tool()`; optional broker for two-tier tools |
| **Config** | `get_config(ctx, key)` / `localwriter.json` | Module config: `services.config.proxy_for("chatbot")`, keys like `system_prompt`, `tool_broker`, `query_history` |
| **Prompts** | `get_chat_system_prompt_for_document(model, additional_instructions)` from framework.constants | `get_system_prompt(doc_type, extra, broker=)` from `plugin.modules.chatbot.constants` |
| **Extras** | Web search checkbox, direct image, model/prompt/image selectors, config listeners, watchdog, undo grouping | Spinner (Braille dots in query label), query history (up/down), ChatSettingsPanel in same factory, REST handler |

## Summary

- **Same:** File location (after move), UNO component name, threading model, high-level flow (session, send, stream, tools).
- **Different:** We use XDL and a single file; he uses programmatic layout and splits session/adapter/listener into `panel.py`. We have more UI features (selectors, web search, direct image); he has spinner, query history, and a REST chat API.
- **Phase 4:** Use `git mv` so our implementation lives at `plugin/modules/chatbot/panel_factory.py`. Keep our XDL, config, and features.

To diff after the move:

```bash
diff plugin/modules/chatbot/panel_factory.py localwriter2/plugin/modules/chatbot/panel_factory.py
```

## Optional later: split like localwriter2

Extract `ChatSession`, `SendButtonListener` (and optionally a thin adapter) into `plugin/modules/chatbot/panel.py`, and have `panel_factory.py` import them. Reduces per-file size and aligns structure with `localwriter2/plugin/modules/chatbot/`.
