# Chatbot review — simplify and harden

Review of `plugin/chatbot` (about 27,000 lines, six read-only passes; 2026-10-02). The numbered findings below are implemented in `plugin/chatbot/`, `plugin/ppt_master/`, and `extension/Dialogs/`; one starter-button cleanup remains in `## Open`.

The tree is in good structural shape. The pure tool-loop FSM, frame-only document lookup, `StreamQueueKind` draining, compaction v1, and the librarian/smol split stay as they are. The fixes below align the screen, the session, and the next request, and ensure paths stop cleanly when the user cancels.

Coverage:

- Send / FSM: `tool_loop_state.py`, `tool_loop_actions.py`, `tool_loop.py`, `send_state.py`, `send_handlers.py`, `sticky_reply.py`, `chat_sidebar_mode.py`, `panel.py`
- Panel shell: `panel_factory.py`, `panel_wiring.py`, `panel_resize.py`, `sidebar_state.py`, `sidebar_test_hooks.py`, `dialogs.py`
- Settings: `dialog_views.py`, `settings_dialog.py`, `settings_fields.py`, `settings_tab_order.py`, `config_ui_helpers.py`, `module_config_dialog.py`, `quick_setup.py`, `eval_dashboard_ui.py`, `hamburger_menu.py`
- Transcript: `rich_text.py`, `rich_text_control.py`, `rich_text_paste.py`, `slash_commands.py`, `slash_popup.py`, `selection.py`, `external_editor.py`
- Research and history: `web_research.py`, `web_research_cache.py`, `web_research_chat.py`, `web_research_deep.py`, `research_cache_fluff.py`, `deep_research_session.py`, `compaction.py`, `history_db.py`
- Modes and audio: `smol_agent.py`, `smol_examples.py`, `librarian.py`, `memory.py`, `brainstorming.py`, `writing.py`, `skills.py`, `agent_manual.py`, `ppt_master.py`, `audio_recorder.py`, `audio_recorder_state.py`, `record_gesture.py`, `grammar_status.py`, `bug_report.py`, `extension_update_check.py`

Constraints for any follow-up (from `plugin/chatbot/AGENTS.md` and the root `AGENTS.md`):

- Keep `next_state` pure. No UNO and no I/O inside it.
- `send_handlers.py` and `tool_loop.py` stay mixins on `SendButtonListener`. Do not add `session.py` or `send.py`.
- Do not merge smol or librarian into the main chat FSM. No second HTTP client. Smol stays on `WriterAgentSmolModel` → `LlmClient.request_with_tools`.
- Send and the tool loop refresh through `ChatSession.refresh_document_context`. They must not import `get_document_context_for_chat` or `panel_factory`.
- The factory resolves the document from the frame only and must not import `get_document_context_for_chat`.
- Stop uses `SendCancellation` (spawn-time `host._send_cancellation` closed over in workers). Queue items start with a `StreamQueueKind` enum member. No raw threads.
- Do not split `panel.py` or `dialog_views.py` just because they are long. Prefer one existing function over a new module.

## Implemented fixes

### 1. Stop and Clear match what the user sees

- **Partial-text persistence on stop:** `note_stop_partial` / `take_stop_partial` and `close_stopped` in `plugin/chatbot/tool_loop_actions.py` preserve streamed assistant text on stop and close any open tool-call ids so `sanitize_tool_pairs` keeps finished tool bodies.
- **Clear vs. queued chunks:** `_append_response` (`plugin/chatbot/panel.py:741-751`) gates writes on `running_turn(self)` existing and `turn.session is session`, so queued chunks do not paint back onto a cleared transcript. With no live turn, `_append_response` returns early (idle errors go through `_start_local_error_turn`, per `docs/chat/sidebar-implementation.md`).
- **`SEND_COMPLETED` `has_text` sync:** After both drain `finally` blocks, `TEXT_UPDATED` is dispatched from the live query control so text typed during streaming is not lost.
- **`ChatSession.clear()` reset:** `ChatSession.clear()` (`plugin/chatbot/chat_session.py:233-240`) resets `compaction`, `active_specialized_domain`, and `python_tool_domain` alongside messages.
- **Disposed-document handling:** `tool_loop_actions.py` checks `is_disposal` and queues `StreamQueueKind.ERROR` so disposal ends the loop instead of turning into a normal tool error.
- **Drain-side persistence and error banners:** Worker answers ride `STREAM_DONE` and persist on the drain, API-error banners persist via `persist_assistant_on_turn` (unless `on_error` handles overflow/fallback), agent user lines append after backend checks, and web-research approval setup returns an error payload instead of failing open.
- **Why / do not:** Do not stop forcing `has_text=False` inside the FSM alone; the live query control is the source of truth (`panel.py:1060`).

### 2. Mode changes during a send stay on the turn session

- `panel_voice.py`, `tool_loop.py`, and `tool_loop_actions.py` target `session_for_turn` for TTS, document refresh, and domain updates, binding `bind_turn_session` before `pump_ui_idle`, while `_append_response` checks `running_turn(self)` and `turn.session is session`.
- `ChatModeListener.on_item_state_changed` (`plugin/chatbot/panel_factory.py`) ignores combo changes while `send.is_busy`.
- **Why / do not:** Leave `apply_mode` itself alone because the librarian handoff calls it while the send is still busy. `set_session` has one production caller (`_apply_sidebar_mode`).

### 3. Deep Research Reject stops the run

- `plugin/chatbot/web_research.py` returns `USER_STOPPED` on Reject and passes any Change query override into both the preview and `run_deep_research`.
- **Why / do not:** Reject must still return `USER_STOPPED` so the multi-minute deep loop cannot run after the sidebar goes idle or cache a normal result.

### 4. Failed page fetches are not marked visited

- `_VisitWebpageDedupTool.forward` (`plugin/chatbot/web_research.py`) records the URL in `visited_urls` only after a non-error return, under the lock.
- **Why / do not:** Keep the lock around the check-and-add so failed CDP/HTTP fetches remain retryable by later sub-queries.

### 5. Non-English research skips legacy English cache keys

- `lookup_research_cache` (`plugin/chatbot/web_research_cache.py`) tries the bare key only when `snowball_lang == "english"`.
- `resolve_research_locale` re-raises `is_disposal`.
- `store_research_cache_embeddings` inserts only while the parent `web_cache` row still exists in the same transaction.
- **Why / do not:** Keep the English bare-then-prefixed order (`tests/chatbot/test_web_research_cache.py` and `docs/chat/search.md` lock it). `_web_cache_get` intentionally touches `created_at`, so stale non-English rows must not match bare English keys.

### 6. PPT-Master follows the turn document and honours Stop

- `execute_ppt_master_turn` passes the frame URL as IPC `session_id` into `host_rpc.py` (`document_for_script_session`), and `plugin/ppt_master/venv/runner.py`, `ipc.py`, and `host_rpc.py` return `USER_STOPPED` when the host reports Stop.
- **Why / do not:** Leave this runtime out of the chat FSM; librarian, brainstorming, and writing already stop via `SmolAgentExecutor`.

### 7. Impress agent-backend turns get the Draw manual

- `_app` in `plugin/chatbot/agent_manual.py` maps `"impress"` to `"draw"`.
- **Why / do not:** Do not classify the document on the worker, and do not add a second topic list.

## Settings and panel shell

### 8. Settings open avoids UI-thread model fetches

- `dialog_views.py` passes `skip_remote_fetch=True` on the initial combo fill, uses `force_catalog_refresh` (snapshotting generation and `tts_model_id`), syncs the API key only when the resolved URL changes and the field still holds the prior key or is empty, and points the Hugging Face starter button at `https://router.huggingface.co/v1`.
- **Why / do not:** A warm memo already applies from cache with no HTTP; starter buttons still use a hand-written `starters` tuple in `dialog_views.py:298-303` (tracked in `## Open` to build from `ENDPOINT_PRESETS`).

### 9. Vision OK and Eval dialog lifecycle

- `ModuleConfigDialog._apply` (`plugin/chatbot/module_config_dialog.py`) shows a message box on error and keeps the dialog open, and reads controls with `get_optional`.
- `extension/Dialogs/EvalDialog.xdl` marks `endpoint` `dlg:readonly="true"`, and `eval_dashboard_ui.py` guards callbacks after close.
- **Why / do not:** `input_box` skips `dispose()` after Esc because dispose after Esc/close segfaults on some Linux builds (`docs/framework/uno-dialogs.md`); do not change the message-box helpers from this.

### 10. Panel create, button widths, and image-model combo

- `panel_factory.py` clears `self.toolpanel = None` if `wire_chatpanel_controls` raises.
- `_pin_button_width` (`plugin/chatbot/panel_wiring.py:74`) measures `_(lab)` across translated labels.
- `panel_factory.py` wires `image_model_selector` with `_wire_selector_sync_listeners(..., commit_on_focus_lost=True)` (`panel_factory.py:406-407`), so typing persists `image_model` on focus lost rather than per keystroke (per `sidebar-implementation.md`).
- `dialogs.py` uses `BaseActionListener` and `try` / `finally: dlg.dispose()` consistently.
- **Why / do not:** Leave the `self.toolpanel` assignment before `wire_chatpanel_controls` because wiring reads `self.toolpanel` for the resize parent. The width pin exists to stop the Record/Send `windowResized` growth loop.

## Transcript and history

### 11. Rich-text copy and slash popup

- `get_control_text_length` (`plugin/chatbot/rich_text_control.py`), `_plain_append_messages`, and direct hidden-doc copy handle formatted rows without clipboard transfer; `build_message_html` and `SlashPopupController._attach_item_listener` are removed.
- **Why / do not:** Do not wire an item listener on the slash popup; item listeners deadlock there.

### 12. JSON history does not fork away from SQLite

- `get_chat_history` (`plugin/chatbot/history_db.py:345-353`) raises on `sqlite3.Error` when `writeragent_history.db` already exists (`connect()` waits on the lock; there is no retry loop). `JSONHistory.add_message` propagates save errors, and `_json_history_filename` sanitizes JSON filenames.
- **Why / do not:** Do not add a second store, and do not persist tool results from `history_db` (tool-only assistant turns are skipped on purpose; compaction is not the place to "fix" that).

## Test hooks

### 13. The debug harness targets the right deck

- Shipped in `plugin/chatbot/sidebar_test_hooks.py`: multi-panel disambiguation returns `None` when no frame or `_live_panel_uid` matches, `uno_same` stays on the main thread, adopt/read skip `_panel_teardown`, and `clear_sidebar_chat` / `press_record` / `press_stop_rec` drive the controls directly.

## Small collapses

Implemented across `plugin/chatbot/` and `scripts/`:

- Librarian session creation reuses `greeting_for_sidebar_mode(CHAT_MODE_LIBRARIAN, model)` (`panel_factory.py`).
- Settings, Edit/Extend `input_box`, and `EvalDialog` load and translate through `load_writeragent_dialog` (`dialog_views.py`, `eval_dashboard_ui.py`).
- `scripts/manifest_registry.py` shares `_build_inline_maps` with `settings_tab_order.py`.
- `apply_field_specs_result` (`plugin/chatbot/settings_fields.py`) uses `spec.get("config_key") or name.replace("__", ".")`, preventing divergence if a module name contains a dot.
- `module_config_dialog.py` walks `btn_tab_*` in control order instead of a duplicate `_TAB_PAGE_MAP`.
- `resolve_research_stem_language` (`web_research_cache.py`), `chatbot.deep_research_depth`, and `approval_required` (`web_research_chat.py`) are removed.
- `_cache_key_inputs` (`web_research.py`) applies the fluff filter once.
- `get_optional` (`dialogs.py:638`) re-raises when `"DisposedException"` is in the exception type name (a bare `RuntimeException` stays `"missing"`).
- `docs/chat/sidebar-implementation.md` / factory alignment: do not change the factory to match the doc.
- `_compose_issue_body` (`plugin/chatbot/bug_report.py`) reserves the environment header and fits the exception tail within the encoded URL budget.
- `dialog_views.py:312` calls `setup_module_tabs` directly (`_setup_module_tabs` is removed).

### Slash popup

`ENABLE_SLASH` is true in product builds. `QueryKeyListener` always calls `handle_key`; when the popup consumes Enter, Send does not run. These contracts are in the code so the overlay does not revive the headed bugs:

- Frame and toolkit `XKeyHandler`s call `_on_document_key` → `handle_key(..., from_overlay=False)` (nav only). The listbox listener calls `_on_list_key` → `from_overlay=True`. `from_overlay=True` appends printable characters to Ask and returns true, so it must not be used on handlers that also see document keys.
- Accept and Esc post ``run_slash_command(cmd, host, …)`` / ``hide`` with ``main.post`` (``_defer_to_next_turn``). The key or mouse callback returns before ``hide()`` disposes the toolkit window. There is no inline fallback: that would dispose on the listener stack. Ask textChanged (``on_query_text``) posts ``hide`` and ``_show_matches`` through the same helper. A ``TimeoutError`` from ``main.post`` is logged and does not run that work on the listener stack.
- `_show_matches` calls `reposition()` once, before `setVisible`. It does not reposition after the overlay is mapped (`getPosSize` on Ask after the overlay exists deadlocks VCL — `plugin/chatbot/panel.py`). The host is a `floatingwindow` with WindowAttributes 0: a borderless VCL overlap window in the document frame, not a system window. A `"window"` + TOP host was a `WorkWindow`, which the toolkit gives a WM title bar on qt6/kf6. Create measures the dialog-to-frame offset once (`_overlay_offset`, accessible screen locations of the host at 0,0 and of Ask), and `reposition` adds it to the dialog-local rectangle. Height comes from the listbox `getMinimumSize(0, rows)`.
- The toolkit `XKeyHandler` is added when the controller is created (flag on) and removed only by `dispose()` from panel `disposing`. The sidebar `FocusManager` is also an Application key listener and consumes Esc in panel content (focus jumps to the panel title). Listeners run in registration order, so ours must be registered at panel creation, before the deck connects the panel's key handler. Do not attach it per show or detach it in `hide()`.
- The `[mock]` commands are not in `SLASH_COMMANDS`. Only `enable_mock_slash_commands()` from the debug-only sidebar test hooks registers them.
- `/stop` runs `StopButtonListener` (speech, inline web approval, hands-free, `STOP_CLICKED`) and does not clear Ask. `/help`, `/clear`, and mocks clear Ask.
- `QueryTextListener` still returns before `TEXT_UPDATED` for a slash draft. `hide()` dispatches `TEXT_UPDATED` from the live Ask text after the popup is closed, so Esc on a draft such as `/he` re-enables Send. Recreate passes `restore_send=False` so that dispatch does not run while the next overlay is about to map.

## Leave alone

Verified and not a fix:

- Both `next_state` functions stay pure. Stop cannot emit a spawn (`stop_effects_exclude_spawns`, `stopped_effects_exclude_tool_spawns`). Side effects stay in the interpreters.
- Stop uses `SendCancellation` created in `StartSendEffect`. `disposing` cancels that same scope and sets `_panel_teardown` so the `finally` does not write status, arm the mic, or clear `_send_cancellation`. MCP `mcp:request` / `mcp:result` unsubscribe from the services bus saved as `_mcp_event_bus`. `grammar:status` stays on `get_event_bus()`. MCP result UI, Ask text changes, and audio auto-stop callbacks no-op once teardown has started or `ctx` is `None`. Approval chrome is restored from `_approval_ui_backup`.
- Queue items are `StreamQueueKind`. LLM and async tools use `run_in_background(..., dedicated=True)`. The UI thread drains. Sync tools run on the drain on purpose. The sync `execute_fn` path checks `cancel.is_cancelled()`. A long sync tool still blocks Stop until it returns. That is the shape of sync work on the UI thread. Do not add a thread for it.
- Chat send and a successful mutating tool refresh through `ChatSession.refresh_document_context`. This area does not import `get_document_context_for_chat` or call `uno.getComponentContext()`. The document comes from the frame.
- Tool-loop history writes go through `persist_*_on_turn`. `compact_session` aborts if `session.messages` was replaced. `messages_for_llm` always runs `sanitize_tool_pairs`.
- `SEND_CLICKED` while busy is ignored. `STOP_CLICKED` keeps `is_busy` until completion. Image and agent Stop are not overwritten to Ready when status is Error or Stopped.
- The document comes only from the frame (`get_document_from_frame`). Create hops to the main thread before `get_extension_url`. XDL load uses `ContainerWindowProvider` plus the extension `base_url`. Deck close goes through the resize listener's `disposing` → `release_live_sidebar`, and `unregister_live_panel` will not drop a newer window that reused the uid.
- `getHeightForWidth` sizes only the AWT window. `windowResized` relayouts and does not snap width back to the last deck hint. `_in_refresh_controls` covers model, image, mode, and voice listeners. Config subscribe is `weak=True`.
- Settings OK skips a missing control instead of writing `""`. Placeholders are stripped before `text_model`, `image_model`, `audio.stt_model`, and `audio.tts_model` are stored. Speech voice is not written until OK. STT uses `set_control_enabled`, not `setVisible`. One `set_configs` batch, including the LRU lists for keys that changed (not a follow-up write per list). A sidebar model change writes `text_model` and `model_lru` in one `set_configs`. Unchanged values do not emit `config:changed`. Checkboxes go through `populate_settings_control` / `read_settings_control`. `config_dialog` modules (vision) are left out of the main field list.
- Research stays plain text. `WEB_RESEARCH_PLAIN_TEXT_FORMAT` forbids HTML. Workers build `LlmClient` plus `WriterAgentSmolModel`. Deep vs shallow cache keys do not cross-match. Stop during planning, extraction, or synthesis returns `USER_STOPPED` and is not cached. `history_text` for sub-agents skips `system` and `tool`, so a stale `[DOCUMENT CONTENT]` row is not copied into research.
- Compaction v1 keeps `messages[0]`, summarizes `messages[prev_kept:new_cut]` with `prev_kept >= 1`, strips a `[DOCUMENT CONTENT]` span from the summary slice only, snaps the cut onto a user or assistant message, and aborts if `session.messages` was replaced. `chat_compaction_enabled` is the only kill switch. Do not add an OpenClaw-style wrapper layer. The 200-character tool stub in the summary input is the written policy in `docs/chat/compaction-dev-plan.md`.
- Librarian history stays on `LIBRARIAN_HISTORY_SESSION_ID`. Web research is `session_id + "_web"`. Do not gate `_do_send` on a missing `USER.md`. Default selection uses `chatbot.librarian_invoked`.
- `memory_upsert` is tier `core`. Writes hold `_MEMORY_WRITE_LOCK`, use `mkstemp` plus `os.replace`, refuse invalid JSON and non-objects, and refuse a dotted key that would replace a string with `{}`. `MEMORY_GUIDANCE` stays in `plugin/framework/prompts.py`.
- One guidance map. `MANUAL_SECTIONS` / `_TOPIC_SUMMARY` are the writer topics. Calc, draw, and generic share `_GENERIC_SECTIONS`. `GetGuidance` calls into `agent_manual.py`.
- Brainstorming and writing already share `run_subagent_tool` and `run_smol_side_turn`. Instruction order is mode text, then `WRITER_APPLY_DOCUMENT_HTML_RULES`, then `get_chat_response_format_instructions`. A third copy of `collect_*_tools` would be the time to share one collector. Writing's missing `target` coerce is not reachable through `execute_safe`.
- Audio does not start raw threads. The stdout monitor is `run_in_background(..., dedicated=True)`. WAV paths use `mkstemp`. `STOP_REC` is a no-op unless `is_recording` and emits one stop plus one send. The update check fetches only `raw.githubusercontent.com/KeithCu/writeragent/...` on a dedicated thread and refuses to notify when the identifier does not match.
- With the slash flag off, Ask Enter still sends. Failed single-message rich copy rolls back the separator and cell-link spans. Copy walks the hidden Writer and does not paste the system clipboard into the open document. `external_editor.py` does not use `mktemp`. `EDITOR` / `VISUAL` are `shlex.split` and passed as an argv list.
- Chat system rows are write-once on disk. `refresh_document_context` updates `messages[0]` in memory and does not write history. `history_db` has no update API. The on-disk system row stays the first snapshot until clear. That is not a compaction bug.
- `_web_cache_get` uses one `created_at` for both LRU eviction and validity. A row that is hit again before `web_cache_validity_days` never expires. The touch is intentional. A second timestamp would be a cache-policy change.

## Cross-slice notes

These live outside `plugin/chatbot` and show up in the findings above.

- `plugin/framework/async_stream.py` `_finish_on_stop` / `_apply_queued_display`: the stop tail is applied after Clear, and `TOOL_DONE` in that queue is dropped before `_handle_stream_stopped`.
- `plugin/framework/client/llm_client.py` `request_with_tools`: stop still returns accumulated content. The chatbot worker ignores it. Partial `tool_calls` must not be executed.
- `plugin/framework/errors.py`: `is_disposal` is the strict disposal predicate (`DocumentDisposedError` and `DisposedException` only). UI lifecycle uses `suppress_disposed` / `_looks_like_teardown` for broader bridge teardown.
- `plugin/scripting/host_rpc.py` and `plugin/scripting/venv_worker.py`: PPT document binding and cancel. See item 6.
- `plugin/framework/config.py` `validate()` already runs `endpoint_from_selector_text`. Do not add a second normalizer in the settings dialog.

## Open

- `[eng S]` Build the Settings starter buttons from `ENDPOINT_PRESETS` (one URL list). (§ 8 Settings open avoids UI-thread model fetches)
