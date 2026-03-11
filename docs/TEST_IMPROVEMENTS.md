# Test Code Improvements Report

## 1. Executive Summary
This report analyzes the consolidated tests in the `plugin/tests/` directory of the `writeragent` codebase. The project uses a mix of standard `unittest` unit tests (which run quickly outside LibreOffice) and integration tests (which run natively inside LibreOffice's internal Python interpreter using custom `run_x_tests` harnesses like `test_writer.py`, `test_calc.py`, and `test_draw.py`).

Because external packages like `pytest` cannot be assumed to exist inside the bundled LibreOffice Python environment, the custom native harness pattern is necessary. However, while the test coverage is reasonable for core framework components, there are significant gaps in testing edge cases, complex tool interactions, error recovery, and UI logic. Additionally, the native test harness architecture introduces boilerplate and maintenance challenges that can be streamlined.

## 2. Structural & Architectural Improvements

### 2.1 Streamlining the Native Test Harness
Currently, `test_writer.py`, `test_calc.py`, and `test_draw.py` use a custom assertion harness (`ok()`, `fail()`, and tracking `passed`/`failed` counts manually) designed to run inside the LibreOffice environment without relying on `pytest`.

*   **Problem:** This pattern requires wrapping every test block in a bare `try...except`, making tests verbose and harder to read. It also lacks standardized setup and teardown logic.
*   **Recommendation:** Evolve `plugin/testing_runner.py` to provide a lightweight, decorator-based or class-based runner that natively mimics the *structure* of standard tests (like `setUp` and `tearDown`) without requiring `unittest` or `pytest` dependencies. By abstracting the `try...except` blocks and state tracking, you can dramatically reduce boilerplate in the `run_x_tests` files.

### 2.2 Fixture Management & Mocking
*   **Problem:** Some files (like `test_writer_navigation.py` and `test_image_service_refactor.py`) make heavy use of local stubs or `unittest.mock.patch`. While useful, the mocks are repeated across files. Conversely, the integration tests (`test_calc.py`) do a lot of raw UNO setup.
*   **Recommendation:** Create a centralized `testing_utils.py` or `fixtures/` library for common UNO mocks (e.g., `MockDocument`, `MockTextCursor`, `MockSheet`) and standard test documents. This will accelerate adding new tests for edge cases and standardize the setup for native LibreOffice tests.

## 3. Missing Coverage & Classes of Tests

### 3.1 Edge Case Testing for Format Preservation
The `format_support.py` logic (specifically `_replace_text_preserving_format`) is critical and complex. Tests currently exist (e.g., in `format_tests.py`), but lack deep edge case coverage.
*   **Missing Tests:**
    *   Replacements that span across paragraph boundaries or page breaks.
    *   Text containing complex fields (like citations or page numbers).
    *   Applying styles when the text string is extremely long (performance tests to prevent `O(N^2)` regressions).
    *   Replacing characters with mixed formatting (e.g., replacing "a**b**c" with "xy").

### 3.2 Error Recovery & Network Fallbacks
The AI and Network components (like HTTP interactions and MCP server) need robustness testing.
*   **Missing Tests:**
    *   **LLM API Timeouts/Failures:** Tests that simulate an API timeout or malformed JSON response and verify the UI shows the correct error message without crashing.
    *   **Streaming Connection Loss:** Simulating a connection drop halfway through a streaming response in `async_stream.py`.
    *   **SQLite Unavailability Fallback:** Explicit tests verifying that if the `sqlite3` module is broken or locked, the JSON file fallback history mechanism in `history_db.py` behaves correctly without losing state.

### 3.3 State Contamination (Multi-Document Handling)
A recent bug involved AI operations affecting the wrong document when multiple documents were open.
*   **Missing Tests:**
    *   Integration tests that explicitly open two mock documents, trigger a tool execution scoped to Document A, and verify Document B remains entirely untouched.

### 3.4 UI & Sidebar Logic
The UI layer, specifically the chat panel (`panel_factory.py`) and settings dialog logic, is largely untested.
*   **Missing Tests:**
    *   **Button State Management:** Tests verifying `_set_button_states` properly disables "Send" and enables "Stop" during generation, and strictly resets them in the `finally` block, even if an exception occurs mid-stream.
    *   **Configuration Sync:** Tests ensuring that when a setting is changed in the UI, the weakref listeners fire correctly and update the in-memory state of the sidebar.

### 3.5 Specific Tool Edge Cases (Writer/Calc/Draw)
*   **Calc:**
    *   Applying formulas that result in circular references.
    *   Using the `execute_calc_tool` with extremely large 2D arrays to verify the batch range operation logic (`setDataArray()`).
    *   Importing malformed CSVs via `CellManipulator`.
*   **Draw:**
    *   Testing operations on grouped shapes or shapes on master slides.
*   **Writer:**
    *   Track Changes interactions: Verifying that `reject_all_changes` or `accept_all_changes` functions correctly when the document has overlapping or conflicting changes.

## 4. Prioritized Recommendations

1.  **Streamline the Native LibreOffice Harness:** Create a lightweight framework in `testing_runner.py` to abstract away the repetitive `try/except` and logging boilerplate in `run_x_tests` files.
2.  **Add Format Preservation Edge Cases:** Expand `format_tests.py` to cover paragraph boundaries and complex mixed-formatting replacements.
3.  **Implement Multi-Document Isolation Tests:** Ensure regressions don't occur when operating with multiple active frames.
4.  **Simulate Network/API Failures:** Add tests for robust error handling in the LLM streaming paths and tool-calling execution.
