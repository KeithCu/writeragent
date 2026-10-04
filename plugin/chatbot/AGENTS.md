# Chatbot / sidebar

Root invariants still apply (`self.ctx`, pure FSM in `tool_loop_state.next_state`,
`StreamQueueKind`, stream-on-worker / drain-on-UI). This file is only
the area gotchas.

## Entry points

- Sidebar factory / panel / document resolve: `panel_factory.py`, `panel.py`
- Tool loop / chat FSM: `tool_loop.py`, `tool_loop_state.py`
- Smol / librarian ReAct (separate runtime; shares `LlmClient`): `smol_agent.py`
- Dialogs / settings: `dialogs.py`, `dialog_views.py`, `settings_dialog.py`
- Product UI owned here (not framework): `eval_dashboard_ui.py`, `bug_report.py`, `agent_manual.py`
- Memory (shipped): `memory.py` (`upsert_memory`, tier core, `USER.md`) — `MEMORY_GUIDANCE` is in `plugin/framework/prompts.py`
- Librarian is a **sidebar mode** (last in the dropdown). Do **not** gate `_do_send` on missing `USER.md`. Default selection uses `chatbot.librarian_invoked` (first open only), not `USER.md`. History is a global `ChatSession` (`LIBRARIAN_HISTORY_SESSION_ID`), not per document.

## File ownership (existing modules; no new panel/dialog/session files)

- `panel_factory.py` — UNO `XUIElementFactory` / XDL load / control wiring / listener attach. Resolves the document from the frame (`get_document_from_frame`). Does **not** import or call `get_document_context_for_chat`.
- `panel.py` — `ChatSession` + button listeners. `ChatSession.refresh_document_context` rebuilds system prompt + `[DOCUMENT CONTENT]` on Chat-mode switch, each send, and mid-loop refresh after a mutating tool.
- `send_handlers.py` / `tool_loop.py` — mixins on `SendButtonListener`. Route send / mid-loop refresh through `ChatSession.refresh_document_context`. Stay mixins (no `session.py` / `send.py`).
- `dialogs.py` — shared XDL / msgbox / checkbox / translate kit. LibrePy Settings and Run Python Script import this.
- `dialog_views.py` — WriterAgent Settings pages, provider buttons, venv probe UI. `input_box` (Edit/Extend selection) stays here; it is not a generic XDL helper.
- `eval_dashboard_ui.py` — prompt-optimization eval dashboard XDL (`show_eval_dashboard`).
- `bug_report.py` — GitHub issue URL builder / browser launcher (`msgbox_with_report`, Report bug menu). LibrePy bundles this.
- `agent_manual.py` — `get_guidance` topic map + `full_manual_for_model` (sidebar hybrid prompt and MCP).

## Legal imports

- factory → `panel` (session + listeners), `dialogs` (control helpers), `get_document_from_frame`
- factory must **not** import `get_document_context_for_chat`
- views → `dialogs` helpers; views must **not** import `tool_loop`
- send / tool loop → `ChatSession.refresh_document_context`; must **not** import `get_document_context_for_chat` or `panel_factory`

Topic docs: [docs/chat/sidebar-implementation.md](../../docs/chat/sidebar-implementation.md),
[docs/chat/smol-tool-architecture.md](../../docs/chat/smol-tool-architecture.md),
[docs/chat/llm-hacks.md](../../docs/chat/llm-hacks.md),
[docs/framework/streaming-and-threading.md](../../docs/framework/streaming-and-threading.md),
[docs/framework/uno-dialogs.md](../../docs/framework/uno-dialogs.md).

## Sharp edges

- Resolve the document from the **frame only** (`frame.getController().getModel()` in `panel`).
- Each open frame has one `FrameSession` (`plugin/framework/frame_session.py`), created when the sidebar factory receives the frame and destroyed when the frame closes. It owns that frame's listeners, focus pin, and sidebar. The panel is constructed with the session's document id. Clicks, stream restore, and focus restore close over that session. Do not use `getCurrentComponent()`, `get_active_document()`, or `panels[0]` when the action already has a frame.
- For Stop / cancel, use **`resolve_stop_checker()`** — not a panel boolean alone. Workers capture it on the send thread at spawn with **`capture_send_stop`** (the scope object and that checker). Do not call `resolve_stop_checker()` or read `_send_cancellation` inside the worker body.
- MCP `mcp:request` / `mcp:result` subscribe on `get_tools()._services.events`. `disposing` unsubscribes that saved bus (`_mcp_event_bus`). `grammar:status` stays on `global_event_bus`. Do not assume the two buses are the same object. After `_panel_teardown` or `ctx is None`, do not post MCP or audio UI.
- Mode-UI wiring can fail. `_wireControls` must still hand `_wire_buttons` a `SidebarModeFlags`, never `None`. A `None` used to raise inside the Send/Stop `try`, and that `except` left Send and Stop unwired.
- Load XDL with `DialogProvider` and the extension `base_url` (see `dialogs` module doc). Settings UI is in `dialog_views`.
- Do **not** merge smol/librarian with the main chat FSM. Smol must use `WriterAgentSmolModel` → `LlmClient.request_with_tools` — no second HTTP client.
- In tests, resolve tools with `plugin.main.get_tools().get("tool_name")`.
