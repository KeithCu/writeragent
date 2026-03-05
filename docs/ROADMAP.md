# LocalWriter Roadmap & General Improvements

This document consolidates general planned improvements, feature ideas, and technical debt reduction strategies for LocalWriter. It serves as a central roadmap, combining insights from various past analyses.

For specific, detailed feature plans, please see the **Referenced Feature Plans** section at the bottom of this document.

---

## 1. High-Priority UX & Configuration

These are high-value, relatively contained changes that directly improve the user experience.

### 1.1 Config & Endpoint Presets
- **Config Presets**: Add a "Load from file" or preset dropdown in Settings so users can easily switch between different configuration files (e.g., `localwriter.json`, `localwriter.openrouter.json`, etc.) without manually moving files around.
- **Endpoint Presets**: Add optional preset buttons or a dropdown in Settings that set the endpoint URL and API type (and optionally model) in one click for common providers (Local, OpenRouter, Together, etc.).

### 1.2 Unified Settings Dialog
- **Goal**: Make "LocalWriter -> Settings" the single, full-featured configuration UI (with tabs) and remove/minimize the legacy "Tools -> Options -> LocalWriter" auto-generated page to avoid confusion.
- **Implementation**: Define all AI-related UI configurations directly in `SettingsDialog.xdl`. For non-AI modules (like Core, Writer, Calc), add `settings` annotations in their `module.yaml` files so the Settings dialog can dynamically render them into an "Other Settings" tab. This creates one single source of truth for the user.

### 1.3 UI/UX Refinements
- **EditInputDialog Multiline**: Make the Edit Selection instruction field multiline so that longer prompts are easier to enter and view.
- **Advanced Settings Toggle**: Hide advanced settings (like max tokens, context length, reasoning effort) behind an "Advanced" toggle to declutter the main settings view.
- **Status Indicators**: Improve status reporting in the UI. Make the status bar more dynamic (e.g., auto-clearing after timeouts, using colors to indicate success/warning/error states).
- **Typing Indicators**: Show a visual typing indicator or animation in the chat panel when the AI is processing or responding.

---

## 2. Advanced Document & Tool Capabilities

### 2.1 Format-Preserving Replacement Improvements
- **Proportional Format Mapping**: For large length differences in replacements, distribute the original formatting pattern proportionally across the new text instead of strict 1:1 character mapping.
- **Paragraph-Style Preservation**: Handle replacements that span paragraph breaks (multiple paragraphs or paragraph-level styles).
- **Edit Selection Streaming**: Apply the same format-preserving logic to the Edit Selection streaming path so that live edits retain character-level formatting (not just tool-calling paths).

### 2.2 Richer Context (Metadata)
- Enhance `get_document_context_for_chat` and its Calc equivalent to include optional document metadata. This provides better summaries for the model.
  - **Writer**: Word/paragraph count, fonts used, table/image counts, style stats.
  - **Calc**: Formula counts, chart counts, error counts, column types.
- Add an optional configuration setting (e.g., `context_include_metadata`) to toggle this for speed.

### 2.3 Safer Workflows (Propose-First / Confirm)
- **Safe Edit Mode**: Add an optional mode where tool-calling shows a preview (e.g., a diff or short description) and waits for user confirmation ("Accept"/"Reject") before applying changes to the document. This builds trust and avoids accidental overwrites.
- **Undo Grouping**: Wrap changes in a UNO undo context (`model.enterUndoContext("AI Edit")`) so all changes from a single tool call or chat turn can be undone at once.

### 2.4 Generative AI Feature Enhancements
- **Predictive Suggestions ("Ghost Text")**: Implement a lightweight suggestion model (e.g., n-gram/trigram) trained on the user's recent document context to provide autocomplete suggestions.
- **Smart Auto-Correct**: AI-powered spell check and grammar correction tool that learns from document context and user preferences over time.
- **Style Consistency Checker**: Identify and fix style inconsistencies throughout the document (e.g., inconsistent heading formats, spacing, font usage).
- **Context-Aware Auto-Save Summaries**: Automatically generate brief summaries of document changes at save points, stored as metadata.
- **Template Assistant**: Analyze document content and structure to suggest or create templates.
- **Collaborative Editing Assistant**: Track and suggest resolutions for conflicts when multiple users edit the same document.
- **Multi-Document Analysis**: Analyze relationships and content across multiple open documents.

---

## 3. Architecture & Code Quality Improvements

### 3.1 Consolidate and Simplify Logging
- Merge different log sinks (`log_to_file`, `debug_log`) into a single unified debug logger writing to one file.
- Implement Log Rotation (e.g., rotate when file > 5MB) to prevent disk filling.
- Defer log path resolution until the first `debug_log` call to prevent early initialization failures.
- Remove redundant log paths and the fragile watchdog thread (replace with simpler timeout logging in the main loop).
- Centralize logging code into a single module with context-aware prefixes (e.g., `[API]`, `[Chat]`).

### 3.2 Refactor Large Monolithic Files
- Break down `main.py` (~500+ lines): Move settings/input dialogs to `core/dialogs.py` and edit actions to `core/edit_actions.py`.
- Break down `plugin/modules/chatbot/panel.py` and `panel_factory.py`: Extract `ChatSession`, listeners, and streaming/draining logic into separate files (e.g., `core/chat_session.py`, `core/streaming.py`).
- Abstract XDL dialog wiring into a reusable `DialogWiring` class in `core/xdl_utils.py` to reduce duplicate UI boilerplate.

### 3.3 Reduce UI Dispatch Frequency
- Optimize `toolkit.processEventsToIdle()` calls. Call it only after processing a full batch of queue items or on a drain-loop timeout, rather than per-chunk or aggressively when idle, to prevent hangs and UI stuttering.

### 3.4 Centralize UNO Utilities
- Create a `core/uno_utils.py` to centralize common UNO patterns, reducing code duplication. Include helpers for getting current documents/frames, creating cursors, handling limits, and creating common structs like `PropertyValue`.

### 3.5 Framework & Core Logic Consolidation
- **Config Management**: Centralize config I/O into `core/config_io.py`. Implement schema validation on write via `jsonschema`. Cleanly deprecate legacy keys (like migrating `chat_system_prompt` to `additional_instructions`).
- **Tool Registry**: Implement a unified `ToolRegistry` in `plugin/framework/tool_registry.py` instead of maintaining separate lists (`WRITER_TOOLS`, `CALC_TOOLS`, `DRAW_TOOLS`). Use an auto-discovery mechanism based on a centralized `DocumentType` enum.
- **Image Generation Simplification**: Merge the AI Horde and Endpoint image provider logic into a single `ImageService` to centralize timeouts, retries, and error handling.
- **Streaming Unification**: Standardize queue items across all streaming paths (Chat, Editor, Calc `=PROMPT()`) using a `QueueItem` named tuple, and process them in a single `run_stream_drain_loop`.
- **LiteLLM Provider Auto-Detection**: Instead of bundling the heavy 50MB LiteLLM library, create a minimal `core/providers.py` that auto-detects provider endpoints and auth headers (e.g., `x-api-key` for Anthropic, `?key=` for Gemini) based on the model name prefix in the settings.

### 3.6 Robust Error Handling & Recovery
- Implement a comprehensive error handling system with specific exception classes (`LocalWriterError`, `APIError`, `DocumentError`).
- Provide better user-facing error messages with actionable suggestions (e.g., "Network timeout: Check endpoint"). Never write error stack traces directly into the user's document selection.
- Add graceful degradation or fallback mechanisms when specific tools or operations fail.

### 3.7 Testing & QA
- **Evaluation Framework Expansion**: Expand the `EvalRunner` suite (introduced in `Eval_Framework` branch) to include the full 50+ test cases outlined in `evaluation-plan.md`. Implement multimodal vision tests.
- Expand standard unit test coverage, specifically mock tests for API requests, streaming logic, malformed tool JSON, and multi-doc scoping (preventing regressions in document targeting).
- Implement automated testing workflows and performance benchmarks (document processing speed, memory usage).

---

## 4. Referenced Feature Plans

The following specific features and integrations have dedicated planning documents. Please refer to them for detailed implementation strategies:

*   **[Web Search Sub-Agent](agent-search.md)**: Architecture for autonomous web research using vendored smolagents.
*   **[Calc Integration](calc-integration.md)**: Details on the existing deep LibreCalc integration and planned expansions.
*   **[Evaluation Plan](evaluation-plan.md)**: The comprehensive list of 50+ test cases for Writer, Calc, Draw, and Multimodal testing.
*   **[Evaluation Dev Plan](eval-dev-plan.md)**: The architecture and roadmap for the integrated evaluation dashboard and runner.
*   **[Prompt Optimization (DSPy)](dspy-prompt-optimization-plan.md)**: Strategy for using DSPy to systematically optimize system prompts.
*   **[Impress Tools](impress-tools.md)**: Roadmap for extending Draw tools to support presentation generation and management in LibreOffice Impress.
*   **[LangChain Integration](langchain-plan.md)**: Phased plan for integrating `langchain-core` for memory, history persistence, and RAG.
*   **[Localization (i18n)](localization.md)**: High-level plan for adding multi-language support to UI and AI prompts.
*   **[AI Horde Improvements](next-steps-horde.md)**: Next steps for improving the image generation UX with AI Horde.
*   **[Section Replacement Options](section-replace-options.md)**: Technical options for fixing coordinates when using `get_markdown(scope="range")`.
*   **[Tool Simplification](tool_simplify.md)**: Proposal to consolidate fine-grained tools into grouped operations to reduce LLM overhead.
