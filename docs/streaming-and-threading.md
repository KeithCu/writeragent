# Streaming, Tool Calling, and Request Batching: How the APIs Work

This document explains how OpenAI-compatible chat APIs handle **streaming**, **tool calling**, and **request batching**, and how **reasoning/thinking** appears in streams. It is aimed at developers who need to implement or debug clients (e.g. WriterAgent’s chat sidebar).

References: OpenAI [Streaming](https://platform.openai.com/docs/api-reference/streaming), [Tool calling](https://platform.openai.com/docs/guides/function-calling). Your endpoint may have its own docs for streaming and reasoning tokens.

---

## Table of Contents

1. [Chat completions: streaming (no tools)](#1-chat-completions-streaming-no-tools)
2. [Streaming when tools are in the request](#2-streaming-when-tools-are-in-the-request)
3. [Reasoning / thinking in the stream](#3-reasoning--thinking-in-the-stream)
4. [Summary table](#4-summary-table)
5. [Testing with OpenRouter](#5-testing-with-openrouter)
6. [Implementation: Streaming deltas](#6-implementation-streaming-deltas)
7. [Error Handling and UI Threading](#7-error-handling-and-ui-threading)
8. [Parallel Tool Calling](#8-parallel-tool-calling)
9. [Producer-Side Batching of Display Text & Global Audit (2026-05)](#9-producer-side-batching-of-display-text--global-audit-2026-05)

---

## 1. Chat completions: streaming (no tools)

**Request:** Same URL, `stream: true`.

**Response:** HTTP body is **Server-Sent Events (SSE)**. Each event is a line starting with `data: `. The payload is JSON. Last event is usually `data: [DONE]`.

**Chunk shape (content-only):**

```json
{
  "id": "chatcmpl-...",
  "choices": [
    {
      "index": 0,
      "delta": { "content": "The ", "role": "assistant" },
      "finish_reason": null
    }
  ]
}
```

Later chunks may have only new content:

```json
{ "choices": [{ "delta": { "content": "capital" }, "finish_reason": null }] }
```

Final chunk:

```json
{ "choices": [{ "delta": {}, "finish_reason": "stop" }] }
```

**Client behavior:**

- Read line by line; skip empty lines and comments (some providers send processing hints).
- If line is `data: [DONE]`, stop.
- Otherwise parse `data: <json>`. From `choices[0].delta` take:
  - `content` — append to the displayed reply.
  - `finish_reason` — when non-null, stream is done (and may be `stop`, `length`, etc.).

The **delta** only contains what **changed** in this chunk; the client accumulates content itself.

---

## 2. Streaming when tools are in the request

When you send `stream: true` **and** `tools` in the request, the API can still return a stream, but the **delta** now includes **partial tool call** data. The client must **accumulate** these deltas into a full message before it can run tools.

**Chunk shape (streaming with tool_calls):**

- Early chunks may contain **reasoning/thinking** (see section 3) and/or **content** deltas.
- Chunks for tool calls look like:

```json
{
  "choices": [{
    "delta": {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        { "index": 0, "id": "call_abc", "type": "function", "function": { "name": "get_weather", "arguments": "" } }
      ]
    },
    "finish_reason": null
  }
}
```

Later chunks add **partial arguments** (only the new fragment):

```json
{
  "choices": [{
    "delta": {
      "tool_calls": [
        { "index": 0, "function": { "arguments": "{\"location\":" } }
      ]
    }
  }
}
```

```json
{
  "choices": [{
    "delta": {
      "tool_calls": [
        { "index": 0, "function": { "arguments": " \"Paris\"}" } }
      ]
    }
  }
}
```

So **one** tool call is spread across **many** chunks. The client must:

- Maintain a buffer per `index` (or `id` when present): `id`, `type`, `function.name`, `function.arguments`.
- For each chunk, **merge** `delta.tool_calls[i]` into the buffer for that index (e.g. append `function.arguments`).
- When the stream ends (`finish_reason` set or `[DONE]`), parse the accumulated `function.arguments` as JSON and run the tools.

Order of appearance in the stream is typically: optional reasoning deltas, optional content deltas, then tool_calls deltas (often after content/reasoning). The exact order is provider-dependent.

---

## 3. Reasoning / thinking in the stream

Some models send **reasoning** or **thinking** tokens in addition to the main reply. These appear in the **same** SSE stream, in the **delta**.

### 3.1 Provider-style: `reasoning_details`

Some providers use **reasoning_details**. In **streaming** responses, each chunk may contain:

- `choices[0].delta.reasoning_details`: **array** of objects. Each object can be:
  - `type: "reasoning.text"` and `text`: string to show as thinking.
  - `type: "reasoning.summary"` and `summary`: string summary.
  - `type: "reasoning.encrypted"` and `data`: opaque (e.g. redacted).

So the client should:

- For each chunk, read `delta.reasoning_details` (if present).
- For each element, if `type === "reasoning.text"` append `text`; if `type === "reasoning.summary"` append `summary` (or treat similarly).
- Pass that concatenated string to the UI (e.g. “thinking” area or same box as content).

Reasoning chunks often **precede** content chunks; the model “thinks” then “replies”. So in the same stream you may see:

1. Several chunks with only `delta.reasoning_details`.
2. Then chunks with `delta.content`.
3. Optionally chunks with `delta.tool_calls`.

### 3.2 Other providers: `reasoning_content`

Some APIs use a single string field in the delta, e.g. `delta.reasoning_content`. Same idea: if present, append it to the thinking buffer and show it in the UI.

---

## 4. Summary table

| Mode                   | Request                  | Response   | Content              | Tool calls                        | Reasoning / thinking  |
|------------------------|--------------------------|------------|----------------------|-----------------------------------|------------------------|
| Chat, stream           | `stream: true`           | SSE chunks | `delta.content`      | N/A (no tools)                    | `delta.reasoning_*`    |
| Chat + tools, stream   | `stream: true`, `tools`  | SSE chunks | `delta.content`      | `delta.tool_calls` (accumulate)   | `delta.reasoning_*`    |

\* When the API supports it and the model returns it.

---

## 5. Testing with your endpoint

If you have an API key for your endpoint, you can verify how streaming, tool calls, and reasoning actually behave.

### 5.1 What to test

1. **Streaming without tools**
   - `POST <your-endpoint>/chat/completions`, `stream: true`, no `tools`.
   - Inspect each SSE chunk: `choices[0].delta.content`, `finish_reason`. Confirm content is incremental and `[DONE]` or final chunk ends the stream.

2. **Streaming with a reasoning model**
   - Same URL, `stream: true`, use a model that returns reasoning if your provider supports it. Some providers accept `reasoning: { effort: "low" }` in the body.
   - Inspect chunks for `choices[0].delta.reasoning_details`: you should see arrays of `{ type: "reasoning.text", text: "..." }` (or similar) before or interleaved with `delta.content`.

3. **Streaming with tools**
   - Same URL, `stream: true`, add a minimal `tools` array (e.g. one function) and ask the model to call it.
   - Inspect chunks for `delta.tool_calls`: first chunk(s) may have `id`, `type`, `function.name`, `arguments: ""`; later chunks add fragments to `function.arguments`. Confirm you can concatenate `arguments` and parse as JSON.

4. **Streaming with tools + reasoning**
   - Combine (2) and (3): model that supports reasoning, with tools. You should see reasoning_details chunks, then content and/or tool_calls. Order and exact shape depend on the model; the doc above is the generic pattern.

### 5.2 What you’ll learn

- **Exact chunk order** for your chosen model (reasoning → content → tool_calls, or interleaved).
- **Exact field names** your provider uses (`reasoning_details` vs any variant).
- **Whether** `function.arguments` is split across many small chunks or fewer larger ones (affects accumulation logic).
- **Whether** `finish_reason` is `"tool_calls"` when the model stops to call tools, and what the final chunk looks like.

### 5.3 How to run tests

- **Manual:** Use `curl` or a small script: set `Authorization: Bearer <OPENROUTER_API_KEY>`, `Content-Type: application/json`, body with `model`, `messages`, `stream: true`, and optionally `tools` and `reasoning`. Parse SSE line by line and log each chunk (or key fields).
- **In WriterAgent:** Set your endpoint URL and API key in Settings; use a reasoning model if your endpoint supports it. Observe the sidebar to see when thinking vs content appears.

Once you’ve run these tests, you can document the **actual** chunk shapes and order in this file or in a short “streaming notes” section so the implementation can be aligned with real responses.

### 5.4 Empty final assistant text (tool loop)

If a streamed tool-loop round ends with no `content` and no `tool_calls`, the sidebar shows `[No text from model; any tool changes were still applied.]` followed by `[Debug: round=…, finish_reason=…, content=…, usage=…]` from the accumulated API response ([`format_empty_model_response_debug`](../plugin/chatbot/tool_loop_state.py)). Release builds also emit the same summary at **warning** level in `writeragent_debug.log`.

---

## 6. Implementation: Streaming deltas

We chose the **lightweight, dependency-free** approach:

- We copied **[`accumulate_delta`](https://github.com/openai/openai-python/blob/main/src/openai/lib/streaming/_deltas.py)** from the OpenAI Python SDK into **`plugin/framework/async_stream.py`**.
- This function handles the complex logic of merging partial tool call arguments (which can be split across many chunks) and concatenating content strings.
- logic: `accumulate_delta(snapshot, delta)` -> updates snapshot in place.

This avoids adding the heavy `openai` dependency to the LibreOffice extension while ensuring 100% compatibility with OpenAI-style streaming deltas.

---

## 7. Event Loop and UI Threading

LibreOffice’s UI (VCL) is single-threaded. To keep the UI responsive during long-running network operations, WriterAgent uses a **flat event loop** architecture on the main thread, combined with worker threads that push messages to a single `queue.Queue`.

**The Architecture:**

1. **Worker Threads (Producers):**
   - The LLM stream (`_spawn_llm_worker`) runs on a background thread. It pushes messages like `("chunk", text)`, `("thinking", text)`, `("stream_done", response)`, or `("error", e)` to the queue.
   - Long-running network tools (like Web Search or Image Generation) also run on background threads and push `("tool_thinking", text)", `("status", text)`, and `("tool_done", ...)` to the *same* queue.

2. **Main Thread (Consumer):**
   - Runs a single `while True` event loop in `_start_tool_calling_async`.
   - Blocks briefly on `q.get(timeout=0.1)`.
   - If the queue is empty, it calls `toolkit.processEventsToIdle()` to pump LibreOffice UI events, keeping the application perfectly responsive.
   - If an item is received, it dispatches based on the message type (e.g., appending text, updating status, executing tools).

This flat architecture avoids nested callbacks and makes state transitions explicit.

> [!WARNING]
> **`job_done` ownership invariant:** `job_done[0]` must **only** be written by the drain loop (main thread) when it processes a terminal queue item (`STREAM_DONE`, `ERROR`, or `STOPPED`). The worker thread must never set `job_done[0] = True` directly, even in a `finally` block.
>
> **Why this matters:** Setting `job_done[0] = True` from the worker thread races with the drain loop's `while not job_done[0]` check. A fast-returning worker (common for short LLM responses) can set the flag before the main thread dequeues and processes the `STREAM_DONE` item. The drain loop then exits without ever calling `on_done` — which means cleanup callbacks such as `leaveUndoContext()` are never invoked, leaving LibreOffice's `XUndoManager` with an open, orphaned context. All subsequent text insertions are recorded under that context as `"Insert $1"` and the undo stack is permanently corrupted for that document session.
>
> The worker's `finally` block should only post the sentinel `(STREAM_DONE, None)` to the queue (which guarantees the drain loop unblocks). The drain loop itself sets `job_done[0] = True` when it processes that item.



## 8. Tool Execution and Queuing

### Overview

When the LLM finishes a stream and requests tools (`"stream_done"`), WriterAgent must execute them. Some tools (like modifying the spreadsheet) must run synchronously on the main thread because LibreOffice UNO calls are not thread-safe. Other tools (like Web Research) must run on a background thread because they do heavy network I/O and would freeze the UI.

### The `next_tool` Queuing System

To handle both sync and async tools without freezing the UI, WriterAgent uses an internal dispatch queue:

1. **Queueing:** When `"stream_done"` is received with `tool_calls`, the calls are added to a `pending_tools` list, and a `("next_tool",)` message is pushed onto the queue.
2. **Dispatching:** The main loop picks up `"next_tool"`. It pops the first tool from `pending_tools`:
   - **Async Tools (`ASYNC_TOOLS` set):** Spawned in a daemon thread. The main loop immediately returns to pumping UI events. When the thread finishes, it pushes a `("tool_done", ...)` message to the queue.
   - **Sync Tools (UNO operations):** Executed immediately on the main thread. A `("tool_done", ...)` message is pushed to the queue.
3. **Completion:** When `"tool_done"` is received, the result is saved to the session history, and another `("next_tool",)` message is pushed.
4. **Next Round:** When `"next_tool"` finds an empty `pending_tools` list, all tools are finished. The loop increments the round counter and spawns a new LLM worker to send the results back to the model.

This sequentializes tool execution while guaranteeing the UI never freezes during network-bound tool operations.

### Stop / cancellation

Each sidebar **Send** runs under a **`SendCancellation`** scope ([`plugin/framework/queue_executor.py`](../plugin/framework/queue_executor.py) `agent_session()`). **Stop** calls `scope.cancel()` once.

#### What `scope.cancel()` does

- Sets a **thread-safe** cancelled flag (`scope.is_cancelled()`).
- Calls `stop()` on every **`LlmClient`** registered for that send (closes the persistent HTTP socket so blocking reads fail fast).
- Cancels pending main-thread queue work ([`QueueExecutor.cancel_pending_work`](../plugin/framework/queue_executor.py)) and runs registered agent-backend `stop()` hooks.

#### Why the first implementation looked fixed but was not

The initial **`SendCancellation`** change fixed the symptom users noticed on the **main thread** (sidebar buttons and drain loop exit) but **web research and other smolagents sub-agents kept running steps in the background**. That was not a race; the worker thread genuinely still believed the send was active.

Two separate bugs caused that:

1. **Stop checker cleared when the UI “finished”**

   Web research runs in a **background worker** (`run_search` in [`send_handlers.py`](../plugin/chatbot/send_handlers.py)) while the **main thread** runs `run_stream_drain_loop`. When the user clicked Stop, the drain loop saw `stop_checker()` → true and exited. `_do_send` then returned and the `finally` in [`panel.py`](../plugin/chatbot/panel.py) set **`_send_cancellation = None`**.

   The worker still used `stop_checker=lambda: self.stop_requested`. After the scope pointer was cleared, `stop_requested` fell back to **`_stop_requested_fallback`**, which was still **False** (only `scope.cancel()` had run—it does not set the fallback unless Stop goes through the panel path). So from step 5 onward the sub-agent’s `SmolAgentExecutor` loop thought nothing was cancelled and kept calling the model.

   **Fix:** pass a **stable** predicate: `scope.is_cancelled` (bound method on the same `SendCancellation` object), via [`bind_send_stop_checker()`](../plugin/framework/queue_executor.py) / [`SendButtonListener.resolve_stop_checker()`](../plugin/chatbot/panel.py). Capture that when starting the worker; do not re-read `panel._send_cancellation` from the worker after the drain exits.

2. **Sub-agent `LlmClient` never registered for `stop()`**

   Auto-registration used a **`contextvars.ContextVar`** set in `agent_session()` on the **main thread**. Python does **not** copy that context into new `threading.Thread` workers. The web-research worker therefore constructed `LlmClient` with **no scope**, so `scope.cancel()` never called `stop()` on the sub-agent’s connection—HTTP could run to completion for the current step and the agent continued.

   **Fix:** pass **`send_cancellation`** on [`ToolContext`](../plugin/framework/tool.py) and **`cancellation_scope=...`** into [`LlmClient.__init__`](../plugin/framework/client/llm_client.py) from [`build_toolcalling_agent`](../plugin/chatbot/smol_agent.py) / [`web_research.py`](../plugin/chatbot/web_research.py).

3. **Smolagents only checked stop between full steps**

   [`SmolAgentExecutor`](../plugin/chatbot/smol_agent.py) used `for step in agent.run(stream=True)`, which calls `next()` on the stream **before** the stop check at the top of the loop body—so one whole step always runs after the previous check. That is acceptable only if (1) and (2) are correct. Additionally, on stop we now call **`agent.interrupt()`** (sets `interrupt_switch` in vendored smolagents) and check stop **before** each `next()`.

#### Correct wiring checklist (for new code)

| Need | Do this |
|------|---------|
| Main-thread drain / streaming | `stop_checker=self.resolve_stop_checker()` (not `lambda: self.stop_requested` alone). |
| Background worker (web research, async tool) | At worker start: `stop_checker = self.resolve_stop_checker()` and `cancel_scope = self._send_cancellation`; pass both into `ToolContext(..., stop_checker=stop_checker, send_cancellation=cancel_scope)`. |
| New `LlmClient` on a worker | `LlmClient(config, ctx, cancellation_scope=ctx.send_cancellation)` (or register manually on the scope). |
| Long-running smol sub-agent | Use [`SmolAgentExecutor`](../plugin/chatbot/smol_agent.py); do not hand-roll `agent.run` without the same stop/interrupt behavior. |
| UNO + HTTP (document research) | Open/close document on main thread only; run inner smol agent on the **async worker**—never wrap the whole agent in `execute_on_main_thread`. |

Tests: [`tests/framework/test_send_cancellation.py`](../tests/framework/test_send_cancellation.py) (stable checker after scope cleared, executor abort before next step).

#### Related threading rule

Sub-agents must not run long HTTP on the main thread; [`delegate_read_document`](../plugin/doc/document_research_specialized.py) opens/closes on the main thread only and runs the inner read agent on the async worker.

## 9. Producer-Side Batching of Display Text & Global Audit (2026-05)

> **Scope note (added 2026-05-25):** This section documents the **producer-side 250 ms batching** feature for chat display text (`CHUNK` and `THINKING` items) and, crucially, exactly what was left as deliberate future work under the "global audit" bucket.

### Why producer-side batching was introduced

During streaming (especially the RichTextControl sidebar), the background LLM reader thread (and other producers) were emitting very small `CHUNK` deltas — sometimes a few characters or even single characters at a time — at the natural cadence of the SSE stream (often every 30–80 ms).

Each such item:

1. Crosses the queue boundary.
2. Wakes the main-thread drain loop (`run_stream_drain_loop` in `async_stream.py`).
3. Triggers `apply_chunk_fn` (ultimately `append_text_chunk` or `append_rich_text`).
4. May cause `toolkit.processEventsToIdle()` and RichTextControl scroll nudges (`nudge_rich_control_view_to_end`) on the formatted sidebar path.

The net visual effect for the user was **micro-stutter** during long assistant answers: the sidebar would repaint/relayout far more often than necessary.

**Design decision (user direction 2026-05-25, refined same day):**

- Do **not** touch the consumer-side drain loop timeout (still `0.1 s`).
- Move batching to the **producer** (network reader thread) with a *hard 250 ms deadline from the first fragment of each burst* (i.e. "send data every 250 ms max, or when done").
  Downstream code receives one larger joined string no later than 250 ms after the first tiny delta of a burst, while still coalescing rapid fragments. A boundary item (STREAM_DONE etc.) forces immediate emission even if the 250 ms window has not yet elapsed.
- This is a pure smoothing / UX win orthogonal to scroll behavior on the RichTextControl transcript (`nudge_rich_control_view_to_end`); see [rich-text-control-sidebar.md](rich-text-control-sidebar.md).

### The `BatchingStreamQueue` contract (the single source of truth)

See the full class and docstring in [`plugin/framework/async_stream.py`](../plugin/framework/async_stream.py) (`BatchingStreamQueue`).

Key guarantees the implementation provides:

- **Simple append only.** Internal buffers are `list[str]`; each `put((CHUNK, delta))` or `put((THINKING, delta))` just does `buf.append(delta)`.
- **Hard max-latency timer from first fragment ("every 250 ms max, or when done").** The *first* display delta that starts a new burst arms a one-shot `threading.Timer` for exactly `batch_interval` (default 0.25 s) measured from the arrival of that first fragment. Subsequent deltas during the same burst are simply appended to the buffer; they do **not** reset or postpone the deadline. When the timer fires we emit one joined string. This guarantees the consumer sees an update at least every 250 ms even during a very fast continuous stream from the model. No main-thread sleeps.
- **One joined emission.** When the timer fires **or** `.flush()` is called, the batcher does a single `raw_q.put((StreamQueueKind.CHUNK, "".join(content_buf)))` (and the equivalent for THINKING), then clears the buffer. Downstream never sees the intermediate fragments.
- **Strict flush-before-boundary discipline (the most important rule):**
  Any non-display control item forces an immediate flush of any pending display text **before** the control item is forwarded:
  - `STREAM_DONE`, `FINAL_DONE`, `ERROR`, `STOPPED`
  - `APPROVAL_REQUIRED`
  - `NEXT_TOOL`, `TOOL_DONE`, `TOOL_THINKING`, `TOOL_CALL`, `TOOL_RESULT`
  - `STATUS` (and any other future control kinds)
- Convenience factories: `.content_cb()` and `.thinking_cb()` so old `lambda t: q.put((CHUNK, t))` sites become one-liners with almost no diff.
- The wrapper is transparent for code that still wants the raw `Queue`: `batched.raw` gives the underlying queue; the drain loop and most legacy sites continue to work unchanged.
- `run_async_worker_with_drain` has defensive support: if you pass a `BatchingStreamQueue` as the `q` argument it will automatically flush on error paths and on the terminal sentinel, then post the sentinel on the real queue.

### Current implemented scope (what *was* wired in the initial change)

The **primary user-visible chat streaming path** was updated:

- `plugin/chatbot/tool_loop.py`:
  - `_start_tool_calling_async` creates both the raw queue (`_active_q`) **and** a `BatchingStreamQueue` wrapper (`_active_batched_q`).
  - `_spawn_llm_worker` and `_spawn_final_stream` accept either a raw `Queue` or a `BatchingStreamQueue`. When the latter is supplied they use the `.content_cb()` / `.thinking_cb()` helpers (or the equivalent manual `batched.put(...)` + `batched.flush()` before every boundary put).
  - All terminal / control puts in those two workers now do `if batched: batched.flush()` before emitting `STREAM_DONE`, `FINAL_DONE`, `STOPPED`, `ERROR`, etc.
- `plugin/framework/async_stream.py`:
  - The `BatchingStreamQueue` class itself.
  - `run_async_worker_with_drain` was made batcher-aware so any code path that goes through the generic runner automatically gets correct flush-on-boundary + terminal behavior.
- `tests/framework/test_async_stream.py`:
  - Four new unit tests covering join-on-flush, auto-flush on boundary, the callback helpers, and simulated timer expiry.
- Documentation:
  - See [rich-text-control-sidebar.md](rich-text-control-sidebar.md) for formatted sidebar behavior; producer batching is described in this section.
  - This section (here) is the detailed permanent record.

All of the above passed a full `make test` gate (ty + mypy + pyright + ruff + bandit + pytest + native UNO tests) with zero failures and zero unrelated changes.

### What was deliberately left in the "global audit" bucket (future work)

Per the implementation plan and the final status after the May 2025-25 change, the following items were **explicitly scoped out** of the initial delivery and marked as the "global audit" bucket. They remain open exactly as described in the todo list at the time of the change:

1. **Full audit of every direct `CHUNK` / `THINKING` put site in the entire codebase**
   - Not every background producer was converted.
   - Places that still do raw `q.put((StreamQueueKind.CHUNK, text))` (or the THINKING equivalent) will continue to emit tiny fragments until they are either:
     - Switched to use a `BatchingStreamQueue` wrapper for that send, **or**
     - Wrapped with an ad-hoc flush discipline if they are one-off paths.
   - Recommended search to start the audit:
     ```bash
     rg 'StreamQueueKind\.(CHUNK|THINKING)' --type py
     ```
   - Special attention areas called out in the plan:
     - All paths in `plugin/chatbot/send_handlers.py` (direct web research, librarian mode, image generation results, approval flows, etc.).
     - `plugin/agent_backend/acp_backend.py` and any other ACP / Hermes / CLI agent backends that emit display text.
     - Any "last tiny terminator" or `last_streamed` accumulation patterns (the final `FINAL_DONE` payload must be preceded by a flush of the last real content batch).
     - The rich-text-specific "1 character" + `_tighten_list_indent` path inside `append_rich_text` / `append_text_chunk` (see `rich_text.py`).

2. **ACP backend and other non-LLM-stream producers**
   - The ACP (Actor Context Protocol) path in `agent_backend/` can produce `CHUNK` / `THINKING` (and tool-related display items) on its own reader thread.
   - These were left untouched in the initial rollout. They need the same wrapper + flush-before-boundary treatment (or an equivalent local batcher) if we want consistent 250 ms smoothing for Hermes-style agents.

3. **Explicit rerender / clear flush coordination (panel.py)**
   - `rerender_rich_text_session` (the method that does the big `setString('')` + re-HTML pass after streaming finishes) currently benefits *indirectly* because a normal completion always ends with a `STREAM_DONE` / `FINAL_DONE` which the batcher flushes.
   - However, there is no *explicit* hand-off from the per-send batcher to the rerender path. If a future "mid-stream clear" or "switch to librarian mode while a send is still producing" scenario ever appears, the batcher buffer could still hold un-emitted text that would then be lost or appear after the clear.
   - The plan item was: "in `panel.py`, before the `setString('')` in rerender and before the explicit rich-text clear path (~line 1186 at the time), ensure any in-flight producer batcher for the *current* send is flushed (coordinate via the active send scope or a sentinel)."
   - This was left for the global audit.

4. **Streaming fuzz test / frequency assertions (optional but valuable)**
   - The existing streaming fuzz tests exercise the drain loop with many small items.
   - A useful follow-up is to add assertions (or at least logging) that, when the producer is using a `BatchingStreamQueue`, the observed `CHUNK` frequency on the consumer side drops to roughly ≤ 4–5 items per second even for a very chatty model.
   - Not required for correctness, but excellent for regression protection.

5. **Making the batcher the default in more generic helpers**
   - `run_stream_drain_loop` and the various `run_*_async` helpers still accept a raw `queue.Queue`.
   - Some call sites create the queue themselves and could be updated to create a `BatchingStreamQueue` by default (with an opt-out for tests that want exact fragment timing).
   - This is a nice-to-have polish item inside the same audit.

6. **Any other "display text" producers that were added after the initial wiring**
   - Grammar proofreader, realtime status, audio transcription feedback, future specialized tool result renderers, etc.
   - Every new background producer that ever wants to show incremental text to the user should be taught the batcher + flush discipline from day one.

### How to perform (or continue) the global audit

1. Start with the grep above.
2. For each site, answer:
   - Is this inside a send that already has an `_active_batched_q` (or equivalent) in scope?
   - If yes, change the put to go through the batcher (or the `.content_cb()`).
   - If no (one-off path, test, or different send lifetime), either create a short-lived `BatchingStreamQueue` around the raw queue for that operation, or at minimum insert an explicit `batcher.flush()` immediately before every control/boundary item.
3. Pay special attention to any place that does a "final tiny chunk" followed immediately by a terminal kind — that tiny chunk must be flushed.
4. After changing a site, add or update a unit test that proves the flush-before-boundary behavior for that path.
5. Update this section and the todo list in the original plan document when a sub-item is completed.

### Cross references

- Implementation: `plugin/framework/async_stream.py` (`BatchingStreamQueue`, the defensive bits in `run_async_worker_with_drain`)
- Primary wiring: `plugin/chatbot/tool_loop.py` (`_active_batched_q`, `_spawn_llm_worker`, `_spawn_final_stream`)
- Tests: `tests/framework/test_async_stream.py` (the four new batcher tests)
- UX context & scroll work: [rich-text-control-sidebar.md](rich-text-control-sidebar.md) (`nudge_rich_control_view_to_end`)
- Original plan / todo items: the conversation transcript and the todo list that existed at the moment the change landed (items such as `boundary-flush-audit`, `wire-acp-and-other-backends`, `flush-for-rerender-clear`, etc. were deliberately cancelled / marked "deferred to global audit" rather than completed).

### Status summary (as of the change that added this section)

- **Completed in the initial delivery:** Core class, generic runner support, primary LLM chat path (tool calling + final stream), basic unit tests, documentation hooks.
- **Left open (global audit bucket):** Everything listed in the numbered items 1–6 above.
- The decision to ship the primary-path win + the detailed "what remains" record here, rather than attempting a complete rollout in one PR, was explicit and user-approved.

This section exists so that future developers (or a later focused pass) have a single, authoritative place that explains both the mechanism and the exact remaining surface area.

# writeragent2 Threading Bug Fix: Why the "Background Thread" Was Actually Freezing/Crashing the UI

You noticed that [writeragent2](file:///home/keithcu/Desktop/Python/writeragent/writeragent2) already contained a `threading.Thread` call in [panel_factory.py](file:///home/keithcu/Desktop/Python/writeragent/writeragent2/plugin/chatbot/panel_factory.py) with the comment `Run in background thread to avoid UI freeze`. It's understandable to wonder why we needed to introduce a complex queuing system if the work was already happening off the main thread.

The short answer is: **the previous implementation threw everything onto a raw background worker, causing illegal, non-thread-safe modifications to LibreOffice's VCL (Visual Components Library) and UNO services, which frequently results in deadlocks (hard freezes) and memory corruption (segfaults/crashes).**

Here is a detailed breakdown of the original bug and how our new architecture fixes it safely.

---

## The Original Implementation (The Bug)

In [writeragent2/plugin/chatbot/panel_factory.py](file:///home/keithcu/Desktop/Python/writeragent/writeragent2/plugin/chatbot/panel_factory.py), clicking "Send" fired [actionPerformed](file:///home/keithcu/Desktop/Python/writeragent/plugin/chat_panel.py#1108-1113) on the main LibreOffice UI thread. The original code immediately delegated all work to a background thread like this:

```python
# The Old Way
def actionPerformed(self, evt):
    def _worker():
        # Executes EVERYTHING on a background thread
        self._listener.send(...)
    threading.Thread(target=_worker, daemon=True).start()
```

While this appears to free up the main thread, the problem lies in what `_listener.send(...)` was actually doing on that background thread:

1. **Network I/O:** Calling `provider.stream(...)` (Perfectly fine for a background thread).
2. **UI Updates:** Triggering callbacks like `self.on_append_response` which executed `response_ctrl.getModel().Text += text` directly on VCL components (Illegal on a background thread).
3. **Synchronous UNO Tool Execution:** Executing `adapter.execute_tool(...)`, which performed arbitrary UNO callbacks to read or modify the document model (Highly unstable/illegal on a background thread).

### Why LibreOffice Freaks Out
LibreOffice's VCL is strictly **single-threaded**. Only the main thread is allowed to safely manipulate the UI or alter the active document state. When a background thread attempts to mutate the document or append text to the chat window, it causes race conditions inside LibreOffice's internal C++ state.
The VCL often "catches" these illegal crosses and attempts to wait on a lock, resulting in a **deadlock**. The main thread is stuck waiting for something the background thread messed up, and the GUI freezes completely—meaning the background thread caused the exact UI freeze it was supposedly trying to prevent!

---

## The Fix: The Flat Event Loop Architecture

To solve this we implemented the **Flat Event Loop** pattern, which properly separates duties.

Instead of throwing the entire process on a background thread, the main thread maintains control, but we offload only the safe, slow pieces. To do this, we updated [panel_factory.py](file:///home/keithcu/Desktop/Python/writeragent/writeragent2/plugin/chatbot/panel_factory.py) to stop launching its own thread, and execute [_do_send()](file:///home/keithcu/Desktop/Python/writeragent/plugin/chat_panel.py#296-617) directly on the Main Thread.

### How [_do_send()](file:///home/keithcu/Desktop/Python/writeragent/plugin/chat_panel.py#296-617) Works Now
Inside [panel.py](file:///home/keithcu/Desktop/Python/writeragent/plugin/chat_panel.py), the [_do_send()](file:///home/keithcu/Desktop/Python/writeragent/plugin/chat_panel.py#296-617) establishes a cross-thread `queue.Queue()`.

1. **The Network Worker:**
   We launch a `def worker()` background thread whose *only* job is connecting to the API and fetching chunks via [chat_event_stream](file:///home/keithcu/Desktop/Python/writeragent/writeragent2/plugin/chatbot/streaming.py#21-114). It pushes UI updates and Tool requests into the queue as standard Python objects. It touches zero UNO objects.

2. **The Main Thread Pumping Loop:**
   The [_do_send()](file:///home/keithcu/Desktop/Python/writeragent/plugin/chat_panel.py#296-617) method acts as a message loop **on the main thread**:
   ```python
   while not self.stop_requested:
       # Wait max 100ms for network/tool results
       item = q.get(timeout=0.1)

       # Update UI / execute Sync Tools safely on the MAIN thread
       if kind == "event":
           self.on_append_response(item)

       # Vital step: Tells LibreOffice to repaint and process user clicks
       toolkit.processEventsToIdle()
   ```

Because we are doing `q.get(timeout=0.1)` followed by `toolkit.processEventsToIdle()`, the loop yields control back to the UI 10 times a second. **The UI stays smooth and responsive**, and when a chunk of text or a tool execution request arrives from the worker, it is safely executed natively on the Main Thread.

### The `next_tool` Queuing System
The final piece of the puzzle handles slow external tools (like Web Research or Image Generation).
If we executed [web_research](file:///home/keithcu/Desktop/Python/writeragent/plugin/core/document_tools.py#213-291) on the main thread, the `processEventsToIdle` loop would halt until the search returned, freezing the UI again.

Our solution is the `next_tool` dispatcher:
- When a tool is popped from the queue, we check `if name in ASYNC_TOOLS`.
- **Sync Tools (UNO calls):** Run instantly on the main thread, avoiding VCL crashes.
- **Async Tools (Network/OS calls):** A new minimal daemon thread is launched to execute the tool, pushing a [("tool_done", result)](file:///home/keithcu/Desktop/Python/writeragent/writeragent2/plugin/chatbot/panel_factory.py#393-404) message back onto the queue when finished. The Main event loop keeps ticking and pumping the UI while it waits for the async tool thread to return.

## Summary

The original [writeragent2](file:///home/keithcu/Desktop/Python/writeragent/writeragent2) attempted to dodge UI freezes by forcing the entire program state onto a background thread, which ironically caused deadlocks and crashes because LibreOffice does not allow cross-thread UI/Document manipulation.

Our new architecture keeps the orchestration safely on the main thread while using threading purely for I/O and explicitly pumping LibreOffice events (`processEventsToIdle`). This guarantees a perfectly responsive sidebar without compromising application stability.
