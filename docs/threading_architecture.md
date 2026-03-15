# WriterAgent Threading Architecture

This document outlines the threading and concurrency model used within the WriterAgent project (located in the `plugin/` directory). It details how backgrounds tasks, asynchronous network communication, streaming LLM execution, and external process management are handled without blocking the LibreOffice/UNO main UI thread.

## Overview

The LibreOffice UNO environment is **not thread-safe**. Calling UNO API methods from background threads can lead to unexpected UI behavior, corruption, or outright crashes, particularly with complex documents or frequent UI updates.

Because WriterAgent connects to external LLM services and relies on streaming responses, it cannot block the main UI thread during these network calls or when waiting for AI generation. Therefore, WriterAgent relies heavily on standard Python threading for asynchronous I/O and process monitoring, coupled with specific mechanisms to marshal results back to the UNO main thread when document manipulation or UI updates are required.

## Threading Components

### 1. Main Thread Dispatch (`plugin/framework/main_thread.py`)

This is the core concurrency bridge. Because background threads (like the HTTP server or AI streaming loop) cannot safely execute UNO commands, they use `execute_on_main_thread(fn, *args, **kwargs)` to offload UNO interactions back to the main thread.

*   **Mechanism:** It pushes a `_WorkItem` containing the callable and arguments onto a `queue.Queue`. It then signals LibreOffice to wake up and process the queue using `com.sun.star.awt.AsyncCallback`.
*   **Synchronization:** The calling background thread blocks on a `threading.Event()` (`_WorkItem.event.wait()`) until the main thread picks up the item, executes it, and sets the result or exception. This provides a synchronous feel to the caller while executing safely on the UI thread.
*   **Safety:** A `threading.Lock` (`_init_lock`) protects the lazy initialization of the AsyncCallback UNO service.

### 2. HTTP Server and MCP Protocol (`plugin/modules/http/`)

The plugin runs an embedded HTTP server to provide a local API and support the Model Context Protocol (MCP).

*   **`server.py`:** The `HTTPServer` runs in a dedicated daemon thread (`name="http-server"`) via `self._thread = threading.Thread(target=self._run, daemon=True)`. This allows the server to perpetually listen for incoming requests without blocking LibreOffice.
*   **`mcp_protocol.py`:** Incoming HTTP requests land on the server's thread. To execute tool logic (which requires UNO access), the MCP handlers push work to the main thread.
    *   **Synchronization:** It implements its own simple `_Future` class wrapping a `threading.Event` to wait for main-thread execution, similar to `main_thread.py`.
    *   **Concurrency limits:** A `threading.Semaphore(1)` (`_tool_semaphore`) is used to enforce backpressure, ensuring only one tool execution runs concurrently.

### 3. Agent Backends and CLI Management (`plugin/modules/agent_backend/`)

When interacting with external CLI-based agent tools (like Hermes), WriterAgent spawns background processes and needs to monitor their streams asynchronously.

*   **`cli_backend.py`:** Manages the lifecycle of CLI tools.
    *   **Threads:** It spawns `_reader_thread` (monitoring `stdout`) and `_stderr_thread` (draining `stderr`) so that the main application isn't blocked reading from pipes.
    *   **Synchronization:** Uses `threading.Lock` to protect internal state (like the process reference). It uses `threading.Event` (`_reader_ready`, `_response_done`) to signal when the backend is ready to accept input or has finished generating a response.
*   **`hermes_proxy.py`:** Implements the Actor Context Protocol (ACP) over standard streams.
    *   **Threads:** Spawns a dedicated daemon thread `hermes-acp-reader` to continuously parse JSON-RPC messages from the subprocess stdout.
    *   **Synchronization:** Uses a `threading.Lock` to protect the `_pending` requests dictionary. Each outbound request creates a `threading.Event` which the caller waits on until the reader thread receives the corresponding response and sets the event.

### 4. Chatbot Streaming and Tool Execution (`plugin/modules/chatbot/`)

The core chatbot interaction relies heavily on threads to handle streaming LLM responses and asynchronous tool executions.

*   **`send_handlers.py`:** When a user sends a message, handlers (like `run_agent`, `run_search`, `run_direct_image`) are wrapped in a daemon `threading.Thread` to prevent blocking the UI while calling external APIs.
*   **`tool_loop.py`:** Manages the ReAct (Reasoning and Acting) loop.
    *   **Threads:** Uses `threading.Thread` to run `run_async` (spawning background LLM generation), `run` (evaluating tool responses), and `run_final` (handling the final no-tools stream).
    *   This architecture allows the UI to stay responsive while the system generates text chunk-by-chunk or waits for API responses.

### 5. Utilities, UI Updates, and Monitoring

*   **`plugin/framework/async_stream.py`:** Provides an `async_stream` decorator and helper functions that automatically wrap generator functions (like streaming network calls) in a `threading.Thread(target=worker, daemon=True)`. The worker consumes the stream and periodically calls a main-thread UI update function.
*   **`plugin/main.py`:** Uses background threads to pre-load icons into the `ImageManager` (`_update_menu_icons`) and dispatch menu updates (`notify_menu_update`) without freezing the startup or dispatch sequence.
*   **`plugin/modules/tunnel/__init__.py`:** Runs local tunneling tools (like ngrok or localtunnel) in a `threading.Thread(target=self._run_and_parse, daemon=True)` to parse the tunnel URL from the subprocess output asynchronously. Uses a `threading.Lock()` to protect access to the `_process` and tunnel URL.
*   **`plugin/modules/launcher/__init__.py`:** Spawns a `launcher-monitor` thread to `wait()` on launched external processes (like Claude or Gemini desktop apps) so the menu status can be updated when the user closes the external app. Uses `threading.Lock()` to protect process references.
*   **`plugin/framework/logging.py`:** Spawns a background thread (`_watchdog_loop`) to periodically flush status logs or monitor system health without interrupting document flow. Uses `_init_lock` and `_activity_lock` to protect logging state.
*   **`plugin/framework/dialogs.py`:** Spawns a probe update thread (`threading.Thread(target=_probe_update, daemon=True)`) to dynamically update dialog UI elements in the background.

---

## Duplication and Consolidation Recommendations

While the threading model successfully prevents blocking the main UNO thread, there are several areas where duplicate concurrency patterns have evolved independently. Consolidating these will improve maintainability, reduce edge-case bugs, and simplify the codebase.

### 1. Unified Background Process Monitoring
**Where:** `launcher/__init__.py` (waiting for exit), `tunnel/__init__.py` (running and parsing output), `agent_backend/cli_backend.py` (reader loops).
**Issue:** Multiple modules manually spawn `subprocess.Popen` and immediately wrap it in a `threading.Thread` to monitor stdout/stderr or wait for the process to exit.
**Recommendation:** Create a unified `plugin/framework/process_manager.py` that provides a generic `AsyncProcess` class. This class should encapsulate process spawning, thread-based stream monitoring (via callbacks or a unified queue), and exit handling, replacing the bespoke implementations in launcher, tunnel, and agent backends.

### 2. Main Thread Execution (`main_thread.py` vs `mcp_protocol.py`)
**Where:** `framework/main_thread.py` and `modules/http/mcp_protocol.py`.
**Issue:** Both implement near-identical logic for pushing work to the main thread and waiting on a `threading.Event` (`_WorkItem` vs `_Future`).
**Recommendation:** Refactor `mcp_protocol.py` to exclusively use `execute_on_main_thread` from `main_thread.py` for its UNO tool calls, or expose the internal `_Future`/Queue mechanism of `main_thread.py` if the MCP server needs tighter control over queue state. The custom `_Future` implementation in `mcp_protocol.py` should be removed.

### 3. Asynchronous Worker Spawning
**Where:** `modules/chatbot/tool_loop.py`, `modules/chatbot/send_handlers.py`, `framework/async_stream.py`, `main.py`.
**Issue:** Raw `threading.Thread(target=..., daemon=True).start()` is scattered throughout the codebase for "fire-and-forget" tasks (like updating menu icons or starting a chatbot run). If these threads throw unhandled exceptions, they die silently (except for what might be caught by custom excepthooks).
**Recommendation:** Implement a central `plugin/framework/worker_pool.py` or a managed `run_in_background(func, *args, error_callback=None)` function. This would standardize exception handling, logging, and potentially allow for graceful shutdown tracking of background workers, rather than having raw thread instantiation scattered across 10+ files.

### 4. Streaming Execution Wrappers
**Where:** `tool_loop.py` (`_start_tool_calling_async`, `_spawn_final_stream`) and `send_handlers.py` manually spawn threads to interact with `queue.Queue` objects, while `async_stream.py` provides a very similar abstraction via its decorator.
**Issue:** `chatbot/` components manually manage threads and queues to stream UI updates, bypassing the generic `async_stream` wrapper which was built for this purpose.
**Recommendation:** Evaluate adapting the ReAct and final generation loops in `chatbot/` to utilize the standard `async_stream.py` mechanism (or a slightly upgraded version of it). Consolidating how streams feed into the UI will make the complex ReAct loop significantly easier to read and debug.
