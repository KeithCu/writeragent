# LLM Hacks and Workarounds

This document tracks technical workarounds and "hacks" implemented to handle the quirks, inconsistencies, and limitations of LLMs when interacting with LibreOffice tools.

## 1. Delimited text in `write_range`
**Problem**: A plain string in `write_range` is one cell value. If a model sends a whole table or delimited row as one string (with line breaks, `,`, or `;`), it would silently land in a single cell or repeat across the range with `status: ok`.

### [Workaround] Refusal Guard (`delimited_text_message`)
In `plugin/calc/manipulator.py`, `delimited_text_message` refuses plain (non-formula, non-JSON) strings with `INVALID_PARAM` when:
- the string contains `\n` (any target); or
- the string contains `,` or `;` and the target has more than one cell.

Formulas (leading `=` after strip) are exempt so `=PY` code with line breaks and formulas with `;` are untouched. One-line prose with commas or semicolons into a single cell stays literal. To keep line breaks inside one cell, the caller sends `[["a\nb"]]`.

## 2. JSON-string arrays
**Problem**: LLMs sometimes send a 1-D or 2-D array serialized as a JSON string (e.g., `'["Name"; "Age"; "Salary"]'`) instead of a native JSON array.

### [Workaround] `parse_json_array_string`
`parse_json_array_string` in `plugin/calc/manipulator.py` parses strings starting with `[` as JSON arrays (normalizing Calc-style `;` separators outside quotes to `,`). The old one-line `Name;Age;Salary` and multiline CSV splits were removed in favour of the `INVALID_PARAM` refusal above.

**Preferred shape:** Tool schemas advertise a 2-D native array (one inner array per row).

## 3. Formula-Safe JSON Normalization
**Problem**: LLMs often use semicolons inside JSON arrays (e.g., `["A"; "B"]`). Standard `json.loads` fails here. A blind `replace(";", ",")` fix breaks real formulas that *need* semicolons (e.g., `=SUM(A1;B1)` becomes `=SUM(A1,B1)` which is an error in Calc).

### [Workaround] Character-Loop Normalization
In `plugin/calc/manipulator.py`, `parse_json_array_string` walks the string with a character loop tracking double-quoted strings and backslash escapes.
- **Behavior**: This only replaces semicolons that are **not** inside double quotes with commas. This preserves semicolons inside formula strings while fixing the JSON structure.

## 4. Formula dialect acceptance
**Problem**: Calc's UNO API expects `GRAM_API` (`;` separators, `Sheet.A1` dot sheet refs, `{1;2|3;4}` inline arrays), while models frequently emit Excel syntax (`,`, `Sheet!A1`, `{1,2;3,4}`).

### [Solution] `FormulaDialects.to_api`
[`plugin/calc/formula_dialect.py`](../../plugin/calc/formula_dialect.py) translates incoming formula text via Calc's own `com.sun.star.sheet.FormulaParser` cascade (Calc API grammar as written first, then Excel English, then Calc punctuation with `Sheet!A1`) before writing, so both Excel and Calc syntax work without prompt steering.

## 5. Schema-driven argument coercion
**Problem**: Models frequently send numeric strings (`"1"`), boolean words (`"true"`, `"yes"`), whole floats (`1.0`) for integers, bare values or JSON strings for arrays/objects, or case/hyphen/typo variants of enum values.

### [Solution] `coerce_call_args` in `prepare_tool_call`
[`coerce_call_args`](../../plugin/framework/tool_schema.py) runs inside [`prepare_tool_call`](../../plugin/framework/tool.py) before `ToolBase.validate()` on every path (main chat, MCP, `wa.*` scripts, and smol sub-agents via `WriterAgentToolCallingAgent` skipping smolagents' pre-validator for adapters):
- **Numeric strings**: ASCII digits and finite numbers convert to `int`/`float`; `integer` properties accept whole values only (`"3.0"` → `3`, `"3.5"` stays rejected).
- **Booleans**: case-insensitive `true`/`yes`/`1` and `false`/`no`/`0` or ints `0`/`1` convert to `bool`.
- **Strings from numbers**: non-bool `int`/`float` convert to `str` when the schema type includes `string`.
- **Arrays & objects**: JSON strings starting with `[` or `{` are parsed; a parsed dict becomes `[dict]` for array-only types, and other bare non-list values wrap into `[value]` for arrays. Nested `properties`, `items`, and `additionalProperties` maps (when `patternProperties` is absent) are walked recursively; union items and array items are not converted (a bare row of an array-of-arrays stays unwrapped so the tool's row-type check still runs), and strings for `array`/`string` unions are not parsed. Colour parameters use `color_param` (`["string", "integer"]`, including as list `items`) so RGB integers stay integers instead of stringifying into hex shorthands.
- **Unique-match enums**: string enum misses are matched only when a unique string member matches under (a) casefold + space/hyphen→`_`, (b) plural `s`, or (c) one edit for values of 5+ characters; ambiguous or unmatched values fall through to `VALIDATION_ERROR`.
- **Nested optional nulls**: `null` on declared optional properties inside nested objects (including `items` and `additionalProperties` objects) is dropped unless the property's `type`, `enum`, or `nullable` allows null (or `type` is missing).
- **Schema defaults**: after `validate()` passes, `prepare_tool_call` fills omitted optional parameters from the schema `"default"` (deep-copied). Properties without a `"default"` stay omitted (three-state parameters rely on this); `RemoteMcpTool` opts out via `fills_schema_defaults=False`. A parameter with a schema `"default"` is read as `kwargs[name]` in `execute` (no second literal); a test that calls `execute` directly passes it explicitly or runs `prepare_tool_call` first. Config-driven values such as `image_generate`'s size and aspect ratio take no schema default.

## 6. Robust Linebreak Normalization
**Problem**: LLMs (especially when streaming or acting as a sub-agent) are inconsistent with line endings, often mixing `\n`, `\r\n`, and occasionally `\n\r` or legacy `\r`. This breaks structural operations like paragraph splitting (`split("\n\n")`) and can cause search/replace failures if the AI's response uses a different sequence than the document's internal representation.

### [Workaround] Centralized Normalization
The `normalize_linebreaks` utility in `plugin/doc/text_helpers.py` ensures all incoming and outgoing text uses a consistent `\n` (LF) sequence.
- **Implementation**: It performs a chain of replacements: `text.replace("\r\n", "\n").replace("\n\r", "\n").replace("\r", "\n")`.
- **Reasoning**: This prevents "invisible character" mismatches. Since LibreOffice internally represents hard line breaks as `\n` in UNO strings, this ensures that the text we process, the text the AI sees, and the text we write back into the document are byte-compatible for string operations.

## 7. Local LLM thinking (Ollama vs LM Studio) — sidebar `[Thinking]` gap

**Reported symptom**: Wireshark on Ollama and LM Studio both show OpenAI-style streaming JSON where `choices[0].delta.content` sometimes includes the closing think marker (JSON-escaped as `"\u003c/think\u003e"`, i.e. ``). LM Studio’s reasoning often appears in the chat sidebar under `[Thinking]`; Ollama’s often does not.

**Not a transport bug**: Unicode escapes in JSON are normal. A lone `` in `content` does **not** mean both backends behave the same — it is often just a closing marker while the real trace arrived on another delta key.

### How WriterAgent routes stream text today

Main chat: `LlmClient._run_streaming_loop` → `OpenAIShim.parse_response_chunk` → queue → `run_stream_drain_loop` ([`plugin/framework/async_stream.py`](../../plugin/framework/async_stream.py)).

| Wire source | Code path | Sidebar appearance |
|-------------|-----------|-------------------|
| `delta.reasoning_content`, `delta.reasoning`, `delta.thinking`, `delta.thought`, `delta.reasoning_details` | `extract_thinking_from_delta` in [`stream_normalizer.py`](../../plugin/framework/client/stream_normalizer.py) → `append_thinking_callback` → `StreamQueueKind.THINKING` | Drain loop prefixes `[Thinking] ` then text; closes with ` /thinking\n` |
| `delta.content` (including `<think>` … `</think>` text) | `ThinkTagStreamSplitter` / `strip_think_tags` in [`stream_normalizer.py`](../../plugin/framework/client/stream_normalizer.py) | Tags stripped; thinking routed to `StreamQueueKind.THINKING` and clean content to `StreamQueueKind.CHUNK` |

**Tag parsing and remaining edge cases:** `ThinkTagStreamSplitter` runs in `llm_client._run_streaming_loop` and `strip_think_tags` runs on completed responses. Both handle standard `<think>...</think>`, unclosed `<think>` (all text after `<think>` is treated as thinking, a deliberate tradeoff so models that omit closing tags do not dump thinking into the document/chat), and close-only `</think>`. When not inside thinking, a trailing partial `</` (prefix of length >= 2) is held in the buffer until the next chunk or `flush` rather than guessing early; close-only across chunks still classifies the prefix as thinking once `</think>` completes. Extend/Edit drops thinking chunks, which is why a wrong classification deletes document text. After a real `<think>` block has opened and closed, any later `</think>` is treated as literal content rather than close-only thinking. In `strip_think_tags`, every close-only separator is stripped when the original text had no `<think>`, not only the first. Alternate tags like `<thinking>` or case variations (`<THINK>`) are intentionally left untouched to avoid eating ordinary prose that mentions those tags. A bare `<` split exactly before `/think>` remains an open-tag hold and can leak the prefix into content.

### Root cause (web research, revised)

**Primary (Ollama + Qwen3 / thinking models):** On `/v1/chat/completions`, Ollama often streams thinking in **`delta.reasoning`**, not `delta.reasoning_content`. WriterAgent reads both (see `_THINKING_STRING_FIELDS` in [`stream_normalizer.py`](../../plugin/framework/client/stream_normalizer.py)).

**Why LM Studio often works:** Developer setting **“When applicable, separate reasoning_content and content in API responses”** maps reasoning into `reasoning_content`, which WriterAgent already reads ([LM Studio #480](https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/480)). Without it, models still emit `<think>` inside `content` ([LM Studio #1569](https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/1569)).

**Secondary (both backends):** When separation fails, thinking stays in `content` between think tags; `ThinkTagStreamSplitter` strips it from visible text and routes it to `[Thinking]`.

**Other context:**

- Ollama **native** `/api/chat` uses `message.thinking` ([Ollama thinking](https://docs.ollama.com/capabilities/thinking)); WriterAgent uses `/v1/chat/completions` only.
- A non-empty sidebar level is sent through `effort_payload` in `base_provider_shim.py` (`reasoning.effort` on OpenRouter, `output_config.effort` on Anthropic, top-level `reasoning_effort` elsewhere), and an empty level sends nothing. Mapping is per provider, never per model: `_PROVIDER_EFFORTS` lists the effort words each provider's current API documents, and a level the provider lacks becomes the nearest lower word (Gemini `max` → `high`). Custom, LM Studio and other unlisted hosts get the level unchanged. When a model rejects the value (HTTP 400 mentioning "effort", before any output), a 400 whose text contains `does not support the effort parameter` sends one omit body and, on success, is remembered for that endpoint and model at every sidebar level. Any other HTTP 400 that mentions effort still walks lower, then higher, then no field. Those attempts consume the same per-request `RetryBudget` as other sync-path retries (429/503, connection reset). Only grammar's `minimal` may send a thinking-off word (Mistral `none`); a rejected `low` on Mistral Small 4 moves up to `high`, not down to `none`.
- **`chatbot.show_search_thinking`**: tool/web paths only, not main LLM stream ([`tool_loop.py`](../../plugin/chatbot/tool_loop.py)).

### Verification (one capture, same prompt/model)

Compare full SSE deltas (not a single packet):

| Field | Typical Ollama (Qwen3) | Typical LM Studio (setting on) |
|-------|------------------------|--------------------------------|
| `delta.reasoning` | Non-empty during think phase | Often empty |
| `delta.reasoning_content` | Often empty | Non-empty during think phase |
| `delta.content` | Answer + sometimes `<think>` markers | Answer + sometimes `<think>` markers |

If Ollama has `reasoning` and LM Studio has `reasoning_content`, the one-line field fix below is sufficient for many reports.

### Workarounds still open

**Step 1 (done):** `delta.reasoning` is in the string field list; chunks are normalized via `choices[0].delta` once (no recursion). Tests in `tests/framework/test_stream_normalizer.py`.

**Step 2 (done):** Stateful `ThinkTagStreamSplitter` and `strip_think_tags` in `stream_normalizer.py` split `<think>` / `</think>` inside `content`, holding partial `</` prefixes until resolved, preserving literal `</think>` tags after a real `<think>` block, and stripping all close-only separators when no `<think>` was present. Extend/Edit drops thinking chunks, which is why wrong classification deletes document text. Chunks are normalized via `choices[0].delta`.

**Step 3 (optional):** `OllamaShim.build_chat_request` — `"think": true` or `reasoning_effort` for thinking-capable models (gate by model name); only if separated fields still missing.

**Reporter / LM Studio users:** Enable “separate reasoning_content and content” in Developer settings if thinking should appear in clients that only read `reasoning_content`.

**Debugging a provider:** Reproduce with `python scripts/run_streaming_test.py` and inspect `writeragent_debug.log` for `stream thinking: no extractable text`.

See also: [`../framework/streaming-and-threading.md`](../framework/streaming-and-threading.md) §3 (reasoning in streams).

## 8. Smolagents tool-call echo mistaken for final_answer

**Problem**: Web-research / librarian sub-agents (`ToolCallingAgent`) teach the model `Action:\n{"name": ..., "arguments": ...}` in few-shot examples, but older memory replay used `Calling tools:\n` + Python `str([tc.dict()])` (single-quoted repr). When the API returned no native `tool_calls`, the model sometimes echoed that memory shape. `parse_json_blob` only accepted strict JSON, parsing failed, and `agents.py` treated the whole blob as `final_answer` — so the sidebar showed raw tool-call text instead of running `web_search`.

### [Workaround] Parse + guard + aligned memory

- **`parse_json_blob` / `get_tool_call_from_text`** ([`plugin/contrib/smolagents/utils.py`](../../plugin/contrib/smolagents/utils.py), [`models.py`](../../plugin/contrib/smolagents/models.py)): strip `Calling tools:` / `Action:` prefixes, unwrap `[{...}]` lists, fall back to `ast.literal_eval` for Python repr, normalize OpenAI nested `{function: {name, arguments}}` to flat smol shape.
- **`content_looks_like_tool_call`** + guard in [`agents.py`](../../plugin/contrib/smolagents/agents.py): if parsing still fails but text looks like a tool invocation, raise `AgentParsingError` instead of finishing with garbage.
- **`ActionStep.to_messages`** ([`memory.py`](../../plugin/contrib/smolagents/memory.py)): replay tool steps as `Action:` + JSON (matches examples).
- **`WriterAgentSmolModel.generate`** ([`smol_agent.py`](../../plugin/chatbot/smol_agent.py)): post-hoc `remove_content_after_stop_sequences` on content (stop is not on the HTTP wire today).
- **Answer-only JSON** (e.g. Inception Mercury 2): models sometimes emit `{"answer": ["<p>…</p>", …]}` (document-array habit) instead of `{"name": "final_answer", "arguments": …}`. `try_parse_implicit_final_answer_tool_call` in [`utils.py`](../../plugin/contrib/smolagents/utils.py) joins HTML arrays into one sidebar string before `final_answer` runs; `ast.literal_eval` is limited to Python-repr blobs to avoid `SyntaxWarning` on LaTeX `\\(` in JSON.

Tests: [`tests/contrib/smolagents/test_tool_call_parsing.py`](../../tests/contrib/smolagents/test_tool_call_parsing.py).

---

## 9. Optional tool params and strict provider validation (Groq)

**Problem**: Some providers (notably Groq) validate tool calls against the JSON Schema before WriterAgent runs the tool. Models often send `"max_chars": null` (or omit meaning “use default”) for optional parameters. A wire schema of `"type": "integer"` rejects `null`, so the whole call fails upstream — e.g. `get_content` with `max_chars: null`.

**Solution**: [`to_openai_schema` / `to_mcp_schema`](../../plugin/framework/tool_schema.py) (re-exported from `tool.py`) run `_normalize_schema_for_strict_providers`, which emits `"type": [scalar, "null"]` for **optional** scalar properties (`integer`, `number`, `boolean`, `string`), including an optional union of those scalars (`["string", "number"]` becomes `["string", "number", "null"]`). Required parameters stay non-nullable. Every other source type stays too: `apply_content` `content` remains `["array", "string"]` because `validate` and execute accept a string or a list. A schema with `properties` and no `type` is still walked. `items` stays when `type` is missing or includes `array`. Tool implementations already treat omitted/`null` as default (e.g. no truncation when `max_chars` is null).

**MCP vs OpenAI**: ``to_mcp_schema`` passes ``for_mcp=True`` into ``_normalize_schema_for_strict_providers``, so optional properties with an ``enum`` do not get JSON ``null`` in ``type`` or ``enum`` (Gemini rejects null enum elements when agy forwards MCP schemas). Non-enum optional scalars on MCP still get ``["scalar", "null"]``. The OpenAI wire from ``to_openai_schema`` is unchanged.

Tests: [`tests/framework/test_tool_schema_convert.py`](../../tests/framework/test_tool_schema_convert.py).

---

## 9.1 One tool-call check before the provider shim

**Problem**: An empty ``properties: {}`` was treated as "no schema", so a no-arg tool such as ``sheet_list`` accepted hallucinated kwargs. The same name registered twice (Writer/Calc ``shape_upsert``) kept only the last ``required_core_tools``. Each provider shim then built the next request from the unchecked call.

**Solution**: [`call_properties`](../../plugin/framework/tool_schema.py) / [`without_unknown_kwargs`](../../plugin/framework/tool_schema.py) are the allow-list. ``properties: {}`` is closed. [`normalize_outbound_tool_calls`](../../plugin/framework/tool_schema.py) runs in [`make_chat_request`](../../plugin/framework/client/llm_client.py) before ``build_chat_request``, so OpenAI and Anthropic format the same checked arguments. Duplicate advertised names union ``properties`` instead of last-wins. A tool name has exactly one class; a duplicate registration raises. Shared shape and chart classes declare every app's readers. ``ToolRegistry.execute`` strips with the same helper before ``validate``.

Tests: [`tests/framework/test_tool.py`](../../tests/framework/test_tool.py), [`tests/framework/test_tool_schema_convert.py`](../../tests/framework/test_tool_schema_convert.py), [`tests/framework/test_client_llm.py`](../../tests/framework/test_client_llm.py).

---

## 9.2 Anthropic Messages usage, tool ids, and image URLs

**Problem**: The native Messages shim (`plugin/framework/client/anthropic_shim.py`) speaks OpenAI-shaped chat on the inside and `/v1/messages` on the wire.

- Stream usage: `message_start` nests input tokens under `message.usage`. `message_delta.usage` is a cumulative total (`output_tokens` goes from 1 to 15, not 1+15). Summing those counts reports 16. Sync parse already aliased `input_tokens` → `prompt_tokens`; the stream path did not, so the main chat result kept Anthropic's names and dropped input tokens.
- A tool result with no `tool_call_id` used the tool name as `tool_use_id`. Anthropic returns HTTP 400. A missing id was also stored as `None` in the dropped-id set, which then swallowed every later tool message that also had no id.
- An `http(s)` image part was dropped. Messages accepts `source.type` `url` as well as base64 (see `docs/images/generation.md`).
- A blank Settings model was sent as the retired id `claude-3-5-sonnet-20241022`.

**Solution**: Normalize usage in one helper and replace usage keys on the stream snapshot instead of adding them. Require a non-empty string `tool_call_id`. Map `http(s)` images to `source.type = "url"`. Raise `NetworkError` when the Anthropic model is blank.

Tests: [`tests/framework/client/test_anthropic_shim.py`](../../tests/framework/client/test_anthropic_shim.py), [`tests/framework/test_client_llm.py`](../../tests/framework/test_client_llm.py).

---

## 10. Calc `=PROMPT()` empty cell (reasoning vs content)

**Problem**: Some reasoning models (e.g. Inception Mercury) spend `max_tokens` on `reasoning_tokens` and return `"content": null` with `finish_reason: length`. [`chat_completion_sync`](../../plugin/framework/client/llm_client.py) coerces missing content to `""`, so Calc showed a blank cell with no error.

**Workaround** ([`plugin/calc/prompt_function.py`](../../plugin/calc/prompt_function.py)):
- Default `calc_prompt_max_tokens` is **4096** (override via formula 4th arg or `writeragent.json`). Values below 100 (e.g. the old shipped default of 70) are upgraded to 4096 on load.
- `=PROMPT()` uses `request_with_tools` (full result dict). If content is empty, the cell gets a visible diagnostic (`finish_reason`, token usage, model, short reasoning excerpt when present) instead of a silent blank.
- Default system prompt is a short plain-text cell instruction (`CALC_PROMPT_CELL_SYSTEM_PROMPT` in [`prompt_function.py`](../../plugin/calc/prompt_function.py)), not the long `=PY` sandbox hint.

Tests: [`tests/calc/test_prompt_function.py`](../../tests/calc/test_prompt_function.py).

---

## 11. Ollama / llama.cpp prompt overflow (HTTP 500)

**Problem**: A WriterAgent chat request can exceed Ollama's *live* `n_ctx` (often a VRAM-based 4096 on small GPUs) even when the model's trained `context_length` is 32K. Ollama logs `truncating input prompt` and llama.cpp can die at first prefill (`llama-server process has terminated`, Windows `0xc0000005`). The sidebar used to dump the raw `{status, code, message, details}` HTTP 500 dict.

**What this is not:** WriterAgent does not silently trim document/history, refuse the send, or enforce a minimum context floor. Those are later product decisions.

**Workaround** (issue #570):

- [`format_http_error_response`](../../plugin/framework/client/errors.py) and [`_handle_stream_error`](../../plugin/chatbot/tool_loop.py) detect llama-server terminated / `0xc0000005` / prompt overflow and show a plain sentence that the local Ollama/llama.cpp process crashed because the prompt overflowed a too-small context. Live `num_ctx` from cached Ollama `POST /api/show` (`parameters` / Modelfile only) is named when known (e.g. 4K). Missing `/api/show` still emits the generic overflow sentence.
- Compact-and-retry (PR2): when `chat_compaction_enabled` and the drain error is prompt-too-large (`is_context_overflow_error` — e.g. `prompt is too long`, `truncating input prompt`), `_handle_stream_error` respawns `_spawn_llm_worker(..., force_compact=True)` up to 3 times instead of appending that sentence. Process death (`llama-server process has terminated`, `0xc0000005`) is **not** overflow and still shows the plain sentence with no retry. `nothing_to_compact` / `no_window` / `failed` / shrink `< 5%` also skip retry. The JSON kill switch disables **both** proactive compact and this overflow path. Compaction itself runs on the worker inside the existing `llm_request_lane`, never on the drain thread.
- ERROR logs stay loud: the existing provider-body line plus [`log_http_500_request_diag`](../../plugin/framework/client/errors.py) (PR 571 shape: provider, model, host/path, stream, message/tool counts, payload bytes — plus `n_ctx`, `prompt_chars`, `max_tokens`, `exit_code`). No prompt text or tool schemas.

Tests: [`tests/framework/client/test_client_errors.py`](../../tests/framework/client/test_client_errors.py), [`tests/framework/test_client_llm.py`](../../tests/framework/test_client_llm.py), [`tests/chatbot/test_tool_loop_errors.py`](../../tests/chatbot/test_tool_loop_errors.py), [`tests/framework/client/test_model_fetcher.py`](../../tests/framework/client/test_model_fetcher.py).

---

## 12. OpenAI reasoning models reject `max_tokens`

**Problem**: `api.openai.com` o1, o3, o4, and gpt-5 (including dated snapshots and `ft:gpt-5-…`) return HTTP 400 for `max_tokens` and for any `temperature` other than the API default `1`.

**Workaround** ([`OpenAIShim.build_chat_request`](../../plugin/framework/client/openai_shim.py)): when the provider is `openai` and the model is in those families, the chat body sends `max_completion_tokens` and drops a non-default temperature. Other providers, including OpenRouter and Groq using an OpenAI-compatible body, keep `max_tokens`. Image `response_format` is unchanged.

Tests: [`tests/framework/test_client_llm.py`](../../tests/framework/test_client_llm.py).

---

## 13. Provider sticky cache routing (`session_id`)

**Problem**: Large LLM providers support sticky routing or explicit cache keys so multi-turn conversations hit warm prompt caches across distributed clusters. Different providers use different fields, caps, or transport mechanisms:
- **OpenRouter**: Top-level body `session_id` (capped at 256 characters; body takes priority over `x-session-id`).
- **OpenAI**: Chat Completions and Responses API use body `prompt_cache_key` (no length cap).
- **Cerebras**: Chat Completions body `prompt_cache_key` (capped at 1024 characters).
- **xAI**: Header `x-grok-conv-id` (native xAI stays on Chat Completions via `GrokShim`, so header is used; omitted if containing non-printable ASCII).

Anthropic Messages has no session routing key (cache breakpoints use explicit `cache_control` blocks via `annotate_anthropic_cache`). Instead, native Anthropic requests send `diagnostics: {"previous_message_id": <msg_...>}` to receive cache hit/miss metadata, storing the last `msg_...` identifier on the panel's `LlmClient` keyed by `cache_session_id`. Other providers do not send or receive this field. Non-chat requests (compaction, smol, grammar, images, web research, forms, `=PROMPT()`) do not pass `cache_session_id`, sending `null` and leaving any active chat session ID untouched. Every unnamed host (`google`, `groq`, `ollama`, `together`, `mistral`, `custom`) gets nothing to avoid HTTP 400 rejection on unknown fields.

`cache_read_count` normalizes cache read metrics across providers, checking `cache_read_input_tokens` (Anthropic), `prompt_tokens_details.cached_tokens` (OpenAI and OpenRouter), and `prompt_cache_hit_tokens` (DeepSeek).

**Workaround** ([`cache_route`](../../plugin/framework/client/base_provider_shim.py)):
`ChatSession.session_id` is passed as `cache_session_id` during tool-loop and final stream requests, applying provider-specific body fields or headers without altering message history or system prompt text. The chat session id overrides any `session_id` in the model's extra JSON, on purpose, so every turn of one chat keeps the same routing key. All librarian chats share [`LIBRARIAN_HISTORY_SESSION_ID`](../../plugin/chatbot/chat_sidebar_mode.py), which is harmless at one user's request rate.

See also: [chat-prompt-caching.md](chat-prompt-caching.md) (document-snapshot layout and Anthropic `cache_control` markers).

Tests: [`tests/framework/client/test_openai_shim.py`](../../tests/framework/client/test_openai_shim.py), [`tests/framework/client/test_anthropic_shim.py`](../../tests/framework/client/test_anthropic_shim.py), [`tests/chatbot/test_tool_loop_errors.py`](../../tests/chatbot/test_tool_loop_errors.py).

---

## 14. gpt-oss harmony tag on tool names; unknown-tool suggestions

**Problem**:
1. Models using the harmony chat format (such as `gpt-oss`) sometimes leak a trailing channel tag onto a tool call's function name (e.g. `read_range<|channel|>commentary`), causing `UNKNOWN_TOOL` even though the base tool name and arguments are valid.
2. When a model genuinely hallucinates a tool name (e.g. `write_formulas`), an error that only says the name is not registered gives no hint toward the real tool (`write_range`).

**Workarounds**:
- **Name cleanup** ([`coalesce_split_tool_calls`](../../plugin/framework/stream_delta.py)): `_tool_name_without_template_suffix` drops everything from the first `<|` onward on non-empty function names (leaving tag-only names intact so they are not merged as split fragments). Because `LlmClient` routes both streaming and sync tool calls through `coalesce_split_tool_calls`, main chat, smol/specialized sub-agents, and the eval harness all receive clean tool names before execution and history storage.
- **Unknown-tool suggestions** ([`ToolRegistry._unknown_tool_error`](../../plugin/framework/tool.py)): `UNKNOWN_TOOL` runs `difflib.get_close_matches` against the offered tools for the current `doc_type`, `uno_services_supported`, and `active_domain` (respecting default tier exclusions), returning up to 3 close names in the error message and in `details.suggestions`.

Tests: [`tests/framework/test_stream_delta.py`](../../tests/framework/test_stream_delta.py), [`tests/framework/test_tool.py`](../../tests/framework/test_tool.py).

---

*This document should be updated as new hacks are discovered or as improvements in models allow us to remove them.*

## Open

- `[fidelity S few]` Send `"think": true` or a reasoning effort from `OllamaShim.build_chat_request` for thinking-capable models if Ollama's reasoning still does not reach the sidebar. (§ 7. Local LLM thinking, Step 3)
- `[fidelity S few]` Stop a bare `<` split exactly before `/think>` from leaking into content in the think-tag splitter. (§ 7. Local LLM thinking, Tag parsing and remaining edge cases)
