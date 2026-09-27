# Context-window resolution fidelity (WriterAgent)

**Author:** Eliyezer (via Chief)  
**From:** Eliyezer research pass (2026-09-10)  
**Status:** Implemented  
**Scope:** Sidebar compaction denominator (`resolve_context_window`).

## Status

`tool_loop` passes `window=resolve_context_window(client)` into `compact_session`. Order:

1. **Ollama:** live `num_ctx` from cached `POST /api/show` (`parameters` / Modelfile only). Missing → `None`. No `/v1/models` cache, no catalog, no trained `model_info["*.context_length"]` (#570).
2. **Cached live metadata:** `cached_v1_context_tokens(endpoint, model_id, provider)` reads the process memo filled when Settings/sidebar already fetched `/v1/models`. Accepts `context_length` **or** `context_window` (prefer `context_length` if both). OpenRouter `:nitro` / dynamic-suffix equivalence. **Does not HTTP.**
3. **`DEFAULT_MODELS`:** provider-keyed walk, then any-id match for custom (`writeragent-mock` → 32768).
4. **`None`** → `compact_session` returns `reason="no_window"` (proactive **and** force compact skip).

Harvest only. Compact never GETs `/v1/models`. Sidebar `populate_combobox_with_lru` skips OpenRouter and Together (`massive_providers`); those live lengths exist only after Settings `_bg_fetch`. Groq / DeepSeek / Mistral / Gemini / LM Studio / custom / mock populate the cache on sidebar open when the row publishes a length field.

Do **not** harvest `max_context_length` (LM Studio trained max ≠ loaded window; #570-class). Ignore `top_provider.context_length`. `openrouter/free` uses the catalog **200000** (OpenRouter’s advertised Free Models Router window); the routed hop may be smaller.

## Table

| Provider | Source-of-truth for context | What WA reads | Gap / note | Fix |
|---|---|---|---|---|
| **Ollama** | Live `num_ctx` in `/api/show` `parameters` / Modelfile; trained length in `model_info.*.context_length` (different number) | Live `num_ctx` only via `query_ollama_runtime_num_ctx`; missing → `None` (no v1 cache, no catalog) | Correct for #570; if Modelfile omits `num_ctx`, compaction stays off even when trained length exists | **Keep as-is.** Optional later: surface “unknown window” in `/tokens` UI — do not revive trained fallback |
| **OpenRouter** | `GET /api/v1/models` → `context_length` (and `top_provider.context_length`) | Cached `context_length` after Settings fetch, then catalog + `:nitro` equivalence | Sidebar does **not** fetch OR (massive list). Non-default ids stay `None` until Settings. `openrouter/free` is a router (catalog **200000**, OpenRouter listing) | Shipped. Ignore `top_provider.context_length` |
| **Together** | `GET /models` (array) → `context_length` | Cached length after Settings fetch, else catalog (MiniMax M3 **1_000_000**) | Sidebar skips Together fetch. Catalog may lag docs (**524288**) until Settings or catalog sync | Shipped harvest; catalog hygiene stays offline |
| **Groq** | `GET …/openai/v1/models` → `context_window` | Cached `context_window` on sidebar fetch, else catalog | Field name differs; now harvested | Shipped |
| **Google Gemini** | Native `models.get` → `inputTokenLimit` (+ separate `outputTokenLimit`); WA uses OpenAI-compat base | Cached OpenAI-compat length if present, else catalog `1048576` | Native `inputTokenLimit` unused | **Do not** invent native Gemini client |
| **DeepSeek / Mistral / Z.ai** | Provider model docs / their `/v1/models` (varies) | Cached length when the row publishes one, else catalog | Non-default ids → `None` if the endpoint omits a length field | Shipped same cache path |
| **Custom OpenAI-compat** (incl. mock) | Whatever the server puts on `/v1/models` (mock advertises `context_length: 32768`) | Cached length when present, else any-id catalog (`writeragent-mock` → 32768) | Servers that omit both keys stay `None` | Shipped |
| **LM Studio** | Native REST: `max_context_length` (trained) + loaded `context_length`; OpenAI `/v1/models` often has neither | Same OpenAI-compat cache; **not** Ollama `/api/show` | Inert unless the OpenAI row has `context_length` / `context_window`. Do not harvest `max_context_length` | Same cache path |
| **llama.cpp** (llama-server) | Shared `n_ctx` (prompt + completion); often not on OpenAI `/v1/models` | Cached length if exposed, else `None`. Overflow **detection** strings unchanged | Unknown → inert compact; process-death path already avoids compact-and-retry | **Do not** subtract `chat_max_tokens` |

### Catalog snapshot (`DEFAULT_MODELS` chat-relevant rows)

| Display | `context_length` | Provider ids |
|---|---|---|
| Free Models (Auto) | 200000 | `openrouter`: `openrouter/free` |
| DeepSeek V3 | 163840 | `deepseek`: `deepseek-chat` |
| DeepSeek V4 Flash | 163840 | `together`: `deepseek-ai/DeepSeek-V4-Flash-0731` |
| MiniMax M3 | 1000000 | `together`: `MiniMaxAI/MiniMax-M3` |
| GPT-OSS 120B | 131072 | `together` / `openrouter` (`…:nitro`) / `groq` |
| GPT-OSS 20B | 128000 | `together` / `openrouter` / `groq` |
| Mistral Large 3 | 262144 | `openrouter` / `mistral` |
| Gemini 3.1 Flash Lite Preview / Lite / Pro | 1048576 | `google` + `openrouter` |
| GLM 5.2 | 200000 | `zai`: `glm-5.2` |
| WriterAgent Mock | 32768 | `mock`: `writeragent-mock` |

Audio/image-only rows omit `context_length` (expected). Offline sync helper: `scripts/sync_orca_openrouter_catalog.py` + `scripts/lib/orca_catalog.py` (curated merge, not runtime).

## Ranked small plan (done)

1. **`/v1/models` harvest** in `model_fetcher` (`_context_tokens_from_v1_entries` → `_model_context_cache`). `context_length` or `context_window`; ignore non-positive and `max_context_length`.
2. **`resolve_context_window` order:** (a) Ollama live `num_ctx`; (b) `cached_v1_context_tokens`; (c) `DEFAULT_MODELS`; (d) `None`. No compact-time fetch.
3. **Offline catalog sync** remains the hygiene path for default-dropdown rows (e.g. Together MiniMax catalog 1_000_000 vs docs 524288 until Settings fetch or a catalog edit).
4. **`openrouter/free` / unknown id:** catalog **200000** for the free router (OpenRouter FAQ); unlisted ids stay `None`. The routed hop may be smaller — overflow retry still applies.
5. **Tests** on resolver + fetcher cache. Compaction policy (tiers, `GEN_RESERVE`, overflow retry) untouched.
6. LM Studio / custom use the same cache path. llama.cpp stays `None` unless `/v1/models` publishes `context_length` / `context_window`.

## Non-goals

- Hermes **256k** (or any) universal fallback denominator  
- Subtracting `chat_max_tokens` / rewriting shared-`n_ctx` accounting for llama.cpp (keep `GEN_RESERVE`)  
- Rewriting compaction algorithm / thresholds / summarizer  
- New microservice, new MCP/tool, or native Gemini client solely for limits  
- Using Ollama **trained** `model_info.*.context_length` as compaction denominator (#570)  
- Harvesting `max_context_length` (trained max, not the live load window)

## Sources

**Code / docs (repo @ `25320212`)**

- `plugin/framework/default_models.py` — `DEFAULT_MODELS`, `resolve_model_id`  
- `plugin/chatbot/compaction.py` — `resolve_context_window`, `should_compact`, `compact_session` (`no_window`)  
- `plugin/chatbot/tool_loop.py` — callers with `window=resolve_context_window(client)`  
- `plugin/framework/client/model_fetcher.py` — `parse_ollama_runtime_num_ctx`, `query_ollama_*`, `_model_context_cache`, `cached_v1_context_tokens`  
- `plugin/framework/openrouter_model_id.py` — `:nitro` / dynamic vs static suffixes  
- `plugin/framework/client/provider_detection.py` — `ollama` vs `lmstudio` vs `custom`  
- `docs/chat/compaction-dev-plan.md` §5; `docs/chat/llm-hacks.md` (§ Ollama / #570); `docs/tests/mock-llm-sidebar.md`  
- `tests/chatbot/test_compaction.py` — resolver cases; `tests/framework/client/test_model_fetcher.py` — #570  
- PRs: #712 (module), #713 (wire), #714 (mock Packet K); issue #570  

**Provider docs (URLs)**

- OpenRouter models / `context_length`: https://openrouter.ai/docs/guides/overview/models  
- OpenRouter variants / suffixes FAQ: https://openrouter.ai/docs/faq  
- Ollama `/api/show`: https://docs.ollama.com/api-reference/show-model-details · https://github.com/ollama/ollama/blob/main/docs/api.md  
- Together models `context_length`: https://docs.together.ai/reference/models · context windows: https://docs.together.ai/learn/context-windows  
- Groq list models / `context_window`: https://console.groq.com/docs/api-reference · https://console.groq.com/docs/models  
- Google Gemini `inputTokenLimit`: https://ai.google.dev/api/models · https://ai.google.dev/gemini-api/docs/tokens  
