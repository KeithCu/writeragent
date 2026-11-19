# Chatbot review — simplify and harden

Review of `plugin/chatbot` (about 27,000 lines, six read-only passes; original date: 2026-10-02). Sections and bullets marked **Shipped** have landed. Unmarked findings were not re-checked and may still be open. `## Open` lists the subset converted so far.

The tree is in good structural shape. The pure tool-loop FSM, frame-only document lookup, `StreamQueueKind` draining, compaction v1, and the librarian/smol split should stay as they are. The problems worth fixing are places where the screen, the session, and the next request disagree, plus a few paths that keep going after the user has already said stop.

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

## Fix these first

### 1. Stop and Clear do not match what the user sees

**Shipped (`plugin/chatbot/tool_loop_actions.py` stop path):** Stopping persists the accumulated partial assistant text instead of `"No response."`, and closes any open tool-call ids with a stopped result so `sanitize_tool_pairs` keeps finished tool bodies.

Clear latches `STOP_CLICKED` and wipes the widget (`plugin/chatbot/panel.py` `ClearButtonListener.on_action_performed`, around line 1976), but the drain flushes queued chunks after that pump returns, so the reply paints back onto the empty transcript while history stays cleared. `_append_response` (around line 849) should return when the send is busy and `_turn_accepts_write` is false. That helper already exists for this race. Do not gate when the send is idle: after Clear, `_turn_messages` stays stale until the next `bind_turn_session`, and a pre-bind error line must still show.

`SEND_COMPLETED` forces `has_text=False` (`plugin/chatbot/send_state.py` around line 224). Text typed while a reply streams, and the extracted-peer send path that never clears Ask, then look empty. With recording support the button becomes Record, so the next click captures audio. After both drain `finally`s, dispatch `TEXT_UPDATED` from the live query control. Do not stop forcing `has_text=False` inside the FSM alone. If the clear did not fire the text listener, `has_text` would stay true on an empty box. The control is the source of truth.

**Shipped (`plugin/chatbot/chat_session.py`):** `ChatSession.clear()` resets `active_specialized_domain` and `python_tool_domain` along with messages.

Related, same area:

- **Shipped (`plugin/chatbot/tool_loop_actions.py`):** `execute_fn` checks `is_disposal` and ends the loop on disposal rather than returning a normal tool error payload.
- Web, librarian, brainstorm, writing-plan, PPT, and deep-research answers are still persisted from the `chatbot-send-handler` worker (`plugin/chatbot/send_handlers.py`, the `persist_assistant_on_turn` calls around the answer puts). The agent path was moved onto the drain because a worker write raced Clear. Put the answer on the existing `STREAM_DONE` payload and persist it in `on_stream_done`.
- The API-error banner is widget-only (`plugin/chatbot/tool_loop.py` `_handle_stream_error`). The user row is stored and the assistant row is not, so a retry adds a second user row with a hole. `persist_assistant_on_turn` the same banner. Skip that write when `on_error` returns true (overflow respawn or audio fallback).
- The agent user line is painted before it is in history. `spawn_effects_for_start` appends the user query, then the worker can return before `add_user_message` (disposed document, unknown backend). Append the user line in the same place as `add_user_message`, after the backend checks.
- Web-research approval setup fails open. The `except` around approval setup logs a warning and leaves `approval_cb` as `None`. `web_research.py` prompts only when both `prompt_for_web_research` and `approval_callback` are set, so a config error skips Accept/Change/Reject and the search runs. Return an error payload instead of continuing with no callback.

### 2. A mode change during a send writes the other session

`bind_turn_session` exists because `set_session` can swap `host.session` inside the drain. TTS still speaks `self.session.messages[-1]` (`plugin/chatbot/panel.py` around line 1351), and document refresh plus `set_active_domain` still use `host.session` (`plugin/chatbot/tool_loop_actions.py`, `plugin/chatbot/tool_loop.py` around line 215). Librarian handoff calls `apply_mode(CHAT)` before TTS, so speech reads the document chat's last row. (**Shipped in `plugin/chatbot/panel_factory.py`:** `ChatModeListener.on_item_state_changed` ignores user mode-combo changes while `send.is_busy`.)

Point TTS, refresh, and domain updates at `session_for_turn`. Call `bind_turn_session` once at the start of `_do_send_chat_with_tools`, before the `pump_ui_idle`, not after refresh. Leave `apply_mode` itself alone, because the librarian handoff calls it while the send is still busy. Skip refresh when `_turn_accepts_write` is false.

`set_session` has one production caller, `_apply_sidebar_mode`. `session_for_turn` is already what `_spawn_llm_worker` and `persist_assistant_on_turn` use.

### 3. Deep Research Reject and query override (Shipped)

**Shipped (`plugin/chatbot/web_research.py`):** Deep Research returns `USER_STOPPED` on Reject and passes the edited query override into both the preview and `run_deep_research`.

### 4. Visited-URL dedup on fetch errors (Shipped)

**Shipped (`plugin/chatbot/web_research.py`):** `_VisitWebpageDedupTool.forward` records a URL in the visited set only after a non-error text return, so failed fetches can be retried by later sub-queries.

### 5. Non-English research cache and embedding backfill

**Shipped (`plugin/chatbot/web_research_cache.py`):** `lookup_research_cache` skips the legacy English bare key for non-English queries and keeps the English bare-then-prefixed order (`tests/chatbot/test_web_research_cache.py` and `docs/chat/search.md`). Deep lookup uses only `deep|{lang}|{words}`.

Also: `resolve_research_locale` (`plugin/chatbot/web_research_cache.py` around line 118) treats any exception from the document, including disposal, as `("en_US", "english")`. Re-raise `is_disposal`. Leave the timeout and cancel fallbacks.

**Shipped (`plugin/chatbot/web_research_cache.py`):** `store_research_cache_embeddings` checks that the parent `web_cache` row still exists before inserting embeddings.

### 6. PPT-Master follows the focused window and ignores Stop

The sidebar records the frame document's URL as a skill-cache id (`plugin/chatbot/ppt_master.py` around line 54) and passes it inside the child payload. `execute_ppt_master_turn` calls `_execute_ipc_unlocked` without the IPC `session_id` (`plugin/scripting/venv_worker.py` around line 716). Host tool RPC then uses `get_active_document` (`plugin/scripting/host_rpc.py` around line 351). A long turn with deck B focused exports into B. Named scripts already resolve `document_for_script_session` before `get_active_document`. Pass the frame URL as the IPC `session_id` and use that same lookup in `execute_tool`.

**Shipped (`plugin/ppt_master/venv/runner.py`, `plugin/scripting/host_rpc.py`, `plugin/scripting/venv_worker.py`):** Stop is checked before the venv turn (`plugin/chatbot/ppt_master.py`), inside the child loop (`run_turn` checks `_is_user_stopped(step.error)` and `_is_user_stopped(exc)` and returns `_user_stopped_payload()` with `code="USER_STOPPED"`), and on tool-frame dispatch (`handle_tool_call_frame` checks `is_cancelled(cancel)` and replies with `code="USER_STOPPED"`, with `cancel` passed from `_dispatch_worker_response`). This runtime remains outside the chat FSM.

Librarian, brainstorming, and writing already stop: `SmolAgentExecutor` calls `interrupt()` and raises `USER_STOPPED` before the tool body.

Smaller, same area: `_parse_finished` in `plugin/ppt_master/venv/runner.py` keeps a handoff only until the first apostrophe (`re.search(r"'result': '([^']*)'", ...)`). A finished message containing `'` is cut off. `_selected_chat_model`'s docstring still says send handlers pass the model id via `ToolContext.doc`. On the live path `ctx.doc` is the UNO document.

### 7. Impress agent-backend manual (Shipped)

**Shipped (`plugin/chatbot/agent_manual.py`):** `_app` maps `"impress"` to `"draw"`, sharing the Draw topic tables (matching `get_core_directives_for_type` in `plugin/framework/prompts.py`). Sidebar `manual_read` on the main thread also folds Impress to `"draw"` via `doc_type_family_label`.

## Settings and panel shell

### 8. Settings open and endpoint presets

**Shipped (`plugin/chatbot/dialog_views.py`):** `_populate_fields` passes `skip_remote_fetch=True` on the first model combo fill (LRU plus defaults) instead of fetching synchronously on the UI thread.

The endpoint dropdown's background fetch omits the Together TTS model id (`plugin/chatbot/dialog_views.py` `itemStateChanged`, the `run_in_background` lambda). `_bg_fetch` only calls `fetch_together_tts_voices(..., model_id=...)` when that string is non-empty. The comment there says list-all does not fill the `?model=` entry the Voice combo reads. The lambda also reads `self._debounce_gen` when the worker starts, so the generation check does not see the generation that was current at click time. `force_catalog_refresh` already snapshots `gen` and `tts_model_id`. Use that same call.

`textChanged` calls `_sync_api_key` on every keystroke (`plugin/chatbot/dialog_views.py`). `_sync_api_key` always does `set_control_text(ak_ctrl, get_api_key_for_endpoint(resolved))`. Paste a new key, then fix one character of the URL, and the typed key is gone. OK then persists the restored value. Sync only when the resolved URL changes, and only if the field still holds the saved key for the previous URL (or is empty). Preset clicks should keep loading the saved key.

**Shipped (`plugin/chatbot/dialog_views.py`):** The Hugging Face preset points at `https://router.huggingface.co/v1`. Still open: `ENDPOINT_PRESETS` has no Hugging Face row and `get_provider_from_endpoint` has no HF host, so the button is the only UI that sets this URL. Build the four starter buttons from `ENDPOINT_PRESETS`; OpenRouter, Together, and NVIDIA already match.

### 9. Vision OK closes after a failed save

**Shipped (`plugin/chatbot/module_config_dialog.py`):** `_apply` logs `log.exception(...)`, shows `msgbox`, and returns early without calling `self.close()` when saving fails; `_tab_buttons_in_order`, `_wire_buttons`, `_populate_fields`, and `_extract_result` read controls with `get_optional` so a missing control name does not raise.

`EvalDialog.xdl` marks `endpoint` read-only (**shipped**). Close still disposes the dialog while the dedicated worker can post `_after_test` / `_show_summary` onto it; set a closed flag in `show()`'s `finally` and return at the start of `_paint`, `_show_summary`, and `_show_failure`.

`input_box` skips `dispose()` when `execute()` is false, because dispose after Esc or the window close segfaults on some Linux builds (`docs/framework/uno-dialogs.md`). Settings `_cleanup` and the eval dialog always dispose. Same guard as `input_box` if Cancel or the title-bar close actually crashes. Do not change the message-box helpers from this.

### 10. A failed panel create stays latched

**Shipped (`plugin/chatbot/panel_factory.py`):** `getRealInterface` clears `self.toolpanel = None` before re-raising on a failed create, so the next `getRealInterface` retries instead of returning a half-built panel.

Button widths are measured from the English strings `"Send"`, `"Record"`, `"Stop Rec"`, `"Accept"` (`plugin/chatbot/panel_wiring.py` `_measure_send_button_max_width`, around line 22). Stop and Clear use the same untranslated lists. The live label is `_(effect.send_label)`. The pin exists to stop the Record / Send `windowResized` growth loop. A longer translation still changes `getPosSize().Width` before the English pin is forced back, which is that loop, or the label is clipped. Measure `_(lab)` in `_measure_aux_button_max_width`, which already walks a label list. `_` is already imported in that file.

The image-model combo is editable (`dlg:dropdown="true"`) and only has an item listener (`plugin/chatbot/panel_factory.py` around line 766). The text-model combo gets both `ModelSyncListener` and `ModelTextSyncListener` because list selection and typing do not share one event. A typed image id never reaches `set_image_model`. The next `config:changed` refresh repaints the combo from `get_image_model()` and drops it. Add the same `addTextListener` path, still bailing out while `_in_refresh_controls` is set.

`_OkListener` / `_CancelListener` are raw `unohelper.Base, XActionListener` in several `dialogs.py` helpers. Copy buttons in the same functions use `BaseActionListener`. Those dialogs also `dispose()` after `execute()` with no `finally`. `msgbox` and `show_approval_dialog` already dispose in `finally`. One local `BaseActionListener` that calls `endDialog`, plus `try` / `finally: dlg.dispose()`, matches `msgbox`.

## Transcript and history

### 11. One bad history element drops the whole batch

get_control_text_length returns `None` on exception or missing model (`plugin/chatbot/rich_text_control.py`). Unknown length skips the formatted copy and plain-appends once.

`_plain_append_messages` writes bold `You:` / `Assistant:` prefixes, theme-appropriate role colors, and blank-line gaps between turns.

`build_message_html` is removed. `append_formatted_row` and `append_formatted_rows` use direct hidden-doc copy without clipboard transfer.

`SlashPopupController._attach_item_listener` has been removed (**shipped**; the mouse listener is used instead because item listeners deadlock).

### 12. JSON history can fork away from SQLite

Any `sqlite3.Error` in `get_chat_history` returns a `JSONHistory` for that object (`plugin/chatbot/history_db.py` around line 241). Later turns land under `writeragent_history.db.d/`, and the next successful SQLite open never reads them. Retry a lock. Do not switch an existing `writeragent_history.db` over to JSON.

`JSONHistory.add_message` logs `OSError` / `TypeError` and returns (around line 178). The turn is already in `ChatSession.messages` and is gone on the next open. `get_messages` already re-raises. Do the same on save. `SQLite3History.add_message` already lets `sqlite3.Error` propagate.

**Shipped (`plugin/chatbot/history_db.py`):** `_json_history_filename` sanitises session IDs before building JSON history filenames.

Do not add a second store. Do not persist tool results from `history_db`. Tool-only assistant turns are skipped on purpose (`ChatSession.add_assistant_message`). Reloaded history is user and assistant text. Compaction is not the place to "fix" that.

## Test hooks

### 13. The debug harness can drive the wrong deck

Shipped in `plugin/chatbot/sidebar_test_hooks.py`. Several live panels and no frame or `_live_panel_uid` match return `None` (a single live panel is still returned). `uno_same` is not called off the main thread. Adopt and read skip `_panel_teardown` and drop those listeners from the strong list. `clear_sidebar_chat` calls `clear_listener.on_action_performed` when that listener exists. `press_record` is the Record mouse press/release pair. `press_stop_rec` uses that pair, then the button action only when the label is Stop Rec.

## Small collapses

These remove a second copy of a fact. None of them need a new file.

- **Shipped:** The librarian `ChatSession` reuses `greeting_for_sidebar_mode` (`plugin/chatbot/chat_sidebar_mode.py`, `plugin/chatbot/panel_factory.py`). `LIBRARIAN_HISTORY_SESSION_ID` is the single history id.
- Settings, the Edit/Extend `input_box`, and the eval dashboard load through `load_writeragent_dialog` (`plugin/chatbot/dialog_views.py`, `plugin/chatbot/eval_dashboard_ui.py`), which takes `ctx`, tries `DialogProvider` then `DialogProvider2`, and translates. Settings still calls `translate_dialog` after populate so labels translate after combos are filled. A `None` dialog raises in Settings and `input_box`; the eval dashboard returns without `execute()`.
- `plugin/chatbot/settings_tab_order.py` rebuilds `inline_targets` / `inline_set` / `inline_map`. `scripts/manifest_registry.py` already builds that map, and `generate_settings_dialog_tabs` then copies it. Use `_build_inline_maps` in the generator and delete the copy. If the copies diverge, the tab exists and the fields land on the wrong page.
- **Shipped (`plugin/chatbot/settings_fields.py`):** `apply_field_specs_result` uses `spec.get("config_key") or key.replace("__", ".")`, matching `apply_settings_result`.
- Vision tab steps are a second page list (`plugin/chatbot/module_config_dialog.py` `_TAB_PAGE_MAP`). A new `page:` in `vision/module.yaml` gets a button and a Step, and `_setup_tabs` never listens. Walk `btn_tab_*` in control order and assign Step 1..n, which is the order the generator writes.
- **Shipped (`plugin/chatbot/web_research_cache.py`):** `resolve_research_stem_language` has been removed; callers use `resolve_research_locale`.
- Deep Research rounds are controlled only by `chatbot.deep_research_max_rounds`; the old `depth` parameter and `chatbot.deep_research_depth` config key are removed.
- **Shipped (`plugin/chatbot/web_research_chat.py`):** `approval_required` has been removed.
- `_cache_key_inputs` (`plugin/chatbot/web_research.py`) now applies the fluff filter once: one ordered-unique list, with a sorted copy for the storage key. Storage keys are sorted and embedding text keeps order, on purpose (`docs/chat/search.md`).
- `get_optional` turns a disposed window into "control missing" (`plugin/chatbot/dialogs.py` around line 1056). Re-raise `is_disposal`. Callers that must ignore teardown already have `suppress_disposed`. Keep `None` for a missing name.
- `docs/chat/sidebar-implementation.md` still says the first mode follows an empty `USER.md`. The factory uses `librarian_default_mode` / `chatbot.librarian_invoked`, which matches `plugin/chatbot/AGENTS.md`. Update that sentence. Do not change the factory to match the doc.
- **Shipped (`plugin/chatbot/bug_report.py`):** `_issue_sections` separates the environment `header` (version, LibreOffice, OS, locale, Python, endpoint, chat model) from `rest`, and `_compose_issue_body` / `_fit_section` reserve `header` first (`keep_head=True`), put the exception tail in what remains (`keep_head=False`), and append `rest` when space permits, capped against `_MAX_URL_CHARS` (8000) on the encoded URL. The template has no API-key field.
- Extend/Edit from the hamburger uses the sidebar frame (`get_document_from_frame`). The menubar still calls the handler with no frame, so it edits the focused document. Writer still uses `get_string_without_tracked_deletions`.
- `_setup_module_tabs` wraps `setup_module_tabs` in `except Exception: pass` (`plugin/chatbot/dialog_views.py`). `setup_module_tabs` already logs. Drop the outer try, or log.
- The eval dashboard translates when `load_writeragent_dialog` loads `EvalDialog`.

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
- `plugin/audio/tts_service.py` `sync_tts_ui`: Together voices use the live endpoint argument. OpenRouter voice rows call `_saved_endpoint_is_openrouter()`, which reads the saved URL. Before OK, an OpenRouter choice still builds voices as if the old host were current.
- `plugin/framework/config.py` `validate()` already runs `endpoint_from_selector_text`. Do not add a second normalizer in the settings dialog.

## Open

- `[fidelity S few]` Fire the image-model combo's text listener as well as its item listener so a typed image model id is saved. (§ 10 A failed panel create stays latched)
- `[fidelity S many]` Measure button widths from the translated labels `_(lab)` instead of the English "Send", "Record", "Stop Rec" and "Accept" lists. (§ 10 A failed panel create stays latched)
