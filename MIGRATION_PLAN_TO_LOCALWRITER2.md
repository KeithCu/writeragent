# Incremental Migration Plan: localwriter -> localwriter2

The goal of this plan is to incrementally reduce the diffs between the `localwriter` (current directory with some new features not in localwriter2) and `localwriter2` (refactoring from a week old fork), ensuring the plugin remains functional at every step.

**Current status:** Phases 1–4 are complete (tooling/Make, docs/cleanup, framework infrastructure porting, Writer/Calc/Chatbot module reorganization). We have successfully implemented the dynamic `ToolRegistry`, patched `main.py` to use a `bootstrap()` loader, split the Writer tools into modular files, and completed the Service Decoupling. Most of Phase 5 is also complete, including the port of the **comprehensive framework test suite** (80+ tests).

## Proposed Changes

### Phase 1: Porting Tooling & The Make System ✅ (Completed)
The current `localwriter` uses a simple `build.sh` script, while `localwriter2` uses a robust `Makefile` and python scripts in `scripts/`. We can migrate this by:
1. Copying `Makefile`, `Makefile.local-dist`, and the `scripts/` directory from `localwriter2` into `localwriter`.
2. Copying `plugin/_manifest.py` and `plugin/plugin.yaml` (which the new Make system needs to build `manifest.xml`).
3. Commenting out any modules or configurations in `_manifest.py` that don't yet exist in the old `localwriter` tree.
4. Verifying that `make build` and `make deploy` work for our current codebase, and then retiring `build.sh`.

### Phase 2: Documentation and Root File Cleanup ✅ (Completed)
`localwriter` has a lot of `.md` and `.odt` files cluttering the root directory. `localwriter2` organizes these better.
1. Move the root project notes and design docs into `docs/` or `contrib/` matching the `localwriter2` layout.
2. Remove any obsolete files that `localwriter2` has deleted.

*Documentation and root-file reorganization was completed by another agent.*

### Phase 3: Bringing in Framework Infrastructure ✅ (Completed)
`localwriter2` has a rich abstraction layer in `plugin/framework/` (e.g., `module_base.py`, `service_base.py`, `event_bus.py`, `tool_base.py`). 
1. **Framework files ported** ✅ (Completed) Core framework files are now present in `plugin/framework/`: `event_bus.py`, `service_registry.py`, `main_thread.py`, `uno_context.py`, `dialogs.py`, `module_base.py`, `service_base.py`, `tool_base.py`, `tool_registry.py`, `tool_context.py`, `schema_convert.py`, `constants.py`, `uno_helpers.py`, `logging.py`, `http.py`, `image_utils.py`.
2. **Infrastructure Alignment** ✅ (Completed) `plugin/modules/core/mcp_thread.py` has been refactored to delegate safe UNO execution to the new `plugin.framework.main_thread` module, which uses the superior `AsyncCallback` mechanism.
3. **Writer tools on ToolBase** ✅ (Completed) Writer tools use a `ToolRegistry` and thin `ToolBase` wrapper classes in `plugin/modules/core/document_tools.py`.

### Phase 4: Module Reorganization ✅ (Completed)
`localwriter2` heavily refactors logic out of core and into specific modules under `plugin/modules/writer/`, `plugin/modules/calc/`, `plugin/modules/chatbot/`, etc.
1. **Writer Module:** ✅ (Completed) Fully unified with `localwriter2` by migrating to isolated `ToolBase` subclasses, deprecating the `tools/` wrapper directory, and porting advanced Writer services (`bookmarks`, `tree`, `navigation`).
2. **Calc Module:** ✅ (Completed) Missing directories created; tool implementations moved from core to `plugin/modules/calc/` (by another agent).
3. **Chatbot Module:** ✅ (Completed) UI and chat-specific logic moved out of core:
   - `plugin/chat_panel.py` moved via `git mv` to `plugin/modules/chatbot/panel_factory.py` (history preserved). Manifest and build script updated to register the new path.
   - ChatSession, SendButtonListener, StopButtonListener, and ClearButtonListener extracted into `plugin/modules/chatbot/panel.py`; `panel_factory.py` imports them and passes `ensure_path_fn=_ensure_extension_on_path` into the listener.
   - Extension-root path fix: when loaded from the unpacked .oxt, `panel_factory.py` adds the extension root (4 levels up from itself) to `sys.path` so `import plugin` resolves correctly.
4. **Draw Module:** ✅ (Completed) `draw_tools`, `draw_bridge`, and `draw_tests` moved from core to `plugin/modules/draw/` and imports updated.
5. `AGENTS.md` and tests updated iteratively.

### Phase 5: Additional Core Services ✅ (Completed)
`localwriter2` separates LLM and web services into distinct modules and introduces new processing capabilities.
1. **AI Module:** Port over the `ai` module (`plugin/modules/ai/`) and register it properly in `_manifest.py`.
2. **HTTP Module:** ✅ (Completed) Ported the internal web server and MCP routes (`plugin/modules/http/`).
3. **Batch & Tunnel Modules:** ✅ (Completed) Ported `plugin/modules/batch/` and `plugin/modules/tunnel/`.
4. **Options Framework:** ✅ (Completed) Ported `plugin/options_handler.py`.
5. **Service Granularity:** ✅ (Completed) Aligned split core services (`events.py`, `format.py`).
6. Update `config.py` and remove legacy hardcoded settings in favor of the new modular configuration system.

## Verification Plan

### Automated Tests
- Run `make test` (or `pytest`) after each incremental step to ensure unit tests still pass.

### Manual Verification
- After porting the Make system in Phase 1, we will actively use `make deploy` and verify the plugin starts up cleanly in LibreOffice.
- For each subsequent phase, we will click a few tools via the Sidebar in LibreOffice to test end-to-end functionality.

---

## What to work on later (advised follow-ups)

### Completed: EventBus Reconciliation ✅
`localwriter` now exclusively uses the robust `plugin/framework/event_bus.py`. The legacy `plugin/modules/core/tool_bus.py` was successfully migrated and removed.

### Completed: Writer Tool Auto-Discovery & main.py Patching ✅
The legacy `document_tools.py` monolithic registry was deleted. All Writer, Calc, and Draw tools now inherit from `ToolBase` and live in their respective `tools/` subdirectories. `main.py` was successfully patched to use the dynamic `bootstrap()` loader and `ToolRegistry` auto-discovery, resolving tool discovery dynamically.

### Completed: Service Decoupling ✅
Mocks in `main.py` (`ConfigMock` and `DocumentServiceMock`) were removed. `localwriter` now utilizes fully decoupled `ConfigService` and `DocumentService` modules, and `main.py` acts as a pure bootstrapper. Legacy module imports were refactored to point to the new service abstractions.

### Recommended Next Step (AI Module Porting)
**Phase 5 (AI Module):** Port the AI module (`plugin/modules/ai/`). This may be slightly more complex due to recent AI tool restorations, but it is necessary for full architectural alignment to complete the migration to the localwriter2 module structure.

### Completed: Writer tools with logic in ToolBase ✅
Replaced the thin Writer wrappers with "real" `ToolBase` classes and advanced implementations ported directly from `localwriter2` that leverage `ctx.services`.

### Completed: Framework Testing Suite ✅
Ported the comprehensive unit tests from `localwriter2/tests/` (e.g., `test_config_service.py`, `test_event_bus.py`, etc.) and updated the framework to ensure stability, with 80 tests passing in the main tree.

### Other Follow-ups
- **Config Migration:** Move `config.py` toward the new schema-based system to fully decouple settings.
