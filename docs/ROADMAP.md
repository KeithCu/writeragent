# WriterAgent Roadmap

**Last Updated**: 2026-08-26  
**Status**: Active Development


Planned features, technical debt, and product gaps. For day-to-day orientation use [`AGENTS.md`](../AGENTS.md); this file is the longer backlog.

**Immediate focus (approachability / debt):**
- Finish remaining test-factory edges (Draw/Impress page stubs only if pytest needs them; geometry helpers can stay focused MagicMocks).
- Keep shrinking boilerplate and fixing stale `docs/` paths when you touch a topic.
- Optional: section markers in megamodules (`content.py`, `document_helpers.py`, `format.py`, `panel.py`, …) before any large splits.

`TestingFactory` / `with_native_doc` / `execute_tool` are the standard test path (see §5). Do not reintroduce per-file `@setup` / `@teardown` lifecycle.

---

## Docs: fewer files, still visible

Backlog for a later pass. Goal: **great living docs**, not a dump directory and not an exploding count of one-shot checklists.

**Living feature docs stay in `docs/`.** Files like [scripting/monaco-editor-dev-plan.md](scripting/monaco-editor-dev-plan.md) are the right shape: architecture people can read to understand the feature, plus an **Open** / next-phase list so you can pick the work up later. Implemented does **not** mean archive. Keep those visible; update “what shipped” and “what’s next” in place.

**Policy (agreed):**
- **Stay visible:** do **not** mass-move feature plans, comparison writeups, or idea notes into `archive/` just to shrink `ls docs/`.
- Fold only when two files are the **same topic** and one is leftover after the content already lives in a hub (example: background-thread-pool plan → [framework/threading.md](framework/threading.md)). Then delete the leftover so links do not 404.
- Keep **large feature hubs** large when the code is large (NumPy: `enabling_numpy_in_libreoffice.md`, `scripting/numpy-serialization.md`, `scripting/numpy-domains.md`). Do not smash everything into one mega-file.
- **Archive only a few** files that are truly dead: “we looked at X and said no” with no remaining pointer value (`archive/litellm-integration-notworthit.md` is the model). Do not archive a doc that still has an Open/next list.
- [`repo-map.md`](repo-map.md) Deep dives is the catalog of **hubs**, not every markdown file. Living feature plans can sit beside hubs without being in that table. Root [`AGENTS.md`](../AGENTS.md) keeps invariants only (Hermes 20k cap).

**Optional later folds (only if they actually duplicate a hub):**
- Threading: remaining reentrancy-plan invariants into `framework/threading.md` / `framework/uno-thread-safety.md` (keep streaming, architecture, and UNO guard as distinct living docs).
- Grammar / eval / calc: merge only where two files tell the same story; keep CF, sheet-filter, date-time, Monaco, embeddings, etc. as living docs with next-work lists.

---

## LibreOffice API fidelity (product)

Primary product focus: close the gap between the agent and the full UNO surface so AI can drive professional Writer / Calc / Draw / Impress features.

- **Writer**: Mail Merge (CSV/DB/Sheets), bibliographies, watermarks; Sections lifecycle (multi-column, conditional visibility, password protection).
- **Calc**: Macros & VBA compatibility, Scenarios, external data (SQL/Web), table slicers, sheet protection; deepen [Python/NumPy](enabling_numpy_in_libreoffice.md).
- **Draw & Impress**: Slide animations, layer management, slide show controls; audio/video insertion and 3D shape manipulation.
- **Cross-document** (partially shipped): sibling-folder reads work today ([multi-document plan](chat/multi-document-dev-plan.md)); still ahead: configurable extra directories, `@` mention UI, headless opens, broader directory-wide synthesis.
- **Long documents**: page-at-a-time navigation and caching for 100+ page files.

User-facing feature map: [features.md](features.md).

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

## Medium Priority Roadmap

| Feature | Description |
| :--- | :--- |
| **LLM Response Parsing Refactor** | ✅ **COMPLETED** — Provider-specific parsing and normalizers in `plugin/framework/client/response_normalizers.py` (+ provider shims). |
| **Batch Section Rewriting** | Heading-based document segmentation for whole-document processing. |
| **Advanced Impress Layouts** | Native shape positioning/sizing for Draw/Impress image generation. |

### LLM Parsing for Resilience ✅ **COMPLETED**

Provider-specific response parsing lives in `plugin/framework/client/response_normalizers.py` (`OpenAIShim` and siblings). `llm_client.py` delegates wire/pacing concerns; quirks (Gemini roles, Anthropic nested content, Ollama formatting) stay isolated there.

---

## Medium Priority Features

### 4. **Enhanced Style Management** 🎭 ✅ **COMPLETED**
**Files**: `plugin/writer/styles.py`
**Status**: Fully implemented and tested

- ✅ Implement `styles_create_or_update` (via `StyleCreate` and `StyleUpdate`)
- ✅ Add style inheritance system
- ✅ Support conditional styles
- ✅ Add style import/export (via `StyleImport`)
- [ ] Add style preview functionality

**Dependencies**: UNO style family documentation
**Blockers**: Style inheritance complexity
**Testing**: Need style-heavy test documents

### Settings dialog: larger provider buttons with logos ✅

**Status**: Implemented — 48 px marks in `extension/assets/` on `dlg:img` (ISOTROPIC scale); starter **buttons stay 64×14**. PushButton `ImageURL` does not scale (VCL).

The General page “Get API Key (1-click)” row (`btn_openrouter`, `btn_together`, `btn_hf`, `btn_nvidia` in `extension/Dialogs/SettingsDialog.xdl.tpl`) is four 64×14 text-only command buttons. There is **no existing project code** that puts graphics on XDL buttons.

**What UNO already supports**

- **Bigger buttons:** `dlg:width` / `dlg:height` (or runtime `Width` / `Height` on `UnoControlButtonModel`). Height ~22–28 is the usual chunky range (OK is already 18). Grow the 440×218 dialog and shift the JSON / status / OK row. Font size is **not** an XDL attribute — set `FontDescriptor` / `FontHeight` after load. `dlg:multiline="true"` if the label wraps under an icon.
- **Logos:** first-class on `UnoControlButtonModel` ([API](https://api.libreoffice.org/docs/idl/ref/servicecom_1_1sun_1_1star_1_1awt_1_1UnoControlButtonModel.html)): `ImageURL`, `Graphic` (`XGraphic`; setting it clears `ImageURL` and vice versa), `ImagePosition`, `ImageAlign`.
- **XDL** (`dialog.dtd`): `dlg:image-src`, `dlg:image-position` (`left-center`, …), `dlg:image-align`.

**Packaging (OXT)**

1. Ship small PNGs under `extension/` (e.g. `images/providers/`).
2. Register them in `extension/META-INF/manifest.xml`.
3. Prefer runtime `model.ImageURL` from the extension `base_url` already used with `DialogProvider`, or `GraphicProvider` + `InputStream`. Design-time `image-src` in Basic dialogs is historically flaky.

**Limits**

- Button graphics **do not scale-to-fit** (unlike dialog background). Use **16×16 or 24×24** PNGs; a large image overflows/clips. LO will not auto-HiDPI these the way toolbar icons do.
- Four ~90×24 buttons will not fit the current row — two rows, shorter labels, or a wider dialog.
- Trademark: official Hugging Face / Together / NVIDIA marks often have usage rules; simple wordmarks or unofficial glyphs can be safer.

**Smallest implementation:** bump XDL size + four packaged PNGs + `ImageURL` / `ImagePosition=left-center` after dialog load.

---

## 🛠️ Technical Improvements

### 5. **Test Infrastructure Consolidation** 🧪
**Files**: `tests/testing_utils.py`
**Status**: Mostly done — factory is the standard path

- [x] **Shared stubs via `TestingFactory`**: `create_doc` returns `WriterDocStub` / `CalcDocStub`; `create_context` for ToolContext; native path uses `doc=` + `@with_native_doc` / `native_doc`
- [x] **Calc document stub**: sheet/cell/range, `queryContentCells`, doc props/listeners; `getNumberFormats` / `getNumberFormatSettings` for inspector enrichment pytest
- [x] **Writer factory cleanup**: stub-by-default; `test_styles.py` uses `items=` families; removed unused `setup_tool`
- [x] **Common native pattern**: `TestingFactory.execute_tool` + `create_context(..., services=)`; Calc/Impress `_uno` helpers migrated; fake-native `test_page_uno.py` → pytest `test_page.py`
- [x] **Drop leftover `@setup` / `@teardown` imports** in suites that already use the factory
- [x] **Calc number-format pytest** (`test_cells.py`) uses `TestingFactory.create_doc(..., number_formats=…)` instead of bare MagicMock docs
- [ ] Draw/Impress page stubs only if pytest coverage needs them (not needed today — `supportsService` via stub `doc_type` is enough)
- Note: `test_calc_utils.py` cell-geometry cases stay focused MagicMocks (unit-testing merge-cursor helpers, not document stubs)

**Impact**: Less duplicated lifecycle / stub code; new UNO tests should follow the factory
**Dependencies**: None
**Blockers**: None

### 6. **Error Handling Standardization** ⚠️
**Files**: `plugin/framework/errors.py`, `plugin/framework/logging.py`
**Status**: In Progress (LibrePy subset completed)

- [x] Audit all error codes for consistency (LibrePy subset audited)
- [✅] **Standardize on `log.exception("Context")`** inside `except` blocks to ensure stacktraces are captured for debugging.
- [✅] Enhance `SafeLogger` in `logging.py` to support `exception()` method.
- [✅] Port key modules (`tool_loop.py`, `document_helpers.py`, `service.py`, `format.py`) to the new logging pattern.
- [✅] Standardize error message formats
- [x] Add missing error codes for new features (`ScriptingError`, `VenvError`, `VenvNotFoundError`, `VenvTimeoutError`, `VenvExecutionError`, `WorkerIPCError`, `CalcError`, `FormulaError`, `FormulaSyntaxError`, `SpillCollisionError`, `ExcelConversionError`, `SandboxSecurityError`, `PayloadCodecError`, `DataShapeError`)
- [x] Improve error context reporting (venv worker IPC, diagnostics recording, AST syntax errors with line:offset, Excel conversion issues)
- [x] Add error recovery patterns (worker crash/timeout process tree termination and retry, soft timeout state preservation)


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
**Files**: `plugin/framework/tool.py`
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

### Framework / UNO — later dedicated sessions

Not blocking product work. **One item per session.** Each can change user-visible or threading behavior; a naive patch is worse than leaving it.

| # | Session | Current behavior | Intended change | How not to get it wrong |
|---|---------|------------------|-----------------|-------------------------|
| 1 | **Stop / cancel** — [`queue_executor.py`](../plugin/framework/queue_executor.py) `agent_session`, `SendCancellation` | `agent_session` `finally` resets the context var and decrements the count but does not `scope.cancel()`. `SendCancellation.cancel()` always calls `default_executor.cancel_pending_work()`, even if work was queued on another executor. Aborted sends can leave HTTP clients and main-thread work live. | Cancel the session on **abort** (not on success). Track which executor the session used. | Trace one Stop click **and** one crash-during-send. `scope.cancel()` in `finally` also fires on **success**. Confirm which executor the work was queued on. Start here. |
| 2 | **Missing API key → fake 401** — [`llm_client.py`](../plugin/framework/client/llm_client.py) `_resolve_auth`, [`auth.py`](../plugin/framework/client/auth.py) `resolve_auth_for_config` | `AuthError` (including `missing_api_key`) is caught and turned into `{}`. The client then looks like `"custom"` and the user sees a generic 401. | Propagate `AuthError` for known hosted providers so the UI can say the key is missing. | Local Ollama / `header_style=none` must still work with an empty key. Read every `_resolve_auth` caller before raising. |
| 3 | **Connect vs read timeout** — [`http_transport.py`](../plugin/framework/client/http_transport.py), `LlmClient._timeout` | One value (default 120s) is the socket timeout for connect **and** each stream read. A model that pauses longer than that between tokens looks like a drop. Mid-stream retry after first token is already skipped. | Separate a short connect timeout from a larger (or Stop-aware) read timeout. | Measure real stall times per provider. Do not set read timeout to `None` without a working Stop path. |
| 4 | **Tool schemas for Gemini/Groq** — [`tool.py`](../plugin/framework/tool.py) `_collapse_union_type`, `validate` | Union `type: ["string","array"]` is collapsed only at the top level. Nested properties stay as unions. Tools with empty `properties` skip the unknown-key check, so hallucinated kwargs can reach `execute` as `TypeError`. | Recurse collapse where strict providers need it; treat empty-properties as “reject unknown kwargs.” | Only if you have a provider reject log or a tool `TypeError` from extra keys. Recursing is easy to over-collapse (`integer` vs `number`). Need golden OpenAI/Gemini/Groq schemas plus a fixture. |
| 5 | **Close / termination veto** — [`uno_listeners.py`](../plugin/framework/uno_listeners.py) `_catch_and_log` | Every UNO listener callback is wrapped so Python exceptions do not enter the C++ bridge. That also swallows `CloseVetoException` / `TerminationVetoException`, so a listener cannot block document close or office quit. | Re-raise those two veto types; keep logging everything else. | **Do not** change this until a real listener should block close (e.g. pending edit review). Then a UNO test that vetoes and a check that the close dialog still appears. |
| 6 | **Drain handler errors** — [`async_stream.py`](../plugin/framework/async_stream.py) `_process_batch` **Done** | If a dispatch handler (chunk/thinking UI) raised, the loop put `(ERROR, payload)` back on the queue, continued the current batch, and did not set `job_done`. | Inline `on_error`, set `job_done`, end the batch. Do not re-queue `ERROR` or call `on_stream_done` after the crash. | Must not double-call `on_error` or hang by breaking without `job_done` after `STREAM_DONE` was dequeued. Test: raise on the second chunk. |
| 7 | **Memory / extra instructions in the system prompt** — [`prompts.py`](../plugin/framework/prompts.py), [`memory.py`](../plugin/chatbot/memory.py) | Memory, skills, and `additional_instructions` are appended to the system prompt as raw text. Content the model previously wrote can try to override instructions. | Wrap injected blocks in explicit delimiters plus a “this is data, not instructions” line. | Prompt design, not a one-liner. Delimiters help a bit; they are not a proof. Same session should look at how memory is **written**, not only how it is concatenated. |

**Impact**: Fewer hung sends, clearer auth failures, safer schemas, optional close veto, tighter stream drain, slightly harder prompt injection  
**Priority**: Low until you schedule a dedicated session (start with #1)

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

## Contribution opportunities

### Good first issues
- Fix stale paths in a topic doc when you touch that area (`format.py` not `format_support.py`; chat prompts in `prompts.py`)
- Migrate a remaining MagicMock-heavy pytest to `TestingFactory` where a stub already fits
- Error-message consistency pass (`log.exception`, `format_error_payload`)
- Section markers in a megamodule before any split

### Larger / mentored
- Batch section rewriting; advanced Impress layouts (see Medium Priority table)
- Document acquisition / resolution unification (DocumentService + frame resolution + RuntimeUID)
- Remaining specialized-domain gaps in [features.md](features.md) / toolset docs

### Research
- Agent personality / memory UX beyond current librarian + USER.md
- Voice interface; collaborative editing

---

## Changelog

**2026-08-26**: Framework/UNO dedicated-session leftovers parked in the roadmap (Stop/cancel first; self-contained, no external review doc)

**2026-08-08**: Approachability pass
- MCP docs: live `plugin/mcp/` + `queue_executor` status; historical `core/` narrative quarantined
- Topic docs: `format.py` / `prompts.py` path corrections (AGENTS.md left alone)
- Test factory: CalcDocStub number-format hooks; leftover `@setup`/`@teardown` imports removed
- Roadmap: removed duplicate second title, refreshed status / good-first-issues

**2024-03-25**: Initial roadmap created (Shapes, Fields, Indexes, tech debt inventory)

**2024-03-24**: Tool switching + specialized domains documented

---

## Current status

**Recently completed:**
- ✅ Mail Merge specialized domain ([`plugin/writer/specialized/mail_merge.py`](../plugin/writer/specialized/mail_merge.py))
- ✅ Shape / Fields / Indexes Writer domains
- ✅ Librarian agentic onboarding ([`plugin/chatbot/librarian.py`](../plugin/chatbot/librarian.py))
- ✅ Track Changes ([`plugin/writer/tracking.py`](../plugin/writer/tracking.py))
- ✅ Specialized domain system + Calc tool integration
- ✅ Config / chat model unification (`text_model` via `get_text_model` / `set_text_model`)
- ✅ LLM response parsing refactor (`response_normalizers.py` + shims)
- ✅ Test infrastructure consolidation (factory + native helpers; see §5)
- ✅ MCP bind/port cleanup and shared native tool executor (WIP squash onto master)

**Active:**
- Document acquisition / resolution unification (DocumentService + frame + RuntimeUID)
- Approachability: stale docs, megamodule navigation markers, leftover mock edges
- Product fidelity gaps under [LibreOffice API fidelity](#libreoffice-api-fidelity-product)

**Up next:**
- Section markers (then selective splits) on largest entry modules
- Batch section rewriting / Impress layout work when prioritized
- When a dedicated session is free: Stop/cancel under [Framework / UNO — later dedicated sessions](#framework--uno--later-dedicated-sessions) — one item only