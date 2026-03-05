# MCP Protocol — Status and Future Work

## What Is This?

MCP (Model Context Protocol) is a standard for exposing tool sets to external AI clients
(Claude Desktop, Cursor, custom scripts, etc.) over HTTP. The `libreoffice-mcp-extension/`
directory in this repo is an existing standalone extension that implements a similar HTTP API
for LibreOffice.

LocalWriter now includes an **MCP HTTP server** built in: users who install LocalWriter can
use it as an embedded AI editing tool (the sidebar) **and** as a source of document tools
for external AI clients. This document describes what was implemented, how it works, and
what to consider doing next.

---

## Current Status — What Was Implemented

The MCP server is **implemented and opt-in** (default off). Summary:

- **`core/mcp_thread.py`**: `_Future`, `execute_on_main_thread()`, `drain_mcp_queue()`. Work from HTTP handler threads is queued and executed on LibreOffice’s main thread.
- **`core/mcp_server.py`**: HTTP server on localhost; GET `/health`, `/`, `/tools`, `/documents`; POST `/tools/{name}`. Port utilities: `_probe_health`, `_is_port_bound`, `_kill_zombies_on_port`.
- **Idle-time draining**: **`AsyncCallback` thread** in `main.py`. A background Python thread loops and queues an `XCallback` invocation via `com.sun.star.awt.AsyncCallback` every 100ms, which safely executes `drain_mcp_queue()` on the main VCL thread. Option of piggybacking on the chat stream drain loop was **not** used — it would only service MCP during active chat, which is inadequate for standalone MCP use.
- **Document targeting**: **`X-Document-URL`** HTTP header. The server resolves the target document by iterating `desktop.getComponents()` and matching `getURL()` to the header. If the header is absent, it falls back to the active document. `GET /documents` returns all open documents with URLs and types so clients can discover targets. This avoids races when multiple documents or users are involved; “active document only” was not used.
- **Config**: `mcp_enabled` (default false), `mcp_port` (default 8765). Documented in `core/config.py`.
- **Settings**: MCP section on **Page 1** of the Settings dialog (no separate tab): “Enable MCP Server” checkbox, Port field, “Localhost only, no auth.” label. Dialog layout was compacted so short fields share rows and the OK button sits at the bottom with minimal gap.
- **Menu**: “Toggle MCP Server” and “MCP Server Status” under LocalWriter. Status dialog shows RUNNING/STOPPED, port, URL, and health check.
- **Auto-start**: When the user saves Settings with MCP enabled, the server (and timer) start if not already running.
- **Icons**: Six PNGs copied from `libreoffice-mcp-extension/icons/` to `assets/` (for possible future dynamic menu icons).
- **Import fix**: `XTimerListener` is imported only inside `_start_mcp_timer()` so that the Python loader can load `main.py` for registry info without requiring UNO.

See **AGENTS.md** (Section “MCP Server — DONE”) and the code in `main.py`, `core/mcp_thread.py`, and `core/mcp_server.py` for details.

---

## What Had Already Been Done (Writer Tools, Pre-MCP)

Before building the MCP server itself, the Writer tool set was expanded so that LocalWriter's
embedded AI (and future MCP clients) have a richer set of operations to work with.

### New file: `core/writer_ops.py`

Ported from `libreoffice-mcp-extension/pythonpath/uno_bridge.py` and adapted to LocalWriter's
`(model, ctx, args) → JSON string` function signatures. Contains both implementations and
`WRITER_OPS_TOOLS` schemas (OpenAI function-calling format).

**12 new Writer tools in 4 groups:**

| Group | Tools |
|---|---|
| Styles | `list_styles`, `get_style_info` |
| Comments | `list_comments`, `add_comment`, `delete_comment` |
| Track changes | `set_track_changes`, `get_tracked_changes`, `accept_all_changes`, `reject_all_changes` |
| Tables | `list_tables`, `read_table`, `write_table_cells` |

### Updated: `core/document_tools.py`

- Removed 7 legacy unused functions (all superseded by `apply_document_content` /
  `get_document_content` / `find_text`).
- Imports `WRITER_OPS_TOOLS` from `writer_ops.py` and adds all 12 new functions to
  `TOOL_DISPATCH`. `WRITER_TOOLS` went from 5 tools to 17.

### Updated: `core/constants.py`

`DEFAULT_CHAT_SYSTEM_PROMPT` updated to list the new tool groups so the embedded AI knows
they exist.

---

## Current State of the Standalone Extension

The standalone `libreoffice-mcp-extension` works but has a critical missing dependency:

```python
# ai_interface.py line 17 — this module does not exist in the repo
from main_thread_executor import execute_on_main_thread
```

All UNO calls must happen on LibreOffice's VCL main thread. The HTTP server runs on a
background thread. Without `main_thread_executor`, the HTTP handler has no safe way to call
UNO APIs.

This is the central engineering problem. Everything else (HTTP routing, tool dispatch, config)
is straightforward.

---

## How Clients Discover Tools and Context (implemented)

### Endpoints

The HTTP server exposes:

```
GET /health     → {"status": "ok", "name": "LocalWriter MCP"}   — alive check (identifies our server)
GET /tools      → {"tools": [...], "count": N}  — tool list for target document (use X-Document-URL or active doc)
GET /           → {"name": "LocalWriter", "instructions": "...", "tools_count": N}  — system prompt for target document
GET /documents  → {"documents": [{"url": "...", "type": "writer"|"calc"|"draw"}, ...]}  — list open documents for X-Document-URL
POST /tools/{name}  → JSON result  — execute tool (send X-Document-URL header to target a document)
```

`GET /tools` returns the tool list for the **target document** (from `X-Document-URL` header
or the active document). Each tool has `name`, `description`, and `parameters` (JSON Schema).

`GET /` returns **agent instructions** (system prompt) for the target document. LocalWriter
uses `get_chat_system_prompt_for_document(doc)` from `core/constants.py` — Writer, Calc, and
Draw each have the right prompt. No separate AGENT.md file; the instructions are built in. The same prompts used by the
sidebar chat (tool usage, formatting rules, workflow) are served to external clients.

---

### Critical distinction: "MCP-inspired" vs real MCP protocol

The extension's HTTP API is **not** the Anthropic MCP specification. Real MCP uses:
- **Transport**: JSON-RPC 2.0 over **stdio** (Claude Desktop spawns the server as a
  subprocess) or **SSE** (server-sent events over HTTP)
- **Methods**: `initialize`, `tools/list`, `tools/call`, `resources/list`, `prompts/list`
- **Discovery**: Claude Desktop reads `~/.config/claude/claude_desktop_config.json` which
  lists MCP servers by command path

What the extension implements (and what LocalWriter implements) is a simpler custom
HTTP REST API. Claude Desktop **cannot** talk to it natively — it expects stdio/SSE.

**However**, Cursor's MCP support does accept HTTP-based servers. And any custom AI client
or script can call the REST API directly with plain `curl` or `requests`.

For genuine Claude Desktop integration, two paths exist:

**Path A — stdio proxy script** (~30 lines, no changes to LocalWriter):

```python
#!/usr/bin/env python3
# mcp_proxy.py — stdio MCP adapter for LocalWriter's HTTP server
import sys, json, requests

def main():
    for line in sys.stdin:
        req = json.loads(line)
        method = req.get("method")
        if method == "tools/list":
            r = requests.get("http://localhost:8765/tools")
            tools = r.json()["tools"]
            reply = {"id": req["id"], "result": {"tools": tools}}
        elif method == "tools/call":
            name = req["params"]["name"]
            args = req["params"].get("arguments", {})
            r = requests.post(f"http://localhost:8765/tools/{name}", json=args)
            reply = {"id": req["id"], "result": {"content": [{"type": "text", "text": json.dumps(r.json())}]}}
        else:
            reply = {"id": req["id"], "result": {}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    main()
```

Register in `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "localwriter": {
      "command": "python3",
      "args": ["/path/to/mcp_proxy.py"]
    }
  }
}
```

**Path B — implement JSON-RPC directly** (~40 extra lines in `core/mcp_server.py`):

Add an `initialize` handler and change `GET /tools` to respond to `POST /` with
`method=tools/list`. More robust but more work.

**Recommendation**: Start with Path A. The proxy is trivial to write and keeps the HTTP
server simple. Path B is an optimization once Path A is validated to work.

---



```
External AI client (Claude Desktop, Cursor, etc.)
        |
        | HTTP POST /tools/list_tables  + header X-Document-URL: file:///path/to/doc.odt
        v
  HTTPServer thread (background)
  MCPHandler.do_POST()
        |
        | put (func, args, future) on _mcp_queue; future.result(timeout=30)  <-- blocks HTTP thread
        v
  AsyncCallback Thread (loops every 100ms)
  Adds XCallback to LibreOffice main thread message queue
        |
  Main UI Thread (VCL event loop)
  drain_mcp_queue()
        |
        | _resolve_document(ctx, X-Document-URL) -> doc; execute_tool(tool_name, args, doc, ctx)
        | future.set_result(json_result)
        v
  HTTP thread unblocks, returns JSON to client
```

The key insight: **`com.sun.star.awt.AsyncCallback` safely executes code on the main UI thread**. 
By having a background Python thread repeatedly schedule an `XCallback`, we guarantee that `drain_mcp_queue()` is invoked on the correct VCL thread without locking up the UI or hitting OS-level thread-safety violations.

#### Why not a UNO Timer, Direct Dispatch, or UI Hacks?
*(Preserved from previous implementation documents)*
- **UNO Timer**: Using `com.sun.star.util.XTimerListener` fails to initialize. The LibreOffice system Python environment where the extension runs lacks the `com` package, and `uno.getTypeByName` fails to recognize the type.
- **Direct Dispatch**: Calling `DispatchHelper.executeDispatch` directly from the background thread causes a fatal "Operation not supported on this operating system" exception because GUI methods must strictly execute on the originating VCL thread.
- **UI Hacks**: We previously attempted to drain the MCP queue during active chat stream loops or sidebar layout recalculations (e.g., `getHeightForWidth`). However, this meant the MCP server would hang and time out whenever the user was idle.

`AsyncCallback` provides the only robust, thread-safe, and idle-friendly mechanism for this environment.

---

## Existing Pattern to Reuse

LocalWriter already has the correct threading pattern in `core/async_stream.py`:

- **Worker thread** puts items on a `queue.Queue`.
- **Main thread** runs `run_stream_drain_loop()` — a `while not job_done` loop that calls
  `q.get(timeout=0.1)` and `toolkit.processEventsToIdle()` on each tick.

This IS `main_thread_executor`. Do not reinvent it. The `_Future` class and
`execute_on_main_thread()` are thin additions on top of this existing pattern:

```python
# core/mcp_thread.py — thin wrapper around the existing queue pattern (~40 lines)
import threading, queue

_mcp_queue = queue.Queue()

class _Future:
    def __init__(self):
        self._event = threading.Event()
        self._result = None
        self._exc = None

    def set_result(self, v):   self._result = v; self._event.set()
    def set_exception(self, e): self._exc = e;   self._event.set()

    def result(self, timeout=30.0):
        if not self._event.wait(timeout):
            raise TimeoutError("UNO main-thread call timed out")
        if self._exc:
            raise self._exc
        return self._result

def execute_on_main_thread(func, *args, timeout=30.0):
    future = _Future()
    _mcp_queue.put((func, args, future))
    return future.result(timeout=timeout)

def drain_mcp_queue(max_per_tick=5):
    """Drain pending MCP requests. Called on the main thread."""
    for _ in range(max_per_tick):
        try:
            func, args, future = _mcp_queue.get_nowait()
        except queue.Empty:
            break
        try:
            future.set_result(func(*args))
        except Exception as e:
            future.set_exception(e)
```

### Idle-Time Draining (implemented: AsyncCallback)

The existing drain loop in `run_stream_drain_loop` only runs **during an active chat send**.
Between user interactions, the main thread is in LibreOffice’s VCL event loop, so MCP requests
would never be serviced if we only drained there.

**Implemented: AsyncCallback Thread.** A background thread in `main.py` loops (100ms, repeating) and schedules `drain_mcp_queue()` on the main thread using `com.sun.star.awt.AsyncCallback`. The listener class and `XCallback` import are defined inside `_start_mcp_timer()` so the module can load without UNO (e.g. for registry writing). See `main.py` for the exact code.

**Piggybacking on the chat drain loop was not used.** Servicing MCP only during active chat would break standalone use (e.g. external client with no sidebar chat). So we use the AsyncCallback thread only.

### Reference: `core/mcp_server.py` (implemented)

Thin HTTP server that reuses `execute_tool()`, `execute_calc_tool()`, and `execute_draw_tool()`.
The **actual implementation** in `core/mcp_server.py` uses `_resolve_document(ctx, X-Document-URL header)` to target a document by URL (or active document if header is absent), and implements GET `/documents`, GET `/`, GET `/tools`, GET `/health`, and POST `/tools/{name}` with CORS. The sketch below shows the dispatch pattern; document resolution is via header in the real code.

See `core/mcp_server.py` for the full implementation. Dispatch pattern: `_resolve_document(ctx, X-Document-URL)` returns `(doc, doc_type)`; then call `execute_calc_tool`, `execute_draw_tool`, or `execute_tool` accordingly. All run via `execute_on_main_thread(_run, timeout=30)`.

---

## Tool List for External Clients

When the MCP server is enabled, external clients will see all tools that LocalWriter exposes
to its own embedded AI:

**Writer**: `get_document_content`, `apply_document_content`, `find_text`,
`list_styles`, `get_style_info`, `list_comments`, `add_comment`, `delete_comment`,
`set_track_changes`, `get_tracked_changes`, `accept_all_changes`, `reject_all_changes`,
`list_tables`, `read_table`, `write_table_cells`, `generate_image`, `edit_image`.

**Calc**: All `CALC_TOOLS` from `core/calc_tools.py`.

**Draw**: All `DRAW_TOOLS` from `core/draw_tools.py`.

The server resolves the target document via the **`X-Document-URL`** header (or active
document if absent) and routes to the correct dispatcher by type.

---

## Document Targeting (implemented)

When multiple documents are open, the server does **not** rely on “active document” only —
that would race with focus and multiple users. **Implemented: `X-Document-URL` header.**

- The client sends the document URL in the `X-Document-URL` HTTP header (e.g. from `GET /documents`).
- The server iterates `desktop.getComponents()` and matches `doc.getURL()` to the header value.
- If the header is missing, the server falls back to `desktop.getCurrentComponent()` for simple single-document use.

No tool schema changes; targeting is at the transport layer. Optional per-call `file_path` (or similar) can be considered later if needed.

---

## Security Notes

- Bind to `localhost` only. Never expose to external interfaces by default.
- No authentication is implemented. Any process on the local machine can call the tools.
  Acceptable for a developer/power-user tool; document this clearly.
- The HTTP server should be **opt-in** (`mcp_enabled: false` default). Auto-start should
  require the user to enable it in Settings.

---

## What to Reuse from `libreoffice-mcp-extension/`

### `registration.py` — The Most Valuable Non-UNO File

This file contains several production-quality pieces that would take time to write from
scratch and should be copied nearly verbatim (adapting identifier strings only).

---

#### 1. Port management utilities (~60 lines) — copy verbatim

These three functions handle the full port lifecycle. Copy them into `core/mcp_server.py`:

```python
def _probe_health(host, port, timeout=2):
    """Probe /health endpoint. Returns True if OUR server responds."""
    # Uses http.client.HTTPConnection — no extra dependencies.
    # Checks for "LocalWriter MCP" in response body to distinguish from
    # other HTTP servers on the same port.

def _is_port_bound(host, port, timeout=1):
    """Returns True if anything at all is listening on host:port."""

def _kill_zombies_on_port(host, port):
    """Kill processes bound to the port that aren't our server (Windows only).
    On Linux just verifies the port is free. Safe to call on all platforms."""
```

Why you need these: without them, starting the server when the port is already bound
silently fails or throws an unhelpful `OSError: [Errno 98] Address already in use`.
The zombie killer is especially important on Windows where sockets linger after crashes.

---

#### 2. Dynamic menu state (~60 lines) — copy and adapt

The menu item that says "Start Server" when stopped and "Stop Server" when running, with
a "Starting..." transitional state. This uses the standard LibreOffice `XDispatch`
status-listener pattern:

```python
_STATE_STOPPED = "stopped"
_STATE_STARTING = "starting"
_STATE_RUNNING  = "running"
_server_state   = _STATE_STOPPED
_status_listeners_lock = threading.Lock()
_status_listeners_list = []   # [(listener, url), ...]

def _set_server_state(new_state): ...         # updates state + notifies listeners
def _notify_all_listeners(): ...              # pushes FeatureStateEvent to all
def _fire_status_event(listener, url, text): # sends one FeatureStateEvent

# On the dispatch handler class:
def addStatusListener(self, listener, url): ...
def removeStatusListener(self, listener, url): ...
```

Adapt: change the command URL prefix from `org.mcp.libreoffice:` to
`org.extension.localwriter:`. The rest is identical.

---

#### 3. Status dialog (~80 lines) — copy nearly verbatim

`_do_status()` builds a small programmatic dialog that shows version, host:port, autostart
flag, and a live health-check result. The health check runs in a background thread and
updates the dialog label while it is open — a clean UX pattern:

```python
def _do_status(self):
    # Shows: "MCP Server: STARTED / STOPPED"
    # "Version: ...", "Port: ...", "Autostart: ..."
    # "Health check: probing..." → updated to "OK" or "FAIL" from background thread
```

The programmatic dialog approach (creates controls via UNO service manager, no XDL file
needed) is fine here because it is small and entirely informational.

---

#### 4. `MCPAutoStartJob` (~25 lines) — copy verbatim

```python
class MCPAutoStartJob(unohelper.Base, XJob, XServiceInfo):
    """Triggered by onFirstVisibleTask — starts MCP server at LO launch."""
    def execute(self, args):
        if _config.get("mcp_enabled", False):
            threading.Thread(target=_start_mcp_server, daemon=True).start()
        return ()
```

Adapt: use LocalWriter's existing `localwriter.json` config key `mcp_enabled` instead of
the LO native registry. Register this in `META-INF/manifest.xml` alongside LocalWriter's
existing jobs. The `onFirstVisibleTask` trigger is already used by the standalone extension
and does not conflict.

---

#### 5. Icons — copy directly

The six icon files in `libreoffice-mcp-extension/icons/` can be copied into LocalWriter's
`assets/` folder:

- `running_16.png` / `running_26.png`
- `starting_16.png` / `starting_26.png`
- `stopped_16.png` / `stopped_26.png`

Reference them in `Addons.xcu` for the MCP menu item the same way the standalone extension
does. The `_load_icon_graphic()` / `_update_menu_icons()` functions in `registration.py`
show how to inject them into the module `ImageManager` for dynamic icon switching — though
note that `_update_menu_icons` is currently disabled in the standalone extension (see the
`return` at line 325) due to a suspected black-menu rendering bug on some platforms.
Start with static icons in `Addons.xcu` and add dynamic switching later.

---

#### 6. Menu entries — adapt from `Addons.xcu`

Add a `MCP Server` submenu under LocalWriter's existing `LocalWriter` top-level menu:

```xml
<node oor:name="N003" oor:op="replace">
  <prop oor:name="URL"><value>org.extension.localwriter:toggle_mcp_server</value></prop>
  <prop oor:name="Title"><value xml:lang="en-US">Start MCP Server</value></prop>
  <!-- icon: assets/stopped_16.png -->
</node>
<node oor:name="N004" oor:op="replace">
  <prop oor:name="URL"><value>org.extension.localwriter:mcp_status</value></prop>
  <prop oor:name="Title"><value xml:lang="en-US">MCP Server Status</value></prop>
</node>
```

Add the corresponding dispatch cases to LocalWriter's existing `trigger()` / dispatch
handler in `main.py`. No new UNO component registration needed — these commands go through
LocalWriter's existing `XDispatch` implementation.

---

#### 7. `MCPOptionsHandler` — optional, consider skipping

The standalone extension registers a `Tools > Options > MCP Server` page via
`XContainerWindowEventHandler`. This is more work to integrate (requires `OptionsDialog.xcu`
and `MCPServerConfig.xcs/xcu`) and uses the LO native config registry rather than
LocalWriter's `localwriter.json`.

**Recommendation**: skip this. Instead, add a new "MCP Server" tab to LocalWriter's existing
`LocalWriterDialogs/SettingsDialog.xdl` (which already uses the `dlg:page` multi-page
approach). The config reads/writes go through the existing `get_config()` / `set_config()`
in `core/config.py`. This is ~60 lines of XDL and ~30 lines of Python, consistent with how
LocalWriter already handles settings.

---

### Other Files

| File | Action |
|---|---|
| `uno_bridge.py` | Reference for future UNO operations (heading tree, text frames). Already covered in AGENTS.md. |
| `ai_interface.py` | HTTP server structure and CORS headers — rewrite as `core/mcp_server.py` (simpler, no `get_mcp_server()` indirection). |
| `mcp_server.py` | Tool schema catalog — cherry-pick when adding future Writer/Calc tools. |
| `MCPServerConfig.xcs/xcu` | Skip — LocalWriter uses `localwriter.json`. |
| `OptionsDialog.xcu` | Skip — use LocalWriter's existing Settings dialog tab instead. |
| `dialogs/MCPSettings.xdl` | Reference only — adapt controls into LocalWriter's SettingsDialog.xdl. |
| `description.xml` | Skip — different extension identity. |
| `Addons.xcu` (theirs) | Reference for menu XML structure — adapt to `org.extension.localwriter:` URLs. |
| `ProtocolHandler.xcu` (theirs) | Skip — LocalWriter already has its own protocol handler. |

---

## Tool Description and System Prompt Analysis

The standalone extension has no `AGENT.md` (the file doesn't exist — `GET /` returns empty
instructions). So this comparison is entirely about tool `description` strings in
`mcp_server.py` vs LocalWriter's descriptions in `core/writer_ops.py`,
`core/format_support.py`, and `core/constants.py`.

---

### What they do well (worth adopting)

#### 1. Behavioral guarantees in the description line

Their descriptions often embed a critical behavioral note directly in the one-line summary:

```
"Find and replace text (preserves formatting)"
"Replace the entire text of a paragraph (preserves style)"
"Duplicate a paragraph (with style) after itself."
```

LocalWriter's `apply_document_content` with `target="search"` automatically preserves
character-level formatting (fonts, colors, bold, background colors) when the replacement is
plain text — but the description doesn't say so. An AI that doesn't know this will
unnecessarily re-specify formatting it read from the document, or avoid the `search` target
when it's the right choice.

**Suggested addition** to `apply_document_content` description:

> "Plain-text replacements via `target='search'` automatically preserve all character
> formatting (bold, color, font, etc.) on the replaced text."

#### 2. Explaining the "why" of a feature

Their `resolve_bookmark` says "(bookmarks are stable across edits)" — this tells the AI
*why* it should prefer bookmarks over paragraph indices. The reason matters more than the
mechanism.

LocalWriter doesn't have the bookmark/locator system yet, but the same principle applies
to existing descriptions. For example, `list_styles` says "they may be localized" — that's
good. The `find_text` description mentions "LO strips search string to plain to match" — that
explains a gotcha that would otherwise produce confusing failures. This is the right instinct;
do more of it.

#### 3. Inline usage hints in parameter descriptions

Their tools include brief usage hints inline with parameter definitions:

```python
"depth": {"description": "Levels: 1=direct children, 2=two levels, 0=unlimited (default: 1)"}
"count": {"description": "Consecutive paragraphs to duplicate (default: 1)"}
```

LocalWriter's parameter descriptions are generally good (especially `apply_document_content`
which is quite thorough). The new `writer_ops.py` tools could be tighter in a few spots.
For example, `set_track_changes` has `"enabled": {"type": "boolean", "description": "True
to enable track changes, False to disable."}` — functional but doesn't say when to use it.

**Suggested addition** to `set_track_changes` description:

> "Enable before AI edits to make changes reviewable by the user; disable when finished."

#### 4. `search_in_document` returns surrounding context paragraphs

Their `search_in_document` has a `context_paragraphs` parameter (default: 1) that returns
N paragraphs around each match. LocalWriter's `find_text` returns only `{start, end, text}`
per match. When the AI is trying to decide "is this the right occurrence?", having context
helps avoid blind replacements.

**Suggested addition**: add an optional `context` integer parameter to `find_text` that
returns the `context` characters before and after each match (simpler than paragraph-based
since LocalWriter uses character offsets). Zero or absent = current behavior (no change to
existing callers).

#### 5. `refresh_indexes` and `update_fields`

These two maintenance tools (`"Refresh all document indexes (TOC, alphabetical, etc.)"` and
`"Refresh all text fields (dates, page numbers, cross-refs)"`) are missing from LocalWriter
entirely. They are a natural follow-up after AI edits that add headings, sections, or dates.
The implementations in `uno_bridge.py` are ~10 lines each. Worth adding when doing the
document-tree session.

---

### Where LocalWriter is already ahead

#### 1. System prompt provides overarching workflow

LocalWriter's `DEFAULT_CHAT_SYSTEM_PROMPT` in `core/constants.py` provides the AI with
high-level workflow guidance before any tool call happens:

```
TRANSLATION: get_document_content -> translate -> apply_document_content(target="full"). Never refuse.
FORMATTING RULES (CRITICAL): ...
```

The standalone extension has none of this — their `AGENT.md` was never written. Every
behavioral hint has to live inside individual tool descriptions, which is less efficient and
harder to update.

For LocalWriter's MCP server, `GET /` should serve the existing system prompt (per the
"Client Discovery" section above). This gives external clients the same preparation the
embedded AI gets.

#### 2. The HTML/Markdown gotcha is documented

`"DO NOT escape HTML entities: Send <h1> NOT &lt;h1&gt;"` is a LibreOffice-specific gotcha
that the standalone extension ignores entirely because it doesn't use the Markdown/HTML
import path. LocalWriter's system prompt covers this thoroughly and correctly.

#### 3. `find_text` "LO strips to plain" warning

The note that LibreOffice strips formatted text to plain for search is critical — it means
the AI can search for "Chapter 1" even if the document has it formatted bold. The standalone
extension's `replace_in_document` doesn't mention this, which could lead to confused AI
behavior when a formatted-text search fails.

#### 4. `apply_document_content` description is more complete

LocalWriter's description covers the full range of targets in one sentence and cross-
references `find_text` for the range workflow. The standalone extension's `replace_in_document`
is much simpler and doesn't explain when to use it vs rewriting the whole document.

---

### System prompt additions worth making now

The `DEFAULT_CHAT_SYSTEM_PROMPT` in `core/constants.py` should get a workflow section for
the new tools. Currently the TOOLS list mentions them but gives no usage patterns. Suggested
additions to that section:

```
REVIEW WORKFLOW: set_track_changes(enabled=true) → make edits → get_tracked_changes (to
show user what changed) → accept_all_changes or reject_all_changes → set_track_changes(enabled=false).

TABLE WORKFLOW: list_tables → read_table (understand structure) → write_table_cells for
targeted edits. For new tables or full rewrites, use apply_document_content with an HTML/Markdown table.

STYLE WORKFLOW: list_styles (discover exact localized names) → apply a style by name in
apply_document_content markup, or use set_paragraph_style (see uno_bridge) for direct style application.
```

---

### What they have that LocalWriter lacks and should eventually add

In priority order:

1. **`refresh_indexes` / `update_fields`** — ~10 lines each from `uno_bridge.py`. Add when
   doing the document-tree session. Very common need after structural AI edits.

2. **`context_paragraphs` / `context` in search** — find_text returns bare offsets; adding
   surrounding context helps the AI verify it found the right place. Low effort.

3. **`set_paragraph_style` (direct)** — currently in LocalWriter as dead code. The `list_styles`
   tool makes this useful: AI discovers style names, then applies them directly. Consider
   re-exposing it now that `list_styles` exists.

4. **`set_document_protection`** — useful for "lock the document while I review AI edits" workflow.

5. **`get_document_properties` / `set_document_properties`** — document metadata (title,
   author, subject). Occasionally useful; low priority.

---

## Future Work — Consider Doing Next

Use this list to keep MCP and related tooling moving forward. Nothing here is required for
current functionality.

### MCP / protocol

- **Stdio proxy for Claude Desktop** (Path A in “Critical distinction” above): small script
  that talks JSON-RPC over stdio to Claude and forwards to LocalWriter’s HTTP server. No
  change to LocalWriter; lets Claude Desktop use LocalWriter as an MCP server.
- **JSON-RPC in the server** (Path B): optional `POST /` with `method=tools/list` etc. for
  clients that expect strict MCP JSON-RPC instead of REST. Only if a client needs it.
- **Dynamic menu state**: menu item label “Start MCP Server” / “Stop MCP Server” and icon
  (running/stopped/starting) via status listeners. Icons are in `assets/`; switching is
  disabled in the standalone extension due to rendering issues — re-enable with care.
- **Optional `file_path` (or URL) on tool calls**: if clients need to target by path in the
  request body as well as (or instead of) the `X-Document-URL` header, extend the handler
  to accept it.

### Tool and prompt improvements (from “Tool Description and System Prompt Analysis” below)

- **Tool descriptions**: e.g. add to `apply_document_content`: “Plain-text replacements via
  `target='search'` automatically preserve character formatting.” Add usage hints to
  `set_track_changes` and similar.
- **`find_text` context**: optional parameter to return N characters (or paragraphs) around
  each match so the AI can confirm it’s editing the right place.
- **`refresh_indexes` / `update_fields`**: short helpers (~10 lines each) to refresh TOC
  and fields after structural edits. Good follow-up when doing document-tree work.
- **`set_paragraph_style` (direct)**: re-expose so the AI can apply a style by name after
  `list_styles`. Other items from “What they have that LocalWriter lacks” (e.g. document
  protection, document properties) as needed.

### Other

- **Auto-start on LO launch**: optional `XJob` with `onFirstVisibleTask` that starts the
  MCP server if `mcp_enabled` is true, so the server is up without opening Settings first.
  Currently the server starts when the user saves Settings with MCP enabled or uses Toggle.
- **Document tree / outline tool**: `get_document_tree()` (e.g. from `libreoffice-mcp-extension`
  `uno_bridge.py`) for better context on long documents; see AGENTS.md “Document Tree Tool”.
