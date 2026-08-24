# Chatbot / sidebar

Root invariants still apply (`self.ctx`, pure FSM in `service.next_state`,
`StreamQueueKind`, stream-on-worker / drain-on-UI). This file is only
the area gotchas.

## Entry points

- Sidebar factory / panel / document resolve: `panel_factory.py`, `panel.py`
- Tool loop / chat FSM: `tool_loop.py`, `tool_loop_state.py`
- Smol / librarian ReAct (separate runtime; shares `LlmClient`): `smol_agent.py`
- Dialogs / settings: `dialogs.py`, `dialog_views.py`, `settings_dialog.py`
- Memory (experimental): `memory.py` — `MEMORY_GUIDANCE` is in `plugin/framework/prompts.py`
- Librarian is a **sidebar mode** (last in the dropdown). Do **not** gate `_do_send` on missing `USER.md`. Default selection uses `chatbot.librarian_invoked` (first open only), not `USER.md`. History is a global `ChatSession` (`LIBRARIAN_HISTORY_SESSION_ID`), not per document.

## File ownership (existing modules; no new panel/dialog/session files)

- `panel_factory.py` — UNO `XUIElementFactory` / XDL load / control wiring / listener attach. Resolves the document from the frame (`get_document_from_frame`). Does **not** import or call `get_document_context_for_chat`.
- `panel.py` — `ChatSession` + button listeners. `ChatSession.refresh_document_context` rebuilds system prompt + `[DOCUMENT CONTENT]` when the sidebar switches to Chat (dropdown / `switch_to_document_mode`).
- `send_handlers.py` / `tool_loop.py` — mixins on `SendButtonListener`. Build document context on send / mid-loop refresh. Stay mixins (no `session.py` / `send.py`).
- `dialogs.py` — shared XDL / msgbox / checkbox / translate kit. LibrePy Settings and Run Python Script import this.
- `dialog_views.py` — WriterAgent Settings pages, provider buttons, venv probe UI. `input_box` (Edit/Extend selection) stays here; it is not a generic XDL helper.

## Legal imports

- factory → `panel` (session + listeners), `dialogs` (control helpers), `get_document_from_frame`
- factory must **not** import `get_document_context_for_chat`
- views → `dialogs` helpers; views must **not** import `tool_loop`
- send / tool loop → `get_document_context_for_chat` (still in `document_helpers` unless that builder has moved); must **not** import `panel_factory`

Topic docs: [docs/chat-sidebar-implementation.md](../../docs/chat-sidebar-implementation.md),
[docs/chat-smol-tool-architecture.md](../../docs/chat-smol-tool-architecture.md),
[docs/chat-llm-hacks.md](../../docs/chat-llm-hacks.md),
[docs/framework-streaming-and-threading.md](../../docs/framework-streaming-and-threading.md),
[docs/framework-uno-dialogs.md](../../docs/framework-uno-dialogs.md).

## Sharp edges

- Resolve the document from the **frame only** (`frame.getController().getModel()` in `panel`).
- For Stop / cancel, use **`resolve_stop_checker()`** — not a panel boolean alone.
- Load XDL with `DialogProvider` and the extension `base_url` (see `dialogs` module doc). Settings UI is in `dialog_views`.
- Do **not** merge smol/librarian with the main chat FSM. Smol must use `WriterAgentSmolModel` → `LlmClient.request_with_tools` — no second HTTP client.
- In tests, resolve tools with `plugin.main.get_tools().get("tool_name")`.
