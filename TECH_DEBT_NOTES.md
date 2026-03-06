# Technical Debt Report

Based on a thorough analysis of the codebase (excluding `contrib`), here are the main areas of technical debt, organized by category.

## 1. Dead or Unused Code
Several classes and methods are defined but never explicitly referenced. Some may be dynamically loaded (e.g., via `ToolRegistry.discover`), but others appear to be obsolete legacy code.

**Likely Dead Code:**
*   `plugin/modules/launcher_claude/__init__.py`, `plugin/modules/launcher_gemini/__init__.py`, `plugin/modules/launcher_opencode/__init__.py`: The classes `ClaudeModule`, `GeminiModule`, and `OpenCodeModule` appear unused or obsolete, along with their helper functions like `get_default_cwd` and `on_install`.
*   `plugin/options_handler.py`: There are several legacy LibreOffice GUI listeners (e.g., `itemStateChanged`, `disposing`, `actionPerformed`) that seem to be unused fragments from an older options UI design.
*   `plugin/prompt_function.py`: A large block of methods associated with `PromptFunction` (`getProgrammaticFunctionName`, `getDisplayFunctionName`, `hasFunctionWizard`, etc.) appears to be dead code.
*   `plugin/framework/dialogs.py`: Several legacy dialog helpers and listener methods, such as `msgbox_with_copy`, `status_dialog`, and multiple `actionPerformed`/`disposing` implementations on internal listener classes.
*   `plugin/modules/tunnel/__init__.py`: `TunnelModule` and related functions appear unused or partially implemented.

**Tools that may be unused or only partially wired:**
*   A vast number of Writer tools (e.g., `ListTextFrames`, `GetTextFrameInfo`, `ListComments`, `AddComment`, `DeleteComment`, `AddAiSummary`, `GetAiSummaries`, `SearchInDocument`, `AdvancedSearch`) appear in vulture's dead code analysis. While some tools are dynamically loaded by `ToolRegistry`, checking if all of them are actually exposed and useful to the AI agents is recommended.

## 2. Overly Complex Functions
The following methods have high cyclomatic complexity and should be refactored into smaller, more focused functions to improve readability and maintainability:

*   **`plugin/modules/writer/content.py`**: Many `execute` methods in the Writer tools are complex. Notably:
    *   `ApplyDocumentContent.execute` (Complexity: 25)
*   **`plugin/modules/writer/proximity.py`**:
    *   `ProximityService.get_surroundings` (Complexity: 35)
    *   `ProximityService.navigate_heading` (Complexity: 29)
*   **`plugin/modules/chatbot/panel_factory.py`**:
    *   `ChatPanelElement._wireControls` (Complexity: 63) - This is a massive UI setup method that should be broken down by control section.
*   **`plugin/framework/legacy_ui.py`**:
    *   `settings_box` (Complexity: 47) - Contains heavily nested legacy UI building code.
*   **`plugin/modules/core/format_tests.py`**:
    *   `run_markdown_tests` (Complexity: 58)
    *   `_run_format_preserving_tests` (Complexity: 30)
*   **`plugin/modules/batch/batch.py`**:
    *   `ExecuteBatch.execute` (Complexity: 41) - Needs breaking down, likely by separating validation from execution logic.
*   **`plugin/main.py`**:
    *   `_dispatch_command` (Complexity: 28) - Acts as a giant switch statement for actions; could be refactored using a dictionary or handler classes.
*   **`plugin/options_handler.py`**:
    *   `OptionsHandler._ld_on_initialize` (Complexity: 23)
    *   `OptionsHandler._load_module_fields` (Complexity: 22)
*   **`plugin/modules/http/client.py`**:
    *   `LlmClient._run_streaming_loop` (Complexity: 33) - Streaming logic is deeply nested.
*   **`plugin/modules/ai/tools.py`**:
    *   `WebResearchTool.execute` (Complexity: 31)

## 3. Code Duplication and Architecture
*   **Tool Execution Boilerplate**: The `execute` methods across many tool classes (e.g., in `plugin/modules/writer/content.py`, `plugin/modules/writer/images.py`) share a lot of boilerplate for validating inputs, grabbing contexts, and handling errors. A more robust `ToolBase` could abstract some of this away.
*   **Legacy UI / Sidebar / Panels**: There seems to be overlapping or duplicated logic between `plugin/options_handler.py`, `plugin/framework/legacy_ui.py`, and the `chatbot` module's UI generation (`plugin/modules/chatbot/panel_factory.py`). A unified approach to LibreOffice XDL dialogs vs dynamically generated panels would reduce technical debt.
*   **Main Bootstrapper (`plugin/main.py`)**: The `bootstrap` logic is doing auto-discovery based on directories and importing `ModuleBase` subclasses via introspection. This is clever but somewhat fragile. It could be cleaner to have a centralized registry list or rely entirely on the `_manifest.py` to point directly to class imports instead of guessing directory paths.
*   **Error Reporting**: There are multiple ways errors are shown to the user (e.g., `msgbox` vs `status_dialog` vs logging). Unifying error reporting would clean up the codebase.

## Recommendations
1.  **Purge Legacy Modules**: Delete the unused `launcher_claude`, `launcher_gemini`, and `launcher_opencode` modules if they have been superseded by a generic launcher.
2.  **Refactor ChatPanelElement**: Break down `_wireControls` in `plugin/modules/chatbot/panel_factory.py` into smaller methods.
3.  **Simplify Main Dispatcher**: Extract the framework command logic from `_dispatch_command` in `plugin/main.py`.
4.  **Clean up Options/Prompt Function**: Remove the dead methods from `plugin/options_handler.py` and `plugin/prompt_function.py`.
5.  **Review `execute_batch`**: The `batch.py` tool is very complex (41) and tightly coupled; refactoring how it handles sequential state variables would make it safer.