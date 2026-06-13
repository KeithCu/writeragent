# Langchain & smolagents Integration Plan for WriterAgent

This document outlines a phased development plan to integrate `langchain-core` and adapt code from `smolagents` into WriterAgent, starting with basic conversation history and progressively adding more advanced memory, tools, and agentic features.

> **Status (2026-06):** Most of **Phase 1–2** and **smolagents sub-agent isolation** are **already shipped** without `langchain-core`. See [What is already implemented](#what-is-already-implemented-2026-06) and [What is still worth doing](#what-is-still-worth-doing-next). Full LangChain wiring is **optional**, not a prerequisite for the remaining roadmap.

## Goal Description
Enhance WriterAgent's AI capabilities by replacing manual prompt construction with `langchain-core`'s robust memory and agent abstractions, while vendoring and adapting secure, zero-dependency code from `smolagents`. This will allow the AI to "remember" past interactions, provide a seamless chat experience, and eventually perform complex multi-step document operations autonomously.

---

## What is already implemented (2026-06)

| Planned item | Shipped replacement | Entry points |
|--------------|---------------------|--------------|
| In-memory + persistent chat history | `ChatSession` + `SQLite3History` / `JSONHistory` fallback | [`plugin/chatbot/panel.py`](../plugin/chatbot/panel.py), [`plugin/chatbot/history_db.py`](../plugin/chatbot/history_db.py) |
| Session keyed by document | `WriterAgentSessionID` on document model (URL hash fallback) | [`plugin/chatbot/panel_factory.py`](../plugin/chatbot/panel_factory.py) |
| Document context per send (separate from history) | `get_document_context_for_chat` + `session.set_system_context` | [`plugin/chatbot/tool_loop.py`](../plugin/chatbot/tool_loop.py), [`plugin/doc/document_helpers.py`](../plugin/doc/document_helpers.py) |
| Custom tool-calling loop (not LangChain `AgentExecutor`) | `ToolCallingMixin` / `tool_loop.py` + `LlmClient` | [`plugin/chatbot/tool_loop.py`](../plugin/chatbot/tool_loop.py) |
| Smolagents sub-agents (web research, librarian) | `web_research`, `librarian_onboarding` tools; final answer only in main history | [`plugin/chatbot/web_research.py`](../plugin/chatbot/web_research.py), [`plugin/chatbot/librarian.py`](../plugin/chatbot/librarian.py) |
| Long-term user profile memory (partial) | `upsert_memory` → JSON in `USER.md`; librarian injects profile | [`plugin/chatbot/memory.py`](../plugin/chatbot/memory.py) |
| Scientific Python / NumPy (out-of-process) | Warm venv worker (`run_venv_python_script`, `=PYTHON()`) | [enabling_numpy_in_libreoffice.md](enabling_numpy_in_libreoffice.md) |

**Not in `pyproject.toml`:** `langchain-core` — the sidebar does not depend on it today.

---

## What is still worth doing (next)

Prioritized by leverage vs. complexity. **Do not** re-implement Phases 1–2 via LangChain unless you explicitly want that dependency.

| Priority | Item | Why it matters | Suggested approach |
|----------|------|----------------|-------------------|
| **1** | **Inject `USER.md` into main chat** (Hermes-style read path) | Model sees prefs without `upsert_memory` / `read` calls; librarian already injects for onboarding only | Append `[USER PROFILE]` block in `get_chat_system_prompt_for_document` or `set_system_context`; keep `upsert_memory` for writes |
| **2** | **Chat history summarization** (old Phase 3) | Very long sidebar sessions can exceed model context even when document excerpt is capped at 8k | Token/char budget on `session.messages`; when over threshold, one summarizer LLM call replaces oldest turns with a single system/assistant summary message (persist via `history_db`) |
| **3** | **Local embedder + routing** | MVP: `sentence-transformers` in venv; cloud HTTP tier-two | See [embeddings.md](embeddings.md) — Phase A bench + `embedding_client.py` |
| **4** | **Document embeddings index** (old Phase 4, retrieval) | Outer document_research: semantic find instead of grep; minimal locator cache | See [embeddings.md](embeddings.md) |
| **5** | **Background memory reviewer** | Passive “should we save this?” without burdening main tool loop | Optional second LLM pass after reply (Hermes pattern); uses `MemoryStore` in code, not extra main-chat tools |
| **6** | **Skills tools** | Procedures on disk; guidance exists in constants | Register [`plugin/chatbot/skills.py`](../plugin/chatbot/skills.py) when ready; optional index injection like memory |
| **Low** | **Full `langchain-core` integration** | Runnable chains, community vendoring | Only if you want LangChain ecosystem interop; duplicates working `ChatSession` + `tool_loop` |

**Explicitly deprioritized:** Replacing `tool_loop.py` with LangChain `create_tool_calling_agent` / `AgentExecutor` — current FSM, streaming, and UNO drain are tightly coupled to the custom loop.

---

## Proposed Changes

### Phase 1: Foundation & Short-Term Memory — **DONE (without LangChain)**
**Objective**: Introduce `langchain-core` and implement basic `ConversationBufferMemory` for the current session's chat.

> **Superseded:** `ChatSession` + `history_db.py` fulfill this phase. Skip LangChain `ConversationBufferMemory` unless you adopt `langchain-core` for other reasons.

- **Dependency Management**: 
  - Add `langchain-core` (and potentially `langchain` or specific provider packages) to the project requirements.
  - Ensure compatibility with LibreOffice's bundled Python environment.
- **Refactor [core/api.py](file:///home/keithcu/Desktop/Python/writeragent/core/api.py)**:
  - Implement a custom LangChain `BaseChatModel` wrapper (`WriterAgentLangChainModel`) around the existing `LlmClient`. This avoids the bloat of native provider packages (like `langchain-openai` which brings heavy dependencies like `httpx`) and retains our LibreOffice-optimized streaming loop, connection pooling, and error mapping.
  - Introduce `ConversationBufferMemory` to automatically manage the message history.
- **Update `chat_panel.py`**:
  - Instead of rebuilding the context string manually via `get_document_context_for_chat` with every message, inject the document state as a dynamic system prompt or context variable within a LangChain `Runnable` or `Chain`.

### Phase 2: Persistent Conversation History — **DONE**
**Objective**: Allow chats to persist across LibreOffice restarts.

> **Shipped:** `writeragent_history.db` (SQLite) with per-`session_id` rows; JSON fallback when `sqlite3` unavailable. Clear via session API / UI wiring as implemented in the panel.

- **Storage Mechanism**:
  - Implement a local storage solution (e.g., a simple JSON file per document URL under `~/.config/libreoffice/4/user/config/writeragent_chat_history/` or an **SQLite database** — Python’s `sqlite3` is stdlib on all major OSes, so no extra dependency).
  - Use LangChain's `BaseChatMessageHistory` interface (e.g., `FileChatMessageHistory` or a custom implementation) to load and save messages.
- **Session Management**:
  - Tie conversation histories to document URLs (`doc.getURL()`).
  - Add a "Clear History" button to the chat sidebar.

### Phase 3: Token Management & Summarization Memory
**Objective**: Prevent the conversation history from exceeding the LLM's context window during long sessions.

- **Summarization**:
  - Replace `ConversationBufferMemory` with `ConversationSummaryBufferMemory`.
  - Configure a background LLM call to summarize older messages when the token count reaches a configured threshold (e.g., 80% of `chat_context_length`).
- **Config Updates**:
  - Add settings for `memory_strategy` (Buffer vs. Summary) and `max_memory_tokens`.(

### Phase 4: Long-Term Document Memory (RAG) — **NEXT (see embeddings.md)**

**Objective**: Enable cross-document find for **document_research** (outer agent) and optional in-document RAG for huge single files.

> **Canonical plan:** [embeddings.md](embeddings.md) — **one minimal index** (vectors + locators); outer document_research `search_embeddings`; **MVP local embed** via `sentence-transformers` in venv; cloud APIs tier-two; ~60 s incremental maintenance.


### Phase 5: Agentic Workflows & Multi-Step Reasoning
**Objective**: Transition from a simple "Chat + Tools" model to autonomous problem solving.

- **Agent Orchestration**:
  - Use LangChain's `create_tool_calling_agent` and `AgentExecutor` to replace the custom tool execution loop in `chat_panel.py`.
  - Allow the agent to plan multi-step tasks (e.g., "Analyze this table, find errors, and format the erroneous cells red").
- **Human-in-the-Loop**:
  - Implement LangChain callbacks to pause execution and ask the user for confirmation before applying destructive changes to the document.

## Note: SQLite ships with Python

**`sqlite3` is part of the Python standard library** on all major OSes (Windows, macOS, Linux) in normal CPython builds — no `pip install` required. That opens several storage options without adding dependencies:

- **Phase 2 (persistent chat history)**: SQLite is a natural fit for conversation history (e.g. one table per document URL, or a single DB with a doc key). No extra dependency.
- **Phase 4 (RAG / embeddings)**: Stdlib SQLite does **not** provide vector similarity search. Use SQLite for chunk metadata and FTS5; keep vectors in a float32 sidecar or sqlite-vec in the user venv. See [embeddings.md](embeddings.md).

Keeping this in mind makes it easier to choose stdlib-friendly storage (e.g. SQLite for history and RAG metadata) without pulling in heavier backends.

**Vector extension in stdlib?** As of early 2025 there is **no plan or PEP** to add a vector/similarity-search extension to Python’s standard library. Stdlib `sqlite3` stays as the DB-API interface to stock SQLite; vector search is provided by **loadable extensions** (e.g. `sqlite-vec`, `sqlite-vector`) that are third-party and require `conn.enable_load_extension(True)` and `conn.load_extension(...)`. So for the foreseeable future, “stdlib-only” RAG means our own vector store (binary + JSON, pure-Python or optional NumPy) — we can’t rely on stdlib SQLite gaining vector search.

---

## Research: `langchain-community`

**Value it can add:**
`langchain-community` provides a massive collection of third-party integrations. For WriterAgent, its main value would be ready-made components for Phase 2 (e.g., `SQLChatMessageHistory` to store conversations in SQLite) and Phase 4 (various document loaders, text splitters, and vector store wrappers).

**Dependency weight and NumPy:**
While it offers convenience, `langchain-community` is a very heavy package. A basic `pip install langchain-community` pulls in numerous dependencies including `SQLAlchemy`, `PyYAML`, `requests`, `aiohttp`, `dataclasses-json`, and **`numpy`**.
Because it forces a `numpy` installation (and other heavy libraries) just for the base package, it directly conflicts with our "minimal dependencies" constraint for LibreOffice.

**Conclusion: Vendoring Strategy**
Instead of installing `langchain-community` as a dependency, we should treat its [open-source repository](https://github.com/langchain-ai/langchain) as a reference implementation library. We continue to depend strictly on `langchain-core` as planned. When we need specific functionality, we will **find the relevant code in `langchain-community`, copy it into our source tree (vendoring), and adapt it** to work within our LibreOffice constraints. This allows us to leverage community-built logic while keeping our footprint small and `numpy` cleanly optional.

### Vendoring Candidates
Based on a review of the `langchain-community` codebase, here are specific components we can vendor:

- **Database Chat History (`SQLChatMessageHistory`)**: Located in `chat_message_histories/sql.py`. The upstream version is tightly coupled to `SQLAlchemy` to support multiple database engines. We can use its structural design as a reference but rewrite the database interface to use Python's built-in `sqlite3` module, avoiding the `SQLAlchemy` dependency.
- **SQLite Vector Store (`SQLiteVec`)**: Located in `vectorstores/sqlitevec.py`. It uses the standard library `sqlite3` and `struct` for storing embeddings as raw bytes. While it relies on the `sqlite-vec` C-extension, we can take its class structure and replace the similarity search backend with our own pure-Python streaming search logic.
- **File/Text Based Components**: Components like `FileChatMessageHistory` (`chat_message_histories/file.py`) and `TextLoader` (`document_loaders/text.py`) have zero external dependencies. They rely solely on standard Python modules like `pathlib` and `json`, and can be copy-pasted almost verbatim if needed.

#### Future Possibilities (Catalog of Ideas)
While we don't need these immediately for the core LibreOffice integration, the repository contains a massive collection of reference implementations we could vendor if users request specific features:
- **Document Loaders (170+ integrations)**: If we ever want to allow users to load data into LibreOffice from external sources, there are ready-made classes for Cloud Drives (Google Drive, OneDrive, S3), Workspaces (Confluence, Notion, Slack), and file formats (PDFs, ePub, Dataframes).
- **Agent Orchestration and Tools (via `smolagents`)**: We have actively begun vendoring and integrating `smolagents` into WriterAgent to handle complex, multi-step sub-agent tasks. This serves as a lightweight alternative to heavier `langchain` paradigms:
  - **ToolCallingAgent & Memory (`smolagents.agents`, `smolagents.memory`)**: We've vendored the core `ToolCallingAgent` and its associated memory structures structure (`ActionStep`). We bridged this to WriterAgent's existing `LlmClient` via a custom `WriterAgentSmolModel` wrapper, allowing for autonomous ReAct loops (like web searching) without polluting the main LangChain agent's context.
  - **Zero-Dependency Web Tools (`smolagents/default_tools.py`)**: We adapted their `DuckDuckGoSearchTool` and `VisitWebpageTool` to use pure `urllib.request` and standard library `html.parser`, bypassing external dependencies like `requests`, `beautifulsoup4`, or `markdownify`.
  - **Secure Local Python Execution (`smolagents.local_python_executor`)**: (Future Candidate) This zero-dependency gem uses Python's `ast` to safely evaluate Python code with strict bounds (preventing dangerous imports, limiting loops). We can vendor this to give our AI a `python_interpreter` tool for processing LibreCalc data safely without heavy sandboxes.
  - **Web Browsing (`smolagents/vision_web_browser.py`)**: Currently uses `selenium` and `helium`. For WriterAgent, we should conceptually port the interaction logic (like `_escape_xpath_string` and semantic navigation) to a PyCDP (Chrome) or Marionette (Firefox) backend for a lightweight, dependency-free browser automation implementation.
- **Retrievers (40+ strategies)**: Beyond standard vector search, it contains implementations for Lexical/Keyword search (BM25, TF-IDF, SVM) and Hybrid approaches, which we could adapt for local document search.
- **Third-Party Model Integrations**: Communication plates for nearly every LLM provider, providing a solid reference if we ever need to expand our `LlmClient` to support obscure model gateways.

---

## Architecture Decision: Custom Wrapper vs. Provider Packages
We will proceed with writing a custom LangChain wrapper (`WriterAgentLangChainModel`) around our existing `LlmClient` rather than importing heavy provider packages like `langchain-openai` or `langchain-ollama`. WriterAgent runs in LibreOffice's constrained Python environment; keeping dependencies minimal (just `langchain-core`) is critical to avoid bloat and cross-platform installation issues, while allowing us to keep our custom UI streaming loops and connection management.

For Phase 4 (embeddings / RAG), see [embeddings.md](embeddings.md): **sqlite-vec + LangGraph in the user venv only** (ingest + search pipelines); host has no langchain dependency. LibreOffice in-process still has no sqlite-vec/FAISS/NumPy encode.

---

## Appendix: HNSW and hnsw-lite

Moved to [embeddings.md — Appendix: HNSW and hnsw-lite](embeddings.md#appendix-hnsw-and-hnsw-lite) (optional in-venv ANN for bounded in-RAM subsets).

## Verification Plan
### Automated & Manual Verification
- **Phase 1**: Verify that multi-turn conversations maintain context without manually re-reading the entire chat history in the prompt.
- **Phase 2**: Close a document, reopen it, and verify the chat sidebar restores previous context.
- **Phase 3**: Conduct a very long chat session and verify that older messages are summarized and the LLM does not return context limit errors.
