# LiteLLM Integration — What We Took From It

This document records what LocalWriter learned from [LiteLLM](https://github.com/BerriAI/litellm) and where to verify it in both our codebase and theirs. A reviewer can use the references below to find the same logic locally.

---

## Why Full LiteLLM Is Overkill

- **Size and deps**: ~54MB, ~82K lines, 113 providers; requires `pydantic`, `httpx`, `tiktoken`, `openai` SDK. Not shippable inside a LibreOffice extension.
- **Architecture**: Streaming is built around `pydantic` models and `httpx`; their “minimal” path is still tied to that stack. Ripping out 90% leaves you maintaining their abstractions, not a small adapter.
- **Scope**: We only need one path — OpenAI-compatible `/v1/chat/completions` — with a few provider quirks. We already had persistent connections, SSE, tool-calling, thinking tokens, and retries.

So we did **not** adopt LiteLLM’s code or dependencies. We kept our implementation and pulled in only **specific edge-case behavior** and **test ideas** from their streaming handler.

---

## What We Found Valuable

The useful part was **edge-case handling** in their OpenAI-compatible streaming path:

1. **`finish_reason="error"`** — Treat as a hard failure and raise so the UI can show an error instead of silently ending.
2. **Repeated identical chunks** — Some models loop and send the same content chunk forever; detect and raise (they fixed this in [issue #5158](https://github.com/BerriAI/litellm/issues/5158)).
3. **`finish_reason="stop"` with tool_calls** — Some providers send `stop` even when tool calls were made; remap to `tool_calls` so our tool loop is correct.
4. **Mistral/Azure delta quirks** — `role` or `tool.type` or `function.arguments` can be `None`; normalize to `"assistant"`, `"function"`, and `""` so `accumulate_delta` and downstream code don’t break.

We also reused their **test ideas**: SSE with/without space after `data:`, comment lines, malformed JSON, error/repeat/tool-call edge cases. Our tests live in `tests/test_streaming.py` and cite the LiteLLM anchors below.

---

## Code We Adopted (With References)

### 1. `finish_reason="error"` → raise

**Our code:** [core/api.py](core/api.py) — in `_run_streaming_loop`, right after `extract_content_from_response`.

**LiteLLM source:** `streaming_handler.py`, in `chunk_creator`, branch where `response_obj["is_finished"]` and `response_obj["finish_reason"] == "error"` (search for `finish_reason: error, no content string given`).

**Local path (if LiteLLM is in this repo or a sibling):**  
`litellm/litellm_core_utils/streaming_handler.py`

---

### 2. Repeated-chunk detection (infinite loop)

**Our code:** [core/api.py](core/api.py) — `REPEATED_STREAMING_CHUNK_LIMIT = 20`, and in `_run_streaming_loop` a `deque(maxlen=REPEATED_STREAMING_CHUNK_LIMIT)` of content strings; when the deque is full and all entries are identical and non-empty, we raise.

**LiteLLM source:** `streaming_handler.py`, method `safety_checker()` (and constant `REPEATED_STREAMING_CHUNK_LIMIT`). See also [GitHub issue #5158](https://github.com/BerriAI/litellm/issues/5158).

**Local path:**  
`litellm/litellm_core_utils/streaming_handler.py` — search for `safety_checker` or `REPEATED_STREAMING_CHUNK_LIMIT` or `5158`.

---

### 3. `finish_reason="stop"` → `"tool_calls"` when tool_calls present

**Our code:** [core/api.py](core/api.py) — in `stream_request_with_tools`, after the streaming loop: if `last_finish_reason == "stop"` and `message_snapshot.get("tool_calls")`, set `last_finish_reason = "tool_calls"`.

**LiteLLM source:** `streaming_handler.py`, method `finish_reason_handler()` (search for `## if tool use` or “finish_reason == \"stop\" and self.tool_call”).

**Local path:**  
`litellm/litellm_core_utils/streaming_handler.py` — search for `finish_reason_handler` or `## if tool use`.

---

### 4. Delta normalization (role, tool type, function.arguments)

**Our code:** [core/api.py](core/api.py) — `_normalize_delta(delta)` and its use in `_run_streaming_loop` (before `on_delta(delta)`) and in `request_with_tools` (on the parsed `message`).

**LiteLLM source:** `streaming_handler.py`, inside `chunk_creator`, in the block that builds `_json_delta` for OpenAI/Azure:

- **role:** search for `mistral's api returns role as None` — set `_json_delta["role"] = "assistant"` when missing/None.
- **tool type:** search for `mistral's api returns type: None` — set `tool["type"] = "function"` when `tool_calls` entries have no type or type None.
- **function.arguments:** search for `## AZURE - check if arguments is not None` — set `function_call.arguments = ""` when None.

**Local path:**  
`litellm/litellm_core_utils/streaming_handler.py` — search for the three strings above (role, type, AZURE).

---

## Test Coverage

Our tests that mirror these behaviors (and cite LiteLLM in docstrings) are in:

- [tests/test_streaming.py](tests/test_streaming.py)

Run:

```bash
python -m unittest tests.test_streaming -v
```

---

## Quick Reference for a Local LiteLLM Clone

If LiteLLM is cloned in this directory or a sibling (e.g. `../litellm`), a reviewer can open:

| What to verify | File |
|----------------|------|
| finish_reason=error, safety_checker, finish_reason_handler, _json_delta normalizations | `litellm/litellm_core_utils/streaming_handler.py` |
| SSE prefix stripping (we already handled `data:` with/without space) | Same file; search for `_strip_sse_data_from_chunk` or “data:” handling |

GitHub blob with line numbers:  
https://github.com/BerriAI/litellm/blob/main/litellm/litellm_core_utils/streaming_handler.py

Our inline comments in [core/api.py](core/api.py) use the form:  
`# LiteLLM: streaming_handler.py ~L<approx>` or a search phrase so the same spots can be found if line numbers drift.

---

## Suggested follow-ups (completed)

After the integration review, the following were done:

1. **AGENTS.md** — Added a "Streaming edge cases (LiteLLM-inspired)" bullet under Section 3b (Streaming I/O), with a link back to this document.
2. **litellm_integration.md** — Added a one-line pointer at the top: for what was already adopted, see this doc; that file describes the optional provider auto-detection proposal.
3. **format_error_message** in [core/api.py](core/api.py) — When the exception message contains `finish_reason=error`, return the user-facing string: "The AI provider reported an error. Try again." (Repeated-chunk message was already user-friendly.)

---

## Other code worth considering (not yet adopted)

We only need one path — OpenAI-compatible `/v1/chat/completions` — so the following are optional.

- **Skip `null` chunks** — Some proxies or providers send `data: null`. After `json.loads(payload)`, if `chunk is None`, skip the chunk (avoid `AttributeError` on `chunk.get(...)`). **Done:** we skip `if chunk is None: continue` in the streaming loop.
- **map_finish_reason** — LiteLLM’s `core_helpers.map_finish_reason()` normalizes provider-specific values to OpenAI’s set (e.g. Cohere `MAX_TOKENS` → `"length"`, Anthropic `tool_use` → `"tool_calls"`, Vertex `SAFETY` → `"content_filter"`). We only call OpenAI-compatible endpoints, so most providers already return `stop` / `length` / `content_filter` / `tool_calls`. If we ever see non-OpenAI values from a proxy, a small mapping dict could normalize them. **Optional:** add only if we observe real provider values in the wild.
- **content_filter in the UI** — When `finish_reason == "content_filter"`, we could show a short message (e.g. “[Content filter: response was truncated.]”) instead of the generic “[No text from model...]”. **Optional polish.**
- **Sagemaker/HF special tokens** — LiteLLM strips tokens like `<|assistant|>` for Sagemaker/HuggingFace. We use OpenAI-compatible endpoints only; no change unless we add a native Sagemaker/HF path.
