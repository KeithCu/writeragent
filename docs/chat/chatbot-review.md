# Chatbot review — simplify and harden

Suggestion-only review of `plugin/chatbot` (about 27,000 lines, six read-only passes). Nothing in this note has been implemented. Date: 2026-10-02.

The tree is in good structural shape. The pure tool-loop FSM, frame-only document lookup, `StreamQueueKind` draining, compaction v1, and the librarian/smol split should stay as they are. The problems worth fixing are places where the screen, the session, and the next request disagree, plus a few paths that keep going after the user has already said stop.

Coverage:

- Send / FSM: `state_machine.py`, `tool_loop_state.py`, `tool_loop_actions.py`, `tool_loop.py`, `send_state.py`, `send_handlers.py`, `sticky_reply.py`, `chat_sidebar_mode.py`, `panel.py`
- Panel shell: `panel_factory.py`, `panel_wiring.py`, `panel_resize.py`, `sidebar_state.py`, `sidebar_test_hooks.py`, `dialogs.py`
- Settings: `dialog_views.py`, `settings_dialog.py`, `settings_fields.py`, `settings_tab_order.py`, `config_ui_helpers.py`, `module_config_dialog.py`, `quick_setup.py`, `eval_dashboard_ui.py`, `hamburger_menu.py`
- Transcript: `rich_text.py`, `rich_text_control.py`, `rich_text_paste.py`, `slash_commands.py`, `slash_popup.py`, `selection.py`, `external_editor.py`
- Research and history: `web_research.py`, `web_research_cache.py`, `web_research_chat.py`, `web_research_deep.py`, `research_cache_fluff.py`, `deep_research_session.py`, `compaction.py`, `history_db.py`
- Modes and audio: `smol_agent.py`, `smol_examples.py`, `librarian.py`, `memory.py`, `brainstorming.py`, `writing.py`, `skills.py`, `agent_manual.py`, `ppt_master.py`, `todo.py`, `audio_recorder.py`, `audio_recorder_state.py`, `record_gesture.py`, `grammar_status.py`, `bug_report.py`, `extension_update_check.py`

Constraints for any follow-up (from `plugin/chatbot/AGENTS.md` and the root `AGENTS.md`):

- Keep `next_state` pure. No UNO and no I/O inside it.
- `send_handlers.py` and `tool_loop.py` stay mixins on `SendButtonListener`. Do not add `session.py` or `send.py`.
- Do not merge smol or librarian into the main chat FSM. No second HTTP client. Smol stays on `WriterAgentSmolModel` → `LlmClient.request_with_tools`.
- Send and the tool loop refresh through `ChatSession.refresh_document_context`. They must not import `get_document_context_for_chat` or `panel_factory`.
- The factory resolves the document from the frame only and must not import `get_document_context_for_chat`.
- Stop uses `resolve_stop_checker()`. Queue items start with a `StreamQueueKind` enum member. No raw threads.
- Do not split `panel.py` or `dialog_views.py` just because they are long. Prefer one existing function over a new module.

## Fix these first

### 1. Stop and Clear do not match what the user sees

`STOP_REQUESTED` always stores the assistant text `"No response."` (`plugin/chatbot/tool_loop_state.py` around line 426) even when the widget already shows the tokens that streamed. The next turn sends that placeholder. The agent path already keeps the partial (`plugin/chatbot/send_handlers.py`). Pass the accumulated text into the existing stop effect when there are no tool calls, and close any open tool-call ids with a short stopped result so `sanitize_tool_pairs` does not drop a finished tool body.

Clear latches `STOP_CLICKED` and wipes the widget (`plugin/chatbot/panel.py` `ClearButtonListener.on_action_performed`, around line 1976), but the drain flushes queued chunks after that pump returns, so the reply paints back onto the empty transcript while history stays cleared. `_append_response` (around line 849) should return when the send is busy and `_turn_accepts_write` is false. That helper already exists for this race. Do not gate when the send is idle: after Clear, `_turn_messages` stays stale until the next `bind_turn_session`, and a pre-bind error line must still show.

`SEND_COMPLETED` forces `has_text=False` (`plugin/chatbot/send_state.py` around line 224). Text typed while a reply streams, and the extracted-peer send path that never clears Ask, then look empty. With recording support the button becomes Record, so the next click captures audio. After both drain `finally`s, dispatch `TEXT_UPDATED` from the live query control. Do not stop forcing `has_text=False` inside the FSM alone. If the clear did not fire the text listener, `has_text` would stay true on an empty box. The control is the source of truth.

`ChatSession.clear()` resets messages and leaves `active_specialized_domain` set (`plugin/chatbot/panel.py` around line 196). The next send still advertises the previous delegate tools (`plugin/chatbot/tool_loop.py` around line 223). Set `active_specialized_domain`, `python_tool_domain`, and `tool_streamed_texts` back to empty in `clear()`.

Related, same area:

- A disposed document becomes a normal tool error and the loop continues. `execute_fn`'s `except Exception` in `plugin/chatbot/tool_loop_actions.py` (around line 263) turns `DisposedException` into a JSON tool payload and queues `TOOL_DONE`. Use `is_tool_document_disposed` (not `is_disposed_exception`, which treats any `RuntimeException` as disposal). Queue `StreamQueueKind.ERROR` and let the existing error handler end the loop.
- Web, librarian, brainstorm, writing-plan, PPT, and deep-research answers are still persisted from the `chatbot-send-handler` worker (`plugin/chatbot/send_handlers.py`, the `persist_assistant_on_turn` calls around the answer puts). The agent path was moved onto the drain because a worker write raced Clear. Put the answer on the existing `STREAM_DONE` payload and persist it in `on_stream_done`.
- The API-error banner is widget-only (`plugin/chatbot/tool_loop.py` `_handle_stream_error`). The user row is stored and the assistant row is not, so a retry adds a second user row with a hole. `persist_assistant_on_turn` the same banner. Skip that write when `on_error` returns true (overflow respawn or audio fallback).
- The agent user line is painted before it is in history. `spawn_effects_for_start` appends the user query, then the worker can return before `add_user_message` (disposed document, unknown backend). Append the user line in the same place as `add_user_message`, after the backend checks.
- Web-research approval setup fails open. The `except` around approval setup logs a warning and leaves `approval_cb` as `None`. `web_research.py` prompts only when both `prompt_for_web_research` and `approval_callback` are set, so a config error skips Accept/Change/Reject and the search runs. Return an error payload instead of continuing with no callback.

### 2. A mode change during a send writes the other session

`bind_turn_session` exists because `set_session` can swap `host.session` inside the drain. TTS still speaks `self.session.messages[-1]` (`plugin/chatbot/panel.py` around line 1351), and document refresh plus `set_active_domain` still use `host.session` (`plugin/chatbot/tool_loop_actions.py`, `plugin/chatbot/tool_loop.py` around line 215). Librarian handoff calls `apply_mode(CHAT)` before TTS, so speech reads the document chat's last row. The mode combo stays enabled while busy, so a click during `processEventsToIdle` writes `[DOCUMENT CONTENT]` onto the other `ChatSession`.

Point TTS, refresh, and domain updates at `session_for_turn`. Call `bind_turn_session` once at the start of `_do_send_chat_with_tools`, before the `pump_ui_idle`, not after refresh. Ignore user combo changes while `send.is_busy`, in `ChatModeListener.on_item_state_changed` only. Leave `apply_mode` itself alone, because the librarian handoff calls it while the send is still busy. Skip refresh when `_turn_accepts_write` is false.

`set_session` has one production caller, `_apply_sidebar_mode`. `session_for_turn` is already what `_spawn_llm_worker` and `persist_assistant_on_turn` use.

### 3. Deep Research Reject does not stop the run

Shallow search returns `USER_STOPPED`. The deep path only skips the DuckDuckGo preview and then calls `run_deep_research(query_str)` (`plugin/chatbot/web_research.py` around lines 571 and 599). Reject never latches `stop_checker`, so the multi-minute loop runs after the sidebar has gone idle and a normal result can be cached. A Change override is applied only to the preview. Planning, sub-queries, and the cache key still use the original query.

Return the same `USER_STOPPED` payload on Reject, and pass the edited query into both the preview and `run_deep_research`.

### 4. A failed page fetch is remembered as visited

`_VisitWebpageDedupTool.forward` inserts the URL before the inner tool returns (`plugin/chatbot/web_research.py` around line 230). CDP and HTTP errors come back as strings (`"Error visiting..."`, `"Failed to navigate..."`), the URL stays in the set, and later sub-queries in the same run get "Already visited" and never retry. `VisitWebpageTool` already refuses to cache fetch errors. This set undoes that for the rest of the run.

Add the URL only after a non-error return, or remove it when `forward` raises or the text starts with `Error` / `Failed`. Keep the lock around the check-and-add.

### 5. Non-English research can take a legacy English cache row

Shallow exact lookup tries the bare word key before `{lang}|{words}` (`plugin/chatbot/web_research_cache.py` `lookup_research_cache`, around line 668). Fuzzy and embedding matches already refuse a different language. A French query whose normalized key is `paris restaurants` hits a legacy English row and never reads `french|paris restaurants`. `_web_cache_get` also slides `created_at` on that hit, so the stale row stays inside the validity window.

Try the bare key only when `snowball_lang == "english"`. Keep the English bare-then-prefixed order. `tests/chatbot/test_web_research_cache.py` and `docs/chat/search.md` lock that order. Deep lookup already uses only `deep|{lang}|{words}`.

Also: `resolve_research_locale` (`plugin/chatbot/web_research_cache.py` around line 118) treats any exception from the document, including disposal, as `("en_US", "english")`. Re-raise `is_disposed_exception`. Leave the timeout and cancel fallbacks.

Embedding backfill reads missing keys, drops the cache lock, embeds, then `INSERT OR REPLACE` (`plugin/chatbot/web_research_cache.py` `store_research_cache_embeddings`). Eviction can delete the parent row in that window. The later insert does not check that `web_cache` still has the key. Orphans are not served (lookup joins `web_cache`) but nothing deletes them, and the size cap sums only `web_cache.size`. Insert only while the parent row still exists, in the same transaction.

### 6. PPT-Master follows the focused window and ignores Stop

The sidebar records the frame document's URL as a skill-cache id (`plugin/chatbot/ppt_master.py` around line 54) and passes it inside the child payload. `execute_ppt_master_turn` calls `_execute_ipc_unlocked` without the IPC `session_id` (`plugin/scripting/venv_worker.py` around line 716). Host tool RPC then uses `get_active_document` (`plugin/scripting/host_rpc.py` around line 351). A long turn with deck B focused exports into B. Named scripts already resolve `document_for_script_session` before `get_active_document`. Pass the frame URL as the IPC `session_id` and use that same lookup in `execute_tool`.

Stop is checked once, before the venv turn (`plugin/chatbot/ppt_master.py` around line 77). The child loop (`plugin/ppt_master/venv/runner.py`) does not look again, and tool frames are dispatched with no stop check. The sidebar returns while the worker can still export. The child should return `USER_STOPPED` when the host reports stop, and `handle_tool_call_frame` should refuse the call when stop is already set. Leave this runtime out of the chat FSM.

Librarian, brainstorming, and writing already stop: `SmolAgentExecutor` calls `interrupt()` and raises `USER_STOPPED` before the tool body.

Smaller, same area: `_parse_finished` in `plugin/ppt_master/venv/runner.py` keeps a handoff only until the first apostrophe (`re.search(r"'result': '([^']*)'", ...)`). A finished message containing `'` is cut off. `_selected_chat_model`'s docstring still says send handlers pass the model id via `ToolContext.doc`. On the live path `ctx.doc` is the UNO document.

### 7. An Impress agent-backend turn gets the Writer manual

`_app` maps every unknown label, including `"impress"`, to `"writer"` (`plugin/chatbot/agent_manual.py` around line 159). `_SECTIONS_BY_APP` has no `impress` key. The agent-backend worker passes the cached label `"impress"` straight into `full_manual` (`plugin/chatbot/send_handlers.py`). Core directives already treat Impress as Draw (`get_core_directives_for_type` in `plugin/framework/prompts.py`). Map `impress` to `draw` in `_app`. One place, same topic tables.

Sidebar `get_guidance` on the main thread is fine, because `doc_type_of` sees a Draw model (`is_draw` is true for Impress). Do not call `full_manual_for_model` on the worker, and do not add a second topic list.

## Settings and panel shell

### 8. Settings open fetches models on the UI thread

The initial combo fill calls `fetch_available_models` before `execute()` (`plugin/chatbot/dialog_views.py` `_populate_fields`). Timeout is 10 seconds (`_MODEL_FETCH_TIMEOUT`), and a failure is not cached, so a dead Ollama, Groq, or custom URL freezes LibreOffice on every open. The endpoint listener's scheduler returns immediately unless the provider is OpenRouter or Together. The eval dashboard does the same synchronous populate.

Pass `skip_remote_fetch=True` on that first fill (LRU plus defaults), then use the debounced scheduler for every provider. A warm memo already applies from cache with no HTTP.

The endpoint dropdown's background fetch omits the Together TTS model id (`plugin/chatbot/dialog_views.py` `itemStateChanged`, the `run_in_background` lambda). `_bg_fetch` only calls `fetch_together_tts_voices(..., model_id=...)` when that string is non-empty. The comment there says list-all does not fill the `?model=` entry the Voice combo reads. The lambda also reads `self._debounce_gen` when the worker starts, so the generation check does not see the generation that was current at click time. `force_catalog_refresh` already snapshots `gen` and `tts_model_id`. Use that same call.

`textChanged` calls `_sync_api_key` on every keystroke (`plugin/chatbot/dialog_views.py`). `_sync_api_key` always does `set_control_text(ak_ctrl, get_api_key_for_endpoint(resolved))`. Paste a new key, then fix one character of the URL, and the typed key is gone. OK then persists the restored value. Sync only when the resolved URL changes, and only if the field still holds the saved key for the previous URL (or is empty). Preset clicks should keep loading the saved key.

The Hugging Face button still stores `https://api-inference.huggingface.co/v1` (`plugin/chatbot/dialog_views.py` starter tuple, and the same URL in `plugin/chatbot/quick_setup.py` `PROVIDER_STARTERS`). That host is retired. The OpenAI-compatible base is `https://router.huggingface.co/v1`. `ENDPOINT_PRESETS` has no Hugging Face row, and `get_provider_from_endpoint` has no Hugging Face host, so the button is the only UI that sets this URL. `PROVIDER_STARTERS` has no production caller. Point the button at the router. Build the four starter buttons from `ENDPOINT_PRESETS` plus `get_signup_url_for_endpoint` (today that helper is only used from tests). OpenRouter, Together, and NVIDIA already match the presets.

### 9. Vision OK closes after a failed save

`_apply` logs `ConfigValidationError` and still calls `close()` (`plugin/chatbot/module_config_dialog.py` around line 236). Settings shows a message box and stays open. Do the same here, and read controls with `get_optional` so one missing name does not abort `show()` with no dialog. `getControl` raises on a missing name. `if ctrl is None` does not catch that.

The eval dialog's endpoint text field is editable and is never read. `run_benchmark_suite` uses the saved endpoint only. Close disposes the dialog while the dedicated worker can still post `_after_test` / `_show_summary` onto it. Mark `endpoint` readonly in `EvalDialog.xdl`. Set a closed flag in `show()`'s `finally` and return at the start of `_paint`, `_show_summary`, and `_show_failure`.

`input_box` skips `dispose()` when `execute()` is false, because dispose after Esc or the window close segfaults on some Linux builds (`docs/framework/uno-dialogs.md`). Settings `_cleanup` and the eval dialog always dispose. Same guard as `input_box` if Cancel or the title-bar close actually crashes. Do not change the message-box helpers from this.

### 10. A failed panel create stays latched

`getRealInterface` assigns `self.toolpanel` before `wire_chatpanel_controls` (`plugin/chatbot/panel_factory.py` around line 420). If wiring throws (`get_extension_url` in `_wire_buttons`, or `getControl("send"|"query"|"response")`), the `except` logs and raises `UnoObjectError` without clearing `toolpanel`. The next `getRealInterface` returns the half-built panel and never retries. Set `self.toolpanel = None` before re-raising. Leave the assignment where it is. Wiring reads `self.toolpanel` for the resize parent. `createContainerWindow` failure happens before the assignment and is not this bug. The main-thread hop in `_run_on_main_thread` is already correct.

Button widths are measured from the English strings `"Send"`, `"Record"`, `"Stop Rec"`, `"Accept"` (`plugin/chatbot/panel_wiring.py` `_measure_send_button_max_width`, around line 22). Stop and Clear use the same untranslated lists. The live label is `_(effect.send_label)`. The pin exists to stop the Record / Send `windowResized` growth loop. A longer translation still changes `getPosSize().Width` before the English pin is forced back, which is that loop, or the label is clipped. Measure `_(lab)` in `_measure_aux_button_max_width`, which already walks a label list. `_` is already imported in that file.

The image-model combo is editable (`dlg:dropdown="true"`) and only has an item listener (`plugin/chatbot/panel_factory.py` around line 766). The text-model combo gets both `ModelSyncListener` and `ModelTextSyncListener` because list selection and typing do not share one event. A typed image id never reaches `set_image_model`. The next `config:changed` refresh repaints the combo from `get_image_model()` and drops it. Add the same `addTextListener` path, still bailing out while `_in_refresh_controls` is set.

`status_dialog` in `plugin/chatbot/dialogs.py` (around line 647) sets a label from a pool thread while `dlg.execute()` blocks the main thread, and `except: pass` drops disposal. No `plugin/` caller. MCP status is a different dialog (`ServerStatusDialog` in `plugin/mcp/__init__.py`). If this helper stays, post the label update with `post_to_main_thread` and log disposal.

`_OkListener` / `_CancelListener` are raw `unohelper.Base, XActionListener` in several `dialogs.py` helpers. Copy buttons in the same functions use `BaseActionListener`. Those dialogs also `dispose()` after `execute()` with no `finally`. `msgbox` and `show_approval_dialog` already dispose in `finally`. One local `BaseActionListener` that calls `endDialog`, plus `try` / `finally: dlg.dispose()`, matches `msgbox`.

## Transcript and history

### 11. One bad history element drops the whole batch

A paragraph exception in `_copy_formatted_from_hidden_doc_to_control` sets `element_skipped` and fails the batch (`plugin/chatbot/rich_text_paste.py` around line 556). `append_rich_messages_via_clipboard` rolls the control back and writes nothing (around line 693). A live stream keeps the plain tail via `rerender_last_assistant_if_html`. History has no second copy. One bad element drops up to `HISTORY_RENDER_BATCH_CHARS` (16384) of messages. After that rollback, and in the `except` when `inserted` is still false, call the existing `_plain_append_messages` for the same batch.

`finalize_sidebar_assistant_response` appends `stripper.finalize()` only when there is no rich widget (`plugin/chatbot/rich_text.py` around line 342). The default sidebar has the widget, so a split entity or an unclosed tag held by `StreamingHTMLStripper` is discarded on Stop and on error. A successful rerender does not need this tail. The session HTML replaces it. Append the leftover when this call did not replace the tail. The Error early-return also clears the stripper without `finalize()`.

If the HTML filter throws, the handler logs at debug and `insertString`s the raw tags (`plugin/chatbot/rich_text.py` around line 290). `used_html_import` stays false, the hidden doc still has text, and the copy returns success. Rerender then does not put the stripped stream back, so the sidebar shows tags. Release logs are WARN, so the debug line does not show up. Do not insert the raw string. Signal failure and plain-append `_plain_fallback_text`. Sanitizing full documents belongs in that same call: `_wrap_html_fragment` in `plugin/writer/html_import.py` passes a fragment through unchanged when it already contains `<html>` and `<body>`.

`get_control_text_length` returns `0` on any exception (`plugin/chatbot/rich_text_control.py` around line 1248). Both paste paths use that as the rollback point, and `truncate_control_from` treats zero as "delete from the start." The same zero is the cell-link span start. Return `None` and skip rollback when the length is unknown. `CellLinkSpanRegistry.add` already ignores `start >= end`.

`_plain_append_messages` paints `assistant_color` and inserts no `You:` / `Assistant:` prefix and no blank line, so user and assistant turns run together. The formatted path adds both inside `append_rich_text`. One prefix-and-gap helper for the plain fallback. Do not add a second theme table.

`build_message_html` (`plugin/chatbot/rich_text_paste.py`) is unused in production. The module docstring still says the pipeline falls through to transferable / `SystemClipboard` / Ctrl+V. Those calls are gone. `append_*_via_clipboard` only direct-copies. Update the comment so it does not invite pasting into the real document again.

`SlashPopupController._attach_item_listener` is never called. The comment above the mouse listener says item listeners deadlock. Delete the method. Do not wire it.

### 12. JSON history can fork away from SQLite

Any `sqlite3.Error` in `get_chat_history` returns a `JSONHistory` for that object (`plugin/chatbot/history_db.py` around line 241). Later turns land under `writeragent_history.db.d/`, and the next successful SQLite open never reads them. Retry a lock. Do not switch an existing `writeragent_history.db` over to JSON.

`JSONHistory.add_message` logs `OSError` / `TypeError` and returns (around line 178). The turn is already in `ChatSession.messages` and is gone on the next open. `get_messages` already re-raises. Do the same on save. `SQLite3History.add_message` already lets `sqlite3.Error` propagate.

The JSON filename is the raw session id (`plugin/chatbot/history_db.py` around line 162). `os.path.join(history_dir, f"{session_id}.json")` escapes the directory when the id is absolute or contains `..`. Regenerated ids are SHA-256 hex (`plugin/chatbot/panel_factory.py` around line 993), but an existing `WriterAgentSessionID` document property is used as-is (around line 985). SQLite binds `session_id` as a parameter, so this is JSON-fallback only. Reject a session id that is not one path segment, or hash it for the filename only.

Do not add a second store. Do not persist tool results from `history_db`. Tool-only assistant turns are skipped on purpose (`ChatSession.add_assistant_message`). Reloaded history is user and assistant text. Compaction is not the place to "fix" that.

## Test hooks

### 13. The debug harness can drive the wrong deck

Shipped in `plugin/chatbot/sidebar_test_hooks.py`. Several live panels and no frame or `_live_panel_uid` match return `None` (a single live panel is still returned). `uno_same` is not called off the main thread. Adopt and read skip `_panel_teardown` and drop those listeners from the strong list. `clear_sidebar_chat` calls `clear_listener.on_action_performed` when that listener exists. `press_record` is the Record mouse press/release pair. `press_stop_rec` uses that pair, then the button action only when the label is Stop Rec.

## Small collapses

These remove a second copy of a fact. None of them need a new file.

- The librarian greeting string is written twice, in `_greeting_for_sidebar_mode` and in the librarian `ChatSession` constructor (`plugin/chatbot/panel_factory.py`, around lines 795 and 1031). Build the session from `_greeting_for_sidebar_mode(CHAT_MODE_LIBRARIAN, model)`. `LIBRARIAN_HISTORY_SESSION_ID` is already the single history id.
- Settings, the Edit/Extend `input_box`, and the eval dashboard each call `get_extension_url()` with no context and never try `DialogProvider2` (`plugin/chatbot/dialog_views.py`, `plugin/chatbot/eval_dashboard_ui.py`). `load_writeragent_dialog` in `dialogs.py` takes `ctx`, tries `DialogProvider` then `DialogProvider2`, and translates. Module config and the TTS progress box already use it. Keep Settings' second `translate_dialog` after populate so labels still translate after combos are filled.
- `plugin/chatbot/settings_tab_order.py` rebuilds `inline_targets` / `inline_set` / `inline_map`. `scripts/manifest_registry.py` already builds that map, and `generate_settings_dialog_tabs` then copies it. Use `_build_inline_maps` in the generator and delete the copy. If the copies diverge, the tab exists and the fields land on the wrong page.
- `apply_settings_result` (`plugin/chatbot/settings_dialog.py`) saves `key.replace("__", ".")`. `apply_field_specs_result` (`plugin/chatbot/settings_fields.py`) saves `spec["config_key"]`. For `audio` and `calc` those match. A module name with a dot would diverge. Use `spec.get("config_key") or key.replace("__", ".")`. No current module name contains a dot.
- Vision tab steps are a second page list (`plugin/chatbot/module_config_dialog.py` `_TAB_PAGE_MAP`). A new `page:` in `vision/module.yaml` gets a button and a Step, and `_setup_tabs` never listens. Walk `btn_tab_*` in control order and assign Step 1..n, which is the order the generator writes.
- `resolve_research_stem_language` (`plugin/chatbot/web_research_cache.py`) has no callers. Callers use `resolve_research_locale`. Delete the wrapper.
- `run_deep_research(..., depth=)` (`plugin/chatbot/web_research_deep.py`) is unused. Production rounds come from `chatbot.deep_research_max_rounds`. The parameter only applies when `max_rounds == 3`, so it is not a real alias of `chatbot.deep_research_depth`. Delete the parameter. Keep the config key.
- `approval_required` in `plugin/chatbot/web_research_chat.py` is deleted and ignored. Only a test still passes it.
- `_get_unique_words_key` and `_get_embedding_words_text` (`plugin/chatbot/web_research.py`) repeat the fluff filter. One ordered-unique list, then sort a copy for the storage key, keeps a single fluff rule. Storage keys are sorted and embedding text keeps order, on purpose (`docs/chat/search.md`).
- `todo.py` is a docstring. Importing it registers no tool. Delete it when something next touches that area. Do not invent a todo sub-agent to match the filename.
- `get_optional` turns a disposed window into "control missing" (`plugin/chatbot/dialogs.py` around line 1056). Re-raise `is_disposed_exception`. Callers that must ignore teardown already have `suppress_disposed`. Keep `None` for a missing name.
- `docs/chat/sidebar-implementation.md` still says the first mode follows an empty `USER.md`. The factory uses `librarian_default_mode` / `chatbot.librarian_invoked`, which matches `plugin/chatbot/AGENTS.md`. Update that sentence. Do not change the factory to match the doc.
- A long traceback drops the bug-report environment. The cap is 7500 raw characters, applied before `urlencode` (`plugin/chatbot/bug_report.py` `_compose_issue_body`). When the exception fills the budget, version, OS, locale, Python, endpoint, and model are discarded. Reserve a short environment header and put the exception tail in what remains. Cap the encoded URL. A failed open logs the full URL. The template has no API-key field.
- Extend/Edit from the hamburger uses the sidebar frame (`get_document_from_frame`). The menubar still calls the handler with no frame, so it edits the focused document. Writer still uses `get_string_without_tracked_deletions`.
- `_setup_module_tabs` wraps `setup_module_tabs` in `except Exception: pass` (`plugin/chatbot/dialog_views.py`). `setup_module_tabs` already logs. Drop the outer try, or log.
- The eval dashboard never calls `translate_dialog`. Fixed if it goes through `load_writeragent_dialog`.

### Slash popup (flag is off)

`ENABLE_SLASH` is false, and `QueryKeyListener` does not call `handle_key` while it is false. Ask Enter still sends. Fix this set before turning the flag on:

- The frame and toolkit `XKeyHandler`s call `handle_key(..., from_overlay=True)` (`plugin/chatbot/slash_popup.py`). That path appends every printable character to Ask and returns true, so those handlers steal document keys. They should use `from_overlay=False` (nav keys only). The listbox listener is the one that should pass `from_overlay=True`.
- `accept_selected` → `run_slash_command` → `hide()` disposes the list from inside the key or mouse callback. Dispose on a posted main-thread turn.
- `_show_matches` calls `reposition()` after `setVisible(True)`. `reposition` `getPosSize`s Ask. `QueryTextListener` says not to `getPosSize` Ask once the TOP overlay exists, because that deadlocks VCL (`plugin/chatbot/panel.py`). Delete the unused `_screen_bounds_above_ready` call.
- `/stop` is not the Stop button. `run_slash_command` always clears Ask, then only dispatches `STOP_CLICKED` (`plugin/chatbot/slash_commands.py`). The Stop button also stops TTS and answers the inline approval dialog, and it does not clear the draft. Clear Ask for help, clear, and mocks only. Run the same stop listener the button uses.
- `QueryTextListener` returns before `TEXT_UPDATED` for any slash draft (`plugin/chatbot/panel.py`). `hide()` / Esc does not dispatch `has_text`. After Esc, Send stays disabled until the text changes to something that is not a slash prefix.

## Leave alone

Verified and not a fix:

- Both `next_state` functions stay pure. Stop cannot emit a spawn (`stop_effects_exclude_spawns`, `stopped_effects_exclude_tool_spawns`). Side effects stay in the interpreters.
- Stop uses `resolve_stop_checker()` bound to the `SendCancellation` created in `StartSendEffect`. `disposing` cancels that same scope and sets `_panel_teardown` so the `finally` does not write status or arm the mic. Approval chrome is restored from `_approval_ui_backup`.
- Queue items are `StreamQueueKind`. LLM and async tools use `run_in_background(..., dedicated=True)`. The UI thread drains. Sync tools run on the drain on purpose. The sync `execute_fn` path does receive `resolve_stop_checker()` via the default argument. A long sync tool still blocks Stop until it returns. That is the shape of sync work on the UI thread. Do not add a thread for it.
- Chat send and a successful mutating tool refresh through `ChatSession.refresh_document_context`. This area does not import `get_document_context_for_chat` or call `uno.getComponentContext()`. The document comes from the frame.
- Tool-loop history writes go through `persist_*_on_turn`. `compact_session` aborts if `session.messages` was replaced. `messages_for_llm` always runs `sanitize_tool_pairs`.
- `SEND_CLICKED` while busy is ignored. `STOP_CLICKED` keeps `is_busy` until completion. Image and agent Stop are not overwritten to Ready when status is Error or Stopped.
- The document comes only from the frame (`get_document_from_frame`). Create hops to the main thread before `get_extension_url`. XDL load uses `ContainerWindowProvider` plus the extension `base_url`. Deck close goes through the resize listener's `disposing` → `release_live_sidebar`, and `unregister_live_panel` will not drop a newer window that reused the uid.
- `getHeightForWidth` sizes only the AWT window. `windowResized` relayouts and does not snap width back to the last deck hint. `_in_refresh_controls` covers model, image, mode, and voice listeners. Config subscribe is `weak=True`.
- Settings OK skips a missing control instead of writing `""`. Placeholders are stripped before `text_model` / `image_model` are stored. Speech voice is not written until OK. STT uses `set_control_enabled`, not `setVisible`. One `set_configs` batch. Unchanged values do not emit `config:changed`. Checkboxes go through `populate_settings_control` / `read_settings_control`. `config_dialog` modules (vision) are left out of the main field list.
- Research stays plain text. `WEB_RESEARCH_PLAIN_TEXT_FORMAT` forbids HTML. Workers build `LlmClient` plus `WriterAgentSmolModel`. Deep vs shallow cache keys do not cross-match. Stop during planning, extraction, or synthesis returns `USER_STOPPED` and is not cached. `history_text` for sub-agents skips `system` and `tool`, so a stale `[DOCUMENT CONTENT]` row is not copied into research.
- Compaction v1 keeps `messages[0]`, summarizes `messages[prev_kept:new_cut]` with `prev_kept >= 1`, strips a `[DOCUMENT CONTENT]` span from the summary slice only, snaps the cut onto a user or assistant message, and aborts if `session.messages` was replaced. `chat_compaction_enabled` is the only kill switch. Do not add an OpenClaw-style wrapper layer. The 200-character tool stub in the summary input is the written policy in `docs/chat/compaction-dev-plan.md`.
- Librarian history stays on `LIBRARIAN_HISTORY_SESSION_ID`. Web research is `session_id + "_web"`. Do not gate `_do_send` on a missing `USER.md`. Default selection uses `chatbot.librarian_invoked`.
- `upsert_memory` is tier `core`. Writes hold `_MEMORY_WRITE_LOCK`, use `mkstemp` plus `os.replace`, refuse invalid JSON and non-objects, and refuse a dotted key that would replace a string with `{}`. `MEMORY_GUIDANCE` stays in `plugin/framework/prompts.py`.
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
- `plugin/framework/errors.py`: `is_tool_document_disposed` is the tool-boundary predicate. `is_disposed_exception` is the UI heuristic and matches a bare `RuntimeException`.
- `plugin/scripting/host_rpc.py` and `plugin/scripting/venv_worker.py`: PPT document binding and cancel. See item 6.
- `plugin/audio/tts_service.py` `TtsSettingsListener.sync_ui`: Together voices use the live endpoint argument. OpenRouter voice rows call `_saved_endpoint_is_openrouter()`, which reads the saved URL. Before OK, an OpenRouter choice still builds voices as if the old host were current.
- `plugin/framework/config.py` `validate()` already runs `endpoint_from_selector_text`. Do not add a second normalizer in the settings dialog.
