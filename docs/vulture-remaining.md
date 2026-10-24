# Vulture Dead Code Analysis — Remaining Hits

This document records the output of `make vulture` after trimming `vulture_whitelist.py`.
The whitelist strictly keeps only legitimate false positives that Vulture cannot trace statically:

1. **UNO callbacks, listener methods, and pyuno interface implementations** invoked directly by the LibreOffice C++ runtime.
2. **ToolBase classes registered with tool registries and tool schema fields/flags** inspected via reflection during LLM dispatch.
3. **Dynamic dispatch functions and ServiceRegistry names** resolved by string name across IPC or service lookup.
4. **Test hooks, test fixtures, and test utilities** defined in `plugin/` that are exercised only from `tests/`.

> [!NOTE]
> `plugin/scripting/writeragent_api.py` is excluded per project policy.
> None of the items listed below have been deleted or modified; this list serves as a reference of likely real dead code for later review.

**Total remaining hits:** 491

## Summary by Subsystem

| Subsystem / Area | Hits | Description |
|---|---|---|
| `contrib` | 79 | Vendored third-party dependencies (smolagents, nbformat, cdp) |
| `framework` | 77 | Framework core, config, logging, client, process worker |
| `writer` | 73 | Writer text, formatting, styles, search, indexes, tracking |
| `chatbot` | 67 | Sidebar UI, dialogs, state machine, compactions, prompts |
| `scripting` | 65 | Venv sandbox, python runner, payload codec, domain templates |
| `calc` | 31 | Calc manipulation, cell discovery, excel conversion, vision egress |
| `mcp` | 23 | MCP server endpoints, protocol handlers, SSE transport |
| `embeddings` | 18 | Document research embeddings, llama-index integration |
| `ppt_master` | 18 | Presentation master fidelity, notes, shape processing |
| `vision` | 8 | Vision templates, image egress, paddle integration |
| `main.py` | 7 | UNO lifecycle variables and properties |
| `notebook` | 6 | Notebook importer, cell controls, runner |
| `doc` | 5 | Document helpers, diagnostics, text helpers, live panels |
| `main_core.py` | 4 | LibrePy UNO lifecycle variables |
| `draw` | 3 | Draw designs, transitions, shapes |
| `librepy` | 3 | LibrePy settings, sidebar menus |
| `acp` | 2 | ACP connection reader thread |
| `audio` | 2 | Kokoro pool workers, audio recorder |

---

## `contrib` (79 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/contrib/cdp/browser_cdp_tool.py:579](file:///plugin/contrib/cdp/browser_cdp_tool.py#L579) | unused variable | `BROWSER_CDP_SCHEMA` | 60% |
| [plugin/contrib/cdp/browser_supervisor.py:268](file:///plugin/contrib/cdp/browser_supervisor.py#L268) | unused variable | `console_errors` | 60% |
| [plugin/contrib/cdp/browser_supervisor.py:430](file:///plugin/contrib/cdp/browser_supervisor.py#L430) | unused method | `respond_to_dialog` | 60% |
| [plugin/contrib/cdp/browser_supervisor.py:491](file:///plugin/contrib/cdp/browser_supervisor.py#L491) | unused method | `evaluate_runtime` | 60% |
| [plugin/contrib/cdp/browser_supervisor.py:1427](file:///plugin/contrib/cdp/browser_supervisor.py#L1427) | unused method | `get_or_start` | 60% |
| [plugin/contrib/cdp/browser_supervisor.py:1478](file:///plugin/contrib/cdp/browser_supervisor.py#L1478) | unused method | `stop_all` | 60% |
| [plugin/contrib/nbformat/_struct.py:242](file:///plugin/contrib/nbformat/_struct.py#L242) | unused method | `allow_new_attr` | 60% |
| [plugin/contrib/nbformat/v4/__init__.py:41](file:///plugin/contrib/nbformat/v4/__init__.py#L41) | unused variable | `reads_json` | 60% |
| [plugin/contrib/nbformat/v4/__init__.py:42](file:///plugin/contrib/nbformat/v4/__init__.py#L42) | unused variable | `writes_json` | 60% |
| [plugin/contrib/ppt_master/config.py:21](file:///plugin/contrib/ppt_master/config.py#L21) | unused function | `templates_dir` | 60% |
| [plugin/contrib/ppt_master/config.py:25](file:///plugin/contrib/ppt_master/config.py#L25) | unused function | `references_dir` | 60% |
| [plugin/contrib/ppt_master/config.py:29](file:///plugin/contrib/ppt_master/config.py#L29) | unused function | `workflows_dir` | 60% |
| [plugin/contrib/ppt_master/config.py:33](file:///plugin/contrib/ppt_master/config.py#L33) | unused function | `skill_md_path` | 60% |
| [plugin/contrib/ppt_master/upstream.py:20](file:///plugin/contrib/ppt_master/upstream.py#L20) | unused function | `upstream_scripts_path` | 60% |
| [plugin/contrib/smolagents/agent_types.py:115](file:///plugin/contrib/smolagents/agent_types.py#L115) | unused method | `_ipython_display_` | 60% |
| [plugin/contrib/smolagents/agent_types.py:235](file:///plugin/contrib/smolagents/agent_types.py#L235) | unused method | `_ipython_display_` | 60% |
| [plugin/contrib/smolagents/agents.py:40](file:///plugin/contrib/smolagents/agents.py#L40) | unused variable | `Live` | 60% |
| [plugin/contrib/smolagents/agents.py:41](file:///plugin/contrib/smolagents/agents.py#L41) | unused variable | `Markdown` | 60% |
| [plugin/contrib/smolagents/agents.py:175](file:///plugin/contrib/smolagents/agents.py#L175) | unused variable | `initial_plan` | 60% |
| [plugin/contrib/smolagents/agents.py:176](file:///plugin/contrib/smolagents/agents.py#L176) | unused variable | `update_plan_pre_messages` | 60% |
| [plugin/contrib/smolagents/agents.py:177](file:///plugin/contrib/smolagents/agents.py#L177) | unused variable | `update_plan_post_messages` | 60% |
| [plugin/contrib/smolagents/agents.py:202](file:///plugin/contrib/smolagents/agents.py#L202) | unused variable | `pre_messages` | 60% |
| [plugin/contrib/smolagents/agents.py:203](file:///plugin/contrib/smolagents/agents.py#L203) | unused variable | `post_messages` | 60% |
| [plugin/contrib/smolagents/agents.py:218](file:///plugin/contrib/smolagents/agents.py#L218) | unused variable | `planning` | 60% |
| [plugin/contrib/smolagents/agents.py:341](file:///plugin/contrib/smolagents/agents.py#L341) | unused attribute | `agent_name` | 60% |
| [plugin/contrib/smolagents/default_tools.py:189](file:///plugin/contrib/smolagents/default_tools.py#L189) | unused class | `PreTool` | 60% |
| [plugin/contrib/smolagents/default_tools.py:359](file:///plugin/contrib/smolagents/default_tools.py#L359) | unused method | `handle_starttag` | 60% |
| [plugin/contrib/smolagents/default_tools.py:377](file:///plugin/contrib/smolagents/default_tools.py#L377) | unused method | `handle_endtag` | 60% |
| [plugin/contrib/smolagents/default_tools.py:389](file:///plugin/contrib/smolagents/default_tools.py#L389) | unused method | `handle_data` | 60% |
| [plugin/contrib/smolagents/default_tools.py:530](file:///plugin/contrib/smolagents/default_tools.py#L530) | unused method | `handle_starttag` | 60% |
| [plugin/contrib/smolagents/default_tools.py:534](file:///plugin/contrib/smolagents/default_tools.py#L534) | unused method | `handle_endtag` | 60% |
| [plugin/contrib/smolagents/default_tools.py:538](file:///plugin/contrib/smolagents/default_tools.py#L538) | unused method | `handle_data` | 60% |
| [plugin/contrib/smolagents/local_python_executor.py:652](file:///plugin/contrib/smolagents/local_python_executor.py#L652) | unused variable | `signum` | 100% |
| [plugin/contrib/smolagents/local_python_executor.py:809](file:///plugin/contrib/smolagents/local_python_executor.py#L809) | unused attribute | `__signature__` | 60% |
| [plugin/contrib/smolagents/local_python_executor.py:887](file:///plugin/contrib/smolagents/local_python_executor.py#L887) | unused attribute | `__ast__` | 60% |
| [plugin/contrib/smolagents/local_python_executor.py:890](file:///plugin/contrib/smolagents/local_python_executor.py#L890) | unused attribute | `__signature__` | 60% |
| [plugin/contrib/smolagents/local_python_executor.py:1779](file:///plugin/contrib/smolagents/local_python_executor.py#L1779) | unused attribute | `_authorized_imports` | 60% |
| [plugin/contrib/smolagents/local_python_executor.py:1889](file:///plugin/contrib/smolagents/local_python_executor.py#L1889) | unused attribute | `_authorized_imports` | 60% |
| [plugin/contrib/smolagents/memory.py:243](file:///plugin/contrib/smolagents/memory.py#L243) | unused method | `get_succinct_steps` | 60% |
| [plugin/contrib/smolagents/memory.py:255](file:///plugin/contrib/smolagents/memory.py#L255) | unused method | `replay` | 60% |
| [plugin/contrib/smolagents/memory.py:280](file:///plugin/contrib/smolagents/memory.py#L280) | unused method | `return_full_code` | 60% |
| [plugin/contrib/smolagents/models.py:29](file:///plugin/contrib/smolagents/models.py#L29) | unused import | `RateLimiter` | 90% |
| [plugin/contrib/smolagents/models.py:29](file:///plugin/contrib/smolagents/models.py#L29) | unused import | `Retrying` | 90% |
| [plugin/contrib/smolagents/models.py:41](file:///plugin/contrib/smolagents/models.py#L41) | unused import | `StoppingCriteriaList` | 90% |
| [plugin/contrib/smolagents/models.py:46](file:///plugin/contrib/smolagents/models.py#L46) | unused variable | `RETRY_WAIT` | 60% |
| [plugin/contrib/smolagents/models.py:48](file:///plugin/contrib/smolagents/models.py#L48) | unused variable | `RETRY_EXPONENTIAL_BASE` | 60% |
| [plugin/contrib/smolagents/models.py:49](file:///plugin/contrib/smolagents/models.py#L49) | unused variable | `RETRY_JITTER` | 60% |
| [plugin/contrib/smolagents/models.py:50](file:///plugin/contrib/smolagents/models.py#L50) | unused variable | `STRUCTURED_GENERATION_PROVIDERS` | 60% |
| [plugin/contrib/smolagents/models.py:51](file:///plugin/contrib/smolagents/models.py#L51) | unused variable | `CODEAGENT_RESPONSE_FORMAT` | 60% |
| [plugin/contrib/smolagents/models.py:144](file:///plugin/contrib/smolagents/models.py#L144) | unused method | `model_dump_json` | 60% |
| [plugin/contrib/smolagents/models.py:168](file:///plugin/contrib/smolagents/models.py#L168) | unused method | `render_as_markdown` | 60% |
| [plugin/contrib/smolagents/models.py:1838](file:///plugin/contrib/smolagents/models.py#L1838) | unused variable | `MODEL_REGISTRY` | 60% |
| [plugin/contrib/smolagents/monitoring.py:22](file:///plugin/contrib/smolagents/monitoring.py#L22) | unused variable | `highlight` | 100% |
| [plugin/contrib/smolagents/monitoring.py:150](file:///plugin/contrib/smolagents/monitoring.py#L150) | unused method | `get_total_token_counts` | 60% |
| [plugin/contrib/smolagents/monitoring.py:182](file:///plugin/contrib/smolagents/monitoring.py#L182) | unused variable | `OFF` | 60% |
| [plugin/contrib/smolagents/monitoring.py:235](file:///plugin/contrib/smolagents/monitoring.py#L235) | unused method | `log_code` | 60% |
| [plugin/contrib/smolagents/monitoring.py:285](file:///plugin/contrib/smolagents/monitoring.py#L285) | unused method | `visualize_agent_tree` | 60% |
| [plugin/contrib/smolagents/serialization.py:348](file:///plugin/contrib/smolagents/serialization.py#L348) | unused method | `_extract_method_body` | 60% |
| [plugin/contrib/smolagents/serialization.py:376](file:///plugin/contrib/smolagents/serialization.py#L376) | unused method | `get_safe_serializer_code` | 60% |
| [plugin/contrib/smolagents/serialization.py:451](file:///plugin/contrib/smolagents/serialization.py#L451) | unused method | `get_deserializer_code` | 60% |
| [plugin/contrib/smolagents/tool_validation.py:19](file:///plugin/contrib/smolagents/tool_validation.py#L19) | unused attribute | `undefined_names` | 60% |
| [plugin/contrib/smolagents/tool_validation.py:73](file:///plugin/contrib/smolagents/tool_validation.py#L73) | unused method | `visit_AnnAssign` | 60% |
| [plugin/contrib/smolagents/tools.py:39](file:///plugin/contrib/smolagents/tools.py#L39) | unused variable | `CommitOperationAdd` | 60% |
| [plugin/contrib/smolagents/tools.py:39](file:///plugin/contrib/smolagents/tools.py#L39) | unused variable | `create_commit` | 60% |
| [plugin/contrib/smolagents/tools.py:39](file:///plugin/contrib/smolagents/tools.py#L39) | unused variable | `create_repo` | 60% |
| [plugin/contrib/smolagents/tools.py:40](file:///plugin/contrib/smolagents/tools.py#L40) | unused variable | `hf_hub_download` | 60% |
| [plugin/contrib/smolagents/tools.py:40](file:///plugin/contrib/smolagents/tools.py#L40) | unused variable | `metadata_update` | 60% |
| [plugin/contrib/smolagents/tools.py:63](file:///plugin/contrib/smolagents/tools.py#L63) | unused import | `mcp` | 90% |
| [plugin/contrib/smolagents/tools.py:94](file:///plugin/contrib/smolagents/tools.py#L94) | unused variable | `CONVERSION_DICT` | 60% |
| [plugin/contrib/smolagents/tools.py:257](file:///plugin/contrib/smolagents/tools.py#L257) | unused method | `to_code_prompt` | 60% |
| [plugin/contrib/smolagents/tools.py:896](file:///plugin/contrib/smolagents/tools.py#L896) | unused function | `add_description` | 60% |
| [plugin/contrib/smolagents/tools.py:1130](file:///plugin/contrib/smolagents/tools.py#L1130) | unused attribute | `__signature__` | 60% |
| [plugin/contrib/smolagents/tools.py:1358](file:///plugin/contrib/smolagents/tools.py#L1358) | unused function | `get_tools_definition_code` | 60% |
| [plugin/contrib/smolagents/utils.py:352](file:///plugin/contrib/smolagents/utils.py#L352) | unused function | `parse_code_blobs` | 60% |
| [plugin/contrib/smolagents/utils.py:594](file:///plugin/contrib/smolagents/utils.py#L594) | unused function | `make_init_file` | 60% |
| [plugin/contrib/smolagents/utils.py:644](file:///plugin/contrib/smolagents/utils.py#L644) | unused function | `create_agent_gradio_app_template` | 60% |
| [plugin/contrib/smolagents/utils.py:665](file:///plugin/contrib/smolagents/utils.py#L665) | unused class | `RateLimiter` | 60% |
| [plugin/contrib/smolagents/utils.py:685](file:///plugin/contrib/smolagents/utils.py#L685) | unused method | `throttle` | 60% |
| [plugin/contrib/smolagents/utils.py:696](file:///plugin/contrib/smolagents/utils.py#L696) | unused class | `Retrying` | 60% |

## `framework` (77 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/framework/ast_stmt_edit.py:32](file:///plugin/framework/ast_stmt_edit.py#L32) | unused function | `is_name_call_expr` | 60% |
| [plugin/framework/ast_stmt_edit.py:89](file:///plugin/framework/ast_stmt_edit.py#L89) | unused function | `iter_matching_statements` | 60% |
| [plugin/framework/ast_stmt_edit.py:95](file:///plugin/framework/ast_stmt_edit.py#L95) | unused function | `iter_matching_expr_statements` | 60% |
| [plugin/framework/ast_stmt_edit.py:268](file:///plugin/framework/ast_stmt_edit.py#L268) | unused function | `remove_expr_statements` | 60% |
| [plugin/framework/client/http_transport.py:242](file:///plugin/framework/client/http_transport.py#L242) | unused attribute | `auto_open` | 60% |
| [plugin/framework/client/http_transport.py:292](file:///plugin/framework/client/http_transport.py#L292) | unused attribute | `auto_open` | 60% |
| [plugin/framework/client/llm_client.py:400](file:///plugin/framework/client/llm_client.py#L400) | unused method | `_get_connection` | 60% |
| [plugin/framework/client/request_controls.py:309](file:///plugin/framework/client/request_controls.py#L309) | unused function | `claim_host_send_slot` | 60% |
| [plugin/framework/client/request_controls.py:319](file:///plugin/framework/client/request_controls.py#L319) | unused function | `remaining_host_gap` | 60% |
| [plugin/framework/config.py:299](file:///plugin/framework/config.py#L299) | unused function | `parse_config_json_text` | 60% |
| [plugin/framework/config.py:1281](file:///plugin/framework/config.py#L1281) | unused function | `set_api_key_for_endpoint` | 60% |
| [plugin/framework/config_schema.py:101](file:///plugin/framework/config_schema.py#L101) | unused function | `get_manifest_modules` | 60% |
| [plugin/framework/config_schema.py:356](file:///plugin/framework/config_schema.py#L356) | unused variable | `chat_max_tokens` | 60% |
| [plugin/framework/config_schema.py:359](file:///plugin/framework/config_schema.py#L359) | unused variable | `chat_compaction_enabled` | 60% |
| [plugin/framework/config_schema.py:360](file:///plugin/framework/config_schema.py#L360) | unused variable | `request_timeout` | 60% |
| [plugin/framework/config_schema.py:362](file:///plugin/framework/config_schema.py#L362) | unused variable | `api_keys_by_endpoint` | 60% |
| [plugin/framework/config_schema.py:363](file:///plugin/framework/config_schema.py#L363) | unused variable | `image_base_size` | 60% |
| [plugin/framework/config_schema.py:364](file:///plugin/framework/config_schema.py#L364) | unused variable | `image_default_aspect` | 60% |
| [plugin/framework/config_schema.py:365](file:///plugin/framework/config_schema.py#L365) | unused variable | `image_steps` | 60% |
| [plugin/framework/config_schema.py:366](file:///plugin/framework/config_schema.py#L366) | unused variable | `image_auto_gallery` | 60% |
| [plugin/framework/config_schema.py:367](file:///plugin/framework/config_schema.py#L367) | unused variable | `image_insert_frame` | 60% |
| [plugin/framework/config_schema.py:370](file:///plugin/framework/config_schema.py#L370) | unused variable | `embedding_provider` | 60% |
| [plugin/framework/config_schema.py:372](file:///plugin/framework/config_schema.py#L372) | unused variable | `enable_agent_log` | 60% |
| [plugin/framework/config_schema.py:375](file:///plugin/framework/config_schema.py#L375) | unused variable | `extension_update_check_epoch` | 60% |
| [plugin/framework/config_schema.py:376](file:///plugin/framework/config_schema.py#L376) | unused variable | `librepy_update_check_epoch` | 60% |
| [plugin/framework/config_schema.py:377](file:///plugin/framework/config_schema.py#L377) | unused variable | `libreharper_update_check_epoch` | 60% |
| [plugin/framework/config_schema.py:379](file:///plugin/framework/config_schema.py#L379) | unused variable | `extend_selection_system_prompt` | 60% |
| [plugin/framework/config_schema.py:380](file:///plugin/framework/config_schema.py#L380) | unused variable | `edit_selection_system_prompt` | 60% |
| [plugin/framework/config_schema.py:381](file:///plugin/framework/config_schema.py#L381) | unused variable | `audio_support_map` | 60% |
| [plugin/framework/config_schema.py:386](file:///plugin/framework/config_schema.py#L386) | unused variable | `vision_support_map` | 60% |
| [plugin/framework/config_schema.py:392](file:///plugin/framework/config_schema.py#L392) | unused variable | `parallel_tool_calls` | 60% |
| [plugin/framework/config_schema.py:395](file:///plugin/framework/config_schema.py#L395) | unused variable | `last_python_script_name_writer` | 60% |
| [plugin/framework/config_schema.py:396](file:///plugin/framework/config_schema.py#L396) | unused variable | `last_python_script_name_calc` | 60% |
| [plugin/framework/config_schema.py:397](file:///plugin/framework/config_schema.py#L397) | unused variable | `last_python_script_name_draw` | 60% |
| [plugin/framework/config_schema.py:404](file:///plugin/framework/config_schema.py#L404) | unused variable | `text_analytics_sentiment_model` | 60% |
| [plugin/framework/config_schema.py:407](file:///plugin/framework/config_schema.py#L407) | unused variable | `last_latex_input` | 60% |
| [plugin/framework/config_schema.py:408](file:///plugin/framework/config_schema.py#L408) | unused variable | `last_latex_display_block` | 60% |
| [plugin/framework/config_schema.py:414](file:///plugin/framework/config_schema.py#L414) | unused variable | `doc_agent_edit_review_mode` | 60% |
| [plugin/framework/config_schema.py:909](file:///plugin/framework/config_schema.py#L909) | unused function | `prune_default_values` | 60% |
| [plugin/framework/constants.py:102](file:///plugin/framework/constants.py#L102) | unused variable | `EMBEDDINGS` | 60% |
| [plugin/framework/constants.py:104](file:///plugin/framework/constants.py#L104) | unused variable | `MODERATIONS` | 60% |
| [plugin/framework/constants.py:105](file:///plugin/framework/constants.py#L105) | unused variable | `REALTIME` | 60% |
| [plugin/framework/constants.py:106](file:///plugin/framework/constants.py#L106) | unused variable | `CODE` | 60% |
| [plugin/framework/deal_shim.py:238](file:///plugin/framework/deal_shim.py#L238) | unused function | `asserts_enabled` | 60% |
| [plugin/framework/frame_session.py:170](file:///plugin/framework/frame_session.py#L170) | unused attribute | `_frame_listener` | 60% |
| [plugin/framework/frame_session.py:316](file:///plugin/framework/frame_session.py#L316) | unused attribute | `_frame_listener` | 60% |
| [plugin/framework/frame_session.py:461](file:///plugin/framework/frame_session.py#L461) | unused attribute | `_frame_listener` | 60% |
| [plugin/framework/grammar_rule_ids.py:254](file:///plugin/framework/grammar_rule_ids.py#L254) | unused variable | `STOPWORDS_ISO_KEY` | 60% |
| [plugin/framework/http_server.py:328](file:///plugin/framework/http_server.py#L328) | unused variable | `daemon_threads` | 60% |
| [plugin/framework/http_server.py:340](file:///plugin/framework/http_server.py#L340) | unused variable | `address_family` | 60% |
| [plugin/framework/http_server.py:455](file:///plugin/framework/http_server.py#L455) | unused attribute | `address_family` | 60% |
| [plugin/framework/http_server.py:501](file:///plugin/framework/http_server.py#L501) | unused method | `drain_executor` | 60% |
| [plugin/framework/logging.py:54](file:///plugin/framework/logging.py#L54) | unused variable | `enable_agent_log` | 60% |
| [plugin/framework/logging.py:55](file:///plugin/framework/logging.py#L55) | unused variable | `init_lock` | 60% |
| [plugin/framework/logging.py:56](file:///plugin/framework/logging.py#L56) | unused variable | `activity` | 60% |
| [plugin/framework/logging.py:57](file:///plugin/framework/logging.py#L57) | unused variable | `activity_lock` | 60% |
| [plugin/framework/logging.py:58](file:///plugin/framework/logging.py#L58) | unused variable | `hung_shown` | 60% |
| [plugin/framework/logging.py:59](file:///plugin/framework/logging.py#L59) | unused variable | `stacks_dumped` | 60% |
| [plugin/framework/process_worker.py:148](file:///plugin/framework/process_worker.py#L148) | unused method | `from_absolute` | 60% |
| [plugin/framework/process_worker.py:339](file:///plugin/framework/process_worker.py#L339) | unused variable | `tasks_executed` | 60% |
| [plugin/framework/process_worker.py:365](file:///plugin/framework/process_worker.py#L365) | unused attribute | `tasks_executed` | 60% |
| [plugin/framework/process_worker.py:563](file:///plugin/framework/process_worker.py#L563) | unused attribute | `tasks_executed` | 60% |
| [plugin/framework/process_worker.py:894](file:///plugin/framework/process_worker.py#L894) | unused attribute | `tasks_executed` | 60% |
| [plugin/framework/prompts.py:252](file:///plugin/framework/prompts.py#L252) | unused variable | `PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE` | 60% |
| [plugin/framework/prompts.py:519](file:///plugin/framework/prompts.py#L519) | unused variable | `DEFAULT_WRITER_GREETING` | 60% |
| [plugin/framework/prompts.py:576](file:///plugin/framework/prompts.py#L576) | unused variable | `CALC_PYTHON_FORMULA_LLM_HINT` | 60% |
| [plugin/framework/prompts.py:589](file:///plugin/framework/prompts.py#L589) | unused variable | `DEFAULT_CALC_GREETING` | 60% |
| [plugin/framework/prompts.py:848](file:///plugin/framework/prompts.py#L848) | unused variable | `DEFAULT_DRAW_GREETING` | 60% |
| [plugin/framework/prompts.py:1191](file:///plugin/framework/prompts.py#L1191) | unused function | `get_chat_system_prompt_for_kind` | 60% |
| [plugin/framework/prompts.py:1345](file:///plugin/framework/prompts.py#L1345) | unused variable | `PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE` | 60% |
| [plugin/framework/prompts.py:1357](file:///plugin/framework/prompts.py#L1357) | unused variable | `CALC_PYTHON_FORMULA_LLM_HINT` | 60% |
| [plugin/framework/tool.py:616](file:///plugin/framework/tool.py#L616) | unused variable | `batch_mode` | 60% |
| [plugin/framework/tool.py:625](file:///plugin/framework/tool.py#L625) | unused attribute | `batch_mode` | 60% |
| [plugin/framework/tool.py:1007](file:///plugin/framework/tool.py#L1007) | unused function | `execute_unguarded` | 60% |
| [plugin/framework/uno_bootstrap.py:47](file:///plugin/framework/uno_bootstrap.py#L47) | unused method | `create_module` | 60% |
| [plugin/framework/uno_bootstrap.py:49](file:///plugin/framework/uno_bootstrap.py#L49) | unused attribute | `__package__` | 60% |
| [plugin/framework/uno_bootstrap.py:69](file:///plugin/framework/uno_bootstrap.py#L69) | unused method | `create_module` | 60% |

## `writer` (73 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/writer/__init__.py:41](file:///plugin/writer/__init__.py#L41) | unused class | `WriterModule` | 60% |
| [plugin/writer/field_restore.py:385](file:///plugin/writer/field_restore.py#L385) | unused attribute | `SearchRegularExpression` | 60% |
| [plugin/writer/field_restore.py:389](file:///plugin/writer/field_restore.py#L389) | unused attribute | `SearchString` | 60% |
| [plugin/writer/format.py:306](file:///plugin/writer/format.py#L306) | unused variable | `direct_formatting` | 60% |
| [plugin/writer/format.py:308](file:///plugin/writer/format.py#L308) | unused variable | `preserved_char_overrides` | 60% |
| [plugin/writer/format.py:309](file:///plugin/writer/format.py#L309) | unused variable | `removed_char_overrides` | 60% |
| [plugin/writer/format.py:310](file:///plugin/writer/format.py#L310) | unused variable | `cleared_char_properties` | 60% |
| [plugin/writer/format.py:311](file:///plugin/writer/format.py#L311) | unused variable | `cleared_paragraph_properties` | 60% |
| [plugin/writer/format.py:313](file:///plugin/writer/format.py#L313) | unused variable | `capture_incomplete` | 60% |
| [plugin/writer/html_import_styles.py:79](file:///plugin/writer/html_import_styles.py#L79) | unused method | `handle_starttag` | 60% |
| [plugin/writer/html_import_styles.py:101](file:///plugin/writer/html_import_styles.py#L101) | unused method | `handle_startendtag` | 60% |
| [plugin/writer/html_import_styles.py:110](file:///plugin/writer/html_import_styles.py#L110) | unused method | `handle_endtag` | 60% |
| [plugin/writer/html_ruby.py:269](file:///plugin/writer/html_ruby.py#L269) | unused variable | `visible_offset` | 60% |
| [plugin/writer/image_placeholders.py:190](file:///plugin/writer/image_placeholders.py#L190) | unused attribute | `SearchString` | 60% |
| [plugin/writer/image_placeholders.py:191](file:///plugin/writer/image_placeholders.py#L191) | unused attribute | `SearchCaseSensitive` | 60% |
| [plugin/writer/images/image_tools.py:661](file:///plugin/writer/images/image_tools.py#L661) | unused function | `replace_image_in_place` | 60% |
| [plugin/writer/inline_review.py:195](file:///plugin/writer/inline_review.py#L195) | unused function | `has_agent_changes` | 60% |
| [plugin/writer/inline_review.py:320](file:///plugin/writer/inline_review.py#L320) | unused function | `agent_changes` | 60% |
| [plugin/writer/inline_review.py:444](file:///plugin/writer/inline_review.py#L444) | unused function | `pending_agent_change_count` | 60% |
| [plugin/writer/inline_review.py:1093](file:///plugin/writer/inline_review.py#L1093) | unused function | `resolve_all_agent_changes` | 60% |
| [plugin/writer/locale/ai_grammar_proofreader.py:402](file:///plugin/writer/locale/ai_grammar_proofreader.py#L402) | unused attribute | `aProperties` | 60% |
| [plugin/writer/locale/ai_grammar_proofreader.py:403](file:///plugin/writer/locale/ai_grammar_proofreader.py#L403) | unused attribute | `xProofreader` | 60% |
| [plugin/writer/locale/ai_grammar_proofreader.py:551](file:///plugin/writer/locale/ai_grammar_proofreader.py#L551) | unused attribute | `nErrorStart` | 60% |
| [plugin/writer/locale/ai_grammar_proofreader.py:552](file:///plugin/writer/locale/ai_grammar_proofreader.py#L552) | unused attribute | `nErrorLength` | 60% |
| [plugin/writer/locale/ai_grammar_proofreader.py:554](file:///plugin/writer/locale/ai_grammar_proofreader.py#L554) | unused attribute | `nErrorStart` | 60% |
| [plugin/writer/locale/ai_grammar_proofreader.py:555](file:///plugin/writer/locale/ai_grammar_proofreader.py#L555) | unused attribute | `nErrorLength` | 60% |
| [plugin/writer/locale/ai_grammar_proofreader.py:556](file:///plugin/writer/locale/ai_grammar_proofreader.py#L556) | unused attribute | `nErrorType` | 60% |
| [plugin/writer/locale/ai_grammar_proofreader.py:558](file:///plugin/writer/locale/ai_grammar_proofreader.py#L558) | unused attribute | `aSuggestions` | 60% |
| [plugin/writer/locale/ai_grammar_proofreader.py:559](file:///plugin/writer/locale/ai_grammar_proofreader.py#L559) | unused attribute | `aShortComment` | 60% |
| [plugin/writer/locale/ai_grammar_proofreader.py:560](file:///plugin/writer/locale/ai_grammar_proofreader.py#L560) | unused attribute | `aFullComment` | 60% |
| [plugin/writer/locale/ai_grammar_proofreader.py:561](file:///plugin/writer/locale/ai_grammar_proofreader.py#L561) | unused attribute | `aProperties` | 60% |
| [plugin/writer/locale/ai_grammar_proofreader.py:865](file:///plugin/writer/locale/ai_grammar_proofreader.py#L865) | unused variable | `aProperties` | 100% |
| [plugin/writer/locale/grammar_persistence.py:862](file:///plugin/writer/locale/grammar_persistence.py#L862) | unused attribute | `SearchCaseSensitive` | 60% |
| [plugin/writer/locale/grammar_work_queue.py:118](file:///plugin/writer/locale/grammar_work_queue.py#L118) | unused function | `deduplicate_grammar_batch` | 60% |
| [plugin/writer/locale/harper.py:489](file:///plugin/writer/locale/harper.py#L489) | unused function | `shutdown_harper_runtime` | 60% |
| [plugin/writer/math/latex_dialog.py:86](file:///plugin/writer/math/latex_dialog.py#L86) | unused attribute | `FontDescriptor` | 60% |
| [plugin/writer/page.py:1823](file:///plugin/writer/page.py#L1823) | unused attribute | `SearchString` | 60% |
| [plugin/writer/page.py:1824](file:///plugin/writer/page.py#L1824) | unused attribute | `SearchRegularExpression` | 60% |
| [plugin/writer/page.py:1825](file:///plugin/writer/page.py#L1825) | unused attribute | `SearchCaseSensitive` | 60% |
| [plugin/writer/search.py:251](file:///plugin/writer/search.py#L251) | unused attribute | `SearchString` | 60% |
| [plugin/writer/search.py:252](file:///plugin/writer/search.py#L252) | unused attribute | `SearchRegularExpression` | 60% |
| [plugin/writer/search.py:253](file:///plugin/writer/search.py#L253) | unused attribute | `SearchCaseSensitive` | 60% |
| [plugin/writer/search.py:341](file:///plugin/writer/search.py#L341) | unused attribute | `SearchRegularExpression` | 60% |
| [plugin/writer/search.py:342](file:///plugin/writer/search.py#L342) | unused attribute | `SearchString` | 60% |
| [plugin/writer/search.py:346](file:///plugin/writer/search.py#L346) | unused attribute | `SearchCaseSensitive` | 60% |
| [plugin/writer/search.py:354](file:///plugin/writer/search.py#L354) | unused attribute | `SearchCaseSensitive` | 60% |
| [plugin/writer/search.py:374](file:///plugin/writer/search.py#L374) | unused attribute | `SearchRegularExpression` | 60% |
| [plugin/writer/search.py:375](file:///plugin/writer/search.py#L375) | unused attribute | `SearchString` | 60% |
| [plugin/writer/search.py:378](file:///plugin/writer/search.py#L378) | unused attribute | `SearchCaseSensitive` | 60% |
| [plugin/writer/search.py:575](file:///plugin/writer/search.py#L575) | unused attribute | `SearchRegularExpression` | 60% |
| [plugin/writer/search.py:576](file:///plugin/writer/search.py#L576) | unused attribute | `SearchString` | 60% |
| [plugin/writer/search.py:577](file:///plugin/writer/search.py#L577) | unused attribute | `SearchCaseSensitive` | 60% |
| [plugin/writer/search.py:881](file:///plugin/writer/search.py#L881) | unused attribute | `SearchString` | 60% |
| [plugin/writer/search.py:882](file:///plugin/writer/search.py#L882) | unused attribute | `SearchRegularExpression` | 60% |
| [plugin/writer/search.py:883](file:///plugin/writer/search.py#L883) | unused attribute | `SearchCaseSensitive` | 60% |
| [plugin/writer/search.py:912](file:///plugin/writer/search.py#L912) | unused function | `all_start_indices` | 60% |
| [plugin/writer/search.py:1024](file:///plugin/writer/search.py#L1024) | unused attribute | `SearchString` | 60% |
| [plugin/writer/search.py:1025](file:///plugin/writer/search.py#L1025) | unused attribute | `SearchRegularExpression` | 60% |
| [plugin/writer/search.py:1026](file:///plugin/writer/search.py#L1026) | unused attribute | `SearchCaseSensitive` | 60% |
| [plugin/writer/specialized/comments.py:637](file:///plugin/writer/specialized/comments.py#L637) | unused attribute | `Seconds` | 60% |
| [plugin/writer/specialized/footnotes.py:32](file:///plugin/writer/specialized/footnotes.py#L32) | unused attribute | `SearchString` | 60% |
| [plugin/writer/specialized/footnotes.py:33](file:///plugin/writer/specialized/footnotes.py#L33) | unused attribute | `SearchRegularExpression` | 60% |
| [plugin/writer/specialized/footnotes.py:34](file:///plugin/writer/specialized/footnotes.py#L34) | unused attribute | `SearchCaseSensitive` | 60% |
| [plugin/writer/specialized/indexes.py:480](file:///plugin/writer/specialized/indexes.py#L480) | unused variable | `fallback_message` | 100% |
| [plugin/writer/xhtml_style_postprocess.py:186](file:///plugin/writer/xhtml_style_postprocess.py#L186) | unused function | `extract_autostyle_parents_from_fodt` | 60% |
| [plugin/writer/xhtml_style_postprocess.py:199](file:///plugin/writer/xhtml_style_postprocess.py#L199) | unused function | `extract_autostyle_overrides_from_fodt` | 60% |
| [plugin/writer/xhtml_style_postprocess.py:274](file:///plugin/writer/xhtml_style_postprocess.py#L274) | unused method | `handle_data` | 60% |
| [plugin/writer/xhtml_style_postprocess.py:278](file:///plugin/writer/xhtml_style_postprocess.py#L278) | unused method | `handle_entityref` | 60% |
| [plugin/writer/xhtml_style_postprocess.py:282](file:///plugin/writer/xhtml_style_postprocess.py#L282) | unused method | `handle_charref` | 60% |
| [plugin/writer/xhtml_style_postprocess.py:286](file:///plugin/writer/xhtml_style_postprocess.py#L286) | unused method | `handle_comment` | 60% |
| [plugin/writer/xhtml_style_postprocess.py:413](file:///plugin/writer/xhtml_style_postprocess.py#L413) | unused method | `handle_starttag` | 60% |
| [plugin/writer/xhtml_style_postprocess.py:430](file:///plugin/writer/xhtml_style_postprocess.py#L430) | unused method | `handle_startendtag` | 60% |
| [plugin/writer/xhtml_style_postprocess.py:434](file:///plugin/writer/xhtml_style_postprocess.py#L434) | unused method | `handle_endtag` | 60% |

## `chatbot` (67 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/chatbot/__init__.py:38](file:///plugin/chatbot/__init__.py#L38) | unused class | `ChatbotModule` | 60% |
| [plugin/chatbot/agent_manual.py:150](file:///plugin/chatbot/agent_manual.py#L150) | unused variable | `MANUAL_SECTIONS` | 60% |
| [plugin/chatbot/agent_manual.py:255](file:///plugin/chatbot/agent_manual.py#L255) | unused function | `full_manual_for_model` | 60% |
| [plugin/chatbot/audio_recorder.py:408](file:///plugin/chatbot/audio_recorder.py#L408) | unused variable | `time_info` | 100% |
| [plugin/chatbot/bug_report.py:173](file:///plugin/chatbot/bug_report.py#L173) | unused function | `collect_environment_block` | 60% |
| [plugin/chatbot/chat_sidebar_mode.py:37](file:///plugin/chatbot/chat_sidebar_mode.py#L37) | unused variable | `include_brainstorming` | 60% |
| [plugin/chatbot/chat_sidebar_mode.py:38](file:///plugin/chatbot/chat_sidebar_mode.py#L38) | unused variable | `include_writing_plan` | 60% |
| [plugin/chatbot/chat_sidebar_mode.py:39](file:///plugin/chatbot/chat_sidebar_mode.py#L39) | unused variable | `include_ppt_master` | 60% |
| [plugin/chatbot/compaction.py:192](file:///plugin/chatbot/compaction.py#L192) | unused variable | `compacted` | 60% |
| [plugin/chatbot/compaction.py:601](file:///plugin/chatbot/compaction.py#L601) | unused function | `serialize_for_summary` | 60% |
| [plugin/chatbot/config_ui_helpers.py:537](file:///plugin/chatbot/config_ui_helpers.py#L537) | unused function | `get_signup_url_for_endpoint` | 60% |
| [plugin/chatbot/dialog_views.py:91](file:///plugin/chatbot/dialog_views.py#L91) | unused attribute | `ImagePosition` | 60% |
| [plugin/chatbot/dialogs.py:433](file:///plugin/chatbot/dialogs.py#L433) | unused attribute | `HumanPresentableName` | 60% |
| [plugin/chatbot/dialogs.py:487](file:///plugin/chatbot/dialogs.py#L487) | unused function | `msgbox_with_copy` | 60% |
| [plugin/chatbot/dialogs.py:713](file:///plugin/chatbot/dialogs.py#L713) | unused function | `load_module_dialog` | 60% |
| [plugin/chatbot/dialogs.py:726](file:///plugin/chatbot/dialogs.py#L726) | unused function | `load_framework_dialog` | 60% |
| [plugin/chatbot/dialogs.py:1086](file:///plugin/chatbot/dialogs.py#L1086) | unused attribute | `Step` | 60% |
| [plugin/chatbot/extension_update_check.py:110](file:///plugin/chatbot/extension_update_check.py#L110) | unused function | `remote_is_newer` | 60% |
| [plugin/chatbot/panel_factory.py:269](file:///plugin/chatbot/panel_factory.py#L269) | unused variable | `ParentAccessible` | 100% |
| [plugin/chatbot/panel_resize.py:374](file:///plugin/chatbot/panel_resize.py#L374) | unused attribute | `_width_negotiated` | 60% |
| [plugin/chatbot/panel_resize.py:420](file:///plugin/chatbot/panel_resize.py#L420) | unused attribute | `_width_negotiated` | 60% |
| [plugin/chatbot/quick_setup.py:54](file:///plugin/chatbot/quick_setup.py#L54) | unused function | `probe_local_servers` | 60% |
| [plugin/chatbot/record_gesture.py:38](file:///plugin/chatbot/record_gesture.py#L38) | unused variable | `HANDS_FREE_STATUS` | 60% |
| [plugin/chatbot/rich_text.py:116](file:///plugin/chatbot/rich_text.py#L116) | unused attribute | `ParaLeftMargin` | 60% |
| [plugin/chatbot/rich_text.py:117](file:///plugin/chatbot/rich_text.py#L117) | unused attribute | `ParaRightMargin` | 60% |
| [plugin/chatbot/rich_text.py:118](file:///plugin/chatbot/rich_text.py#L118) | unused attribute | `ParaFirstLineIndent` | 60% |
| [plugin/chatbot/rich_text.py:119](file:///plugin/chatbot/rich_text.py#L119) | unused attribute | `ParaTopMargin` | 60% |
| [plugin/chatbot/rich_text.py:120](file:///plugin/chatbot/rich_text.py#L120) | unused attribute | `ParaBottomMargin` | 60% |
| [plugin/chatbot/rich_text.py:125](file:///plugin/chatbot/rich_text.py#L125) | unused attribute | `CharLocale` | 60% |
| [plugin/chatbot/rich_text.py:126](file:///plugin/chatbot/rich_text.py#L126) | unused attribute | `CharLocaleAsian` | 60% |
| [plugin/chatbot/rich_text.py:127](file:///plugin/chatbot/rich_text.py#L127) | unused attribute | `CharLocaleComplex` | 60% |
| [plugin/chatbot/rich_text.py:142](file:///plugin/chatbot/rich_text.py#L142) | unused variable | `USER_COLOR` | 60% |
| [plugin/chatbot/rich_text.py:143](file:///plugin/chatbot/rich_text.py#L143) | unused variable | `ASSISTANT_COLOR` | 60% |
| [plugin/chatbot/rich_text.py:227](file:///plugin/chatbot/rich_text.py#L227) | unused attribute | `NumberingRules` | 60% |
| [plugin/chatbot/rich_text_control.py:714](file:///plugin/chatbot/rich_text_control.py#L714) | unused attribute | `Weight` | 60% |
| [plugin/chatbot/rich_text_control.py:1364](file:///plugin/chatbot/rich_text_control.py#L1364) | unused attribute | `CharBackColor` | 60% |
| [plugin/chatbot/rich_text_paste.py:213](file:///plugin/chatbot/rich_text_paste.py#L213) | unused attribute | `ParaTopMargin` | 60% |
| [plugin/chatbot/rich_text_paste.py:214](file:///plugin/chatbot/rich_text_paste.py#L214) | unused attribute | `ParaBottomMargin` | 60% |
| [plugin/chatbot/rich_text_paste.py:234](file:///plugin/chatbot/rich_text_paste.py#L234) | unused attribute | `ParaTabStops` | 60% |
| [plugin/chatbot/rich_text_paste.py:271](file:///plugin/chatbot/rich_text_paste.py#L271) | unused attribute | `CharFontNameAsian` | 60% |
| [plugin/chatbot/rich_text_paste.py:272](file:///plugin/chatbot/rich_text_paste.py#L272) | unused attribute | `CharFontNameComplex` | 60% |
| [plugin/chatbot/rich_text_paste.py:369](file:///plugin/chatbot/rich_text_paste.py#L369) | unused attribute | `CharBackColor` | 60% |
| [plugin/chatbot/send_handlers.py:487](file:///plugin/chatbot/send_handlers.py#L487) | unused variable | `worker_q` | 100% |
| [plugin/chatbot/skills.py:115](file:///plugin/chatbot/skills.py#L115) | unused method | `get_humanizer_skill_path` | 60% |
| [plugin/chatbot/slash_popup.py:346](file:///plugin/chatbot/slash_popup.py#L346) | unused function | `uses_toolkit_overlay` | 60% |
| [plugin/chatbot/slash_popup.py:456](file:///plugin/chatbot/slash_popup.py#L456) | unused attribute | `WindowServiceName` | 60% |
| [plugin/chatbot/slash_popup.py:457](file:///plugin/chatbot/slash_popup.py#L457) | unused attribute | `Parent` | 60% |
| [plugin/chatbot/slash_popup.py:458](file:///plugin/chatbot/slash_popup.py#L458) | unused attribute | `ParentIndex` | 60% |
| [plugin/chatbot/slash_popup.py:459](file:///plugin/chatbot/slash_popup.py#L459) | unused attribute | `Bounds` | 60% |
| [plugin/chatbot/slash_popup.py:460](file:///plugin/chatbot/slash_popup.py#L460) | unused attribute | `WindowAttributes` | 60% |
| [plugin/chatbot/slash_popup.py:473](file:///plugin/chatbot/slash_popup.py#L473) | unused attribute | `WindowServiceName` | 60% |
| [plugin/chatbot/slash_popup.py:474](file:///plugin/chatbot/slash_popup.py#L474) | unused attribute | `Parent` | 60% |
| [plugin/chatbot/slash_popup.py:475](file:///plugin/chatbot/slash_popup.py#L475) | unused attribute | `ParentIndex` | 60% |
| [plugin/chatbot/slash_popup.py:476](file:///plugin/chatbot/slash_popup.py#L476) | unused attribute | `Bounds` | 60% |
| [plugin/chatbot/slash_popup.py:477](file:///plugin/chatbot/slash_popup.py#L477) | unused attribute | `WindowAttributes` | 60% |
| [plugin/chatbot/slash_popup.py:556](file:///plugin/chatbot/slash_popup.py#L556) | unused attribute | `_frame_controller` | 60% |
| [plugin/chatbot/slash_popup.py:558](file:///plugin/chatbot/slash_popup.py#L558) | unused attribute | `_toolkit` | 60% |
| [plugin/chatbot/slash_popup.py:559](file:///plugin/chatbot/slash_popup.py#L559) | unused attribute | `_ignore_item` | 60% |
| [plugin/chatbot/slash_popup.py:991](file:///plugin/chatbot/slash_popup.py#L991) | unused attribute | `_ignore_item` | 60% |
| [plugin/chatbot/slash_popup.py:996](file:///plugin/chatbot/slash_popup.py#L996) | unused attribute | `_ignore_item` | 60% |
| [plugin/chatbot/slash_popup.py:1020](file:///plugin/chatbot/slash_popup.py#L1020) | unused attribute | `_frame_controller` | 60% |
| [plugin/chatbot/slash_popup.py:1061](file:///plugin/chatbot/slash_popup.py#L1061) | unused attribute | `_toolkit` | 60% |
| [plugin/chatbot/state_machine.py:12](file:///plugin/chatbot/state_machine.py#L12) | unused variable | `SendHandlerKind` | 60% |
| [plugin/chatbot/state_machine.py:13](file:///plugin/chatbot/state_machine.py#L13) | unused variable | `SendHandlerFsmStatus` | 60% |
| [plugin/chatbot/state_machine.py:15](file:///plugin/chatbot/state_machine.py#L15) | unused variable | `SendHandlerCompleteStatus` | 60% |
| [plugin/chatbot/tool_loop.py:177](file:///plugin/chatbot/tool_loop.py#L177) | unused attribute | `_stop_partial_text` | 60% |
| [plugin/chatbot/tool_loop_actions.py:128](file:///plugin/chatbot/tool_loop_actions.py#L128) | unused attribute | `_stop_partial_text` | 60% |

## `scripting` (65 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/scripting/analysis.py:89](file:///plugin/scripting/analysis.py#L89) | unused variable | `get_analysis_script_templates` | 60% |
| [plugin/scripting/analysis.py:90](file:///plugin/scripting/analysis.py#L90) | unused variable | `parse_analysis_script_header` | 60% |
| [plugin/scripting/duckdb_sql.py:117](file:///plugin/scripting/duckdb_sql.py#L117) | unused function | `get_sql_script_templates` | 60% |
| [plugin/scripting/duckdb_sql.py:126](file:///plugin/scripting/duckdb_sql.py#L126) | unused function | `parse_sql_script_header` | 60% |
| [plugin/scripting/duckdb_sql.py:166](file:///plugin/scripting/duckdb_sql.py#L166) | unused function | `is_sql_result` | 60% |
| [plugin/scripting/duckdb_sql.py:182](file:///plugin/scripting/duckdb_sql.py#L182) | unused function | `insert_sql_result_into_calc` | 60% |
| [plugin/scripting/editor_host.py:283](file:///plugin/scripting/editor_host.py#L283) | unused attribute | `_reader_thread` | 60% |
| [plugin/scripting/editor_host.py:369](file:///plugin/scripting/editor_host.py#L369) | unused attribute | `_reader_thread` | 60% |
| [plugin/scripting/editor_host.py:908](file:///plugin/scripting/editor_host.py#L908) | unused attribute | `_on_closed` | 60% |
| [plugin/scripting/forecast.py:83](file:///plugin/scripting/forecast.py#L83) | unused variable | `parse_forecast_script_header` | 60% |
| [plugin/scripting/forecast.py:84](file:///plugin/scripting/forecast.py#L84) | unused variable | `get_forecast_script_templates` | 60% |
| [plugin/scripting/forecast.py:85](file:///plugin/scripting/forecast.py#L85) | unused variable | `get_forecast_template` | 60% |
| [plugin/scripting/forecast.py:115](file:///plugin/scripting/forecast.py#L115) | unused function | `is_forecast_result` | 60% |
| [plugin/scripting/helper_domain.py:913](file:///plugin/scripting/helper_domain.py#L913) | unused function | `supports_calc_or_writer_manual` | 60% |
| [plugin/scripting/import_policy.py:133](file:///plugin/scripting/import_policy.py#L133) | unused function | `venv_blocked_modules` | 60% |
| [plugin/scripting/ipc.py:617](file:///plugin/scripting/ipc.py#L617) | unused function | `write_pickle_frame_with_timeout` | 60% |
| [plugin/scripting/optimize.py:66](file:///plugin/scripting/optimize.py#L66) | unused variable | `parse_optimize_script_header` | 60% |
| [plugin/scripting/optimize.py:67](file:///plugin/scripting/optimize.py#L67) | unused variable | `get_optimize_script_templates` | 60% |
| [plugin/scripting/optimize.py:68](file:///plugin/scripting/optimize.py#L68) | unused variable | `get_optimize_template` | 60% |
| [plugin/scripting/optimize.py:102](file:///plugin/scripting/optimize.py#L102) | unused function | `is_optimize_result` | 60% |
| [plugin/scripting/payload_codec.py:421](file:///plugin/scripting/payload_codec.py#L421) | unused variable | `MAX_BENCH_CELLS` | 60% |
| [plugin/scripting/payload_codec.py:797](file:///plugin/scripting/payload_codec.py#L797) | unused function | `column_kinds_for_grid` | 60% |
| [plugin/scripting/payload_codec.py:1029](file:///plugin/scripting/payload_codec.py#L1029) | unused function | `binary_envelope_skip_reason` | 60% |
| [plugin/scripting/payload_codec.py:1071](file:///plugin/scripting/payload_codec.py#L1071) | unused function | `is_numeric_coercible` | 60% |
| [plugin/scripting/payload_codec.py:1767](file:///plugin/scripting/payload_codec.py#L1767) | unused function | `host_pack_multi_data` | 60% |
| [plugin/scripting/python_runner.py:542](file:///plugin/scripting/python_runner.py#L542) | unused function | `execute_and_insert_result` | 60% |
| [plugin/scripting/quant.py:57](file:///plugin/scripting/quant.py#L57) | unused variable | `parse_quant_script_header` | 60% |
| [plugin/scripting/quant.py:58](file:///plugin/scripting/quant.py#L58) | unused variable | `get_quant_script_templates` | 60% |
| [plugin/scripting/quant.py:59](file:///plugin/scripting/quant.py#L59) | unused variable | `get_quant_template` | 60% |
| [plugin/scripting/sandbox.py:751](file:///plugin/scripting/sandbox.py#L751) | unused function | `_reset_cache` | 60% |
| [plugin/scripting/sandbox.py:1036](file:///plugin/scripting/sandbox.py#L1036) | unused function | `is_safe_workspace_path` | 60% |
| [plugin/scripting/symbolic.py:66](file:///plugin/scripting/symbolic.py#L66) | unused variable | `get_math_script_templates` | 60% |
| [plugin/scripting/symbolic.py:117](file:///plugin/scripting/symbolic.py#L117) | unused function | `is_symbolic_result` | 60% |
| [plugin/scripting/text_analytics.py:55](file:///plugin/scripting/text_analytics.py#L55) | unused variable | `TEXT_ANALYTICS_HEADER_PREFIX` | 60% |
| [plugin/scripting/text_analytics.py:75](file:///plugin/scripting/text_analytics.py#L75) | unused variable | `get_text_analytics_script_templates` | 60% |
| [plugin/scripting/text_analytics.py:80](file:///plugin/scripting/text_analytics.py#L80) | unused function | `supports_text_analytics_manual` | 60% |
| [plugin/scripting/text_analytics.py:295](file:///plugin/scripting/text_analytics.py#L295) | unused function | `is_text_analytics_result` | 60% |
| [plugin/scripting/units.py:69](file:///plugin/scripting/units.py#L69) | unused variable | `get_units_script_templates` | 60% |
| [plugin/scripting/units.py:98](file:///plugin/scripting/units.py#L98) | unused function | `extract_units_insert_kwargs` | 60% |
| [plugin/scripting/units.py:112](file:///plugin/scripting/units.py#L112) | unused function | `is_units_result` | 60% |
| [plugin/scripting/units.py:204](file:///plugin/scripting/units.py#L204) | unused function | `insert_units_result_into_doc` | 60% |
| [plugin/scripting/venv/analysis.py:518](file:///plugin/scripting/venv/analysis.py#L518) | unused function | `quick_stats` | 60% |
| [plugin/scripting/venv/audio_recorder.py:92](file:///plugin/scripting/venv/audio_recorder.py#L92) | unused variable | `time_info` | 100% |
| [plugin/scripting/venv/calc_functions_a_c.py:134](file:///plugin/scripting/venv/calc_functions_a_c.py#L134) | unused variable | `first_interest` | 100% |
| [plugin/scripting/venv/calc_functions_a_c.py:140](file:///plugin/scripting/venv/calc_functions_a_c.py#L140) | unused variable | `calc_method` | 100% |
| [plugin/scripting/venv/coerce.py:999](file:///plugin/scripting/venv/coerce.py#L999) | unused function | `records_from_df` | 60% |
| [plugin/scripting/venv/editor_main.py:328](file:///plugin/scripting/venv/editor_main.py#L328) | unused method | `poll_messages` | 60% |
| [plugin/scripting/venv/editor_main.py:349](file:///plugin/scripting/venv/editor_main.py#L349) | unused method | `is_jedi_available` | 60% |
| [plugin/scripting/venv/editor_main.py:424](file:///plugin/scripting/venv/editor_main.py#L424) | unused method | `notify_dirty` | 60% |
| [plugin/scripting/venv/editor_main.py:453](file:///plugin/scripting/venv/editor_main.py#L453) | unused method | `notify_run` | 60% |
| [plugin/scripting/venv/editor_main.py:456](file:///plugin/scripting/venv/editor_main.py#L456) | unused method | `notify_save_script` | 60% |
| [plugin/scripting/venv/editor_main.py:462](file:///plugin/scripting/venv/editor_main.py#L462) | unused method | `request_scripts` | 60% |
| [plugin/scripting/venv/editor_main.py:465](file:///plugin/scripting/venv/editor_main.py#L465) | unused method | `save_script` | 60% |
| [plugin/scripting/venv/editor_main.py:468](file:///plugin/scripting/venv/editor_main.py#L468) | unused method | `delete_script` | 60% |
| [plugin/scripting/venv/editor_main.py:471](file:///plugin/scripting/venv/editor_main.py#L471) | unused method | `select_script` | 60% |
| [plugin/scripting/venv/editor_main.py:474](file:///plugin/scripting/venv/editor_main.py#L474) | unused method | `notify_cancel` | 60% |
| [plugin/scripting/venv/symbolic.py:254](file:///plugin/scripting/venv/symbolic.py#L254) | unused function | `symbolic_simplify` | 60% |
| [plugin/scripting/venv/symbolic.py:263](file:///plugin/scripting/venv/symbolic.py#L263) | unused function | `differentiate` | 60% |
| [plugin/scripting/venv/symbolic.py:312](file:///plugin/scripting/venv/symbolic.py#L312) | unused function | `solve_equation` | 60% |
| [plugin/scripting/venv/symbolic.py:376](file:///plugin/scripting/venv/symbolic.py#L376) | unused function | `latex_to_math_object` | 60% |
| [plugin/scripting/venv_diagnostics.py:862](file:///plugin/scripting/venv_diagnostics.py#L862) | unused function | `_format_self_check_success` | 60% |
| [plugin/scripting/venv_worker.py:516](file:///plugin/scripting/venv_worker.py#L516) | unused method | `pool_is_running` | 60% |
| [plugin/scripting/viz.py:66](file:///plugin/scripting/viz.py#L66) | unused variable | `get_viz_script_templates` | 60% |
| [plugin/scripting/viz.py:67](file:///plugin/scripting/viz.py#L67) | unused variable | `parse_viz_script_header` | 60% |
| [plugin/scripting/viz.py:98](file:///plugin/scripting/viz.py#L98) | unused function | `is_viz_result` | 60% |

## `calc` (31 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/calc/addin_common.py:103](file:///plugin/calc/addin_common.py#L103) | unused variable | `xSomething` | 100% |
| [plugin/calc/error_detector.py:124](file:///plugin/calc/error_detector.py#L124) | unused function | `get_calc_error_name` | 60% |
| [plugin/calc/excel_py_convert/models.py:25](file:///plugin/calc/excel_py_convert/models.py#L25) | unused variable | `DepRole` | 60% |
| [plugin/calc/excel_py_convert/models.py:28](file:///plugin/calc/excel_py_convert/models.py#L28) | unused variable | `EXCEL_DEP_TOKEN_FIDELITY` | 60% |
| [plugin/calc/excel_py_convert/models.py:50](file:///plugin/calc/excel_py_convert/models.py#L50) | unused variable | `formula_raw` | 60% |
| [plugin/calc/excel_py_convert/models.py:155](file:///plugin/calc/excel_py_convert/models.py#L155) | unused variable | `dag_formula` | 60% |
| [plugin/calc/excel_py_convert/models.py:156](file:///plugin/calc/excel_py_convert/models.py#L156) | unused variable | `excel_formula` | 60% |
| [plugin/calc/excel_py_convert/ooxml_util.py:250](file:///plugin/calc/excel_py_convert/ooxml_util.py#L250) | unused variable | `temp_suffix` | 100% |
| [plugin/calc/excel_py_convert/to_dag.py:372](file:///plugin/calc/excel_py_convert/to_dag.py#L372) | unused function | `rewrite_excel_code` | 60% |
| [plugin/calc/excel_py_convert/to_dag.py:578](file:///plugin/calc/excel_py_convert/to_dag.py#L578) | unused attribute | `dag_formula` | 60% |
| [plugin/calc/excel_py_convert/to_excel.py:379](file:///plugin/calc/excel_py_convert/to_excel.py#L379) | unused attribute | `excel_formula` | 60% |
| [plugin/calc/excel_py_convert/to_excel.py:383](file:///plugin/calc/excel_py_convert/to_excel.py#L383) | unused function | `expand_placeholders_to_literals` | 60% |
| [plugin/calc/excel_py_convert/xlsx_write.py:220](file:///plugin/calc/excel_py_convert/xlsx_write.py#L220) | unused function | `_strip_python_in_excel_parts` | 60% |
| [plugin/calc/manipulator.py:330](file:///plugin/calc/manipulator.py#L330) | unused attribute | `OuterLineWidth` | 60% |
| [plugin/calc/manipulator.py:500](file:///plugin/calc/manipulator.py#L500) | unused attribute | `IsAscending` | 60% |
| [plugin/calc/manipulator.py:501](file:///plugin/calc/manipulator.py#L501) | unused attribute | `IsCaseSensitive` | 60% |
| [plugin/calc/python/cell_discovery.py:75](file:///plugin/calc/python/cell_discovery.py#L75) | unused function | `extract_code_from_formula` | 60% |
| [plugin/calc/python/collabora_formula.py:78](file:///plugin/calc/python/collabora_formula.py#L78) | unused function | `is_collabora_py_formula` | 60% |
| [plugin/calc/python/formula_edit.py:346](file:///plugin/calc/python/formula_edit.py#L346) | unused function | `inline_py_code_has_lexer_collisions` | 60% |
| [plugin/calc/python/geometric_recalc_core.py:163](file:///plugin/calc/python/geometric_recalc_core.py#L163) | unused variable | `n_found` | 100% |
| [plugin/calc/python/xl_static_rewrite.py:127](file:///plugin/calc/python/xl_static_rewrite.py#L127) | unused function | `migrate_bare_data_to_index0` | 60% |
| [plugin/calc/quant_egress.py:22](file:///plugin/calc/quant_egress.py#L22) | unused function | `is_quant_result` | 60% |
| [plugin/calc/quant_egress.py:62](file:///plugin/calc/quant_egress.py#L62) | unused function | `insert_quant_result_into_calc` | 60% |
| [plugin/calc/search.py:107](file:///plugin/calc/search.py#L107) | unused attribute | `ReplaceString` | 60% |
| [plugin/calc/spreadsheet_import/graph.py:147](file:///plugin/calc/spreadsheet_import/graph.py#L147) | unused attribute | `formula_order` | 60% |
| [plugin/calc/spreadsheet_import/models.py:40](file:///plugin/calc/spreadsheet_import/models.py#L40) | unused variable | `formula_order` | 60% |
| [plugin/calc/spreadsheet_import/models.py:52](file:///plugin/calc/spreadsheet_import/models.py#L52) | unused variable | `original_formula` | 60% |
| [plugin/calc/spreadsheet_search.py:41](file:///plugin/calc/spreadsheet_search.py#L41) | unused attribute | `SearchString` | 60% |
| [plugin/calc/spreadsheet_search.py:42](file:///plugin/calc/spreadsheet_search.py#L42) | unused attribute | `SearchRegularExpression` | 60% |
| [plugin/calc/spreadsheet_search.py:43](file:///plugin/calc/spreadsheet_search.py#L43) | unused attribute | `SearchCaseSensitive` | 60% |
| [plugin/calc/vision_egress.py:107](file:///plugin/calc/vision_egress.py#L107) | unused function | `format_vision_structure_for_calc` | 60% |

## `mcp` (23 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/mcp/__init__.py:111](file:///plugin/mcp/__init__.py#L111) | unused class | `McpModule` | 60% |
| [plugin/mcp/__init__.py:597](file:///plugin/mcp/__init__.py#L597) | unused attribute | `Visible` | 60% |
| [plugin/mcp/__init__.py:601](file:///plugin/mcp/__init__.py#L601) | unused attribute | `Visible` | 60% |
| [plugin/mcp/__init__.py:615](file:///plugin/mcp/__init__.py#L615) | unused attribute | `Visible` | 60% |
| [plugin/mcp/__init__.py:619](file:///plugin/mcp/__init__.py#L619) | unused attribute | `Visible` | 60% |
| [plugin/mcp/cors.py:218](file:///plugin/mcp/cors.py#L218) | unused function | `get_extra_allowed_origins` | 60% |
| [plugin/mcp/mcp_protocol.py:444](file:///plugin/mcp/mcp_protocol.py#L444) | unused method | `handle_mcp_sse` | 60% |
| [plugin/mcp/mcp_protocol.py:448](file:///plugin/mcp/mcp_protocol.py#L448) | unused method | `handle_mcp_delete` | 60% |
| [plugin/mcp/mcp_protocol.py:464](file:///plugin/mcp/mcp_protocol.py#L464) | unused method | `handle_sse_stream` | 60% |
| [plugin/mcp/mcp_protocol.py:546](file:///plugin/mcp/mcp_protocol.py#L546) | unused method | `handle_sse_post` | 60% |
| [plugin/mcp/mcp_protocol.py:575](file:///plugin/mcp/mcp_protocol.py#L575) | unused method | `handle_debug_info` | 60% |
| [plugin/mcp/mcp_protocol.py:606](file:///plugin/mcp/mcp_protocol.py#L606) | unused method | `handle_debug_post` | 60% |
| [plugin/mcp/mcp_protocol.py:1243](file:///plugin/mcp/mcp_protocol.py#L1243) | unused method | `_execute_tool_on_main` | 60% |
| [plugin/mcp/mcp_session.py:110](file:///plugin/mcp/mcp_session.py#L110) | unused attribute | `close_connection` | 60% |
| [plugin/mcp/server.py:147](file:///plugin/mcp/server.py#L147) | unused attribute | `close_connection` | 60% |
| [plugin/mcp/server.py:367](file:///plugin/mcp/server.py#L367) | unused attribute | `close_connection` | 60% |
| [plugin/mcp/server.py:369](file:///plugin/mcp/server.py#L369) | unused method | `do_GET` | 60% |
| [plugin/mcp/server.py:372](file:///plugin/mcp/server.py#L372) | unused method | `do_POST` | 60% |
| [plugin/mcp/server.py:375](file:///plugin/mcp/server.py#L375) | unused method | `do_DELETE` | 60% |
| [plugin/mcp/server.py:378](file:///plugin/mcp/server.py#L378) | unused method | `do_OPTIONS` | 60% |
| [plugin/mcp/tunnel.py:178](file:///plugin/mcp/tunnel.py#L178) | unused variable | `authtoken` | 100% |
| [plugin/mcp/wire_types.py:32](file:///plugin/mcp/wire_types.py#L32) | unused variable | `PARSE_ERROR` | 60% |
| [plugin/mcp/wire_types.py:334](file:///plugin/mcp/wire_types.py#L334) | unused function | `progress_notification` | 60% |

## `embeddings` (18 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/embeddings/__init__.py:10](file:///plugin/embeddings/__init__.py#L10) | unused class | `EmbeddingsModule` | 60% |
| [plugin/embeddings/embeddings_cache.py:202](file:///plugin/embeddings/embeddings_cache.py#L202) | unused function | `get_file_index_state` | 60% |
| [plugin/embeddings/embeddings_fs.py:56](file:///plugin/embeddings/embeddings_fs.py#L56) | unused variable | `doc_path` | 60% |
| [plugin/embeddings/embeddings_fs.py:306](file:///plugin/embeddings/embeddings_fs.py#L306) | unused function | `paragraph_chunks_from_path` | 60% |
| [plugin/embeddings/embeddings_locale.py:23](file:///plugin/embeddings/embeddings_locale.py#L23) | unused variable | `OFFICE_NS` | 60% |
| [plugin/embeddings/search_ui.py:97](file:///plugin/embeddings/search_ui.py#L97) | unused attribute | `_hb_start` | 60% |
| [plugin/embeddings/venv/embeddings_index_dispatch.py:13](file:///plugin/embeddings/venv/embeddings_index_dispatch.py#L13) | unused function | `dispatch_trusted` | 60% |
| [plugin/embeddings/venv/embeddings_ingest_graph.py:43](file:///plugin/embeddings/venv/embeddings_ingest_graph.py#L43) | unused variable | `documents` | 60% |
| [plugin/embeddings/venv/embeddings_llama_index.py:103](file:///plugin/embeddings/venv/embeddings_llama_index.py#L103) | unused variable | `node_ids` | 60% |
| [plugin/embeddings/venv/embeddings_llama_index.py:272](file:///plugin/embeddings/venv/embeddings_llama_index.py#L272) | unused method | `_aget_query_embedding` | 60% |
| [plugin/embeddings/venv/embeddings_llama_index.py:275](file:///plugin/embeddings/venv/embeddings_llama_index.py#L275) | unused method | `_aget_text_embedding` | 60% |
| [plugin/embeddings/venv/embeddings_llama_index.py:278](file:///plugin/embeddings/venv/embeddings_llama_index.py#L278) | unused method | `_aget_text_embeddings` | 60% |
| [plugin/embeddings/venv/embeddings_llama_index.py:282](file:///plugin/embeddings/venv/embeddings_llama_index.py#L282) | unused variable | `stores_text` | 60% |
| [plugin/embeddings/venv/embeddings_llama_index.py:348](file:///plugin/embeddings/venv/embeddings_llama_index.py#L348) | unused variable | `delete_kwargs` | 100% |
| [plugin/embeddings/venv/embeddings_llama_index.py:419](file:///plugin/embeddings/venv/embeddings_llama_index.py#L419) | unused method | `_retrieve` | 60% |
| [plugin/embeddings/venv/embeddings_sqlite.py:186](file:///plugin/embeddings/venv/embeddings_sqlite.py#L186) | unused attribute | `row_factory` | 60% |
| [plugin/embeddings/venv/folder_fts.py:72](file:///plugin/embeddings/venv/folder_fts.py#L72) | unused attribute | `row_factory` | 60% |
| [plugin/embeddings/venv/folder_fts_dispatch.py:13](file:///plugin/embeddings/venv/folder_fts_dispatch.py#L13) | unused function | `dispatch_trusted` | 60% |

## `ppt_master` (18 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/ppt_master/adapter/uno_template_fill.py:17](file:///plugin/ppt_master/adapter/uno_template_fill.py#L17) | unused variable | `template_doc` | 100% |
| [plugin/ppt_master/fidelity.py:29](file:///plugin/ppt_master/fidelity.py#L29) | unused variable | `SVG_NS` | 60% |
| [plugin/ppt_master/fidelity.py:38](file:///plugin/ppt_master/fidelity.py#L38) | unused variable | `odf_shape_counts` | 60% |
| [plugin/ppt_master/fidelity.py:43](file:///plugin/ppt_master/fidelity.py#L43) | unused variable | `page_size_pts` | 60% |
| [plugin/ppt_master/fidelity.py:45](file:///plugin/ppt_master/fidelity.py#L45) | unused variable | `file_bytes` | 60% |
| [plugin/ppt_master/fidelity.py:57](file:///plugin/ppt_master/fidelity.py#L57) | unused variable | `reference_pdf` | 60% |
| [plugin/ppt_master/fidelity.py:58](file:///plugin/ppt_master/fidelity.py#L58) | unused variable | `imported_pdf` | 60% |
| [plugin/ppt_master/fidelity.py:59](file:///plugin/ppt_master/fidelity.py#L59) | unused variable | `reference_pdf_info` | 60% |
| [plugin/ppt_master/fidelity.py:60](file:///plugin/ppt_master/fidelity.py#L60) | unused variable | `imported_pdf_info` | 60% |
| [plugin/ppt_master/fidelity.py:112](file:///plugin/ppt_master/fidelity.py#L112) | unused function | `count_svg_text_elements` | 60% |
| [plugin/ppt_master/fidelity.py:250](file:///plugin/ppt_master/fidelity.py#L250) | unused attribute | `page_size_pts` | 60% |
| [plugin/ppt_master/fidelity.py:453](file:///plugin/ppt_master/fidelity.py#L453) | unused attribute | `reference_pdf` | 60% |
| [plugin/ppt_master/fidelity.py:454](file:///plugin/ppt_master/fidelity.py#L454) | unused attribute | `imported_pdf` | 60% |
| [plugin/ppt_master/fidelity.py:455](file:///plugin/ppt_master/fidelity.py#L455) | unused attribute | `reference_pdf_info` | 60% |
| [plugin/ppt_master/fidelity.py:456](file:///plugin/ppt_master/fidelity.py#L456) | unused attribute | `imported_pdf_info` | 60% |
| [plugin/ppt_master/fidelity.py:473](file:///plugin/ppt_master/fidelity.py#L473) | unused function | `run_project_fidelity` | 60% |
| [plugin/ppt_master/fidelity.py:542](file:///plugin/ppt_master/fidelity.py#L542) | unused function | `write_agent_summary` | 60% |
| [plugin/ppt_master/project_notes.py:25](file:///plugin/ppt_master/project_notes.py#L25) | unused function | `notes_for_slides` | 60% |

## `vision` (8 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/vision/venv/vision_paddle.py:316](file:///plugin/vision/venv/vision_paddle.py#L316) | unused method | `handle_starttag` | 60% |
| [plugin/vision/venv/vision_paddle.py:332](file:///plugin/vision/venv/vision_paddle.py#L332) | unused method | `handle_endtag` | 60% |
| [plugin/vision/venv/vision_paddle.py:353](file:///plugin/vision/venv/vision_paddle.py#L353) | unused method | `handle_data` | 60% |
| [plugin/vision/vision_availability.py:97](file:///plugin/vision/vision_availability.py#L97) | unused function | `vision_packages_probe_ready` | 60% |
| [plugin/vision/vision_egress.py:19](file:///plugin/vision/vision_egress.py#L19) | unused function | `is_vision_result` | 60% |
| [plugin/vision/vision_runner.py:35](file:///plugin/vision/vision_runner.py#L35) | unused function | `extract_vision_insert_kwargs` | 60% |
| [plugin/vision/vision_templates.py:24](file:///plugin/vision/vision_templates.py#L24) | unused function | `get_vision_script_templates` | 60% |
| [plugin/vision/vision_templates.py:29](file:///plugin/vision/vision_templates.py#L29) | unused function | `parse_vision_script_header` | 60% |

## `main.py` (7 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/main.py:805](file:///plugin/main.py#L805) | unused attribute | `IsEnabled` | 60% |
| [plugin/main.py:807](file:///plugin/main.py#L807) | unused attribute | `IsEnabled` | 60% |
| [plugin/main.py:808](file:///plugin/main.py#L808) | unused attribute | `Requery` | 60% |
| [plugin/main.py:1052](file:///plugin/main.py#L1052) | unused variable | `Arguments` | 100% |
| [plugin/main.py:1141](file:///plugin/main.py#L1141) | unused variable | `aArguments` | 100% |
| [plugin/main.py:1157](file:///plugin/main.py#L1157) | unused variable | `TargetFrameName` | 100% |
| [plugin/main.py:1168](file:///plugin/main.py#L1168) | unused variable | `Arguments` | 100% |

## `notebook` (6 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/notebook/cell_registry.py:75](file:///plugin/notebook/cell_registry.py#L75) | unused variable | `last_run_status` | 60% |
| [plugin/notebook/notebook_controls.py:345](file:///plugin/notebook/notebook_controls.py#L345) | unused attribute | `ButtonType` | 60% |
| [plugin/notebook/notebook_controls.py:460](file:///plugin/notebook/notebook_controls.py#L460) | unused attribute | `ApplyFormDesignMode` | 60% |
| [plugin/notebook/notebook_runner.py:999](file:///plugin/notebook/notebook_runner.py#L999) | unused attribute | `last_run_status` | 60% |
| [plugin/notebook/notebook_runner.py:1001](file:///plugin/notebook/notebook_runner.py#L1001) | unused attribute | `last_run_status` | 60% |
| [plugin/notebook/writer_importer.py:1587](file:///plugin/notebook/writer_importer.py#L1587) | unused function | `format_all_outputs` | 60% |

## `doc` (5 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/doc/doc_type.py:41](file:///plugin/doc/doc_type.py#L41) | unused variable | `WRITER_DOCUMENT_SERVICE` | 60% |
| [plugin/doc/document_helpers.py:117](file:///plugin/doc/document_helpers.py#L117) | unused function | `get_full_document_text` | 60% |
| [plugin/doc/document_research.py:65](file:///plugin/doc/document_research.py#L65) | unused variable | `size_bytes` | 60% |
| [plugin/doc/text_helpers.py:296](file:///plugin/doc/text_helpers.py#L296) | unused variable | `body_paragraphs` | 60% |
| [plugin/doc/text_helpers.py:307](file:///plugin/doc/text_helpers.py#L307) | unused variable | `chapter_number` | 60% |

## `main_core.py` (4 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/main_core.py:159](file:///plugin/main_core.py#L159) | unused variable | `Arguments` | 100% |
| [plugin/main_core.py:192](file:///plugin/main_core.py#L192) | unused variable | `aArguments` | 100% |
| [plugin/main_core.py:204](file:///plugin/main_core.py#L204) | unused variable | `TargetFrameName` | 100% |
| [plugin/main_core.py:213](file:///plugin/main_core.py#L213) | unused variable | `Arguments` | 100% |

## `draw` (3 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/draw/__init__.py:24](file:///plugin/draw/__init__.py#L24) | unused class | `DrawModule` | 60% |
| [plugin/draw/designs.py:352](file:///plugin/draw/designs.py#L352) | unused function | `resolve_design` | 60% |
| [plugin/draw/designs.py:1310](file:///plugin/draw/designs.py#L1310) | unused function | `create_presentation_from_design` | 60% |

## `librepy` (3 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/librepy/panel_factory.py:91](file:///plugin/librepy/panel_factory.py#L91) | unused variable | `ParentAccessible` | 100% |
| [plugin/librepy/settings.py:92](file:///plugin/librepy/settings.py#L92) | unused attribute | `PositionX` | 60% |
| [plugin/librepy/settings.py:153](file:///plugin/librepy/settings.py#L153) | unused attribute | `Step` | 60% |

## `acp` (2 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/acp/acp_connection.py:191](file:///plugin/acp/acp_connection.py#L191) | unused attribute | `_reader_thread` | 60% |
| [plugin/acp/acp_connection.py:221](file:///plugin/acp/acp_connection.py#L221) | unused attribute | `_reader_thread` | 60% |

## `audio` (2 hits)

| Location | Type | Symbol | Confidence |
|---|---|---|---|
| [plugin/audio/kokoro_pool.py:260](file:///plugin/audio/kokoro_pool.py#L260) | unused variable | `num_workers` | 60% |
| [plugin/audio/kokoro_pool.py:281](file:///plugin/audio/kokoro_pool.py#L281) | unused attribute | `num_workers` | 60% |

## Complete Raw Vulture Output

```text
plugin/acp/acp_connection.py:191: unused attribute '_reader_thread' (60% confidence)
plugin/acp/acp_connection.py:221: unused attribute '_reader_thread' (60% confidence)
plugin/audio/kokoro_pool.py:260: unused variable 'num_workers' (60% confidence)
plugin/audio/kokoro_pool.py:281: unused attribute 'num_workers' (60% confidence)
plugin/calc/addin_common.py:103: unused variable 'xSomething' (100% confidence)
plugin/calc/error_detector.py:124: unused function 'get_calc_error_name' (60% confidence)
plugin/calc/excel_py_convert/models.py:25: unused variable 'DepRole' (60% confidence)
plugin/calc/excel_py_convert/models.py:28: unused variable 'EXCEL_DEP_TOKEN_FIDELITY' (60% confidence)
plugin/calc/excel_py_convert/models.py:50: unused variable 'formula_raw' (60% confidence)
plugin/calc/excel_py_convert/models.py:155: unused variable 'dag_formula' (60% confidence)
plugin/calc/excel_py_convert/models.py:156: unused variable 'excel_formula' (60% confidence)
plugin/calc/excel_py_convert/ooxml_util.py:250: unused variable 'temp_suffix' (100% confidence)
plugin/calc/excel_py_convert/to_dag.py:372: unused function 'rewrite_excel_code' (60% confidence)
plugin/calc/excel_py_convert/to_dag.py:578: unused attribute 'dag_formula' (60% confidence)
plugin/calc/excel_py_convert/to_excel.py:379: unused attribute 'excel_formula' (60% confidence)
plugin/calc/excel_py_convert/to_excel.py:383: unused function 'expand_placeholders_to_literals' (60% confidence)
plugin/calc/excel_py_convert/xlsx_write.py:220: unused function '_strip_python_in_excel_parts' (60% confidence)
plugin/calc/manipulator.py:330: unused attribute 'OuterLineWidth' (60% confidence)
plugin/calc/manipulator.py:500: unused attribute 'IsAscending' (60% confidence)
plugin/calc/manipulator.py:501: unused attribute 'IsCaseSensitive' (60% confidence)
plugin/calc/python/cell_discovery.py:75: unused function 'extract_code_from_formula' (60% confidence)
plugin/calc/python/collabora_formula.py:78: unused function 'is_collabora_py_formula' (60% confidence)
plugin/calc/python/formula_edit.py:346: unused function 'inline_py_code_has_lexer_collisions' (60% confidence)
plugin/calc/python/geometric_recalc_core.py:163: unused variable 'n_found' (100% confidence)
plugin/calc/python/xl_static_rewrite.py:127: unused function 'migrate_bare_data_to_index0' (60% confidence)
plugin/calc/quant_egress.py:22: unused function 'is_quant_result' (60% confidence)
plugin/calc/quant_egress.py:62: unused function 'insert_quant_result_into_calc' (60% confidence)
plugin/calc/search.py:107: unused attribute 'ReplaceString' (60% confidence)
plugin/calc/spreadsheet_import/graph.py:147: unused attribute 'formula_order' (60% confidence)
plugin/calc/spreadsheet_import/models.py:40: unused variable 'formula_order' (60% confidence)
plugin/calc/spreadsheet_import/models.py:52: unused variable 'original_formula' (60% confidence)
plugin/calc/spreadsheet_search.py:41: unused attribute 'SearchString' (60% confidence)
plugin/calc/spreadsheet_search.py:42: unused attribute 'SearchRegularExpression' (60% confidence)
plugin/calc/spreadsheet_search.py:43: unused attribute 'SearchCaseSensitive' (60% confidence)
plugin/calc/vision_egress.py:107: unused function 'format_vision_structure_for_calc' (60% confidence)
plugin/chatbot/__init__.py:38: unused class 'ChatbotModule' (60% confidence)
plugin/chatbot/agent_manual.py:150: unused variable 'MANUAL_SECTIONS' (60% confidence)
plugin/chatbot/agent_manual.py:255: unused function 'full_manual_for_model' (60% confidence)
plugin/chatbot/audio_recorder.py:408: unused variable 'time_info' (100% confidence)
plugin/chatbot/bug_report.py:173: unused function 'collect_environment_block' (60% confidence)
plugin/chatbot/chat_sidebar_mode.py:37: unused variable 'include_brainstorming' (60% confidence)
plugin/chatbot/chat_sidebar_mode.py:38: unused variable 'include_writing_plan' (60% confidence)
plugin/chatbot/chat_sidebar_mode.py:39: unused variable 'include_ppt_master' (60% confidence)
plugin/chatbot/compaction.py:192: unused variable 'compacted' (60% confidence)
plugin/chatbot/compaction.py:601: unused function 'serialize_for_summary' (60% confidence)
plugin/chatbot/config_ui_helpers.py:537: unused function 'get_signup_url_for_endpoint' (60% confidence)
plugin/chatbot/dialog_views.py:91: unused attribute 'ImagePosition' (60% confidence)
plugin/chatbot/dialogs.py:433: unused attribute 'HumanPresentableName' (60% confidence)
plugin/chatbot/dialogs.py:487: unused function 'msgbox_with_copy' (60% confidence)
plugin/chatbot/dialogs.py:713: unused function 'load_module_dialog' (60% confidence)
plugin/chatbot/dialogs.py:726: unused function 'load_framework_dialog' (60% confidence)
plugin/chatbot/dialogs.py:1086: unused attribute 'Step' (60% confidence)
plugin/chatbot/extension_update_check.py:110: unused function 'remote_is_newer' (60% confidence)
plugin/chatbot/panel_factory.py:269: unused variable 'ParentAccessible' (100% confidence)
plugin/chatbot/panel_resize.py:374: unused attribute '_width_negotiated' (60% confidence)
plugin/chatbot/panel_resize.py:420: unused attribute '_width_negotiated' (60% confidence)
plugin/chatbot/quick_setup.py:54: unused function 'probe_local_servers' (60% confidence)
plugin/chatbot/record_gesture.py:38: unused variable 'HANDS_FREE_STATUS' (60% confidence)
plugin/chatbot/rich_text.py:116: unused attribute 'ParaLeftMargin' (60% confidence)
plugin/chatbot/rich_text.py:117: unused attribute 'ParaRightMargin' (60% confidence)
plugin/chatbot/rich_text.py:118: unused attribute 'ParaFirstLineIndent' (60% confidence)
plugin/chatbot/rich_text.py:119: unused attribute 'ParaTopMargin' (60% confidence)
plugin/chatbot/rich_text.py:120: unused attribute 'ParaBottomMargin' (60% confidence)
plugin/chatbot/rich_text.py:125: unused attribute 'CharLocale' (60% confidence)
plugin/chatbot/rich_text.py:126: unused attribute 'CharLocaleAsian' (60% confidence)
plugin/chatbot/rich_text.py:127: unused attribute 'CharLocaleComplex' (60% confidence)
plugin/chatbot/rich_text.py:142: unused variable 'USER_COLOR' (60% confidence)
plugin/chatbot/rich_text.py:143: unused variable 'ASSISTANT_COLOR' (60% confidence)
plugin/chatbot/rich_text.py:227: unused attribute 'NumberingRules' (60% confidence)
plugin/chatbot/rich_text_control.py:714: unused attribute 'Weight' (60% confidence)
plugin/chatbot/rich_text_control.py:1364: unused attribute 'CharBackColor' (60% confidence)
plugin/chatbot/rich_text_paste.py:213: unused attribute 'ParaTopMargin' (60% confidence)
plugin/chatbot/rich_text_paste.py:214: unused attribute 'ParaBottomMargin' (60% confidence)
plugin/chatbot/rich_text_paste.py:234: unused attribute 'ParaTabStops' (60% confidence)
plugin/chatbot/rich_text_paste.py:271: unused attribute 'CharFontNameAsian' (60% confidence)
plugin/chatbot/rich_text_paste.py:272: unused attribute 'CharFontNameComplex' (60% confidence)
plugin/chatbot/rich_text_paste.py:369: unused attribute 'CharBackColor' (60% confidence)
plugin/chatbot/send_handlers.py:487: unused variable 'worker_q' (100% confidence)
plugin/chatbot/skills.py:115: unused method 'get_humanizer_skill_path' (60% confidence)
plugin/chatbot/slash_popup.py:346: unused function 'uses_toolkit_overlay' (60% confidence)
plugin/chatbot/slash_popup.py:456: unused attribute 'WindowServiceName' (60% confidence)
plugin/chatbot/slash_popup.py:457: unused attribute 'Parent' (60% confidence)
plugin/chatbot/slash_popup.py:458: unused attribute 'ParentIndex' (60% confidence)
plugin/chatbot/slash_popup.py:459: unused attribute 'Bounds' (60% confidence)
plugin/chatbot/slash_popup.py:460: unused attribute 'WindowAttributes' (60% confidence)
plugin/chatbot/slash_popup.py:473: unused attribute 'WindowServiceName' (60% confidence)
plugin/chatbot/slash_popup.py:474: unused attribute 'Parent' (60% confidence)
plugin/chatbot/slash_popup.py:475: unused attribute 'ParentIndex' (60% confidence)
plugin/chatbot/slash_popup.py:476: unused attribute 'Bounds' (60% confidence)
plugin/chatbot/slash_popup.py:477: unused attribute 'WindowAttributes' (60% confidence)
plugin/chatbot/slash_popup.py:556: unused attribute '_frame_controller' (60% confidence)
plugin/chatbot/slash_popup.py:558: unused attribute '_toolkit' (60% confidence)
plugin/chatbot/slash_popup.py:559: unused attribute '_ignore_item' (60% confidence)
plugin/chatbot/slash_popup.py:991: unused attribute '_ignore_item' (60% confidence)
plugin/chatbot/slash_popup.py:996: unused attribute '_ignore_item' (60% confidence)
plugin/chatbot/slash_popup.py:1020: unused attribute '_frame_controller' (60% confidence)
plugin/chatbot/slash_popup.py:1061: unused attribute '_toolkit' (60% confidence)
plugin/chatbot/state_machine.py:12: unused variable 'SendHandlerKind' (60% confidence)
plugin/chatbot/state_machine.py:13: unused variable 'SendHandlerFsmStatus' (60% confidence)
plugin/chatbot/state_machine.py:15: unused variable 'SendHandlerCompleteStatus' (60% confidence)
plugin/chatbot/tool_loop.py:177: unused attribute '_stop_partial_text' (60% confidence)
plugin/chatbot/tool_loop_actions.py:128: unused attribute '_stop_partial_text' (60% confidence)
plugin/contrib/cdp/browser_cdp_tool.py:579: unused variable 'BROWSER_CDP_SCHEMA' (60% confidence)
plugin/contrib/cdp/browser_supervisor.py:268: unused variable 'console_errors' (60% confidence)
plugin/contrib/cdp/browser_supervisor.py:430: unused method 'respond_to_dialog' (60% confidence)
plugin/contrib/cdp/browser_supervisor.py:491: unused method 'evaluate_runtime' (60% confidence)
plugin/contrib/cdp/browser_supervisor.py:1427: unused method 'get_or_start' (60% confidence)
plugin/contrib/cdp/browser_supervisor.py:1478: unused method 'stop_all' (60% confidence)
plugin/contrib/nbformat/_struct.py:242: unused method 'allow_new_attr' (60% confidence)
plugin/contrib/nbformat/v4/__init__.py:41: unused variable 'reads_json' (60% confidence)
plugin/contrib/nbformat/v4/__init__.py:42: unused variable 'writes_json' (60% confidence)
plugin/contrib/ppt_master/config.py:21: unused function 'templates_dir' (60% confidence)
plugin/contrib/ppt_master/config.py:25: unused function 'references_dir' (60% confidence)
plugin/contrib/ppt_master/config.py:29: unused function 'workflows_dir' (60% confidence)
plugin/contrib/ppt_master/config.py:33: unused function 'skill_md_path' (60% confidence)
plugin/contrib/ppt_master/upstream.py:20: unused function 'upstream_scripts_path' (60% confidence)
plugin/contrib/smolagents/agent_types.py:115: unused method '_ipython_display_' (60% confidence)
plugin/contrib/smolagents/agent_types.py:235: unused method '_ipython_display_' (60% confidence)
plugin/contrib/smolagents/agents.py:40: unused variable 'Live' (60% confidence)
plugin/contrib/smolagents/agents.py:41: unused variable 'Markdown' (60% confidence)
plugin/contrib/smolagents/agents.py:175: unused variable 'initial_plan' (60% confidence)
plugin/contrib/smolagents/agents.py:176: unused variable 'update_plan_pre_messages' (60% confidence)
plugin/contrib/smolagents/agents.py:177: unused variable 'update_plan_post_messages' (60% confidence)
plugin/contrib/smolagents/agents.py:202: unused variable 'pre_messages' (60% confidence)
plugin/contrib/smolagents/agents.py:203: unused variable 'post_messages' (60% confidence)
plugin/contrib/smolagents/agents.py:218: unused variable 'planning' (60% confidence)
plugin/contrib/smolagents/agents.py:341: unused attribute 'agent_name' (60% confidence)
plugin/contrib/smolagents/default_tools.py:189: unused class 'PreTool' (60% confidence)
plugin/contrib/smolagents/default_tools.py:359: unused method 'handle_starttag' (60% confidence)
plugin/contrib/smolagents/default_tools.py:377: unused method 'handle_endtag' (60% confidence)
plugin/contrib/smolagents/default_tools.py:389: unused method 'handle_data' (60% confidence)
plugin/contrib/smolagents/default_tools.py:530: unused method 'handle_starttag' (60% confidence)
plugin/contrib/smolagents/default_tools.py:534: unused method 'handle_endtag' (60% confidence)
plugin/contrib/smolagents/default_tools.py:538: unused method 'handle_data' (60% confidence)
plugin/contrib/smolagents/local_python_executor.py:652: unused variable 'signum' (100% confidence)
plugin/contrib/smolagents/local_python_executor.py:809: unused attribute '__signature__' (60% confidence)
plugin/contrib/smolagents/local_python_executor.py:887: unused attribute '__ast__' (60% confidence)
plugin/contrib/smolagents/local_python_executor.py:890: unused attribute '__signature__' (60% confidence)
plugin/contrib/smolagents/local_python_executor.py:1779: unused attribute '_authorized_imports' (60% confidence)
plugin/contrib/smolagents/local_python_executor.py:1889: unused attribute '_authorized_imports' (60% confidence)
plugin/contrib/smolagents/memory.py:243: unused method 'get_succinct_steps' (60% confidence)
plugin/contrib/smolagents/memory.py:255: unused method 'replay' (60% confidence)
plugin/contrib/smolagents/memory.py:280: unused method 'return_full_code' (60% confidence)
plugin/contrib/smolagents/models.py:29: unused import 'RateLimiter' (90% confidence)
plugin/contrib/smolagents/models.py:29: unused import 'Retrying' (90% confidence)
plugin/contrib/smolagents/models.py:41: unused import 'StoppingCriteriaList' (90% confidence)
plugin/contrib/smolagents/models.py:46: unused variable 'RETRY_WAIT' (60% confidence)
plugin/contrib/smolagents/models.py:48: unused variable 'RETRY_EXPONENTIAL_BASE' (60% confidence)
plugin/contrib/smolagents/models.py:49: unused variable 'RETRY_JITTER' (60% confidence)
plugin/contrib/smolagents/models.py:50: unused variable 'STRUCTURED_GENERATION_PROVIDERS' (60% confidence)
plugin/contrib/smolagents/models.py:51: unused variable 'CODEAGENT_RESPONSE_FORMAT' (60% confidence)
plugin/contrib/smolagents/models.py:144: unused method 'model_dump_json' (60% confidence)
plugin/contrib/smolagents/models.py:168: unused method 'render_as_markdown' (60% confidence)
plugin/contrib/smolagents/models.py:1838: unused variable 'MODEL_REGISTRY' (60% confidence)
plugin/contrib/smolagents/monitoring.py:22: unused variable 'highlight' (100% confidence)
plugin/contrib/smolagents/monitoring.py:150: unused method 'get_total_token_counts' (60% confidence)
plugin/contrib/smolagents/monitoring.py:182: unused variable 'OFF' (60% confidence)
plugin/contrib/smolagents/monitoring.py:235: unused method 'log_code' (60% confidence)
plugin/contrib/smolagents/monitoring.py:285: unused method 'visualize_agent_tree' (60% confidence)
plugin/contrib/smolagents/serialization.py:348: unused method '_extract_method_body' (60% confidence)
plugin/contrib/smolagents/serialization.py:376: unused method 'get_safe_serializer_code' (60% confidence)
plugin/contrib/smolagents/serialization.py:451: unused method 'get_deserializer_code' (60% confidence)
plugin/contrib/smolagents/tool_validation.py:19: unused attribute 'undefined_names' (60% confidence)
plugin/contrib/smolagents/tool_validation.py:73: unused method 'visit_AnnAssign' (60% confidence)
plugin/contrib/smolagents/tools.py:39: unused variable 'CommitOperationAdd' (60% confidence)
plugin/contrib/smolagents/tools.py:39: unused variable 'create_commit' (60% confidence)
plugin/contrib/smolagents/tools.py:39: unused variable 'create_repo' (60% confidence)
plugin/contrib/smolagents/tools.py:40: unused variable 'hf_hub_download' (60% confidence)
plugin/contrib/smolagents/tools.py:40: unused variable 'metadata_update' (60% confidence)
plugin/contrib/smolagents/tools.py:63: unused import 'mcp' (90% confidence)
plugin/contrib/smolagents/tools.py:94: unused variable 'CONVERSION_DICT' (60% confidence)
plugin/contrib/smolagents/tools.py:257: unused method 'to_code_prompt' (60% confidence)
plugin/contrib/smolagents/tools.py:896: unused function 'add_description' (60% confidence)
plugin/contrib/smolagents/tools.py:1130: unused attribute '__signature__' (60% confidence)
plugin/contrib/smolagents/tools.py:1358: unused function 'get_tools_definition_code' (60% confidence)
plugin/contrib/smolagents/utils.py:352: unused function 'parse_code_blobs' (60% confidence)
plugin/contrib/smolagents/utils.py:594: unused function 'make_init_file' (60% confidence)
plugin/contrib/smolagents/utils.py:644: unused function 'create_agent_gradio_app_template' (60% confidence)
plugin/contrib/smolagents/utils.py:665: unused class 'RateLimiter' (60% confidence)
plugin/contrib/smolagents/utils.py:685: unused method 'throttle' (60% confidence)
plugin/contrib/smolagents/utils.py:696: unused class 'Retrying' (60% confidence)
plugin/doc/doc_type.py:41: unused variable 'WRITER_DOCUMENT_SERVICE' (60% confidence)
plugin/doc/document_helpers.py:117: unused function 'get_full_document_text' (60% confidence)
plugin/doc/document_research.py:65: unused variable 'size_bytes' (60% confidence)
plugin/doc/text_helpers.py:296: unused variable 'body_paragraphs' (60% confidence)
plugin/doc/text_helpers.py:307: unused variable 'chapter_number' (60% confidence)
plugin/draw/__init__.py:24: unused class 'DrawModule' (60% confidence)
plugin/draw/designs.py:352: unused function 'resolve_design' (60% confidence)
plugin/draw/designs.py:1310: unused function 'create_presentation_from_design' (60% confidence)
plugin/embeddings/__init__.py:10: unused class 'EmbeddingsModule' (60% confidence)
plugin/embeddings/embeddings_cache.py:202: unused function 'get_file_index_state' (60% confidence)
plugin/embeddings/embeddings_fs.py:56: unused variable 'doc_path' (60% confidence)
plugin/embeddings/embeddings_fs.py:306: unused function 'paragraph_chunks_from_path' (60% confidence)
plugin/embeddings/embeddings_locale.py:23: unused variable 'OFFICE_NS' (60% confidence)
plugin/embeddings/search_ui.py:97: unused attribute '_hb_start' (60% confidence)
plugin/embeddings/venv/embeddings_index_dispatch.py:13: unused function 'dispatch_trusted' (60% confidence)
plugin/embeddings/venv/embeddings_ingest_graph.py:43: unused variable 'documents' (60% confidence)
plugin/embeddings/venv/embeddings_llama_index.py:103: unused variable 'node_ids' (60% confidence)
plugin/embeddings/venv/embeddings_llama_index.py:272: unused method '_aget_query_embedding' (60% confidence)
plugin/embeddings/venv/embeddings_llama_index.py:275: unused method '_aget_text_embedding' (60% confidence)
plugin/embeddings/venv/embeddings_llama_index.py:278: unused method '_aget_text_embeddings' (60% confidence)
plugin/embeddings/venv/embeddings_llama_index.py:282: unused variable 'stores_text' (60% confidence)
plugin/embeddings/venv/embeddings_llama_index.py:348: unused variable 'delete_kwargs' (100% confidence)
plugin/embeddings/venv/embeddings_llama_index.py:419: unused method '_retrieve' (60% confidence)
plugin/embeddings/venv/embeddings_sqlite.py:186: unused attribute 'row_factory' (60% confidence)
plugin/embeddings/venv/folder_fts.py:72: unused attribute 'row_factory' (60% confidence)
plugin/embeddings/venv/folder_fts_dispatch.py:13: unused function 'dispatch_trusted' (60% confidence)
plugin/framework/ast_stmt_edit.py:32: unused function 'is_name_call_expr' (60% confidence)
plugin/framework/ast_stmt_edit.py:89: unused function 'iter_matching_statements' (60% confidence)
plugin/framework/ast_stmt_edit.py:95: unused function 'iter_matching_expr_statements' (60% confidence)
plugin/framework/ast_stmt_edit.py:268: unused function 'remove_expr_statements' (60% confidence)
plugin/framework/client/http_transport.py:242: unused attribute 'auto_open' (60% confidence)
plugin/framework/client/http_transport.py:292: unused attribute 'auto_open' (60% confidence)
plugin/framework/client/llm_client.py:400: unused method '_get_connection' (60% confidence)
plugin/framework/client/request_controls.py:309: unused function 'claim_host_send_slot' (60% confidence)
plugin/framework/client/request_controls.py:319: unused function 'remaining_host_gap' (60% confidence)
plugin/framework/config.py:299: unused function 'parse_config_json_text' (60% confidence)
plugin/framework/config.py:1281: unused function 'set_api_key_for_endpoint' (60% confidence)
plugin/framework/config_schema.py:101: unused function 'get_manifest_modules' (60% confidence)
plugin/framework/config_schema.py:356: unused variable 'chat_max_tokens' (60% confidence)
plugin/framework/config_schema.py:359: unused variable 'chat_compaction_enabled' (60% confidence)
plugin/framework/config_schema.py:360: unused variable 'request_timeout' (60% confidence)
plugin/framework/config_schema.py:362: unused variable 'api_keys_by_endpoint' (60% confidence)
plugin/framework/config_schema.py:363: unused variable 'image_base_size' (60% confidence)
plugin/framework/config_schema.py:364: unused variable 'image_default_aspect' (60% confidence)
plugin/framework/config_schema.py:365: unused variable 'image_steps' (60% confidence)
plugin/framework/config_schema.py:366: unused variable 'image_auto_gallery' (60% confidence)
plugin/framework/config_schema.py:367: unused variable 'image_insert_frame' (60% confidence)
plugin/framework/config_schema.py:370: unused variable 'embedding_provider' (60% confidence)
plugin/framework/config_schema.py:372: unused variable 'enable_agent_log' (60% confidence)
plugin/framework/config_schema.py:375: unused variable 'extension_update_check_epoch' (60% confidence)
plugin/framework/config_schema.py:376: unused variable 'librepy_update_check_epoch' (60% confidence)
plugin/framework/config_schema.py:377: unused variable 'libreharper_update_check_epoch' (60% confidence)
plugin/framework/config_schema.py:379: unused variable 'extend_selection_system_prompt' (60% confidence)
plugin/framework/config_schema.py:380: unused variable 'edit_selection_system_prompt' (60% confidence)
plugin/framework/config_schema.py:381: unused variable 'audio_support_map' (60% confidence)
plugin/framework/config_schema.py:386: unused variable 'vision_support_map' (60% confidence)
plugin/framework/config_schema.py:392: unused variable 'parallel_tool_calls' (60% confidence)
plugin/framework/config_schema.py:395: unused variable 'last_python_script_name_writer' (60% confidence)
plugin/framework/config_schema.py:396: unused variable 'last_python_script_name_calc' (60% confidence)
plugin/framework/config_schema.py:397: unused variable 'last_python_script_name_draw' (60% confidence)
plugin/framework/config_schema.py:404: unused variable 'text_analytics_sentiment_model' (60% confidence)
plugin/framework/config_schema.py:407: unused variable 'last_latex_input' (60% confidence)
plugin/framework/config_schema.py:408: unused variable 'last_latex_display_block' (60% confidence)
plugin/framework/config_schema.py:414: unused variable 'doc_agent_edit_review_mode' (60% confidence)
plugin/framework/config_schema.py:909: unused function 'prune_default_values' (60% confidence)
plugin/framework/constants.py:102: unused variable 'EMBEDDINGS' (60% confidence)
plugin/framework/constants.py:104: unused variable 'MODERATIONS' (60% confidence)
plugin/framework/constants.py:105: unused variable 'REALTIME' (60% confidence)
plugin/framework/constants.py:106: unused variable 'CODE' (60% confidence)
plugin/framework/deal_shim.py:238: unused function 'asserts_enabled' (60% confidence)
plugin/framework/frame_session.py:170: unused attribute '_frame_listener' (60% confidence)
plugin/framework/frame_session.py:316: unused attribute '_frame_listener' (60% confidence)
plugin/framework/frame_session.py:461: unused attribute '_frame_listener' (60% confidence)
plugin/framework/grammar_rule_ids.py:254: unused variable 'STOPWORDS_ISO_KEY' (60% confidence)
plugin/framework/http_server.py:328: unused variable 'daemon_threads' (60% confidence)
plugin/framework/http_server.py:340: unused variable 'address_family' (60% confidence)
plugin/framework/http_server.py:455: unused attribute 'address_family' (60% confidence)
plugin/framework/http_server.py:501: unused method 'drain_executor' (60% confidence)
plugin/framework/logging.py:54: unused variable 'enable_agent_log' (60% confidence)
plugin/framework/logging.py:55: unused variable 'init_lock' (60% confidence)
plugin/framework/logging.py:56: unused variable 'activity' (60% confidence)
plugin/framework/logging.py:57: unused variable 'activity_lock' (60% confidence)
plugin/framework/logging.py:58: unused variable 'hung_shown' (60% confidence)
plugin/framework/logging.py:59: unused variable 'stacks_dumped' (60% confidence)
plugin/framework/process_worker.py:148: unused method 'from_absolute' (60% confidence)
plugin/framework/process_worker.py:339: unused variable 'tasks_executed' (60% confidence)
plugin/framework/process_worker.py:365: unused attribute 'tasks_executed' (60% confidence)
plugin/framework/process_worker.py:563: unused attribute 'tasks_executed' (60% confidence)
plugin/framework/process_worker.py:894: unused attribute 'tasks_executed' (60% confidence)
plugin/framework/prompts.py:252: unused variable 'PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE' (60% confidence)
plugin/framework/prompts.py:519: unused variable 'DEFAULT_WRITER_GREETING' (60% confidence)
plugin/framework/prompts.py:576: unused variable 'CALC_PYTHON_FORMULA_LLM_HINT' (60% confidence)
plugin/framework/prompts.py:589: unused variable 'DEFAULT_CALC_GREETING' (60% confidence)
plugin/framework/prompts.py:848: unused variable 'DEFAULT_DRAW_GREETING' (60% confidence)
plugin/framework/prompts.py:1191: unused function 'get_chat_system_prompt_for_kind' (60% confidence)
plugin/framework/prompts.py:1345: unused variable 'PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE' (60% confidence)
plugin/framework/prompts.py:1357: unused variable 'CALC_PYTHON_FORMULA_LLM_HINT' (60% confidence)
plugin/framework/tool.py:616: unused variable 'batch_mode' (60% confidence)
plugin/framework/tool.py:625: unused attribute 'batch_mode' (60% confidence)
plugin/framework/tool.py:1007: unused function 'execute_unguarded' (60% confidence)
plugin/framework/uno_bootstrap.py:47: unused method 'create_module' (60% confidence)
plugin/framework/uno_bootstrap.py:49: unused attribute '__package__' (60% confidence)
plugin/framework/uno_bootstrap.py:69: unused method 'create_module' (60% confidence)
plugin/librepy/panel_factory.py:91: unused variable 'ParentAccessible' (100% confidence)
plugin/librepy/settings.py:92: unused attribute 'PositionX' (60% confidence)
plugin/librepy/settings.py:153: unused attribute 'Step' (60% confidence)
plugin/main.py:805: unused attribute 'IsEnabled' (60% confidence)
plugin/main.py:807: unused attribute 'IsEnabled' (60% confidence)
plugin/main.py:808: unused attribute 'Requery' (60% confidence)
plugin/main.py:1052: unused variable 'Arguments' (100% confidence)
plugin/main.py:1141: unused variable 'aArguments' (100% confidence)
plugin/main.py:1157: unused variable 'TargetFrameName' (100% confidence)
plugin/main.py:1168: unused variable 'Arguments' (100% confidence)
plugin/main_core.py:159: unused variable 'Arguments' (100% confidence)
plugin/main_core.py:192: unused variable 'aArguments' (100% confidence)
plugin/main_core.py:204: unused variable 'TargetFrameName' (100% confidence)
plugin/main_core.py:213: unused variable 'Arguments' (100% confidence)
plugin/mcp/__init__.py:111: unused class 'McpModule' (60% confidence)
plugin/mcp/__init__.py:597: unused attribute 'Visible' (60% confidence)
plugin/mcp/__init__.py:601: unused attribute 'Visible' (60% confidence)
plugin/mcp/__init__.py:615: unused attribute 'Visible' (60% confidence)
plugin/mcp/__init__.py:619: unused attribute 'Visible' (60% confidence)
plugin/mcp/cors.py:218: unused function 'get_extra_allowed_origins' (60% confidence)
plugin/mcp/mcp_protocol.py:444: unused method 'handle_mcp_sse' (60% confidence)
plugin/mcp/mcp_protocol.py:448: unused method 'handle_mcp_delete' (60% confidence)
plugin/mcp/mcp_protocol.py:464: unused method 'handle_sse_stream' (60% confidence)
plugin/mcp/mcp_protocol.py:546: unused method 'handle_sse_post' (60% confidence)
plugin/mcp/mcp_protocol.py:575: unused method 'handle_debug_info' (60% confidence)
plugin/mcp/mcp_protocol.py:606: unused method 'handle_debug_post' (60% confidence)
plugin/mcp/mcp_protocol.py:1243: unused method '_execute_tool_on_main' (60% confidence)
plugin/mcp/mcp_session.py:110: unused attribute 'close_connection' (60% confidence)
plugin/mcp/server.py:147: unused attribute 'close_connection' (60% confidence)
plugin/mcp/server.py:367: unused attribute 'close_connection' (60% confidence)
plugin/mcp/server.py:369: unused method 'do_GET' (60% confidence)
plugin/mcp/server.py:372: unused method 'do_POST' (60% confidence)
plugin/mcp/server.py:375: unused method 'do_DELETE' (60% confidence)
plugin/mcp/server.py:378: unused method 'do_OPTIONS' (60% confidence)
plugin/mcp/tunnel.py:178: unused variable 'authtoken' (100% confidence)
plugin/mcp/wire_types.py:32: unused variable 'PARSE_ERROR' (60% confidence)
plugin/mcp/wire_types.py:334: unused function 'progress_notification' (60% confidence)
plugin/notebook/cell_registry.py:75: unused variable 'last_run_status' (60% confidence)
plugin/notebook/notebook_controls.py:345: unused attribute 'ButtonType' (60% confidence)
plugin/notebook/notebook_controls.py:460: unused attribute 'ApplyFormDesignMode' (60% confidence)
plugin/notebook/notebook_runner.py:999: unused attribute 'last_run_status' (60% confidence)
plugin/notebook/notebook_runner.py:1001: unused attribute 'last_run_status' (60% confidence)
plugin/notebook/writer_importer.py:1587: unused function 'format_all_outputs' (60% confidence)
plugin/ppt_master/adapter/uno_template_fill.py:17: unused variable 'template_doc' (100% confidence)
plugin/ppt_master/fidelity.py:29: unused variable 'SVG_NS' (60% confidence)
plugin/ppt_master/fidelity.py:38: unused variable 'odf_shape_counts' (60% confidence)
plugin/ppt_master/fidelity.py:43: unused variable 'page_size_pts' (60% confidence)
plugin/ppt_master/fidelity.py:45: unused variable 'file_bytes' (60% confidence)
plugin/ppt_master/fidelity.py:57: unused variable 'reference_pdf' (60% confidence)
plugin/ppt_master/fidelity.py:58: unused variable 'imported_pdf' (60% confidence)
plugin/ppt_master/fidelity.py:59: unused variable 'reference_pdf_info' (60% confidence)
plugin/ppt_master/fidelity.py:60: unused variable 'imported_pdf_info' (60% confidence)
plugin/ppt_master/fidelity.py:112: unused function 'count_svg_text_elements' (60% confidence)
plugin/ppt_master/fidelity.py:250: unused attribute 'page_size_pts' (60% confidence)
plugin/ppt_master/fidelity.py:453: unused attribute 'reference_pdf' (60% confidence)
plugin/ppt_master/fidelity.py:454: unused attribute 'imported_pdf' (60% confidence)
plugin/ppt_master/fidelity.py:455: unused attribute 'reference_pdf_info' (60% confidence)
plugin/ppt_master/fidelity.py:456: unused attribute 'imported_pdf_info' (60% confidence)
plugin/ppt_master/fidelity.py:473: unused function 'run_project_fidelity' (60% confidence)
plugin/ppt_master/fidelity.py:542: unused function 'write_agent_summary' (60% confidence)
plugin/ppt_master/project_notes.py:25: unused function 'notes_for_slides' (60% confidence)
plugin/scripting/analysis.py:89: unused variable 'get_analysis_script_templates' (60% confidence)
plugin/scripting/analysis.py:90: unused variable 'parse_analysis_script_header' (60% confidence)
plugin/scripting/duckdb_sql.py:117: unused function 'get_sql_script_templates' (60% confidence)
plugin/scripting/duckdb_sql.py:126: unused function 'parse_sql_script_header' (60% confidence)
plugin/scripting/duckdb_sql.py:166: unused function 'is_sql_result' (60% confidence)
plugin/scripting/duckdb_sql.py:182: unused function 'insert_sql_result_into_calc' (60% confidence)
plugin/scripting/editor_host.py:283: unused attribute '_reader_thread' (60% confidence)
plugin/scripting/editor_host.py:369: unused attribute '_reader_thread' (60% confidence)
plugin/scripting/editor_host.py:908: unused attribute '_on_closed' (60% confidence)
plugin/scripting/forecast.py:83: unused variable 'parse_forecast_script_header' (60% confidence)
plugin/scripting/forecast.py:84: unused variable 'get_forecast_script_templates' (60% confidence)
plugin/scripting/forecast.py:85: unused variable 'get_forecast_template' (60% confidence)
plugin/scripting/forecast.py:115: unused function 'is_forecast_result' (60% confidence)
plugin/scripting/helper_domain.py:913: unused function 'supports_calc_or_writer_manual' (60% confidence)
plugin/scripting/import_policy.py:133: unused function 'venv_blocked_modules' (60% confidence)
plugin/scripting/ipc.py:617: unused function 'write_pickle_frame_with_timeout' (60% confidence)
plugin/scripting/optimize.py:66: unused variable 'parse_optimize_script_header' (60% confidence)
plugin/scripting/optimize.py:67: unused variable 'get_optimize_script_templates' (60% confidence)
plugin/scripting/optimize.py:68: unused variable 'get_optimize_template' (60% confidence)
plugin/scripting/optimize.py:102: unused function 'is_optimize_result' (60% confidence)
plugin/scripting/payload_codec.py:421: unused variable 'MAX_BENCH_CELLS' (60% confidence)
plugin/scripting/payload_codec.py:797: unused function 'column_kinds_for_grid' (60% confidence)
plugin/scripting/payload_codec.py:1029: unused function 'binary_envelope_skip_reason' (60% confidence)
plugin/scripting/payload_codec.py:1071: unused function 'is_numeric_coercible' (60% confidence)
plugin/scripting/payload_codec.py:1767: unused function 'host_pack_multi_data' (60% confidence)
plugin/scripting/python_runner.py:542: unused function 'execute_and_insert_result' (60% confidence)
plugin/scripting/quant.py:57: unused variable 'parse_quant_script_header' (60% confidence)
plugin/scripting/quant.py:58: unused variable 'get_quant_script_templates' (60% confidence)
plugin/scripting/quant.py:59: unused variable 'get_quant_template' (60% confidence)
plugin/scripting/sandbox.py:751: unused function '_reset_cache' (60% confidence)
plugin/scripting/sandbox.py:1036: unused function 'is_safe_workspace_path' (60% confidence)
plugin/scripting/symbolic.py:66: unused variable 'get_math_script_templates' (60% confidence)
plugin/scripting/symbolic.py:117: unused function 'is_symbolic_result' (60% confidence)
plugin/scripting/text_analytics.py:55: unused variable 'TEXT_ANALYTICS_HEADER_PREFIX' (60% confidence)
plugin/scripting/text_analytics.py:75: unused variable 'get_text_analytics_script_templates' (60% confidence)
plugin/scripting/text_analytics.py:80: unused function 'supports_text_analytics_manual' (60% confidence)
plugin/scripting/text_analytics.py:295: unused function 'is_text_analytics_result' (60% confidence)
plugin/scripting/units.py:69: unused variable 'get_units_script_templates' (60% confidence)
plugin/scripting/units.py:98: unused function 'extract_units_insert_kwargs' (60% confidence)
plugin/scripting/units.py:112: unused function 'is_units_result' (60% confidence)
plugin/scripting/units.py:204: unused function 'insert_units_result_into_doc' (60% confidence)
plugin/scripting/venv/analysis.py:518: unused function 'quick_stats' (60% confidence)
plugin/scripting/venv/audio_recorder.py:92: unused variable 'time_info' (100% confidence)
plugin/scripting/venv/calc_functions_a_c.py:134: unused variable 'first_interest' (100% confidence)
plugin/scripting/venv/calc_functions_a_c.py:140: unused variable 'calc_method' (100% confidence)
plugin/scripting/venv/coerce.py:999: unused function 'records_from_df' (60% confidence)
plugin/scripting/venv/editor_main.py:328: unused method 'poll_messages' (60% confidence)
plugin/scripting/venv/editor_main.py:349: unused method 'is_jedi_available' (60% confidence)
plugin/scripting/venv/editor_main.py:424: unused method 'notify_dirty' (60% confidence)
plugin/scripting/venv/editor_main.py:453: unused method 'notify_run' (60% confidence)
plugin/scripting/venv/editor_main.py:456: unused method 'notify_save_script' (60% confidence)
plugin/scripting/venv/editor_main.py:462: unused method 'request_scripts' (60% confidence)
plugin/scripting/venv/editor_main.py:465: unused method 'save_script' (60% confidence)
plugin/scripting/venv/editor_main.py:468: unused method 'delete_script' (60% confidence)
plugin/scripting/venv/editor_main.py:471: unused method 'select_script' (60% confidence)
plugin/scripting/venv/editor_main.py:474: unused method 'notify_cancel' (60% confidence)
plugin/scripting/venv/symbolic.py:254: unused function 'symbolic_simplify' (60% confidence)
plugin/scripting/venv/symbolic.py:263: unused function 'differentiate' (60% confidence)
plugin/scripting/venv/symbolic.py:312: unused function 'solve_equation' (60% confidence)
plugin/scripting/venv/symbolic.py:376: unused function 'latex_to_math_object' (60% confidence)
plugin/scripting/venv_diagnostics.py:862: unused function '_format_self_check_success' (60% confidence)
plugin/scripting/venv_worker.py:516: unused method 'pool_is_running' (60% confidence)
plugin/scripting/viz.py:66: unused variable 'get_viz_script_templates' (60% confidence)
plugin/scripting/viz.py:67: unused variable 'parse_viz_script_header' (60% confidence)
plugin/scripting/viz.py:98: unused function 'is_viz_result' (60% confidence)
plugin/vision/venv/vision_paddle.py:316: unused method 'handle_starttag' (60% confidence)
plugin/vision/venv/vision_paddle.py:332: unused method 'handle_endtag' (60% confidence)
plugin/vision/venv/vision_paddle.py:353: unused method 'handle_data' (60% confidence)
plugin/vision/vision_availability.py:97: unused function 'vision_packages_probe_ready' (60% confidence)
plugin/vision/vision_egress.py:19: unused function 'is_vision_result' (60% confidence)
plugin/vision/vision_runner.py:35: unused function 'extract_vision_insert_kwargs' (60% confidence)
plugin/vision/vision_templates.py:24: unused function 'get_vision_script_templates' (60% confidence)
plugin/vision/vision_templates.py:29: unused function 'parse_vision_script_header' (60% confidence)
plugin/writer/__init__.py:41: unused class 'WriterModule' (60% confidence)
plugin/writer/field_restore.py:385: unused attribute 'SearchRegularExpression' (60% confidence)
plugin/writer/field_restore.py:389: unused attribute 'SearchString' (60% confidence)
plugin/writer/format.py:306: unused variable 'direct_formatting' (60% confidence)
plugin/writer/format.py:308: unused variable 'preserved_char_overrides' (60% confidence)
plugin/writer/format.py:309: unused variable 'removed_char_overrides' (60% confidence)
plugin/writer/format.py:310: unused variable 'cleared_char_properties' (60% confidence)
plugin/writer/format.py:311: unused variable 'cleared_paragraph_properties' (60% confidence)
plugin/writer/format.py:313: unused variable 'capture_incomplete' (60% confidence)
plugin/writer/html_import_styles.py:79: unused method 'handle_starttag' (60% confidence)
plugin/writer/html_import_styles.py:101: unused method 'handle_startendtag' (60% confidence)
plugin/writer/html_import_styles.py:110: unused method 'handle_endtag' (60% confidence)
plugin/writer/html_ruby.py:269: unused variable 'visible_offset' (60% confidence)
plugin/writer/image_placeholders.py:190: unused attribute 'SearchString' (60% confidence)
plugin/writer/image_placeholders.py:191: unused attribute 'SearchCaseSensitive' (60% confidence)
plugin/writer/images/image_tools.py:661: unused function 'replace_image_in_place' (60% confidence)
plugin/writer/inline_review.py:195: unused function 'has_agent_changes' (60% confidence)
plugin/writer/inline_review.py:320: unused function 'agent_changes' (60% confidence)
plugin/writer/inline_review.py:444: unused function 'pending_agent_change_count' (60% confidence)
plugin/writer/inline_review.py:1093: unused function 'resolve_all_agent_changes' (60% confidence)
plugin/writer/locale/ai_grammar_proofreader.py:402: unused attribute 'aProperties' (60% confidence)
plugin/writer/locale/ai_grammar_proofreader.py:403: unused attribute 'xProofreader' (60% confidence)
plugin/writer/locale/ai_grammar_proofreader.py:551: unused attribute 'nErrorStart' (60% confidence)
plugin/writer/locale/ai_grammar_proofreader.py:552: unused attribute 'nErrorLength' (60% confidence)
plugin/writer/locale/ai_grammar_proofreader.py:554: unused attribute 'nErrorStart' (60% confidence)
plugin/writer/locale/ai_grammar_proofreader.py:555: unused attribute 'nErrorLength' (60% confidence)
plugin/writer/locale/ai_grammar_proofreader.py:556: unused attribute 'nErrorType' (60% confidence)
plugin/writer/locale/ai_grammar_proofreader.py:558: unused attribute 'aSuggestions' (60% confidence)
plugin/writer/locale/ai_grammar_proofreader.py:559: unused attribute 'aShortComment' (60% confidence)
plugin/writer/locale/ai_grammar_proofreader.py:560: unused attribute 'aFullComment' (60% confidence)
plugin/writer/locale/ai_grammar_proofreader.py:561: unused attribute 'aProperties' (60% confidence)
plugin/writer/locale/ai_grammar_proofreader.py:865: unused variable 'aProperties' (100% confidence)
plugin/writer/locale/grammar_persistence.py:862: unused attribute 'SearchCaseSensitive' (60% confidence)
plugin/writer/locale/grammar_work_queue.py:118: unused function 'deduplicate_grammar_batch' (60% confidence)
plugin/writer/locale/harper.py:489: unused function 'shutdown_harper_runtime' (60% confidence)
plugin/writer/math/latex_dialog.py:86: unused attribute 'FontDescriptor' (60% confidence)
plugin/writer/page.py:1823: unused attribute 'SearchString' (60% confidence)
plugin/writer/page.py:1824: unused attribute 'SearchRegularExpression' (60% confidence)
plugin/writer/page.py:1825: unused attribute 'SearchCaseSensitive' (60% confidence)
plugin/writer/search.py:251: unused attribute 'SearchString' (60% confidence)
plugin/writer/search.py:252: unused attribute 'SearchRegularExpression' (60% confidence)
plugin/writer/search.py:253: unused attribute 'SearchCaseSensitive' (60% confidence)
plugin/writer/search.py:341: unused attribute 'SearchRegularExpression' (60% confidence)
plugin/writer/search.py:342: unused attribute 'SearchString' (60% confidence)
plugin/writer/search.py:346: unused attribute 'SearchCaseSensitive' (60% confidence)
plugin/writer/search.py:354: unused attribute 'SearchCaseSensitive' (60% confidence)
plugin/writer/search.py:374: unused attribute 'SearchRegularExpression' (60% confidence)
plugin/writer/search.py:375: unused attribute 'SearchString' (60% confidence)
plugin/writer/search.py:378: unused attribute 'SearchCaseSensitive' (60% confidence)
plugin/writer/search.py:575: unused attribute 'SearchRegularExpression' (60% confidence)
plugin/writer/search.py:576: unused attribute 'SearchString' (60% confidence)
plugin/writer/search.py:577: unused attribute 'SearchCaseSensitive' (60% confidence)
plugin/writer/search.py:881: unused attribute 'SearchString' (60% confidence)
plugin/writer/search.py:882: unused attribute 'SearchRegularExpression' (60% confidence)
plugin/writer/search.py:883: unused attribute 'SearchCaseSensitive' (60% confidence)
plugin/writer/search.py:912: unused function 'all_start_indices' (60% confidence)
plugin/writer/search.py:1024: unused attribute 'SearchString' (60% confidence)
plugin/writer/search.py:1025: unused attribute 'SearchRegularExpression' (60% confidence)
plugin/writer/search.py:1026: unused attribute 'SearchCaseSensitive' (60% confidence)
plugin/writer/specialized/comments.py:637: unused attribute 'Seconds' (60% confidence)
plugin/writer/specialized/footnotes.py:32: unused attribute 'SearchString' (60% confidence)
plugin/writer/specialized/footnotes.py:33: unused attribute 'SearchRegularExpression' (60% confidence)
plugin/writer/specialized/footnotes.py:34: unused attribute 'SearchCaseSensitive' (60% confidence)
plugin/writer/specialized/indexes.py:480: unused variable 'fallback_message' (100% confidence)
plugin/writer/xhtml_style_postprocess.py:186: unused function 'extract_autostyle_parents_from_fodt' (60% confidence)
plugin/writer/xhtml_style_postprocess.py:199: unused function 'extract_autostyle_overrides_from_fodt' (60% confidence)
plugin/writer/xhtml_style_postprocess.py:274: unused method 'handle_data' (60% confidence)
plugin/writer/xhtml_style_postprocess.py:278: unused method 'handle_entityref' (60% confidence)
plugin/writer/xhtml_style_postprocess.py:282: unused method 'handle_charref' (60% confidence)
plugin/writer/xhtml_style_postprocess.py:286: unused method 'handle_comment' (60% confidence)
plugin/writer/xhtml_style_postprocess.py:413: unused method 'handle_starttag' (60% confidence)
plugin/writer/xhtml_style_postprocess.py:430: unused method 'handle_startendtag' (60% confidence)
plugin/writer/xhtml_style_postprocess.py:434: unused method 'handle_endtag' (60% confidence)
```
