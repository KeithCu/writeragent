# Plan for Code Quality Improvements

This plan outlines steps to improve the simplicity, reliability, and robustness of the LocalWriter codebase without introducing new features.

## Proposed Changes

### Core Architecture & Utility Refactoring

#### [NEW] [uno_utils.py](core/uno_utils.py)
Centralize common UNO patterns to reduce code duplication and improve robustness.
- Helper for getting the current document and frame.
- Helper for creating cursors and handling offset/length limits (e.g., `goRight` chunks).
- Helper for creating common UNO structs like `PropertyValue`.

#### [MODIFY] [config.py](core/config.py)
- Improve `set_config` with basic file locking or a retry mechanism to prevent race conditions during concurrent writes.
- Add stricter validation for configuration keys and types.

#### [MODIFY] [api.py](core/api.py)
- Refactor `stream_request` and `stream_request_with_tools` to share a common streaming engine.
- Pull out SSL context creation into a configurable option (allow users to enable/disable verification).
- Standardize reasoning/thinking token extraction to handle more provider formats.

### Error Handling & Logging

#### [NEW] [exceptions.py](core/exceptions.py)
Define a hierarchy of custom exceptions (e.g., `LocalWriterError`, `ApiError`, `DocumentError`) to replace generic `Exception` usage.

#### [MODIFY] [logging.py](core/logging.py)
- Standardize log formats and ensure context is consistently applied.
- Ensure efficient resource usage (e.g., closing file handles properly).

### Decoupling UI and Logic

#### [NEW] [chat_manager.py](core/chat_manager.py)
Extract the core chat logic (history management, tool execution loop, state tracking) from `chat_panel.py`.
- This allows for easier testing of the chat logic without a live LibreOffice environment.
- Simplifies `chat_panel.py` to focus on UI events and rendering.

#### [MODIFY] [chat_panel.py](chat_panel.py)
- Delegate logic to `ChatManager`.
- Simplify button state and status management using a more declarative approach if possible.

### Document & Format Support

#### [MODIFY] [document.py](core/document.py)
- Use centralized `uno_utils` for document interactions.
- Refine character offset calculations for better reliability.

#### [MODIFY] [format_support.py](core/format_support.py)
- Improve the `_with_temp_buffer` context manager to be even more defensive about cleanup.
- Refactor search candidate generation for clarity.

## Verification Plan

### Automated Tests
- Expand `tests/test_chat_model_logic.py` to test the new `ChatManager` in isolation.
- Add unit tests for `core/config.py` and `core/api.py` (mocking network).
- Ensure all existing tests pass.

### Manual Verification
- Test all major features (Extend, Edit, Chat) in LibreOffice Writer to ensure no regressions.
- Verify that error messages are correctly displayed in the UI when the API is unreachable.
- Check log files for consistency and completeness.
