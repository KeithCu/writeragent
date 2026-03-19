# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""MCP JSON-RPC protocol handler.

Pure protocol logic — no HTTP server, no request handler class.
Route handlers are registered with the HTTP route registry by MCPModule.
"""

import json
import logging
import threading
import time
import uuid

from plugin.framework.main_thread import execute_on_main_thread
log = logging.getLogger(__name__)

log = logging.getLogger("writeragent.mcp.protocol")

# MCP protocol version we advertise
MCP_PROTOCOL_VERSION = "2025-11-25"


# Backpressure — one tool execution at a time
_tool_semaphore = threading.Semaphore(1)
_WAIT_TIMEOUT = 5.0
_PROCESS_TIMEOUT = 60.0


class BusyError(Exception):
    """The VCL main thread is already processing another tool call."""


# JSON-RPC helpers
def _jsonrpc_ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


# Standard JSON-RPC error codes
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603
_SERVER_BUSY = -32000
_EXECUTION_TIMEOUT = -32001

# Session management
_mcp_session_id = None


def _resolve_doc_type_for_url(services, document_url):
    """Run on main thread: resolve document by URL and return doc_type or None."""
    if not document_url:
        return None
    try:
        doc_svc = services.document
        _doc, doc_type = doc_svc.resolve_document_by_url(document_url)
        return doc_type
    except Exception:
        return None


class MCPProtocolHandler:
    """MCP JSON-RPC protocol — route handlers for the HTTP server."""

    def __init__(self, services):
        self.services = services
        self.tool_registry = services.tools
        self.event_bus = getattr(services, "events", None)
        self.version = "unknown"
        try:
            from plugin.version import EXTENSION_VERSION
            self.version = EXTENSION_VERSION
        except ImportError:
            pass

    # ── Raw handlers (receive GenericRequestHandler) ─────────────────

    def handle_mcp_post(self, handler):
        """POST /mcp — MCP streamable-http (JSON-RPC 2.0)."""
        body = self._read_body(handler)
        if body is None:
            return
        document_url = handler.headers.get("X-Document-URL") or None
        self._handle_mcp(body, handler, document_url=document_url)

    def handle_mcp_sse(self, handler):
        """GET /mcp — SSE notification stream (keepalive)."""
        accept = handler.headers.get("Accept", "")
        if "text/event-stream" not in accept:
            self._send_json(handler, 406, {
                "error": "Not Acceptable: must Accept text/event-stream"})
            return
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        self._send_cors_headers(handler)
        handler.end_headers()
        try:
            while True:
                handler.wfile.write(b": keepalive\n\n")
                handler.wfile.flush()
                time.sleep(15)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def handle_mcp_delete(self, handler):
        """DELETE /mcp — session termination."""
        handler.send_response(200)
        self._send_cors_headers(handler)
        handler.end_headers()

    def handle_sse_stream(self, handler):
        """GET /sse — legacy SSE transport (keepalive only)."""
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-cache")
            handler.send_header("Connection", "keep-alive")
            handler.send_header("X-Accel-Buffering", "no")
            self._send_cors_headers(handler)
            handler.end_headers()
            log.info("[SSE] GET stream opened")
            while True:
                handler.wfile.write(b": keepalive\n\n")
                handler.wfile.flush()
                time.sleep(15)
        except (BrokenPipeError, ConnectionResetError, OSError):
            log.info("[SSE] GET stream disconnected")

    def handle_sse_post(self, handler):
        """POST /sse or /messages — streamable HTTP (same as /mcp)."""
        body = self._read_body(handler)
        if body is None:
            return
        document_url = handler.headers.get("X-Document-URL") or None
        msg = body
        method = msg.get("method", "?") if isinstance(msg, dict) else "batch"
        req_id = msg.get("id") if isinstance(msg, dict) else None
        log.info("[SSE] POST <<< %s (id=%s)", method, req_id)

        result = self._process_jsonrpc(msg, document_url=document_url)
        if result is None:
            handler.send_response(202)
            self._send_cors_headers(handler)
            handler.end_headers()
            return

        status, response = result
        handler.send_response(status)
        self._send_cors_headers(handler)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        out = json.dumps(response, ensure_ascii=False, default=str)
        log.info("[SSE] POST >>> %s (id=%s) -> %d", method, req_id, status)
        handler.wfile.write(out.encode("utf-8"))

    # ── Simple handlers (body, headers, query) -> (status, dict) ─────

    def handle_debug_info(self, body, headers, query):
        """GET /debug — show available debug actions."""
        tools = list(self.tool_registry.tool_names) if self.tool_registry else []
        return (200, {
            "debug": True,
            "usage": "POST /debug with JSON body",
            "actions": {
                "call_tool": {
                    "description": "Call a registered tool",
                    "body": {"action": "call_tool", "tool": "get_document_info", "args": {}},
                },
                "trigger": {
                    "description": "Simulate a menu trigger command",
                    "body": {"action": "trigger", "command": "settings"},
                },
                "services": {
                    "description": "List registered services",
                    "body": {"action": "services"},
                },
                "config": {
                    "description": "Get/set config values",
                    "body": {"action": "config", "key": "mcp.port", "value": None},
                },
            },
            "tools": tools,
        })

    def handle_debug_post(self, handler):
        """POST /debug — execute debug actions."""
        # Security: restrict debug actions to localhost
        client_ip = handler.client_address[0]
        if client_ip not in ("127.0.0.1", "::1", "localhost"):
            log.warning("Blocked remote access to /debug from %s", client_ip)
            self._send_json(handler, 403, {"error": "Forbidden: Debug actions restricted to localhost"})
            return

        body = self._read_body(handler)
        if body is None:
            return
        action = body.get("action", "")
        try:
            if action == "call_tool":
                document_url = handler.headers.get("X-Document-URL") or None
                result = self._debug_call_tool(
                    body.get("tool", ""), body.get("args", {}),
                    document_url=document_url)
            elif action == "trigger":
                result = self._debug_trigger(body.get("command", ""))
            elif action == "services":
                result = self._debug_services()
            elif action == "config":
                result = self._debug_config(
                    body.get("key"), body.get("value", "__NOSET__"))
            else:
                result = {"error": "Unknown action: %s" % action}
            self._send_json(handler, 200, {"ok": True, "result": result})
        except Exception as e:
            log.exception("Debug %s error", action)
            self._send_json(handler, 500, {"ok": False, "error": str(e),
                                           "type": type(e).__name__})

    # ── MCP protocol handler ─────────────────────────────────────────

    def _handle_mcp(self, msg, handler, document_url=None):
        """Route MCP JSON-RPC request(s) — single or batch."""
        global _mcp_session_id

        method = msg.get("method", "?") if isinstance(msg, dict) else "batch"
        req_id = msg.get("id") if isinstance(msg, dict) else None
        log.info("[MCP] <<< %s (id=%s)", method, req_id)

        is_initialize = (isinstance(msg, dict)
                         and msg.get("method") == "initialize")

        # Batch request
        if isinstance(msg, list):
            responses = []
            for item in msg:
                result = self._process_jsonrpc(item, document_url=document_url)
                if result is not None:
                    _status, response = result
                    responses.append(response)
            if responses:
                self._send_json(handler, 200, responses)
            else:
                handler.send_response(202)
                self._send_cors_headers(handler)
                handler.end_headers()
            return

        # Single request
        result = self._process_jsonrpc(msg, document_url=document_url)
        if result is None:
            handler.send_response(202)
            self._send_cors_headers(handler)
            if _mcp_session_id:
                handler.send_header("Mcp-Session-Id", _mcp_session_id)
            handler.end_headers()
            return
        status, response = result

        if is_initialize and status == 200:
            _mcp_session_id = str(uuid.uuid4())

        handler.send_response(status)
        self._send_cors_headers(handler)
        handler.send_header("Content-Type", "application/json")
        if _mcp_session_id:
            handler.send_header("Mcp-Session-Id", _mcp_session_id)
        handler.end_headers()
        out = json.dumps(response, ensure_ascii=False, default=str)
        log.info("[MCP] >>> %s (id=%s) -> %d", method, req_id, status)
        handler.wfile.write(out.encode("utf-8"))

    # ── MCP method handlers ──────────────────────────────────────────

    def _mcp_initialize(self, params):
        client_version = params.get("protocolVersion", MCP_PROTOCOL_VERSION)
        return {
            "protocolVersion": client_version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {
                "name": "WriterAgent MCP",
                "version": self.version,
            },
            "instructions": (
                "WriterAgent MCP — AI document workspace. "
                "WORKFLOW: 1) Use tools to interact with LibreOffice documents. "
                "2) Tools are filtered by document type (writer/calc/draw). "
                "3) All UNO operations run on the main thread for thread safety."
            ),
        }

    def _mcp_ping(self, params):
        return {}

    def _mcp_tools_list(self, params, document_url=None):
        if document_url:
            doc_type = execute_on_main_thread(
                _resolve_doc_type_for_url, self.services, document_url,
                timeout=10.0)
            if doc_type is None:
                doc_type = self._detect_active_doc_type()
        else:
            doc_type = self._detect_active_doc_type()
        doc_type = doc_type or "writer"
        schemas = self.tool_registry.get_mcp_schemas(doc_type)
        return {"tools": schemas}

    def _mcp_resources_list(self, params):
        return {"resources": []}

    def _mcp_prompts_list(self, params):
        return {"prompts": []}

    def _mcp_tools_call(self, params, document_url=None):
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        if not tool_name:
            raise ValueError("Missing 'name' in tools/call params")

        log.debug(f"*** tools/call: {tool_name}, event_bus={self.event_bus} ***")

        if self.event_bus:
            pass
        # Fire event for MCP request
        if getattr(self, "event_bus", None) is not None:
            self.event_bus.emit(
                "mcp:request",
                tool=params["name"],
                args=params.get("arguments", {}),
                method="tools/call"
            )

        tool = self.tool_registry.get(tool_name)
        is_long_running = getattr(tool, "long_running", False) if tool else False

        if is_long_running:
            result = self._execute_long_running(
                tool_name, arguments, document_url=document_url)
        else:
            result = self._execute_with_backpressure(
                tool_name, arguments, document_url=document_url)

        if self.event_bus:
            snippet = str(result)[:100] if result else ""
            self.event_bus.emit("mcp:result", tool=tool_name, result_snippet=snippet, args=arguments)

        is_error = (isinstance(result, dict)
                    and result.get("status") == "error")
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False,
                                       default=str),
                }
            ],
            "isError": is_error,
        }

    # ── JSON-RPC processing ──────────────────────────────────────────

    def _process_jsonrpc(self, msg, document_url=None):
        """Process a JSON-RPC message.

        Returns (http_status, response_dict) or None for notifications.
        """
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return (400, _jsonrpc_error(
                None, _INVALID_REQUEST, "Invalid JSON-RPC 2.0 request"))

        method = msg.get("method", "")
        params = msg.get("params", {})
        req_id = msg.get("id")

        if req_id is None:
            return None

        handler = {
            "initialize":      self._mcp_initialize,
            "ping":            self._mcp_ping,
            "tools/list":      self._mcp_tools_list,
            "tools/call":      self._mcp_tools_call,
            "resources/list":  self._mcp_resources_list,
            "prompts/list":    self._mcp_prompts_list,
        }.get(method)

        log.debug(f"*** MCP INCOMING METHOD: {method} (id={req_id}) ***")

        if handler is None:
            return (400, _jsonrpc_error(
                req_id, _METHOD_NOT_FOUND,
                "Unknown method: %s" % method))

        try:
            if method in ("tools/list", "tools/call"):
                result = handler(params, document_url=document_url)
            else:
                result = handler(params)
            log.debug(f"*** MCP RESULT: {str(result)[:100]} ***")
            return (200, _jsonrpc_ok(req_id, result))
        except BusyError as e:
            log.warning("MCP %s: busy (%s)", method, e)
            return (429, _jsonrpc_error(
                req_id, _SERVER_BUSY, str(e),
                {"retryable": True}))
        except TimeoutError as e:
            log.error("MCP %s: timeout (%s)", method, e)
            return (504, _jsonrpc_error(
                req_id, _EXECUTION_TIMEOUT, str(e)))
        except Exception as e:
            log.error("MCP %s error: %s", method, e, exc_info=True)
            return (500, _jsonrpc_error(
                req_id, _INTERNAL_ERROR, str(e)))

    # ── Backpressure execution ───────────────────────────────────────

    def _execute_with_backpressure(self, tool_name, arguments, document_url=None):
        """Execute a tool on the VCL main thread with backpressure."""
        acquired = _tool_semaphore.acquire(timeout=_WAIT_TIMEOUT)
        if not acquired:
            raise BusyError(
                "LibreOffice is busy processing another tool call. "
                "Please wait a moment and retry.")
        try:
            return execute_on_main_thread(
                self._execute_tool_on_main, tool_name, arguments, document_url,
                timeout=_PROCESS_TIMEOUT)
        finally:
            _tool_semaphore.release()

    def _execute_long_running(self, tool_name, arguments, document_url=None):
        """Execute a long-running tool on the current background HTTP thread.
        Context resolution (finding the active doc) is strictly done on the main thread
        to ensure thread safety with LibreOffice UNO."""

        def _get_context():
            doc_svc = self.services.document
            doc = None
            doc_type = "writer"
            if document_url:
                doc, doc_type = doc_svc.resolve_document_by_url(document_url)
            else:
                doc = doc_svc.get_active_document()
                if doc:
                    doc_type = doc_svc.detect_doc_type(doc)
            import uno
            ctx = uno.getComponentContext()
            return doc, doc_type, ctx

        doc, doc_type, ctx = execute_on_main_thread(_get_context, timeout=10.0)

        if doc is None and not document_url:
            return {"status": "error", "message": "No document open in LibreOffice."}
        elif doc is None:
            return {"status": "error", "message": "No document open matching X-Document-URL: %s" % document_url}

        from plugin.framework.tool_context import ToolContext
        context = ToolContext(
            doc=doc,
            ctx=ctx,
            doc_type=doc_type,
            services=self.services,
            caller="mcp",
        )

        t0 = time.perf_counter()
        result = self.tool_registry.execute(tool_name, context, **arguments)
        elapsed = time.perf_counter() - t0

        if isinstance(result, dict):
            result["_elapsed_ms"] = round(elapsed * 1000, 1)

        return result

    def _execute_tool_on_main(self, tool_name, arguments, document_url=None):
        doc = None
        doc_type = "writer"
        try:
            doc_svc = self.services.document
            if document_url:
                doc, doc_type = doc_svc.resolve_document_by_url(document_url)
                if doc is None:
                    return {"status": "error",
                            "message": "No document open matching X-Document-URL: %s" % (document_url or "")}
            else:
                doc = doc_svc.get_active_document()
                if doc:
                    doc_type = doc_svc.detect_doc_type(doc)
        except Exception:
            pass

        if doc is None:
            return {"status": "error",
                    "message": "No document open in LibreOffice."}

        # Get UNO context
        ctx = None
        try:
            import uno
            ctx = uno.getComponentContext()
        except Exception:
            pass

        from plugin.framework.tool_context import ToolContext
        context = ToolContext(
            doc=doc,
            ctx=ctx,
            doc_type=doc_type,
            services=self.services,
            caller="mcp",
        )

        t0 = time.perf_counter()
        result = self.tool_registry.execute(tool_name, context, **arguments)
        elapsed = time.perf_counter() - t0

        if isinstance(result, dict):
            result["_elapsed_ms"] = round(elapsed * 1000, 1)

        return result

    # ── Debug helpers ────────────────────────────────────────────────

    def _debug_call_tool(self, tool_name, arguments, document_url=None):
        if not tool_name:
            return {"error": "Missing 'tool' parameter"}
        result = self._execute_with_backpressure(
            tool_name, arguments, document_url=document_url)
        return result

    def _debug_trigger(self, command):
        from plugin.main import get_services
        if command == "settings":
            from plugin.framework.settings_dialog import show_settings
            from plugin._manifest import MODULES
            config_svc = get_services().config
            execute_on_main_thread(
                show_settings, None, config_svc, MODULES,
                timeout=120.0)
            return "Settings dialog shown"
        return {"triggered": command, "note": "Use menu for UI commands"}

    def _debug_services(self):
        if not self.services:
            return []
        return list(self.services._services.keys())

    def _debug_config(self, key, value):
        if not self.services:
            return {"error": "No service registry"}
        config_svc = self.services.config
        if not config_svc:
            return {"error": "No config service"}
        if key is None:
            return config_svc.get_dict()
        if value == "__NOSET__":
            return {key: config_svc.get(key)}
        config_svc.set(key, value)
        return {key: value, "persisted": True}

    # ── Helpers ───────────────────────────────────────────────────────

    def _detect_active_doc_type(self):
        try:
            doc_svc = self.services.document
            doc = doc_svc.get_active_document()
            if doc:
                return doc_svc.detect_doc_type(doc)
        except Exception:
            pass
        return None

    def _read_body(self, handler):
        """Read and parse JSON body from an HTTP handler."""
        content_length = int(handler.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        raw = handler.rfile.read(content_length).decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Invalid JSON body: %s", raw[:200])
            self._send_json(handler, 400, {"error": "Invalid JSON"})
            return None

    def _send_json(self, handler, status, data):
        """Send a JSON response via an HTTP handler."""
        handler.send_response(status)
        self._send_cors_headers(handler)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps(
            data, ensure_ascii=False, default=str).encode("utf-8"))

    def _send_cors_headers(self, handler):
        origin = handler.headers.get("Origin")
        if origin:
            import re
            if re.match(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$", origin):
                handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Access-Control-Allow-Methods",
                            "GET, POST, DELETE, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers",
                            "Content-Type, Authorization, Mcp-Session-Id")
        handler.send_header("Access-Control-Expose-Headers",
                            "Mcp-Session-Id")