# Codebase Comparison: localwriter vs localwriter2

This document summarizes the current state of the two trees. You have successfully reorganized the main `localwriter` tree into modules, but it still maintains a few monolithic hubs compared to the fully decoupled architecture of `localwriter2`.

## 1. Feature Differences

| Feature | localwriter (Main) | localwriter2 (Refactored) |
| :--- | :--- | :--- |
| **MCP Server** | ✅ Full support (timer-managed) | ❌ Missing |
| **Image Tools** | ✅ AI Horde + Endpoint (Horde async) | ❌ Missing |
| **Draw Tools** | ✅ Full support (shapes, slides) | ❌ Missing |
| **smolagents** | ✅ Web Research (DuckDuckGo, Visit) | ❌ Missing |
| **Writer Tools** | ⚠️ Basic set + registry wrappers | ✅ Advanced (navigation, tree, search) |
| **Calc Tools** | ✅ Full support | ⚠️ Partial / Refactored |
| **Evaluation** | ✅ Eval Runner + Dashboard | ❌ Missing |
| **Pricing** | ✅ Pricing module | ❌ Missing |

## 2. Infrastructure & Architecture

The primary difference lies in how the plugin "starts up" and how tools are discovered.

### localwriter (Current Hybrid)
- **Patched main.py**: `main.py` was patched to use the dynamic `bootstrap()` module loading mechanism. It now completely utilizes the `ServiceRegistry` with isolated `ConfigService` and `DocumentService` modules, acting as a pure, lean bootstrapper.
- **Auto-Discovery Tool Registry**: The manual tool registry in `document_tools.py` has been completely deleted. All modules (`calc`, `draw`, `writer`) now use `ToolBase` subclasses in their `tools/` directories, which are automatically discovered by `ToolRegistry`.
- **Dynamic Manifest**: `_manifest.py` now supports dynamic module loading and topological sorting of dependencies.
- **Direct Imports**: We have successfully refactored tests and the chatbot panel to rely on `get_tools()` instead of legacy direct imports from `plugin.modules.core.document_tools`.

### localwriter2 (Fully Modular)
- **Bootstrap Loader**: `main.py` is a ~700 line "loader" that discovers modules, resolves dependencies, and initializes services.
- **Auto-Discovery**: Tools are automatically discovered from `modules/*/tools/` (or the module root) without being explicitly listed.
- **Service Registry**: A central `ServiceRegistry` handles singletons like `config`, `events`, and `ui`.
- **Event Bus**: Includes a robust `event_bus.py` for decoupled communication between modules.
- **Lifecycle Management**: Modules have `initialize`, `start`, and `shutdown` methods.

## 3. What Remains to be Merged

To achieve full alignment while keeping your new features, the following "refactoring gaps" need to be closed:

### A. Infrastructure (Ported from localwriter2) ✅
The core framework files have been ported to `plugin/framework/`, providing a robust foundation for services and messaging:
- `event_bus.py`: Decoupled, event-keyed messaging with weak-ref support.
- `service_registry.py`: Singleton management and dependency injection.
- `main_thread.py`: Safe execution helper using `AsyncCallback` (prevents UI freezes).
- `uno_context.py`: Reliable context singleton for long-lived services.
- `dialogs.py`: Common dialog abstractions (msgbox, clipboard).

**Refactoring Note**: `plugin/modules/core/mcp_thread.py` has been updated to delegate its UNO execution logic to the new `main_thread.py` helper, consolidating the safe execution pattern.

### B. Feature Migration (Modularization)
Your new features should be converted into "localwriter2-style" modules:
- **MCP**: Move to `plugin/modules/http/` or `plugin/modules/mcp/`.
- **Image/AI**: Move `image_tools.py` and vendors into a formal `ai` module.
- **Draw**: Move `draw_tools.py` and its tests into `plugin/modules/draw/`.
- **Settings**: Move the complex `settings_box` logic into a service or core module.

### C. Writer Enhancements
`localwriter2` has several Writer-specific files that have more advanced logic than your current `writer_ops.py`:
- `modules/writer/navigation.py`: Smart navigation.
- `modules/writer/structural.py`: Structural editing.
- `modules/writer/tree.py`: Document tree representation.
- `modules/writer/bookmarks.py`: Bookmark management.

### D. Startup Philosophy
- **Protocol Handler**: Migrate your menu dispatches in `Addons.xcu` to use the `org.extension.localwriter:module.action` protocol handled by the new `DispatchHandler`. This enables dynamic menu text and icons (e.g., "Stop AI" vs "Send").

## Summary Recommendation
The main tree is **"Feature Rich but Architecturally Legacy"**, while `localwriter2` is **"Feature Lean but Architecturally Modern"**. 

Your next steps should focus on **porting the missing framework files** and then **re-registering your features (MCP, Draw, Image) as localwriter2 modules** to gain the benefits of auto-discovery and decoupled services.
