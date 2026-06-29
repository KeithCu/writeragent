# WriterAgent Roadmap 🗺️

**High Priority / Immediate Action**:
- **Consolidate Test Infrastructure**: Create a `TestingFactory` in `plugin/tests/testing_utils.py` to provide a unified way to setup/teardown document instances and ToolContexts for both native (LO) and mock (pytest) environments. This will significantly reduce test boilerplate and enforce engineering standards for all new features.

---


---
**Bugreport**: One last thing I noticed, though possibly it already might have been fixed by now. Sometimes the sidebar chat acts a bit counterintuitively in copies of documents.
A few times I had the impression that two copies ended up sharing the same chat history. When I had interacted with a model in one of the two files, after I had made the copies, the chat history also turned up in the second document; when I cleared the chat history in the second document it was also gone in the first. However, it appeared that this had 'unlinked' both documents' chat histories.

---

**Last Updated**: 2026-05-06
**Status**: Active Development

This document outlines the planned features, improvements, and technical debt to address in WriterAgent. Items are organized by priority and domain.

---

## Large modules (navigation)

> - **Very large modules:** optional **section markers** (`# --- … ---`) so major entrypoints (e.g. UNO interfaces) stay discoverable.

---

## File license headers (SPDX, attribution, upstream tracking)

**Status**: Planned housekeeping — an optional sweep to align **original / non-derived** source files on a **short, consistent** header. **Derived or adapted** files keep **all upstream copyright lines** and add explicit **upstream metadata** (below).

**Why this section exists**: WriterAgent is **GNU GPL version 3 or later**. Per-file comments should make that obvious **without** pasting the entire GPL into every file. Separately, **attribution** should satisfy upstream licenses **and** give **you** a stable pointer for **re-syncing** when upstream ships fixes or features.

### SPDX — what it is (short tutorial)

**SPDX** is **Software Package Data Exchange** — a standard for labeling **which license applies** to a file or package using short, **machine-readable** identifiers. Registries and tools (compliance scanners, REUSE, distros) recognize these tags; humans can grep them. Official overview: [spdx.dev](https://spdx.dev/).

In source files, the usual pattern is one line:

```text
# SPDX-License-Identifier: GPL-3.0-or-later
```

- **`SPDX-License-Identifier`** — tells automated tools “the license for this file is…”
- **`GPL-3.0-or-later`** — **GNU GPL version 3**, and the recipient may follow **GPLv3 or any later published GPL version** (“GPLv3+”). That matches a typical `LICENSE` file that contains GPLv3 and the project’s “or any later version” intent.

**Important**: That one line does **not** replace shipping the **full license text** to people who receive the software. The **canonical GPLv3 text** lives in the repository root as **`LICENSE`**. The SPDX line answers “which license applies to this file?” in one line; **`LICENSE`** is the actual legal text recipients should read.

If you had never seen SPDX before: think of it as **the license name in a standard spelling** so tools and reviewers do not have to guess from informal wording.

### Decisions (keep sweeps consistent)

| Topic | Decision | Why |
|-------|-----------|-----|
| Copyleft goal | **GPLv3+** | Compared to permissive licenses (e.g. MIT), GPLv3+ **requires** that people who **convey** modified versions generally **share source on the same terms** — aligned with “give back,” not “extract and close.” |
| Full license text | **Single `LICENSE` at repo root** | One canonical GPLv3 document; avoids huge duplicated banners in every file. |
| Original files | **`# Copyright …` + `# SPDX-License-Identifier: GPL-3.0-or-later`** | States license clearly; **same intent** as the long “This program is free software…” GPL banner, in compact form. Long banners are optional legacy style, not “stronger GPL.” |
| Derived / adapted files | **Preserve upstream copyright lines** + optional **`Copyright … KeithCu (adaptations)`** + **Upstream** block | Legal notices from upstream must stay if their license requires them; erasing them is not “cleanup.” |
| **`GPL-3.0-only` vs `GPL-3.0-or-later`** | Prefer **`or-later`** project-wide unless you **intentionally** forbid future GPL versions | Wrong identifier = wrong meaning; do not mix casually across files. |

### Original vs derived — two shapes

**Original (non-derived) work you wrote**

- Project line (existing convention) + **your** copyright + SPDX.
- No upstream block unless you want an internal note.

**Derived, forked, or adapted upstream**

- Keep **all** copyright lines required by the upstream license.
- Add **your** copyright for substantive changes when appropriate.
- Add an **Upstream** comment block so **you** can open the **same revision** later and diff or merge new upstream work.

### Attribution block with link (recommended for adapted code)

**Primary reason for the URL**: **your workflow** — when upstream adds a feature or bugfix, you follow **Source** / **Pinned** and compare or cherry-pick into WriterAgent. **Secondary**: clear provenance for readers and license compliance.

**Rules**

1. Keep **`SPDX-License-Identifier`** as **exactly one line** — **do not** put URLs or prose inside it.
2. Put **Upstream**, **Source**, **Pinned**, **Notes** in normal `#` comments **above** the SPDX line (order: copyrights → upstream block → SPDX).
3. **Pin** a **tag**, **release**, or **commit hash** (and ideally a **date**). Avoid “only `main` branch” as the sole pointer — **`main` moves** and you lose a reproducible “what we imported.”

**Template (Python `#` comments)**

```text
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 20XX Original Author
# Copyright (c) 20YY KeithCu (adaptations for WriterAgent)
#
# Upstream: <short project or component name>
#   Source: https://example.org/repo-or-release-page
#   Pinned: v1.2.3 or commit abcdef1234… (YYYY-MM-DD)
#   Notes: optional — how this file diverges (API, UNO, packaging)
#
# SPDX-License-Identifier: GPL-3.0-or-later
```

**Optional pattern when one upstream touches many files**: a **one-line pointer** in each file (`Upstream: see docs/<topic>-upstream.md`) plus **one** maintained table in `docs/` — fewer duplicated URLs; **per-file Source + Pinned** is still the fastest when you already have the file open.

### Tutorial: applying this when you edit a file

1. **Copyright**: Ensure **your** line is accurate for **your** contributions; **keep** upstream copyright lines on derived files.
2. **SPDX**: If missing, add `# SPDX-License-Identifier: GPL-3.0-or-later` after the copyright block (use `#` for Python; adjust for other languages’ comment syntax).
3. **Adapted code**: Add or refresh **Upstream** / **Source** / **Pinned** when you **import or re-sync** from upstream (update **Pinned** when you deliberately merge new upstream revisions).
4. **Consistency**: Match **`LICENSE`** — if the project ever used **`GPL-3.0-only`**, the SPDX string would change **deliberately** project-wide; do not mix identifiers without a documented reason.

### Roadmap tasks (optional sweep)

- [ ] Audit **`plugin/`** for **purely original** files → normalize to **short header** (copyright + SPDX).
- [ ] Audit **derived** files → replace long GPL boilerplate with SPDX **only where** upstream notices remain complete; add **Upstream** blocks where missing.
- [ ] Treat **`contrib/`**, vendored bundles, and third-party subtrees per **their** documented notices unless there is an explicit project policy to consolidate.

---

## Config / chat model (`text_model` vs `model_lru`)

Unified via `get_text_model()` / `set_text_model()` in [`plugin/framework/client/model_fetcher.py`](../plugin/framework/client/model_fetcher.py) (mirrors `get_image_model` / `set_image_model`). `text_model` is the canonical stored key (not LRU-first). Legacy top-level `model` in `writeragent.json` is ignored.

- [x] Centralize reads/writes; writers use `set_text_model(..., update_lru=...)`.
- [x] MCP / Tools → Options `ai.text_model` writes go through `set_text_model` in [`config_service.py`](../plugin/framework/config_service.py).
- [x] Belt-and-suspenders: [`SettingsDialog._apply_dropdowns`](../plugin/chatbot/dialog_views.py) seeds text model from combobox `getText()` before falling back to `get_text_model()`.

---

## 🧹 Framework Consolidation (Technical Debt)

**Goal**: Reduce technical debt and improve code discoverability by merging small, highly related framework modules.

- [x] **Service Infrastructure**: Merge `service_base.py` and `service_registry.py` into **`service.py`**.
- [x] **Module Infrastructure**: Merge `module_base.py` and `module_loader.py` into **`module_base.py`**.
- [x] **Tool Infrastructure**: Merge `tool_base.py`, `tool_registry.py`, and `tool_context.py` into **`tool.py`**.
- [x] **Image Handling**: Merge `plugin/writer/image_tools.py` and `plugin/writer/image_utils.py`.
- [x] **Specialized Agent Helpers**: Merge `specialized_shapes_context.py` into `plugin/doc/specialized_base.py`.
- [x] **State & Types**: Merge `state.py` and `types.py` into appropriate framework files (`constants.py`, `service.py`, `errors.py`).

---

## 🚀 High Priority Features

### 1. **Shape API Enhancements** 🎨 ✅ **COMPLETED**
**Files**: `plugin/draw/shapes.py`, `plugin/writer/shapes.py`
**Status**: Fully implemented and tested

- ✅ Enhanced `CreateShape` with rich formatting properties
  - ✅ Line properties: color, width, style (solid/dash/dot)
  - ✅ Fill properties: color, style (solid/transparent/gradient)
  - ✅ Text properties: font, size, color
  - ✅ Transformations: rotation angle
- ✅ Support generic UNO shape types (accept any shape type string)
- ✅ Implement `ConnectShapes` using `com.sun.star.drawing.ConnectorShape`
- ✅ Implement `GroupShapes` using `com.sun.star.drawing.GroupShape`
- ✅ Update Writer shapes to inherit new Draw capabilities
- ✅ Test all shape operations across Writer/Draw/Impress

**Commit**: 1200257 "Enhance shape tools in Draw and Writer modules"
**Testing**: Comprehensive UNO shape operation tests added

**Dependencies**: None
**Blockers**: None
**Testing**: Need comprehensive UNO shape operation tests

### 2. **Fields Domain Completion** 📝 ✅ **COMPLETED**
**Files**: `plugin/writer/fields.py`
**Status**: Fully implemented and tested

- ✅ Complete `fields_insert` with full field type support
  - ✅ PageNumber, PageCount, DateTime, Author, FileName
  - ✅ WordCount, CharacterCount, ParagraphCount
  - ✅ Custom fields and properties
- ✅ Implement field master/dependent system
- ✅ Add field refresh patterns and error handling
- ✅ Create field listing with detailed properties
- ✅ Add field deletion with proper cleanup

**Commit**: 2ab8da4 "Add specialized text field tools in Writer module"
**Testing**: Field operation tests added

**Dependencies**: UNO field service documentation
**Blockers**: Complex field type variations
**Testing**: Need test documents with various field types

### 3. **Indexes/TOC Domain** 📚 ✅ **COMPLETED**
**Files**: `plugin/writer/indexes.py`
**Status**: Fully implemented and tested

- ✅ Implement `indexes_create` with full UNO wiring
  - ✅ Support TOC, bibliographies, custom indexes
  - ✅ Handle index types and styles
- ✅ Implement `indexes_add_mark` for manual entries
  - ✅ Support different mark types and levels
  - ✅ Handle mark positioning
- ✅ Enhance `indexes_update_all` with detailed reporting
- ✅ Add index listing and inspection tools
- ✅ Add `indexes_list` for comprehensive index management

**Commit**: 5dab767 "Add index management tools in Writer module"
**Testing**: Index operation tests added

**Dependencies**: UNO index service documentation
**Blockers**: Complex index creation workflows
**Testing**: Need test documents with index structures

---


# ROADMAP.md — WriterAgent Development

This document tracks the long-term vision, medium-term priorities, and immediate research goals for WriterAgent.

---

## 🚀 High-Level Vision
Our primary focus is **LibreOffice Fidelity**—systematically closing the gap between the AI's capabilities and the full breadth of the UNO API to ensure the agent can manipulate every professional feature the suite offers.

---

## 🛠️ Medium Priority Roadmap

| Feature | Description |
| :--- | :--- |
| **LLM Response Parsing Refactor** | Extract provider-specific parsing logic into `plugin/framework/client/parsers.py` to improve resilience against API changes. |
| **Batch Section Rewriting** | Implement heading-based document segmentation for whole-document processing. |
| **Advanced Impress Layouts** | Adopt native shape positioning/sizing for Draw/Impress image generation. |

---

## 💡 Refactoring Plan: LLM Parsing for Resilience

To improve modularity and stability, we are refactoring provider-specific response parsing into a dedicated module. 

### Why this is needed
LLM APIs frequently update their JSON structures. Currently, these parsing quirks are interleaved with request logic in `llm_client.py`, making them hard to test and maintain. We are porting patterns from **LibreAI's** modular C++ clients to create a robust Python equivalent.

### Common Quirks to Address
When refactoring, ensure these provider-specific behaviors are isolated:
1. **Gemini Role Mapping:** Gemini uses `model` for assistant roles, while others use `assistant`. 
   ```python
   # Example pattern to port
   role = "model" if msg.role == "assistant" else "user"
   ```
2. **Anthropic Nested Content:** Anthropic responses wrap text in an array within a `content` object, requiring deeper traversal.
   ```python
   # Port from AnthropicClient::parseResponse
   return data["content"][0]["text"]
   ```
3. **Ollama JSON Non-Standard:** Ollama occasionally returns trailing newline characters or non-standard JSON formatting that requires stricter parsing than OpenAI-compatible providers.
4. **Hardcoded Fallbacks:** If the live model-list endpoint fails, use a predefined list (e.g., `["claude-opus-4-7", "claude-sonnet-4-6"]`) to keep the UI functional.

---


## 📋 Medium Priority Features

### 4. **Enhanced Style Management** 🎭 ✅ **COMPLETED**
**Files**: `plugin/writer/styles.py`
**Status**: Fully implemented and tested

- ✅ Implement `styles_create_or_update` (via `CreateStyle` and `UpdateStyle`)
- ✅ Add style inheritance system
- ✅ Support conditional styles
- ✅ Add style import/export (via `ImportStyles`)
- [ ] Add style preview functionality

**Dependencies**: UNO style family documentation
**Blockers**: Style inheritance complexity
**Testing**: Need style-heavy test documents

---

## 🛠️ Technical Improvements

### 5. **Test Infrastructure Consolidation** 🧪
**Files**: `plugin/tests/testing_utils.py`
**Status**: Identified opportunity

- [ ] Create reusable mock factory functions
  - `create_mock_ctx()` - standardized context mock
  - `create_mock_document()` - with service support
  - `create_mock_cursor()` - with positioning
  - `create_mock_page()` - for Draw/Impress tests
- [ ] Consolidate duplicate UNO mocks across test files
- [ ] Add common test patterns and assertions
- [ ] Document testing best practices

**Impact**: Reduces test code duplication by ~40%
**Dependencies**: None
**Blockers**: None

### 6. **Error Handling Standardization** ⚠️
**Files**: `plugin/framework/errors.py`, `plugin/framework/logging.py`
**Status**: In Progress

- [ ] Audit all error codes for consistency
- [✅] **Standardize on `log.exception("Context")`** inside `except` blocks to ensure stacktraces are captured for debugging.
- [✅] Enhance `SafeLogger` in `logging.py` to support `exception()` method.
- [✅] Port key modules (`tool_loop.py`, `document_helpers.py`, `service.py`, `format.py`) to the new logging pattern.
- [✅] Standardize error message formats
- [ ] Add missing error codes for new features
- [ ] Improve error context reporting
- [ ] Add error recovery patterns

### 8. **Chatbot Import Architecture & Exception Standardization** 🧹 ✅ **COMPLETED**
**Files**: `plugin/chatbot/*.py` (specifically `send_handlers.py`, `selection.py`, `tool_loop.py`)
**Status**: Core refactoring completed and verified.

- ✅ **Top-Level Consolidation**: Promoted 100+ local imports to the module level. This significantly improves static analysis accuracy and makes dependencies explicit.
- ✅ **Circular Dependency Management**: Identified `get_tools` as a critical local import that must remain scoped to methods to prevent import cycles with `plugin.main`.
- ✅ **UNO Exception Normalization**: Implemented the `UNO_DISPOSED_EXCEPTIONS` pattern. By wrapping UNO imports in a `try-except ImportError` block and casting to a standard tuple, the codebase remains testable in non-PyUNO environments (like standard pytest).
- ✅ **"AI Slop" Purge**: Systematically removed repetitive, scattered import blocks and excessive whitespace introduced by iterative AI edits.

#### Architectural Advice (Lessons Learned):
> [!IMPORTANT]
> **Maintaining the "Clean Room" Import Style:**
> 1. **Prefer Top-Level**: Always place stdlib and lightweight framework imports (`json`, `threading`, `logging`, `traceback`) at the top.
> 2. **Protect PyUNO**: Use the `try: from com.sun.star... except ImportError: ...` pattern for UNO types. This prevents test collection failures on developer machines without a live `soffice` bridge.
> 3. **Standardize Cleanup**: Use `UNO_DISPOSED_EXCEPTIONS` in `except` blocks for all LibreOffice component interactions. It ensures consistent logging of "likely disposed" vs. "unexpected" errors.
> 4. **Indent with Care**: When refactoring worker threads (like `run` or `run_final`), ensure the closure structure is preserved. Circular dependency fixes often require careful local scope management.

**Impact**: 100% test pass rate (888/888), improved `ty`/`mypy` diagnostics, and cleaner developer experience.

#### Advice for standardizing logging (The "Essence"):
> [!TIP]
> **When to use `log.exception()` (Stacktraces):**
> 1. **UNO/LibreOffice Boundaries**: Use it where code interacts with the document (cursors, enumerations, table/sheet access). These often fail with opaque UNO errors (e.g., `getCount` failed), and a stacktrace is the only way to identify which specific internal object or state was problematic.
> 2. **Top-Level User Actions**: For complex orchestrations (like `Extend Selection`, `Web Research`, or `Transcription`), log the exception at the worker thread boundary or entry point. This captures the entire "why" of a user-facing failure.
> 3. **Mutations**: Any code that changes document state (like `ensure_heading_bookmarks`) should capture traces on failure to help debug corruption or lock-out issues.
>
> **When to stay with `log.error()` (No Stacktraces):**
> 1. **Routine Network Errors**: Timeouts, 503s, or "Model not found" errors are standard. A stacktrace usually just shows library internals (e.g., `requests` or `urllib3`) which adds noise without diagnostic value.
> 2. **JSON Parsing**: If `safe_json_loads` fails, the raw payload is almost always the only context needed.
> 3. **Config/LRU Issues**: Missing keys or minor persistence hiccups are self-explanatory.

**Impact**: Better debugging and user experience
**Dependencies**: None
**Blockers**: None

### 7. **Performance Optimization** ⚡
**Files**: Various
**Status**: Ongoing

- [ ] Profile tool execution times
- [ ] Optimize UNO service calls
- [ ] Add caching for frequent operations
- [ ] Review memory usage patterns
- [ ] Optimize document context generation

**Impact**: Faster response times, better UX
**Dependencies**: Profiling tools
**Blockers**: None

---

## 🐛 Known Issues & Technical Debt

### 11. **Tool Registry Improvements** 🔧
**Files**: `plugin/framework/tool_registry.py`
**Status**: Technical debt

- [ ] Review tool discovery performance
- [ ] Add tool dependency management
- [ ] Improve error reporting
- [ ] Add tool versioning support

**Impact**: Better tool management
**Priority**: Medium

### 12. **Memory System Enhancements** 🧠
**Files**: `plugin/chatbot/memory.py`
**Status**: Functional but limited

- [ ] Add memory search capabilities
- [ ] Implement memory expiration
- [ ] Add memory compression
- [ ] Improve memory conflict resolution

**Impact**: More robust personalization
**Priority**: Medium

### 13. **Configuration System Review** ⚙️
**Files**: `plugin/framework/config.py`
**Status**: Needs modernization

- [ ] Review configuration structure
- [ ] Add schema validation
- [ ] Improve change detection
- [ ] Add configuration profiles

**Impact**: More maintainable config
**Priority**: Low

---

## 🌐 Integration & Ecosystem

### 14. **MCP Protocol Enhancements** 📡
**Files**: `plugin/mcp/mcp_protocol.py`
**Status**: Functional but expandable

- [ ] Add specialized tool opt-in for MCP
- [ ] Implement domain switching via MCP
- [ ] Add better error reporting
- [ ] Improve document targeting

**Impact**: More powerful remote control
**Priority**: Medium

### 15. **External Tool Integration** 🔌
**Files**: Various
**Status**: Future

- [ ] Design plugin architecture
- [ ] Create extension API
- [ ] Add tool discovery mechanism
- [ ] Implement security sandbox

**Impact**: Extensible ecosystem
**Priority**: Low

---

## 🎯 Future Research & Exploration

### 16. **Agent Personality System** 🤖
**Status**: Conceptual

- [ ] Research personality models
- [ ] Design personality selection
- [ ] Implement personality traits
- [ ] Test user preferences

**Potential Impact**: More engaging user experience

### 17. **Voice Interface** 🎤
**Status**: Future

- [ ] Research speech recognition
- [ ] Design voice command system
- [ ] Implement voice feedback
- [ ] Test accessibility

**Potential Impact**: Hands-free operation

### 18. **Collaborative Features** 👥
**Status**: Future

- [ ] Research real-time collaboration
- [ ] Design change tracking
- [ ] Implement multi-user sessions
- [ ] Add conflict resolution

**Potential Impact**: Team document editing

---

## 📊 Metrics & Analytics

### 19. **Usage Tracking** 📈
**Status**: Not started

- [ ] Design privacy-compliant tracking
- [ ] Implement feature usage logging
- [ ] Add performance metrics
- [ ] Create analytics dashboard

**Priority**: Low (privacy considerations)

### 20. **User Feedback System** 💬
**Status**: Future

- [ ] Design feedback collection
- [ ] Implement rating system
- [ ] Add bug reporting
- [ ] Create feedback analysis

**Priority**: Low

---

## 🎓 Learning & Growth

### 21. **UNO API Documentation** 📚
**Status**: Ongoing

- [ ] Document key UNO services
- [ ] Create service capability matrix
- [ ] Add usage examples
- [ ] Note limitations and quirks

**Impact**: Faster development

### 22. **Code Quality Initiatives** ✨
**Status**: Ongoing

- [ ] Add more type hints
- [ ] Improve docstrings
- [ ] Add code examples
- [ ] Review naming conventions

**Impact**: More maintainable codebase

---

## 🗂️ Backlog (Nice to Have)

### 23. **Theme System** 🎨
- [ ] Implement UI theming
- [ ] Add color scheme support
- [ ] Create theme editor

### 24. **Template System** 📑
- [ ] Design document templates
- [ ] Implement template storage
- [ ] Add template sharing

### 25. **Advanced Search** 🔍
- [ ] Implement full-text search
- [ ] Add regex support
- [ ] Create search history

### 26. **Batch Operations** ⚡
- [ ] Add batch processing
- [ ] Implement queue system
- [ ] Add progress tracking

### 27. **Offline Mode** ✈️
- [ ] Design offline capabilities
- [ ] Implement local caching
- [ ] Add sync mechanism

---

## 📅 Timeline Estimates

### Next 2 Weeks (Sprint 1) ✅ **COMPLETED**
- ✅ Complete Shape API enhancements (with rich formatting, connectors, groups)
- ✅ Finish Fields domain (full field type support, master/dependent system)
- ✅ Complete Indexes domain (TOC creation, marks, comprehensive management)
- [ ] Begin test infrastructure consolidation
- [ ] Review and organize documentation files
- [ ] Add integration tests for new features

### Next 4 Weeks (Sprint 2)
- Complete Fields and Indexes domains
- Continue test improvements

### Next 8 Weeks (Sprint 3)
- Complete remaining specialized domains
- Enhance documentation
- Address technical debt
- Begin future research

---

## 🤝 Contribution Opportunities

### Good First Issues
- Test infrastructure consolidation
- Documentation improvements
- Error message enhancements
- Code quality initiatives

### Mentored Projects
- Shape API enhancements
- Fields domain completion

### Research Projects
- Agent personality system
- Voice interface
- Collaborative features

---

## 📝 Changelog

**2024-03-25**: Initial roadmap created
- Added high priority features (Shapes, Fields, Indexes)
- Organized medium priority features
- Identified technical improvements
- Added documentation tasks
- Listed known issues and technical debt

**2024-03-24**: Previous work
- Completed tool switching architecture
- Implemented specialized domains
- Created comprehensive documentation

---

## 🎯 Vision

WriterAgent aims to be the most powerful, flexible, and user-friendly document automation platform for LibreOffice. By systematically addressing this roadmap, we'll create a tool that:

- **Empowers users** with intuitive interfaces
- **Automates complex tasks** through intelligent tools
- **Adapts to workflows** with personalized experiences
- **Scales with needs** from simple edits to complex document systems
- **Delights users** with thoughtful design and helpful guidance

## 📊 Current Status

**Recently Completed** 🎉:
- ✅ Chatbot import architecture & exception standardization (purged "AI slop", consolidated 100+ imports)
- ✅ Shape API enhancements (rich formatting, connectors, groups)
- ✅ Fields domain (full field type support, master/dependent system)
- ✅ Indexes domain (TOC creation, marks, comprehensive management)
- ✅ Librarian agentic onboarding ([`plugin/chatbot/librarian.py`](../plugin/chatbot/librarian.py))
- ✅ Track Changes domain ([`plugin/writer/tracking.py`](../plugin/writer/tracking.py))
- ✅ Tool switching architecture
- ✅ Specialized domain system
- ✅ Calc tool integration
- ✅ Tunnel module removal
- ✅ Memory management simplification

**Active Development**:
- Test infrastructure consolidation
- Documentation enhancement

**Up Next**:
- Test infrastructure consolidation
- Documentation enhancement

Every item on this roadmap brings us closer to that vision. 🚀