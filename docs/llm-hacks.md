# LLM Hacks and Workarounds

This document tracks technical workarounds and "hacks" implemented to handle the quirks, inconsistencies, and limitations of LLMs when interacting with LibreOffice tools.

## 1. CSV Delimiter Handling
**Problem**: LLMs are inconsistent with CSV delimiters. Since Calc formula syntax favors semicolons (`;`), models often use them for CSV data even when asked for commas (`,`). This leads to data being imported into a single column.

### [Workaround] Auto-Detection
Instead of requiring a `delimiter` parameter, the tool `write_formula_range` now handles it automatically in `plugin/calc/manipulator.py` when CSV data is provided.
- **Implementation**: The tool peeks at the first few lines. If semicolons are significantly more prevalent than commas, it switches the CSV reader to `;`. Otherwise, it defaults to `,`.
- **Reasoning**: Reduces the cognitive load on the LLM and makes the tool "just work" regardless of the model's preferred separator.

## 2. Range Writing Semicolon Splitting
**Problem**: When using `write_formula_range`, the LLM often sends a raw string like `"Name;Age;Salary"` instead of a JSON-encoded array `["Name", "Age", "Salary"]`. Without a workaround, this writes the entire string into a single cell.

### [Workaround] Raw String Splitting
The internal `_parse_formula_or_values_string` helper in `core/calc_manipulator.py` detects raw semicolon-separated strings.
- **Formula-Safe Detection**: It skips strings starting with `=` (standard formulas) but splits other strings containing `;` using `csv.reader`.
- **CSV Reader**: Using `csv.reader` (with `skipinitialspace=True`) ensures that if the LLM puts a semicolon inside a quoted string, it won't be split incorrectly.

## 3. Formula-Safe JSON Normalization
**Problem**: LLMs often use semicolons inside JSON arrays (e.g., `["A"; "B"]`). Standard `json.loads` fails here. A blind `replace(";", ",")` fix breaks real formulas that *need* semicolons (e.g., `=SUM(A1;B1)` becomes `=SUM(A1,B1)` which is an error in Calc).

### [Workaround] Regex-Bound Replacement
In `core/calc_manipulator.py`, we use a regular expression to normalize JSON arrays before parsing.
- **Regex**: `re.sub(r';(?=(?:[^"]*"[^"]*")*[^"]*$)', ',', s_strip)`
- **Behavior**: This only replaces semicolons that are **not** inside double quotes. This preserves semicolons inside formula strings while fixing the JSON structure.

## 4. Prompt Steering for Syntax
**gotcha**: LibreOffice is very strict about formula syntax. Using a comma instead of a semicolon as an argument separator results in "Error 508".

### [Workaround] Explicit Prompt Rules
In `core/constants.py`, the system prompt includes high-pressure instructions on formula syntax.
- **Constraint**: "FORMULA SYNTAX: LibreOffice uses semicolon (;) as the formula argument separator. Wrong: =SUM(A1,A10) (no commas)."
- **Duality**: We also explicitly warn about CSV: "CSV DATA: Use comma (,) for write_formula_range." to counteract the formula semicolon rule.

## 5. Defensive Parameter Handling
**Problem**: Models sometimes pass range names as lists or wrap them in extra quotes.

### [Workaround] Multi-Type Dispatcher
In `core/calc_tools.py`, the `execute_calc_tool` dispatcher often checks if `range_name` is a list or a single string.
- **Looping**: If the model passes a list of ranges (hallucination or efficiency attempt), the code loops over them automatically rather than crashing.

## 6. Robust Linebreak Normalization
**Problem**: LLMs (especially when streaming or acting as a sub-agent) are inconsistent with line endings, often mixing `\n`, `\r\n`, and occasionally `\n\r` or legacy `\r`. This breaks structural operations like paragraph splitting (`split("\n\n")`) and can cause search/replace failures if the AI's response uses a different sequence than the document's internal representation.

### [Workaround] Centralized Normalization
The `normalize_linebreaks` utility in `plugin/doc/document_helpers.py` ensures all incoming and outgoing text uses a consistent `\n` (LF) sequence.
- **Implementation**: It performs a chain of replacements: `text.replace("\r\n", "\n").replace("\n\r", "\n").replace("\r", "\n")`.
- **Reasoning**: This prevents "invisible character" mismatches. Since LibreOffice internally represents hard line breaks as `\n` in UNO strings, this ensures that the text we process, the text the AI sees, and the text we write back into the document are byte-compatible for string operations.

## 7. Local LLM thinking (Ollama vs LM Studio) — sidebar `[Thinking]` gap

**Reported symptom**: Wireshark on Ollama and LM Studio both show OpenAI-style streaming JSON where `choices[0].delta.content` sometimes includes the closing think marker (JSON-escaped as `"\u003c/think\u003e"`, i.e. ``). LM Studio’s reasoning often appears in the chat sidebar under `[Thinking]`; Ollama’s often does not.

**Not a transport bug**: Unicode escapes in JSON are normal. A lone `` in `content` does **not** mean both backends behave the same — it is often just a closing marker while the real trace arrived on another delta key.

### How WriterAgent routes stream text today

Main chat: `LlmClient._run_streaming_loop` → `OpenAIShim.parse_response_chunk` → queue → `run_stream_drain_loop` ([`plugin/framework/async_stream.py`](../plugin/framework/async_stream.py)).

| Wire source | Code path | Sidebar appearance |
|-------------|-----------|-------------------|
| `delta.reasoning_content`, `delta.reasoning`, `delta.thinking`, `delta.thought`, `delta.reasoning_details` | `_extract_thinking_from_delta` in [`stream_normalizer.py`](../plugin/framework/client/stream_normalizer.py) → `append_thinking_callback` → `StreamQueueKind.THINKING` | Drain loop prefixes `[Thinking] ` then text; closes with ` /thinking\n` |
| `delta.content` (including `` … `` text) | `delta.get("content")` → `append_callback` → `StreamQueueKind.CHUNK` | Plain assistant stream — **no** `[Thinking]` wrapper |

**Remaining gap:** There is **no** parser for think tags inside `content` (LM Studio with separation off, some Ollama fallbacks). Enable `enable_agent_log` / debug logging: when a chunk has thinking-shaped delta keys but no extractable text, `_extract_thinking_from_delta` logs `stream thinking: no extractable text; delta hints=...` to `writeragent_debug.log`.

### Root cause (web research, revised)

**Primary (Ollama + Qwen3 / thinking models):** On `/v1/chat/completions`, Ollama often streams thinking in **`delta.reasoning`**, not `delta.reasoning_content`. WriterAgent reads both (see `_THINKING_STRING_FIELDS` in [`stream_normalizer.py`](../plugin/framework/client/stream_normalizer.py)).

**Why LM Studio often works:** Developer setting **“When applicable, separate reasoning_content and content in API responses”** maps reasoning into `reasoning_content`, which WriterAgent already reads ([LM Studio #480](https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/480)). Without it, models still emit `` inside `content` ([LM Studio #1569](https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/1569)).

**Secondary (both backends):** When separation fails, thinking stays in `content` between think tags; WriterAgent shows it as answer text (no `[Thinking]`). Tag parsing is a fallback, not the main Ollama fix.

**Other context:**

- Ollama **native** `/api/chat` uses `message.thinking` ([Ollama thinking](https://docs.ollama.com/capabilities/thinking)); WriterAgent uses `/v1/chat/completions` only.
- WriterAgent does not send Ollama `think: true` / `reasoning_effort` on chat requests today ([`OpenAIShim.build_chat_request`](../plugin/framework/client/llm_client.py)); may matter if separated fields are never emitted.
- **`chatbot.show_search_thinking`**: tool/web paths only, not main LLM stream ([`tool_loop.py`](../plugin/chatbot/tool_loop.py)).

### Verification (one capture, same prompt/model)

Compare full SSE deltas (not a single packet):

| Field | Typical Ollama (Qwen3) | Typical LM Studio (setting on) |
|-------|------------------------|--------------------------------|
| `delta.reasoning` | Non-empty during think phase | Often empty |
| `delta.reasoning_content` | Often empty | Non-empty during think phase |
| `delta.content` | Answer + sometimes `` markers | Answer + sometimes `` markers |

If Ollama has `reasoning` and LM Studio has `reasoning_content`, the one-line field fix below is sufficient for many reports.

### Workarounds still open

**Step 1 (done):** `delta.reasoning` is in the string field list; chunks are normalized via `choices[0].delta` once (no recursion). Tests in `tests/framework/test_stream_normalizer.py`.

**Step 2 (if capture still shows tags-only `content`):** Stateful `ThinkTagStreamSplitter` in `stream_normalizer.py` to split `` / `` inside `content`; strip markers from visible text. Needed when LM Studio setting is off or Ollama falls back to tag-in-content.

**Step 3 (optional):** `OllamaShim.build_chat_request` — `"think": true` or `reasoning_effort` for thinking-capable models (gate by model name); only if separated fields still missing.

**Reporter / LM Studio users:** Enable “separate reasoning_content and content” in Developer settings if thinking should appear in clients that only read `reasoning_content`.

**Debugging a provider:** Reproduce with `python scripts/run_streaming_test.py` and inspect `writeragent_debug.log` for `stream thinking: no extractable text`.

See also: [`streaming-and-threading.md`](streaming-and-threading.md) §3 (reasoning in streams).

---

*This document should be updated as new hacks are discovered or as improvements in models allow us to remove them.*
