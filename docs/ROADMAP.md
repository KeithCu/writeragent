# WriterAgent Roadmap 🗺️

**High Priority / Immediate Action**:
- **Consolidate Test Infrastructure**: Create a `TestingFactory` in `plugin/tests/testing_utils.py` to provide a unified way to setup/teardown document instances and ToolContexts for both native (LO) and mock (pytest) environments. This will significantly reduce test boilerplate and enforce engineering standards for all new features.

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

The sidebar now updates `model_lru@<endpoint>` when the user picks a model (same as Settings apply). Larger cleanup is deferred:

- [ ] Optionally derive active chat model from `model_lru@<endpoint>[0]` with `get_active_text_model` / `set_active_text_model`, legacy fallbacks, and one-shot migration from `text_model` / `model`.
- [ ] Migrate readers (`get_text_model`, `get_api_config`) and writers (`set_config(..., "text_model")`) off the duplicate global key; special-case `AI_SIMPLE_FIELDS` / MCP if needed.
- [ ] Belt-and-suspenders: in [`plugin/framework/legacy_ui.py`](plugin/framework/legacy_ui.py) `_apply_dropdowns`, pass `text_ctrl.getText()` instead of `""` when repopulating text/image/STT combos after endpoint refresh.

---

## 🧹 Framework Consolidation (Technical Debt)

**Goal**: Reduce technical debt and improve code discoverability by merging small, highly related framework modules.

- [ ] **Service Infrastructure**: Merge `service_base.py` and `service_registry.py` into **`services.py`**.
- [ ] **Module Infrastructure**: Merge `module_base.py` and `module_loader.py` into **`modules.py`**.
- [ ] **Tool Infrastructure**: Merge `tool_base.py`, `tool_registry.py`, and `tool_context.py` into **`tools.py`**.
- [ ] **Image Handling**: Merge `image_tools.py` and `image_utils.py` into **`images.py`**.
- [ ] **Error Hierarchy**: Merge `base_errors.py` into **`errors.py`**.
- [ ] **Specialized Agent Helpers**: Merge `specialized_shapes_context.py` into **`specialized_base.py`**.
- [ ] **State & Types**: Merge `state.py` and `types.py` into **`types.py`** (or `common.py`).

---

## 🚀 High Priority Features

### 1. **Shape API Enhancements** 🎨 ✅ **COMPLETED**
**Files**: `plugin/modules/draw/shapes.py`, `plugin/modules/writer/shapes.py`
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
**Files**: `plugin/modules/writer/fields.py`
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
**Files**: `plugin/modules/writer/indexes.py`
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

## 📋 Medium Priority Features

### 4. **Enhanced Style Management** 🎭
**Files**: `plugin/modules/writer/styles.py`
**Status**: Partial implementation exists

- [ ] Implement `styles_create_or_update`
- [ ] Add style inheritance system
- [ ] Support conditional styles
- [ ] Add style preview functionality
- [ ] Implement style import/export

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
**Files**: `plugin/framework/errors.py`
**Status**: Needs review

- [ ] Audit all error codes for consistency
- [ ] Standardize error message formats
- [ ] Add missing error codes for new features
- [ ] Improve error context reporting
- [ ] Add error recovery patterns

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

## 📚 Documentation Tasks

### 8. **API Documentation** 📖
**Files**: `docs/api/` (new directory)
**Status**: Not started

- [ ] Document all tool APIs with examples
- [ ] Create UNO service reference guide
- [ ] Add domain-specific documentation
- [ ] Generate API reference from code
- [ ] Add usage examples and best practices

**Dependencies**: None
**Blockers**: None

### 9. **Developer Guide** 👨‍💻
**Files**: `docs/development.md`
**Status**: Partial

- [ ] Document architecture overview
- [ ] Add contribution guidelines
- [ ] Create tool development guide
- [ ] Add testing patterns
- [ ] Document release process

**Dependencies**: None
**Blockers**: None

### 10. **User Guide** 📚
**Files**: `docs/user-guide.md`
**Status**: Not started

- [ ] Create getting started guide
- [ ] Add feature tutorials
- [ ] Document common workflows
- [ ] Add troubleshooting section
- [ ] Create FAQ

**Dependencies**: None
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
**Files**: `plugin/modules/chatbot/memory.py`
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
**Files**: `plugin/modules/http/mcp_protocol.py`
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
- ✅ Shape API enhancements (rich formatting, connectors, groups)
- ✅ Fields domain (full field type support, master/dependent system)
- ✅ Indexes domain (TOC creation, marks, comprehensive management)
- ✅ Librarian agentic onboarding ([`plugin/modules/chatbot/librarian.py`](../plugin/modules/chatbot/librarian.py))
- ✅ Track Changes domain ([`plugin/modules/writer/tracking.py`](../plugin/modules/writer/tracking.py))
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