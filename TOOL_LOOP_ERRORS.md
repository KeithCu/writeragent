# Chat Tool Loop Error Handling Strategy

This document outlines the error handling strategy implemented in `plugin/modules/chatbot/tool_loop.py` to prevent session crashes and improve the user experience.

## Overview
The chat tool loop is a critical component that interacts with the LibreOffice document, the AI provider, and various tools. Due to its complex interactions, robust error handling is required to manage unexpected failures gracefully.

## Key Error Handling Improvements

### 1. Tool Execution
- **Specific Catch**: `ToolExecutionError` and `UnoObjectError` are caught specifically during tool execution (`execute_fn`). This allows for targeted logging and payload formatting.
- **Contextual Logging**: When an error occurs during tool execution, it is logged using `log.error` with specific context metadata (`extra={"context": "tool_execution"}`). Additionally, `agent_log` is used to capture the error type and message for deeper analysis.
- **Unexpected Error Wrapping**: Any unexpected exception is caught and wrapped in a new `ToolExecutionError` with a generic "Unexpected error executing tool" message. The original error details are preserved in the `details` payload attribute for debugging.

### 2. Document Context
- **Handling Disposed Documents**: Reading the document context can fail if the user closes the document mid-session. This is now specifically handled by catching `UnoObjectError`. The user is presented with a clear "[Document closed or unavailable.]" message, and the loop transitions gracefully to an error state without crashing.
- **Unexpected Error Wrapping**: Similar to tool execution, any other exception during document context retrieval is wrapped in an `UnoObjectError` with the code `DOCUMENT_CONTEXT_ERROR` and a generic fallback message, preserving the original error for debugging.

### 3. Audio Handling
- **Specific IO Handling**: Reading the audio file (when voice input is used) can fail due to disk issues or missing files. This is handled by specifically catching `IOError` and `OSError`.
- **Non-Fatal Degradation**: When an audio handling error occurs, the loop does *not* crash or transition to an error state. Instead, it logs the error, preserves the audio file path for debugging if possible, and falls back to sending only the text query.
- **Unexpected Error Handling**: Unexpected exceptions during audio handling are also caught, preventing crashes and allowing the session to continue with just the text query.

## Implementation Details
These improvements utilize the centralized exception hierarchy defined in `plugin/framework/errors.py`.
- `format_error_payload()` is used to ensure all errors returned from tool execution match the expected JSON schema.
- Custom exceptions like `ToolExecutionError` and `UnoObjectError` ensure that the UI can distinguish between different failure modes.