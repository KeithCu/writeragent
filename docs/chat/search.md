# Agent Search: Autonomous Sub-Agent Web Searching

This document outlines the architecture and implementation of the **web search** capabilities in WriterAgent. The goal has been to provide the main AI agent with a robust tool to search the internet, navigate pages, and return a synthesized answer, all while keeping external dependencies to a minimum. 

---

## 1. Purpose and Architecture

- **Purpose**: Add a `search_web` tool to the main WriterAgent agent allowing it to perform autonomous web research.
- **Sub-Agent Approach**: Instead of having the main agent issue individual search queries and fetch URLs—which consumes significant context and risks derailing the main task—the `search_web` tool delegates the entire research task to an autonomous **sub-agent**.
- **Smolagents**: We have **vendored** parts of the Hugging Face `smolagents` library (specifically the `ToolCallingAgent` and supporting models/tools) to power this sub-agent. The sub-agent runs its own ReAct loop to iteratively search, read, and synthesize information until it finds the answer.

---

## 2. Implementation Details

### Vendoring Smolagents
To avoid introducing a heavy external dependency on `smolagents` (which ordinarily pulls in `requests`, `transformers`, `huggingface_hub`, etc.), we vendored only the core modules (`agents.py`, `models.py`, `tools.py`, `default_tools.py`, etc.) into `plugin/contrib/smolagents/`.
- We removed all HF/Gradio specific code.
- We modified `agents.py` to load its prompts from a bundled Python module (`toolcalling_agent_prompts.py`) rather than depending on `pyyaml`.

### Zero-Dependency Web Tools
The original `smolagents` web tools (`DuckDuckGoSearchTool` and `VisitWebpageTool`) rely on `requests`, `beautifulsoup4`, and `markdownify`. We completely rewrote these tools to use only Python's standard library:
- **Networking**: Replaced `requests` with `urllib.request`. Both tools send a realistic Firefox user agent string (`Mozilla/5.0 (X11; Linux x86_64; rv:148.0) Gecko/20100101 Firefox/148.0`) to reduce 403s from some sites (e.g., Yelp).
- **Parsing**: Replaced `beautifulsoup4` and `markdownify` with custom subclasses of standard `html.parser.HTMLParser` to extract search results and strip tags for page content reading.

### LlmClient Wrapper
To ensure the sub-agent uses the exact same model, endpoint, and API keys as the rest of WriterAgent, we created `WriterAgentSmolModel` (in [`plugin/chatbot/smol_agent.py`](../../plugin/chatbot/smol_agent.py)). This class extends the `smolagents` base `Model` class but overrides `generate()` to proxy all requests directly to WriterAgent's existing `LlmClient`. For CLI testing without LibreOffice we also provide `OpenRouterSmolModel` in `scripts/test_search_web.py`, which talks directly to OpenRouter’s OpenAI-compatible HTTP API.

### The `web_research` Tool
In [`plugin/chatbot/web_research.py`](../../plugin/chatbot/web_research.py), `WebResearchTool` has tier `"specialized"`, so the main chat and MCP (`delegate` and `direct_discovery` modes) reach it only through `delegate_to_subagent(domain="web_research")`. The sidebar Web Research mode and `wa.web.research` call it by name, and MCP `direct_flat` still lists it. It accepts a research `query` and optional history. When invoked:
1. It builds a `WriterAgentSmolModel` via [`plugin/chatbot/smol_agent.py`](../../plugin/chatbot/smol_agent.py).
2. It initializes a `ToolCallingAgent` equipped with the zero-dependency DuckDuckGo and Webpage Visitor tools.
3. It kicks off the `smolagents` run loop, which autonomously researches the query until it calls the `final_answer` tool or hits a timeout/max-steps limit.
4. It catches the final answer and returns it to the main agent as JSON: `{"status": "ok", "result": "<answer>"}` (or `{"status": "error", "message": "..."}` on failure/timeout).

`web_search` accepts an optional `recency` (`day`/`week`/`month`/`year`/`any`) that maps to DuckDuckGo Lite's `df` time filter. Search-cache keys include that token so filtered hits do not collide with evergreen ones. Today's date is injected by [`LlmClient`](../../plugin/framework/client/llm_client.py), not a second research prompt.

`web_search` POSTs to DuckDuckGo Lite through [`sync_request`](../../plugin/framework/client/requests.py) with an explicit `Content-Type: application/x-www-form-urlencoded` header (which `http.client` does not add automatically). `sync_request` reuses a shared persistent keep-alive connection per `(origin, connect_timeout)` serialized by a lock, transparently reconnecting once on a stale closed socket without charging a retry, and closes cached connections on shutdown. Each search gets up to 3 attempts with the shared jittered backoff and per-host cooldown, a 5 s connect budget, and a 10 s read budget. Like LLM traffic, `web_search` connects directly and does not use system or environment proxies. Stop (`SendCancellation`) aborts the lock wait, connect worker, socket, or backoff wait and returns a stopped message. A non-200 response (including DDG's 202) or exhausted retries returns a fixed "service unavailable, do not reword" message to the model and is not cached.

### Prompt for web research (HITL)

When **Prompt for Web Research** is enabled (`chatbot.prompt_for_web_research`), a shallow run blocks on each internal `web_search` until the user accepts, edits, or rejects the DuckDuckGo query. This applies in **main chat** (via delegate `domain=web_research`) and in dedicated **Web Research** sidebar mode. Deep research asks once. Reject returns `USER_STOPPED` and does not start the loop. Change uses the edited query for the preview, the research loop, and the cache write. Sub-queries do not ask again: the sidebar has one approval slot, and a second prompt is treated as Stop. If approval setup itself fails, the search does not run. After an `mcp` delegation in the same chat turn, `web_research` also asks for approval even when `chatbot.prompt_for_web_research` is off.

`PageStore` (`plugin/chatbot/web_pages.py`) records a URL only after the fetch returns non-empty, non-error page text (`is_page_text`). An error string (`Error…` / `Failed…`) or an empty extract is not stored, so a later call or sub-query can retry that URL. When Chrome or Firefox CDP is on, research runs in this process share one local browser. That browser is closed only after the last of those runs leaves, and only once that run's in-flight page reads have returned. A failed deep-research synthesis is returned as notes and is not written to the research cache.

### Page store and chunked reading

`WebResearchTool.execute` resolves the sub-agent's context window once via `resolve_context_window(smol_model.api)`. An unknown window (`None`) falls back to 4,096 tokens on Ollama (default `num_ctx`) and 24,000 tokens on other endpoints. The sub-agent's system prompt + `WEB_RESEARCH_EXAMPLES` (~1,370–1,450 rough tokens) plus wire tool schemas (~565 rough tokens) consume about 2,000 tokens before any query or observation; windows below 4,000 tokens return a `WEB_RESEARCH_CONTEXT_TOO_SMALL` error advising the user to raise `num_ctx` instead of running with tiny parts. Otherwise `PageStore` stores a per-result character budget `budget_chars = clamp(window_tokens // 2, 2_000, 48_000)` and splits stored pages at `put()` into parts of `part_chars = budget_chars // 2` cut at line breaks (4k → 2,000 / 1,000; 8k → 4,000 / 2,000; unknown non-Ollama 24k → 12,000 / 6,000; 32k → 16,000 / 8,000; 128k+ → 48,000 / 24,000).

`visit_webpage` takes a `urls` array (1 to 5 URLs; extras beyond 5 are reported `[skipped]`). Non-empty URLs are deduplicated by `url_key` (`url.strip().rstrip("/")`), already-stored URLs are served from `PageStore` without re-fetching, and pending URLs are fetched in parallel on `run_in_background(..., dedicated=True)` threads with a 45 s batch wait. Each URL finishes independently in input order: valid text is stored in `PageStore` and given `budget_chars // n` characters for an `n`-URL batch — returned whole if it fits, otherwise part 1 (clipped at a newline to `budget_chars // n` when `n >= 3` makes the per-URL share smaller than part 1, with a note that `read_page(url, 1)` returns the full part 1); a raised exception or `Error…`/`Failed…` text is reported as `[failed] <url>: ...` without failing the other URLs; a slow URL past the batch deadline is reported as `[timed out] <url>: no response within 45 s`; and Stop (`stop_checker`) breaks the wait within 0.25 s and reports `[stopped] <url>`. Two companion tools, `read_page(url, part)` and `find_in_page(url, text)`, let the sub-agent read later parts or jump to matches without replaying the full page on every step. The store is shared across deep-research workers via `dataclasses.replace`, capped at 100 pages (evicting the oldest), and freed when the run ends; both HTTP and CDP fetches allow up to 200,000 characters per page. Parts are used instead of line numbers because `TextExtractor` emits one line per HTML text-node fragment (often 7–14 characters).

Before the Accept/Change/Reject wait, the sidebar response area shows a `Tool: web_search` line plus the search-engine preview sentence (`web_research_chat.web_search_engine_step_chat_text`). Reject leaves that line in the transcript; Change appends a second preview for the edited query.

## 3. Vendored Files Overview

To facilitate the sub-agent without burdening WriterAgent with dependencies, we've pulled in a specific subset of the `smolagents` library. Here is an overview of what each file does and its importance to the project:

### Significant Files

**`plugin/contrib/smolagents/agents.py`**
This is the core engine of the sub-agent approach. It contains the `ToolCallingAgent` and `MultiStepAgent` classes which orchestrate the ReAct (Reasoning and Acting) loop. These classes are responsible for setting up the system prompts, delegating to the model, parsing the model's desired tool calls, executing those tools in the environment, and feeding the observations back into the loop until a final answer is reached. Modifications here are primarily focused on removing external prompt loading (like `pyyaml`) in favor of bundled Python prompt strings.

**`plugin/contrib/smolagents/tools.py`**
This file defines the base `Tool` class and the infrastructure for exposing Python functions to the LLM. It contains the logic for inspecting function signatures, generating JSON schemas, and securely mapping the LLM's requested arguments to the actual Python execution. It is vital for ensuring the agent can understand what tools are available and how to call them correctly.

**`plugin/contrib/smolagents/default_tools.py`**
This file houses the actual tools that the web search sub-agent relies on to achieve its goals. Most notably, it contains `DuckDuckGoSearchTool` for querying the web (via [`sync_request`](../../plugin/framework/client/requests.py)) and `VisitWebpageTool` for scraping actual page content (via `urllib.request`), using `html.parser` instead of `requests`, `beautifulsoup4`, or `markdownify`.

**[`plugin/chatbot/smol_agent.py`](../../plugin/chatbot/smol_agent.py)**
This is the bridge between `smolagents` and WriterAgent's `LlmClient`. It provides `WriterAgentSmolModel` (implements `smolagents.models.Model`), plus `to_smol_inputs` / `SmolToolAdapter` and `build_toolcalling_agent`. This ensures the sub-agent uses the exact same model preferences, API keys, and enterprise endpoints configured by the user.

### Trivial / Supporting Files

- **`plugin/contrib/smolagents/models.py`**: Contains the abstract shapes and data classes (like `ChatMessage` and `ToolCall`) used by `smolagents` to model conversational turns, with all heavy provider-specific integrations stripped out.
- **`plugin/contrib/smolagents/memory.py`**: Manages the conversation history and the agent's internal memory tape (`ActionStep`), keeping track of past tool calls and observations within a single run.
- **`plugin/contrib/smolagents/utils.py`**: Provides various helper functions for string formatting, logging, and previously Jinja2 templating (which is in the process of being removed).
- **`plugin/contrib/smolagents/agent_types.py`**: Defines core data structures and typed dicts used throughout the library to enforce type safety.
- **`plugin/contrib/smolagents/serialization.py`, `_function_type_hints_utils.py`, `tool_validation.py`**: These provide the underlying parsing and validation logic that allows `smolagents` to convert python types into strict JSON schemas for the LLM to consume.
- **`plugin/contrib/smolagents/monitoring.py`**: Contains hooks for logging and tracking agent performance, although much of its external telemetry has been bypassed for our local environment.
- **`plugin/contrib/smolagents/__init__.py`**: Exposes the relevant classes for clean importing, heavily pruned to ignore modules we didn't vendor (e.g., Gradio UI, CLI).

---

## 4. Current Status & Testing

**What is Done:**
- **Vendoring core files**: Copied `agents.py`, `models.py`, `tools.py`, `default_tools.py`, etc. to `plugin/contrib/smolagents`.
- **Tool Adaptation**: Rewrote `DuckDuckGoSearchTool` (using [`sync_request`](../../plugin/framework/client/requests.py)) and `VisitWebpageTool` (using `urllib.request`) in `default_tools.py` with standard library HTML parsers and a realistic Firefox user agent to reduce 403s.
- **Model Wrapper**: Built `WriterAgentSmolModel` ([`plugin/chatbot/smol_agent.py`](../../plugin/chatbot/smol_agent.py)) to connect the sub-agent directly to WriterAgent's existing `LlmClient`.
- **Tool Registration**: Registered the `search_web` / web-research path so the ReAct loop runs via the shared smol agent construction.
- **YAML/Jinja2 removal for ToolCallingAgent**: Replaced `populate_template()` in `ToolCallingAgent.initialize_system_prompt()` with `_render_toolcalling_system_prompt()` and prompts in `toolcalling_agent_prompts.py` using simple placeholders. The search_web path no longer depends on Jinja2.
- **Optional Rich/logging**: `monitoring.py` and `agents.py` use lightweight stubs when `rich` is absent; the search_web path logs to stderr/stdout only, and our `Console` stub ignores Rich-only kwargs like `style=...`.
- **Optional heavy deps (PIL/requests/audio)**: `agent_types.py` treats Pillow and requests as optional; image/audio types only require them if actually used. This keeps the vendored package importable on a minimal Python install while search_web (text only) continues to work.
- **Error handling and timeout**: `tool_search_web` catches all exceptions and returns JSON `{"status": "error", "message": "..."}`; optional config key `search_web_timeout` (default 120 s) wraps the sub-agent in a `ThreadPoolExecutor` so long-running searches do not block the UI indefinitely.
- **CLI harness (OpenRouter)**: `scripts/test_search_web.py` provides a standalone CLI that uses `OpenRouterSmolModel` + `ToolCallingAgent` + `DuckDuckGoSearchTool` + `VisitWebpageTool` against OpenRouter. It defaults to `nvidia/nemotron-3-nano-30b-a3b` and falls back to a plain completion if the model does not emit JSON tool-calls.

**Manual Testing Notes:**
- From the repo root:
  - `export OPENROUTER_API_KEY="sk-or-..."`  
  - `python -m scripts.test_search_web "What is the latest stable Python release and when was it released?"`
- You can pass `--model some/other-model` or `--max-tokens 4096` to experiment.
- Models that fully support OpenAI tool-calling will drive the ReAct loop (web_search + visit_webpage + final_answer). Models that ignore tools will still produce a useful answer via the direct-completion fallback.

---

## 5. Web research report cache (fuzzy matching)

Completed `web_research` / delegate `web_research` reports can be cached in the shared SQLite `web_cache` table (`kind="research"`). Keys are normalized query word lists (fluff stripped, min token length 3). Shallow entries are stored as `{snowball_lang}|{word key}` (e.g. `english|elevator physics space`); legacy unprefixed keys still work. Deep research uses the same table with a `deep|` segment before the language (`deep|english|elevator physics space`).

`web_cache_max_mb` of 0 disables that database. Page fetches, search results, and completed research reports all skip the read and the write, so a report already on disk is not served.

**Instruction fluff** ([`research_cache_fluff.py`](../../plugin/chatbot/research_cache_fluff.py)): web-research prompt filler words are listed as standard `_('…')` calls in `translated_research_cache_fluff()` — same gettext path as the rest of the extension (`make extract-strings`, `make auto-translate`). At runtime `get_research_fluff_words()` tokenizes those translated strings for the active LO UI locale and unions grammar stop words from [`stop_words.py`](../../plugin/writer/locale/stop_words.py) for the document/Snowball language (generated from [stopwords-iso](https://github.com/stopwords-iso/stopwords-iso) via `python scripts/generate_stop_words.py`).

**Lookup order:** exact key, then embedding match among same-language keys when local embeddings are configured and already warm, then fuzzy stem match among same-language keys. Exact, embedding, and fuzzy matches stay inside the same mode (shallow or `deep|`) and language. English shallow exact lookup tries the bare word key first, then the language-prefixed key. Other languages try only the prefixed key, so a legacy unprefixed English row is not a hit. Deep lookup tries only the `deep|` key.

**Embedding match:** when the local embeddings venv is configured, WriterAgent opportunistically stores vectors for research cache keys in a companion SQLite table (`web_cache_embeddings`). The web research path does not wait for cold model startup or old-row migration:

- New cache writes enqueue embedding generation in a background worker after the report is saved, using normalized query terms in their original order.
- Old `kind="research"` rows are backfilled in the same background path. Their original prompt order is not recoverable, so backfill uses the stored normalized word key.
- Lookup only attempts embedding search when stored vectors already exist, and uses the embeddings worker timeout for the query vector. Missing dependencies, unsupported providers, model downloads, or worker errors silently fall back to the old stem/Jaccard path.
- Embedding hits use **Research Cache Embedding Match (%)** (default 75) as their cosine threshold.
- Cosine search runs in LibreOffice Python over JSON vectors stored in SQLite; only embedding generation runs in the user venv.

**Fuzzy match** ([`plugin/chatbot/web_research_cache.py`](../../plugin/chatbot/web_research_cache.py)):

- Stems use the same Snowball algorithms as writer full-text search ([`grammar_rule_ids.py`](../../plugin/framework/grammar_rule_ids.py) `ISO_TO_SNOWBALL`). The cached pure-Python stemmer mutates cursor state inside `stemWord`; `stem_word` holds a lock around that call so concurrent research threads can share the cache.
- Language: document `CharLocale` → LibreOffice UI locale → `english`. Both UNO reads are marshalled to the main thread via `execute_on_main_thread` because `web_research` runs on an async worker. A disposed document (`is_disposal`, including `DocumentDisposedError` from the UNO thread guard) aborts that lookup instead of caching the run as `en_US` / `english`. Send cancellation and a main-thread timeout still use that fallback.
- Similarity = `max(union Jaccard, overlap / min(|A|, |B|))` so repeat prompts with extra words still match.
- Gates: similarity ≥ **Research Cache Fuzzy Match (%)** (default 60) and shared stem count ≥ **Min Stem Overlap** (default 8).

Settings UI: `web_research_cache_enabled` only. Cache matching tuning (`web_research_cache_embedding_percent`, `web_research_cache_jaccard_percent`, `web_research_cache_min_overlap`) is **internal** — defaults from module YAML, override in `writeragent.json` if needed. Sidebar shows `hit_embedding` / `hit_fuzzy` with match percent and stored key.

---

## 6. Future Considerations

### Deep Research (sidebar mode)

Choose **Deep Research** in the sidebar mode dropdown (Writer, Calc, Draw, and Impress) to run a **breadth/depth multi-query loop** via a dedicated sub-agent. On Writer it can **`apply_content`** after synthesis when that tool is in the sub-agent tool list; on Calc, Draw, and Impress the session exposes web research only.

Shallow **Web Research** sidebar mode streams results to chat only and does not expose document editing tools.

The loop is ported from [gpt-researcher](https://github.com/assafelovic/gpt-researcher)’s `DeepResearchSkill`. The `gpt-researcher/` subdirectory in this repo is a **reference only** — it is not imported at runtime and is excluded from the OXT bundle (`.gitignore`).

**Flow (adaptive deep mode):**

1. **Round loop** (up to `chatbot.deep_research_max_rounds`, default 3):
   - **Breadth** — LLM generates N search queries per round (`chatbot.deep_research_breadth`, default 4). Round 1 generates the queries directly from the user query and the optional DuckDuckGo preview snippet (there is no separate planning call).
   - **Parallel sub-queries** — up to `chatbot.deep_research_concurrency` shallow ReAct sub-agents run at once (global cap: `chatbot.deep_research_max_sub_queries`, default 14).
   - **Per sub-query** — `_run_web_agent` with research goal, visited-URL dedup, and optional extra step budget.
   - **Extract** — LLM pulls learnings, citations, and source URLs from each sub-report.
   - **Assess** — LLM scores coverage (`chatbot.deep_research_quality_threshold`, default 7/10); stops early when sufficient or continues with gap-filling queries.
2. **Synthesize** — LLM writes one plain-text report (`WEB_RESEARCH_PLAIN_TEXT_FORMAT` in [`web_research.py`](../../plugin/chatbot/web_research.py)) including a sources list. If that last LLM call fails, the orchestrator returns the collected learnings and sources instead of discarding the run. If neither learnings nor raw sub-agent reports were collected, it returns an error payload rather than a “completed” report on empty evidence; a failed extraction keeps that sub-agent's raw report as evidence.

`VisitWebpageTool` does not cache fetch-error strings (a transient 403/timeout must not replay for `web_cache_validity_days`).

Implementation: [`plugin/chatbot/web_research_deep.py`](../../plugin/chatbot/web_research_deep.py) (adaptive orchestrator + JSON parsers); sidebar session [`plugin/chatbot/deep_research_session.py`](../../plugin/chatbot/deep_research_session.py) (`deep_research_search_web` → `apply_content`); shallow agent helper [`_run_web_agent`](../../plugin/chatbot/web_research.py).

**Config (internal, JSON override):** `chatbot.deep_research_breadth`, `chatbot.deep_research_max_rounds`, `chatbot.deep_research_concurrency`, `chatbot.deep_research_max_sub_queries`, `chatbot.deep_research_quality_threshold`, `chatbot.deep_research_sub_agent_steps`.

---

While default page reads use standard, unauthenticated HTTP requests via DuckDuckGo Lite and `urllib.request`, JS-heavy page rendering via a local Chrome or Firefox CDP instance is now shipped (`plugin/contrib/cdp/browser_cdp_tool.py`, `VisitWebpageCdpTool` in `plugin/chatbot/web_research.py`). The following sections cover what remains future reference (authenticated sites, cookie extraction, and connecting to an already-running user browser):

### A) yt-dlp cookie extraction (reference implementation)
If there is a need to mimic the user's browser via cookies (to avoid blocks/CAPTCHAs):
- **Source**: `yt_dlp/cookies.py` (Unlicense).
- **SQLite**: `sqlite3` is part of stdlib. Cookie values are extracted directly from Firefox (`cookies.sqlite`) or Chrome (`Cookies`).
- **Standard library**: `http.cookiejar.CookieJar` can attach cookies to a `urllib.request.Request`. The request then carries a `Cookie` header for the target URL's domain/path.

### B) Easy way to "make requests + run JS" via Chrome (without Playwright)
If the sub-agent needs to render JS-heavy search result pages, we can avoid heavy dependencies like Playwright (~150MB headless browser):
- **PyCDP** ([py-cdp.readthedocs.io](https://py-cdp.readthedocs.io/)): Thin Python client generated from the CDP spec. Avoids bundling a browser. Connects to a Chrome instance via `--remote-debugging-port=9222`. 
- **Pydoll**: Lightweight, CDP-based, async alternative.

### C) Lightweight option for Firefox
Firefox deprecated CDP in favor of WebDriver BiDi (v129+).
- **Marionette**: Firefox's built-in remote protocol via `-marionette` (port 2828). JSON over TCP. 
- Python clients include `marionette_driver` (Mozilla's maintained client) or the tiny `k0s/marionette_client`.

*Phase 2 roadmap*: Local Chrome or Firefox CDP (`plugin/contrib/cdp/browser_cdp_tool.py`, `VisitWebpageCdpTool` in `plugin/chatbot/web_research.py`) is shipped for JS-heavy pages. Connecting to the user's already-running Chrome (CDP) or Firefox (Marionette) with their cookies for authenticated or deeply embedded SPAs remains open as an opt-in launch-script feature.

## 7. Document full-text index (`advanced_search`)

`IndexService` ([`linguistic_index.py`](../../plugin/writer/locale/linguistic_index.py), service `writer_index`) backs the Writer **`advanced_search`** and **`get_index_stats`** tool names in older docs. Those classes are **`ToolBaseDummy`** only ([`search.py`](../../plugin/writer/search.py)) — **parked 2026-11-05**; use **`search`** (no second search tool).

- Coverage is body paragraphs of the top-level text enumeration. A table keeps its slot as the literal text `"[Table]"` and its cell text is not indexed; text frames, headers/footers, and drawing shapes are not walked. Use `search` for text in those places.
- Language: the document-level `CharLocale` wins; otherwise the first non-empty body paragraph with a usable locale (tables and blank paragraphs are skipped, `zxx` is ignored). The response reports `detected_language` (the tag as read), `language` (the Snowball algorithm actually used; still `english` when unsupported) and `stem_fallback: true` when the detected tag has no Snowball algorithm.
- Stop words from [`stop_words.py`](../../plugin/writer/locale/stop_words.py) are normalized through the same tokenizer as document and query tokens, so accented and punctuated entries match.
- Grammar: `AND` is the default, plus `OR`, `NOT`, and one `NEAR/N` per query. `NEAR/N` is *paragraph-index distance* (within N paragraphs), not word proximity. Mixed `AND`/`OR` and a second `NEAR/N` are rejected with an error.

---

## Open

- `[feature L few]` Connect to the user's running Chrome (CDP) or Firefox (Marionette) with their cookies to read JS-heavy or authenticated pages, as an opt-in launch-script feature. (§ 6. Future Considerations)
