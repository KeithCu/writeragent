---
name: LangChain-core memory in LocalWriter
overview: Integrate langchain-core’s conversation memory into LocalWriter’s chat sidebar, starting with in-memory per-document history and extending to persistent and summarizing memory, while continuing to use smolagents only for specialized sub-agents like web search.
todos:
  - id: wrap-llmclient-langchain-model
    content: Create LocalWriterLangChainModel in core/api.py that adapts LlmClient to LangChain BaseChatModel, preserving streaming and error handling.
    status: pending
  - id: add-inmemory-history
    content: Introduce an in-memory BaseChatMessageHistory keyed by document URL and integrate it via RunnableWithMessageHistory in chat sidebar and menu chat flows.
    status: pending
  - id: persist-history-sqlite
    content: Implement a SQLite-backed ChatMessageHistory in the config directory and switch RunnableWithMessageHistory to use it for persistent sessions.
    status: pending
  - id: add-summarizing-memory
    content: Add a summarization step or summary-buffer-style memory to compress old messages when token usage approaches chat_context_length.
    status: pending
  - id: isolate-smolagents-subagent
    content: Keep smolagents ToolCallingAgent confined to web_research and feed only its final answer into LangChain memory as a single AI message.
    status: pending
isProject: false
---

## LangChain vs smolagents memory

### LangChain-core memory model

- **Message-centric design**: LangChain represents history as a list of `BaseMessage` objects (system, human, AI, tool). Memory is usually wired via `RunnableWithMessageHistory`, which automatically:
  - Reads past messages from a `BaseChatMessageHistory` store keyed by a `session_id`.
  - Appends the new human/AI messages after each run.
- **Pluggable history backends**: `BaseChatMessageHistory` has multiple implementations (in-memory, file, SQL/SQLite, custom). This makes it easy to start with an in-process buffer and later persist to disk without changing the agent logic.
- **Buffer vs summary**: Simple setups use buffer-style memory (keep all turns). For long chats, a summarizing memory periodically compresses older messages into a short summary message to stay within context limits.

### Smolagents memory model

- **Step trace, not chat memory**: Smolagents’ `ToolCallingAgent` tracks an internal list of `ActionStep` objects (thought → tool call → observation) for a single agent run. This is great for debugging and reasoning within that run, but it:
  - Is not designed as a general-purpose chat history API.
  - Does not provide pluggable storage or persistent sessions across restarts.
- **Best use in LocalWriter**: Keep smolagents for self-contained sub-agents (like your `web_research` tool) where the agent’s internal step list is sufficient and short-lived.

### Conclusion for LocalWriter

- **Use LangChain-core for user-facing chat memory** (sidebar/menu Chat with Document): it gives you structured, pluggable, and eventually persistent history.
- **Keep smolagents as-is** for the web-search sub-agent and any future specialized tools; its memory is orthogonal and already integrated.

## Integration design for LocalWriter

### 1. Wrap `LlmClient` in a LangChain chat model

- **File**: `[core/api.py](core/api.py)`.
- **Action**:
  - Implement `LocalWriterLangChainModel` subclassing LangChain’s `BaseChatModel`, delegating send/stream to existing `LlmClient` while preserving your connection pooling and streaming loop.
  - Map LangChain `BaseMessage` ↔ your current request format (system, user, assistant, tool messages).

### 2. Introduce in-memory, per-document chat history (Phase 1)

- **Scope**: Only the main chat (sidebar + menu) for Writer/Calc/Draw.
- **Design**:
  - Use `RunnableWithMessageHistory` around a simple chain: `prompt_template | LocalWriterLangChainModel`.
  - Implement a lightweight `BaseChatMessageHistory` using an in-memory dict: key = `(document_url, chat_panel_id or "default")`, value = list of messages.
- **Prompting**:
  - Keep your existing document context behavior from `core/document.py`:
    - On each send, compute `document_context = get_document_context_for_chat(...)`.
    - Use a `ChatPromptTemplate` with:
      - System: base system prompt from `get_chat_system_prompt_for_document(...)` plus `document_context` injected as a variable.
      - Human: `{input}`.
  - Let LangChain memory provide `history` (previous turns) that is *separate* from the full document text.

### 3. Wire memory into the chat sidebar and menu

- **File**: `[plugin/modules/chatbot/panel_factory.py](plugin/modules/chatbot/panel_factory.py)` and the menu-chat path in `main.py`.
- **Action**:
  - Where you currently assemble a manual message list and call `LlmClient`, instead:
    - Build or reuse a `RunnableWithMessageHistory` instance per panel.
    - Derive a `session_id` from `doc.getURL()` (and possibly a per-panel suffix).
    - Call the runnable with `{"input": user_text, "document_context": computed_context}` and stream the output back into the panel using your existing streaming queue.
  - Ensure that only **user queries and AI replies** go into LangChain memory; the large document excerpts stay as dynamic context for each run.

### 4. Persistent history across LibreOffice restarts (Phase 2)

- **Storage choice**: Use stdlib `sqlite3` (preferred) or JSON files in the same config area as `localwriter.json`.
- **Action**:
  - Implement a custom `SQLiteChatMessageHistory` or `FileChatMessageHistory` compatible with `BaseChatMessageHistory`:
    - Schema: `(id, document_url, role, content, created_at, run_id)` or equivalent.
    - Load all messages for `document_url` (and session) into memory on first use; append on each new turn.
  - Replace the in-memory history class in your `RunnableWithMessageHistory` factory with the persistent implementation.
  - Optionally add:
    - A "Clear History" button in the chat sidebar that deletes history rows for that document URL.

### 5. Summarizing memory when context is large (Phase 3)

- **Goal**: Avoid hitting `chat_context_length` / model context limits in very long chats.
- **Action**:
  - Either use LangChain’s summary-buffer-style memory abstraction, or implement your own summarization step:
    - Track an approximate token count for stored messages (e.g., using your existing token limits in `chat_context_length`).
    - When history grows beyond a threshold, run a short summarizer chain (using `LocalWriterLangChainModel`) over the oldest portion of the history.
    - Replace those old messages with a single `SystemMessage` or `AIMessage` containing a brief summary, and keep recent turns verbatim.
  - Expose `memory_strategy` and `max_memory_tokens` in Settings (as outlined in `docs/dev/langchain-plan.md`).

### 6. Keep smolagents usage isolated

- **File**: `plugin/contrib/smolagents/default_tools.py` and `core/document_tools.py` where `web_research` is wired.
- **Action**:
  - Continue running `ToolCallingAgent` with its own internal `ActionStep` list as a **sub-agent tool** that returns a final answer string.
  - Treat that answer as a single AI message in LangChain memory ("AI (web): ...") so the main chat remembers that a web search happened, without importing smolagents’ step trace into main memory.

### 7. Configuration and UX

- **Settings**:
  - Add optional keys to `localwriter.json` (and Settings dialog) for:
    - `memory_enabled` (bool), default true.
    - `memory_persistence` ("in_memory" | "sqlite").
    - `max_memory_tokens` and `memory_strategy` ("buffer" | "summary").
- **UX behavior**:
  - When memory is disabled, run the LangChain chain with an empty history each turn (stateless mode) while still injecting document context.
  - When enabled, ensure that reopening a document restores its chat history in the sidebar if persistence is configured.

