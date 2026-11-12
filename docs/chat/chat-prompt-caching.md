# Chat prompt caching: move the document snapshot to the end

Status: implemented (2026-11-09). Related: [compaction-dev-plan.md](compaction-dev-plan.md),
[../writer/grammar-prompt-caching.md](../writer/grammar-prompt-caching.md).

## Problem

Providers cache a repeated request prefix: Anthropic with `cache_control`, OpenAI models
automatically, and local llama.cpp-style servers through KV reuse. A cached prefix costs
about a tenth of normal input on Anthropic and is processed faster everywhere. The cache
matches exact bytes from the start of the request. The first changed byte ends the cache
hit, so everything from that byte on is read at full price.

WriterAgent puts its most volatile content at byte 0:

- `ChatSession.set_system_context` (`plugin/chatbot/chat_session.py`) stores the base
  system prompt plus `[DOCUMENT CONTENT]…[END DOCUMENT]` as `messages[0]`.
- `refresh_document_context` rewrites it on every send (`send_handlers.py`, `tool_loop.py`)
  and again mid-loop after a mutating tool (`tool_loop_actions.py`).
- The snapshot includes the selection, so even a cursor move changes it.

So every request misses the cache for the whole conversation, and tool loops resend that
history every few seconds.

This design fixes caching. It does **not** make Claude thinking replay safe:

- **The binding rule:** on current Claude models a thinking block is bound to the exact
  prefix that produced it. For accounts created on or after 2026-08-31, the API rejects a
  replayed block after any edit to that prefix.
- **Why this layout still edits history:** dropping the previous request's snapshot edits
  an earlier turn.
- **What that means:** keep thinking replay off for Claude. `anthropic_shim` does not
  replay thinking today, so nothing changes. Revisit only if replaying reasoning ever
  proves worth the cost of keeping every snapshot.

## Design

```
system:     base prompt only                         frozen for the session
messages:   user / assistant / tool history          append-only, never rewritten
            <-- cache breakpoint
            new user message, or the newest tool results
            [DOCUMENT CONTENT] snapshot + selection  built fresh per request, not stored
```

1. **`messages[0]` holds only the base system prompt.** It changes only when the base
   prompt changes: a mode switch, a document type change, or an edit to
   `additional_instructions`. Each of those is a one-time cache miss, and that is fine.
2. **The document snapshot is per request, not part of history.** Build it when the
   request is assembled and always put it **last**. Do not write it to `session.messages`
   or the history DB. One path for every provider:
   - **On a user send:** add it as a text block after the user's text, in the same user
     message.
   - **In a tool loop:** add it as a trailing user message after the tool results.
     OpenAI-format APIs require tool results to follow the assistant message that called
     them directly, so the snapshot cannot sit between them.
   - **No mid-conversation `role: "system"` message.** Many OpenAI-compatible providers
     and local chat templates reject it or mishandle it, and a per-model branch is not
     worth it.
   - **Label it** so the model does not take it for user text:
     `[DOCUMENT CONTENT — current state, not written by the user]`.
   - **Today the old snapshot is overwritten**, so the model never sees past snapshots.
     That stays the same.
3. **Mid-loop refresh after a mutating tool** puts a new snapshot in the same tail
   position. History before it is untouched, so the cache survives the whole tool loop.
4. **Cache markers (two explicit breakpoints):**
   - **Opening system breakpoint (1-hour TTL):** on Anthropic (native and OpenRouter
     `anthropic/*`), the last opening system block receives
     `{"type": "ephemeral", "ttl": "1h"}`. In native Anthropic cache ordering,
     `tools` precede `system`, so the system breakpoint covers both tools and
     system instructions. If system text is omitted, native Anthropic places the
     1-hour breakpoint on the last tool definition instead. OpenRouter chat tool
     schemas do not document tool cache control, so OpenRouter marks only the system
     block. For the six Alibaba models supporting explicit caching (`deepseek/deepseek-v3.2`,
     `qwen/qwen3-max`, `qwen/qwen-plus`, `qwen/qwen3.6-plus`, `qwen/qwen3-coder-plus`,
     `qwen/qwen3-coder-flash`), Alibaba supports only 5-minute TTL without a `ttl`
     parameter, so they receive `{"type": "ephemeral"}` with no `ttl`.
   - **Prefix stability:** the daily date line and `USER.md` are rendered inside the
     system prompt prefix. Changing memory or crossing midnight creates a new 1-hour
     cache entry, but subsequent chat turns and compaction that only rewrite later
     messages hit the warm 1-hour cache.
   - **Pre-snapshot breakpoint (5-minute TTL):** placed as an explicit
     `{"type": "ephemeral"}` marker on the block immediately preceding the live
     document snapshot. The live snapshot itself is never marked.
   - **Breakpoint ordering (1-hour before 5-minute):** Anthropic prompt cache ordering
     is `tools` → `system` → `messages`. A 1-hour breakpoint must always precede any
     5-minute breakpoint; reversing the order would cause a later 1-hour cache miss to
     invalidate an earlier 5-minute hit. Placing the 1-hour breakpoint on the opening
     system prompt covers tools and system instructions, while the subsequent 5-minute
     ephemeral marker covers prior conversational turns.
   - **Why top-level automatic caching is not sent:** Anthropic automatic caching
     places the cache breakpoint on the very last block of the request. In WriterAgent,
     the trailing block is the dynamic document snapshot, so automatic caching would
     write the volatile snapshot and miss on every document change or cursor movement.
   - **OpenRouter exact allow-list:** explicit cache markers are sent only for
     `anthropic/*` and the six Alibaba models above (resolving dynamic suffixes like
     `:nitro` or `:floor` to their base catalog IDs). Static variants like `:free`
     and snapshot models (e.g. `qwen/qwen3.5-plus-02-15`) do not support explicit
     breakpoints and remain unmarked. Other providers (Gemini 2.5+ implicit caching,
     OpenAI automatic caching, Grok, native DeepSeek, Moonshot) need no breakpoints.
   - **Anthropic role order:** in a tool loop the shim merges the trailing snapshot
     message into the user message that carries the tool results. The `tool_result`
     blocks come first and the snapshot text after them. The API would accept two user
     messages in a row, but one merged message keeps the request identical to what the
     model sees.
   - **Provider-layer snapshot split:** in `anthropic_shim` and `OpenRouterShim`
     (for explicit cache families), the provider layer splits the snapshot back out of the
     trailing tool result into a trailing text block (native) or trailing user message
     (OpenRouter). Anthropic caches through the whole run of tool results (measured
     2026-11-09), so keeping the snapshot inside any `tool_result` invalidates the entire
     cache on document edit; splitting it out keeps the prefix cached while commit
     2fd9c1777 (#3093) preserves the folded format for other providers (e.g. gpt-oss-20b).
   - **Other providers:** need nothing. They benefit from the stable prefix
     automatically.
5. **Later, only if measurements justify it:** split the document body, which changes
   only on edit, from the selection and cursor, which change constantly. The body can
   then sit before the breakpoint until the document is edited.

## Code to touch

- `chat_session.py`:
  - `set_system_context` and `refresh_document_context` stop concatenating the document
    into `messages[0]`. They keep the snapshot on the session, for example as
    `document_context`.
  - Add one function that returns the message list for a request (history + snapshot +
    new turn), and route every request builder through it.
- **Request assembly** in `send_handlers.py`, `tool_loop.py` and `tool_loop_actions.py`:
  use that function instead of `session.messages` directly.
- **`compaction.py`:**
  - The view is built as `[messages[0]] + summary + tail`, and `find_cut_index` starts at
    1 to "skip DOCUMENT CONTENT". Update the comments and the token estimates
    (`estimate_message_tokens(messages[0])` no longer includes the document), and add
    the snapshot's tokens to the budget separately.
  - Check that the cut index and the summary stay byte-stable between requests. A cut
    that moves on every request invalidates the cache just as the old `messages[0]` did.
- **History DB:** stop persisting the document text in the first row. Old sessions still
  have it there. Strip the `[DOCUMENT CONTENT]` suffix from `messages[0]` on load, so a
  resumed chat follows the new layout.
- **`plugin/acp/acp_backend.py`** sends `[DOCUMENT CONTENT]` as a prompt block. Check that
  it follows the same rule: snapshot last, not rewritten into earlier turns.
- **Provider layer:** add the cache marker for Anthropic endpoints, and log
  `cache_read_input_tokens`, or OpenRouter's `prompt_tokens_details.cached_tokens`, with
  the existing request logging. Sticky cache routing keys (`session_id` /
  `prompt_cache_key` / `x-grok-conv-id`) are separate; see [llm-hacks §13](llm-hacks.md#13-provider-sticky-cache-routing-session_id).

## Docs to update

- **AGENTS.md:** the invariant "Refresh document context each chat send" becomes "each
  request ends with a fresh `[DOCUMENT CONTENT]` snapshot; the stored history never
  contains it and is never rewritten." Say why: the prompt cache.
- `docs/chat/compaction-dev-plan.md`: the `messages[0]` description.

## Tests

- **Unit:** two consecutive requests in one session produce byte-identical prefixes up to
  the snapshot, after a document edit, a selection change, and a mutating tool round.
- **Unit:** in a tool round the snapshot comes after the tool results (OpenAI format), and
  the Anthropic shim emits one user message with `tool_result` blocks first, then the
  snapshot text, with the cache marker on the last `tool_result`.
- **Unit:** `session.messages` and the history DB never contain the snapshot.
- **Unit:** loading an old-format session strips the document from `messages[0]`.
- **Unit:** compaction view and token estimates with the snapshot outside `messages[0]`.
- **Live, manual:** a multi-turn chat on an Anthropic model shows non-zero
  `cache_read_input_tokens` from the second request on, and it stays non-zero through a
  tool loop.

## Risk to check with the eval

Models may weigh a document at the end of the context differently from one at the top. The
end is usually better for "answer about this document", but run the chat eval
(`make run_eval`) before and after on the main models: gpt-oss-120b, a Claude model, and a
local model.
