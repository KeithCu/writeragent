# Agent Search: Autonomous Sub-Agent Web Searching

This document outlines the architecture and implementation of the **web search** capabilities in LocalWriter. The goal has been to provide the main AI agent with a robust tool to search the internet, navigate pages, and return a synthesized answer, all while keeping external dependencies to a minimum. 

---

## 1. Purpose and Architecture

- **Purpose**: Add a `search_web` tool to the main LocalWriter agent allowing it to perform autonomous web research.
- **Sub-Agent Approach**: Instead of having the main agent issue individual search queries and fetch URLs—which consumes significant context and risks derailing the main task—the `search_web` tool delegates the entire research task to an autonomous **sub-agent**.
- **Smolagents**: We have **vendored** parts of the Hugging Face `smolagents` library (specifically the `ToolCallingAgent` and supporting models/tools) to power this sub-agent. The sub-agent runs its own ReAct loop to iteratively search, read, and synthesize information until it finds the answer.

---

## 2. Implementation Details

### Vendoring Smolagents
To avoid introducing a heavy external dependency on `smolagents` (which ordinarily pulls in `requests`, `transformers`, `huggingface_hub`, etc.), we vendored only the core modules (`agents.py`, `models.py`, `tools.py`, `default_tools.py`, etc.) into `plugin/contrib/smolagents/`.
- We removed all HF/Gradio specific code.
- We modified `agents.py` to load its prompts from a local JSON file (`toolcalling_agent.json`) rather than depending on `pyyaml`.

### Zero-Dependency Web Tools
The original `smolagents` web tools (`DuckDuckGoSearchTool` and `VisitWebpageTool`) rely on `requests`, `beautifulsoup4`, and `markdownify`. We completely rewrote these tools to use only Python's standard library:
- **Networking**: Replaced `requests` with `urllib.request`. Both tools send a realistic Firefox user agent string (`Mozilla/5.0 (X11; Linux x86_64; rv:148.0) Gecko/20100101 Firefox/148.0`) to reduce 403s from some sites (e.g., Yelp).
- **Parsing**: Replaced `beautifulsoup4` and `markdownify` with custom subclasses of standard `html.parser.HTMLParser` to extract search results and strip tags for page content reading.

### LlmClient Wrapper
To ensure the sub-agent uses the exact same model, endpoint, and API keys as the rest of LocalWriter, we created `LocalWriterSmolModel` (in `core/smol_model.py`). This class extends the `smolagents` base `Model` class but overrides `generate()` to proxy all requests directly to LocalWriter's existing `core.api.LlmClient`. For CLI testing without LibreOffice we also provide `OpenRouterSmolModel` in `scripts/test_search_web.py`, which talks directly to OpenRouter’s OpenAI-compatible HTTP API.

### The `search_web` Tool
Exposed in `core/document_tools.py`, the `search_web` tool accepts a `query` and an optional `rationale`. When invoked:
1. It instantiates the `LocalWriterSmolModel`.
2. It initializes a `ToolCallingAgent` equipped with the zero-dependency DuckDuckGo and Webpage Visitor tools.
3. It kicks off the `smolagents` run loop, which autonomously researches the query until it calls the `final_answer` tool or hits a timeout/max-steps limit.
4. It catches the final answer and returns it to the main agent as JSON: `{"status": "ok", "result": "<answer>"}` (or `{"status": "error", "message": "..."}` on failure/timeout).

## 3. Vendored Files Overview

To facilitate the sub-agent without burdening LocalWriter with dependencies, we've pulled in a specific subset of the `smolagents` library. Here is an overview of what each file does and its importance to the project:

### Significant Files

**`plugin/contrib/smolagents/agents.py`**
This is the core engine of the sub-agent approach. It contains the `ToolCallingAgent` and `MultiStepAgent` classes which orchestrate the ReAct (Reasoning and Acting) loop. These classes are responsible for setting up the system prompts, delegating to the model, parsing the model's desired tool calls, executing those tools in the environment, and feeding the observations back into the loop until a final answer is reached. Modifications here are primarily focused on removing external prompt loading (like `pyyaml`) in favor of bundled JSON.

**`plugin/contrib/smolagents/tools.py`**
This file defines the base `Tool` class and the infrastructure for exposing Python functions to the LLM. It contains the logic for inspecting function signatures, generating JSON schemas, and securely mapping the LLM's requested arguments to the actual Python execution. It is vital for ensuring the agent can understand what tools are available and how to call them correctly.

**`plugin/contrib/smolagents/default_tools.py`**
This file houses the actual tools that the web search sub-agent relies on to achieve its goals. Most notably, it contains `DuckDuckGoSearchTool` for querying the web and `VisitWebpageTool` for scraping actual page content. We have completely overhauled this file to use only standard Python libraries (`urllib.request` and `html.parser`), eliminating the need for `requests`, `beautifulsoup4`, or `markdownify`.

**`core/smol_model.py`**
This is a custom file specific to LocalWriter, acting as the bridge between `smolagents` and our existing `core/api.py`. It provides the `LocalWriterSmolModel` class, which implements the `smolagents.models.Model` interface but delegates all LLM generation logic strictly to our `LlmClient`. This ensures the sub-agent uses the exact same model preferences, API keys, and enterprise endpoints configured by the user.

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
- **Tool Adaptation**: Completely rewrote `DuckDuckGoSearchTool` and `VisitWebpageTool` in `default_tools.py` to use `urllib.request` and standard library parsers, with a realistic Firefox user agent to reduce 403s.
- **Model Wrapper**: Built `LocalWriterSmolModel` (`core/smol_model.py`) to connect the sub-agent directly to LocalWriter's existing `LlmClient`.
- **Tool Registration**: Registered the `search_web` task in `core/document_tools.py` executing the ReAct loop inline.
- **YAML/Jinja2 removal for ToolCallingAgent**: Replaced `populate_template()` in `ToolCallingAgent.initialize_system_prompt()` with `_render_toolcalling_system_prompt()` and a JSON prompt (`toolcalling_agent.json`) using simple placeholders. The search_web path no longer depends on Jinja2.
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

## 5. Future Considerations

While the current implementation uses standard, unauthenticated HTTP requests via DuckDuckGo Lite, there is prior research on handling authenticated sites or executing JavaScript natively. The following sections are kept for future reference if we need to escalate beyond basic unauthenticated HTML scraping.

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

*Phase 2 roadmap*: Connect to the user's running browser via PyCDP (Chrome) or Marionette (Firefox) when the AI needs to read complex, JS-heavy pages or deeply embedded SPAs. This acts as an opt-in power feature where the user runs a simple launch script.
