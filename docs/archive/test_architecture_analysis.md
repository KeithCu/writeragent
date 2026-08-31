# Test Architecture & Coverage Strategy
## WriterAgent / WriterAgent - State of Testing Analysis

### 1. Executive Summary

As a new test architect evaluating the current state of WriterAgent's testing, the foundational architecture is solid and intentionally designed to solve a very specific, difficult problem: testing Python code that is deeply integrated with the LibreOffice UNO component model.

The current test suite is divided into two distinct execution paths:
1.  **Pytest for Core/Non-UNO Logic:** Used for pure Python logic, API clients, streaming parsers, and configuration handling.
2.  **Native LO Test Runner (`testing_runner.py`):** A custom, dependency-free runner designed to execute natively within the LibreOffice Python environment, specifically for testing operations against live `com.sun.star` components (Writer documents, Calc sheets, Draw elements).

The design avoids the "mock everything" anti-pattern for integration tests while keeping external dependencies (like `pytest`) out of the end-user's LibreOffice environment. The goal now is to strategically expand coverage without compromising this fast, reliable foundation.

---

### 2. Analysis of the Current State

#### 2.1 Strengths of the Current Design
*   **Zero-Dependency Native Runner:** `plugin/testing_runner.py` is an excellent architectural choice. By using lightweight decorators (`@native_test`, `@setup`, `@teardown`) and aggregating results into a simple JSON payload, tests can run directly inside the complex, stateful LibreOffice environment without requiring users or CI runners to wrestle with installing `pytest` inside the LibreOffice bundled Python.
*   **Clear Separation of Concerns:** `tests/conftest.py` uses `pytest_collection_modifyitems` to deselect native/UNO suites (typically `*_uno.py`) when running pytest outside LibreOffice, so pure unit tests stay fast and do not touch a live soffice bridge.
*   **Live Document Testing over Mocking:** Natively testing against hidden LibreOffice instances (`PropertyValue(Name="Hidden", Value=True)`) ensures that format preservation, cursor movement, and document caching are tested against the actual UNO engine, preventing false positives that often occur when mocking complex third-party APIs.
*   **Centralized Test Utilities:** `testing_utils.py` provides standardized stubs (`WriterDocStub`, `CalcDocStub` with sheet/cell/range helpers, `MockDocument`, `MockContext`) for the pure Python tests that don't need a live LO instance, keeping test files clean. `TestingFactory.create_doc` returns a stateful stub for both Writer and Calc (no MagicMock wrapper). Calc covers default `Sheet1` / A1 selection, `queryContentCells`, `calculateAll` counting, doc props, and event listeners; Writer covers text enumeration, `supportsService`, style families via `items=`, `createInstance`, and `loadStylesFromURL`. Native Calc/Draw suites share `TestingFactory.execute_tool` (live `get_services()` + registry execute) instead of per-file `_execute_calc_tool` copies.
*   **Clean Setup/Teardown Boundaries:** The native runner handles the lifecycle of the hidden test documents properly, ensuring they are closed in the `@teardown` phase, preventing zombie `soffice.bin` processes.

#### 2.2 Current Coverage Gaps
While the infrastructure is robust, coverage is currently concentrated on the "happy paths" of document navigation and basic tool execution. Significant gaps exist in:
1.  **Error Recovery & Resilience:** How the system handles API timeouts, malformed JSON responses from LLMs, and UNO dispatch failures.
2.  **Streaming & Concurrency:** The interaction between the worker thread (fetching AI responses) and the main VCL UI thread draining the queue.
3.  **Complex Format Preservation:** Edge cases in the format-preserving replacement logic (e.g., replacing text spanning paragraph boundaries, mixed character styles).
4.  **Calc-Specific Edge Cases:** Batch updates via `setDataArray` with mismatched dimensions, or parsing complex nested formulas.
5.  **Multimodal & Image Generation:** Verifying the async state machine for the AI Horde provider and endpoint image generation fallbacks.

---

### 3. Strategy for Expanding Coverage

To scale the testing effort effectively, we should focus on the following strategic areas:

#### 3.1 Hardening the API & Streaming Boundaries (Pytest)
Since the `LlmClient` and streaming logic are pure Python, they should be heavily tested via `pytest`.
*   **Simulate Network Instability:** Write tests using `create_mock_http_response` to simulate timeouts, 503 Service Unavailable, and connection resets to verify the fallback and retry logic.
*   **Fuzzing Tool Calls:** Create tests that feed malformed JSON, truncated tool call blocks, and unexpected schema structures into the streaming parser to ensure it fails gracefully (or recovers) without crashing the UI thread.
*   **Queue Draining Logic:** Isolate the `async_stream` queue logic and test it independently of the UI toolkit to ensure events (`chunk`, `tool_call`, `error`) are processed in the correct order.

#### 3.2 Deepening UNO Integration Tests (Native Runner)
The native runner should focus on the "physics" of LibreOffice.
*   **Format Preservation Matrices:** Expand Writer format/native suites (e.g. `tests/writer/test_format_uno.py` and related apply/replace paths) with parameterized matrices: replacing bold with plain, long↔short strings while retaining background colors, and cross-paragraph replacements.
*   **Multi-Document Scoping:** Prefer frame/URL document resolution tests over any `DocumentCache` assumptions (`DocumentCache` is not active). Open two hidden documents and assert operations on one do not bleed into the other.
*   **Calc Batch Operations:** Add specific native tests for `write_formula_range` and `setDataArray` wrappers to ensure they correctly pad 2D arrays and strictly format formulas as strings, as noted in the project guidelines.

#### 3.3 UI & State Machine Testing (Pytest + Mocks)
Testing the LibreOffice UI directly is notoriously difficult. Instead, test the *state management* behind the UI.
*   **ChatSession history:** Pure pytest for [`ChatSession`](../plugin/chatbot/panel.py) message/`[DOCUMENT CONTENT]`/history-DB rules — see [`tests/chatbot/test_chat_session.py`](../tests/chatbot/test_chat_session.py). No UNO.
*   **Send/Stop busy flags:** Pure `next_state` coverage in [`tests/chatbot/test_send_state.py`](../tests/chatbot/test_send_state.py) and [`tests/chatbot/test_sidebar_state.py`](../tests/chatbot/test_sidebar_state.py) (`SendButtonState` / `SidebarCompositeState`). Do not mock the sidebar UNO controls just to assert `_send_busy`.
*   **Config Synchronization:** Verify that changes to the configuration schema correctly notify listeners and update the LRU caches without requiring a live UNO dialog instance.

#### 3.4 Expanding Evaluation Metrics
The project already utilizes an `EvalRunner` for benchmarking LLM outputs (Correctness vs. Cost). This should be formalized as a core metric for pull requests.
*   **Regression Tracking:** Store the output of the eval suite historically. If a prompt change or tool schema update causes a drop in the `Value (C²/$)$` metric for a baseline model (like Gemini Flash), the PR should be flagged.

---

### 3.5 Default pytest filter and profiling

`make pytest` (and the pytest half of `make test-run`) runs:

```bash
PYTHONUNBUFFERED=1 $(PYTHON) -u -m pytest tests -m "not slow and not integration" --ignore-glob='*_uno.py' -n -1 --dist=loadgroup
```

`PYTEST_WORKERS=0` (or empty) drops `-n` for a single process. `make pytest` sets `WRITERAGENT_PYTEST_PROGRESS=1` so collection/count heartbeats print as full stderr lines (pytest/xdist otherwise rewrite one `\r` status line). Collection must stay clean of live LibreOffice: `--ignore-glob='*_uno.py'` plus pyproject `addopts` (`--ignore=tests/uno`) so this invocation does **not** collect `plugin.testing_runner` / `*_uno.py` / soffice suites. `tests/conftest.py` still drops leftover `@native_test` functions in mixed modules. Parallel tests may only write under `tmp_path`, the autouse config temp dir, or a `worker_id`-namespaced path; `MagicMock/` cleanup is controller-only. Do not put UNO / `testing_runner` under xdist.

- **`slow`**: CrossHair hooks, large-file / Opengrep full scans, soffice smoke — also via `make slowtests` / `make opengrep-lint` where applicable.
- **`integration`**: Full subprocess worker IPC / live venv self-check smokes, plus live `--backend lo` eval. Run with `-m integration` when needed.

`make test-run` is: `make pytest`, then `lo-kill`, then `PYTHONUNBUFFERED=1 $(LO_PYTHON) -u -m plugin.testing_runner` (serial UNO; do not parallelize). The runner prints flushed `SUITE start` / `TEST start` / `TEST end` lines on stderr so a glibc `soffice.bin` abort still names the last native test. `test_read_range_format_info_performance` uses a 40×40 grid (not 100×100) so the default suite does not copy tens of 10k-cell PyUNO arrays.

`LO_PYTHON` is LibreOffice's bundled interpreter on Windows (`program/python.exe`) and macOS (`LibreOfficePython.framework/.../python3`). Linux probes `python3` then `python` for a working `import uno`. There is no silent fallback to the project `.venv`: that CPython 3.13 cannot host pyuno and segfaults during import on macOS (CI 33447724981, ~150ms, no output). If nothing is found, `make test-uno` fails with the paths it looked for (`_check-lo-python`).

Opt-in hang diagnostics (`WRITERAGENT_CI_DEBUG=1`, or PR CI `workflow_dispatch` `ci_debug`): per-worker `start`/`end` nodeid trail + faulthandler dump at 240s under `WRITERAGENT_CI_DEBUG_DIR`, and `--max-worker-restart=0` so xdist prints the crashitem nodeid instead of replacing the worker and wedging on re-collection (Windows CI 33447705893: gw3 vanished at 261s with no `Failed: Timeout`; pytest-timeout did not fire). `pytest_serial` sets `PYTEST_WORKERS=0`. This is instrumentation, not a hang fix.

Profile hotspots without the LO native suite:

```bash
make test-durations   # same marker + ignore-glob filter + --durations=40
```

Suite volume (~4500 tests) is cheap; wall time is dominated by real process spawns, intentional concurrency sleeps, and a few heavy-file tests—not collection or the root autouse config isolation fixture.

---

### 4. Recommendations & Next Steps

1.  **Do Not Add Pytest-Cov:** Adhere to the project rule against arbitrary coverage tracking. Focus on testing critical behaviors and regressions rather than chasing a 90% line-coverage metric.
2.  **Standardize Mocking:** Enhance `testing_utils.py` to include generic factory functions for simulating LLM streaming responses. This will reduce boilerplate in the pytest suite when testing how the system handles chunked tool-call arguments.
3.  **Refactor Test Modules:** As testing grows, ensure native test files remain modular. Avoid giant monolithic test files; split them by feature (e.g., `test_writer_tables.py`, `test_writer_styles.py`) while keeping them registered in `testing_runner.py`.
4.  **Continuous Integration:** If not already present, wrap `python -m plugin.testing_runner` in a headless CI job (e.g., GitHub Actions using a base LibreOffice Docker image) to ensure UNO integration tests are run automatically on every commit.

**Conclusion:** The dual-runner architecture is the right approach for a LibreOffice extension. Our focus moving forward should be attacking the boundaries (network I/O, LLM parsing, UNO edge cases) rather than rewriting the testing framework itself.