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

log = logging.getLogger("localwriter.mcp.protocol")

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
        self._handle_mcp(body, handler)

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
        msg = body
        method = msg.get("method", "?") if isinstance(msg, dict) else "batch"
        req_id = msg.get("id") if isinstance(msg, dict) else None
        log.info("[SSE] POST <<< %s (id=%s)", method, req_id)

        result = self._process_jsonrpc(msg)
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
                "eval": {
                    "description": "Evaluate a Python expression",
                    "body": {"action": "eval", "code": "1 + 1"},
                },
                "exec": {
                    "description": "Execute Python code (result in _result var)",
                    "body": {"action": "exec", "code": "_result = 'hello'"},
                },
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
            if action == "eval":
                result = self._debug_eval(body.get("code", ""))
            elif action == "exec":
                result = self._debug_exec(body.get("code", ""))
            elif action == "call_tool":
                result = self._debug_call_tool(
                    body.get("tool", ""), body.get("args", {}))
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

    def _handle_mcp(self, msg, handler):
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
                result = self._process_jsonrpc(item)
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
        result = self._process_jsonrpc(msg)
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
                "name": "LocalWriter MCP",
                "version": self.version,
            },
            "instructions": (
                "LocalWriter MCP — AI document workspace. "
                "WORKFLOW: 1) Use tools to interact with LibreOffice documents. "
                "2) Tools are filtered by document type (writer/calc/draw). "
                "3) All UNO operations run on the main thread for thread safety."
            ),
        }

    def _mcp_ping(self, params):
        return {}

    def _mcp_tools_list(self, params):
        doc_type = self._detect_active_doc_type()
        schemas = self.tool_registry.get_mcp_schemas(doc_type)
        return {"tools": schemas}

    def _mcp_resources_list(self, params):
        return {"resources": []}

    def _mcp_prompts_list(self, params):
        return {"prompts": []}

    def _mcp_tools_call(self, params):
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        if not tool_name:
            raise ValueError("Missing 'name' in tools/call params")

        from plugin.framework.logging import debug_log
        debug_log(f"*** tools/call: {tool_name}, event_bus={self.event_bus} ***", context="MCP Protocol")

        if self.event_bus:
            from plugin.framework.logging import debug_log
        # Fire event for MCP request
        if getattr(self, "event_bus", None) is not None:
            self.event_bus.emit(
                "mcp:request",
                tool=params["name"],
                args=params.get("arguments", {}),
                method="tools/call"
            )

        result = self._execute_with_backpressure(tool_name, arguments)

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

    def _process_jsonrpc(self, msg):
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

        from plugin.framework.logging import debug_log
        debug_log(f"*** MCP INCOMING METHOD: {method} (id={req_id}) ***", context="MCP Protocol")

        if handler is None:
            return (400, _jsonrpc_error(
                req_id, _METHOD_NOT_FOUND,
                "Unknown method: %s" % method))

        try:
            result = handler(params)
            debug_log(f"*** MCP RESULT: {str(result)[:100]} ***", context="MCP Protocol")
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

    def _execute_with_backpressure(self, tool_name, arguments):
        """Execute a tool on the VCL main thread with backpressure."""
        acquired = _tool_semaphore.acquire(timeout=_WAIT_TIMEOUT)
        if not acquired:
            raise BusyError(
                "LibreOffice is busy processing another tool call. "
                "Please wait a moment and retry.")
        try:
            return execute_on_main_thread(
                self._execute_tool_on_main, tool_name, arguments,
                timeout=_PROCESS_TIMEOUT)
        finally:
            _tool_semaphore.release()

    def _execute_tool_on_main(self, tool_name, arguments):
        """Execute a tool via the ToolRegistry. Runs on main thread."""
        from plugin.framework.tool_context import ToolContext

        registry = self.tool_registry
        svc_registry = self.services

        # Resolve active document
        doc = None
        doc_type = "writer"
        try:
            doc_svc = svc_registry.document
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

        context = ToolContext(
            doc=doc,
            ctx=ctx,
            doc_type=doc_type,
            services=svc_registry,
            caller="mcp",
        )

        t0 = time.perf_counter()
        result = registry.execute(tool_name, context, **arguments)
        elapsed = time.perf_counter() - t0

        if isinstance(result, dict):
            result["_elapsed_ms"] = round(elapsed * 1000, 1)

        return result

    # ── Debug helpers ────────────────────────────────────────────────

    def _debug_eval(self, code):
        ns = self._debug_namespace()
        return repr(eval(code, ns))

    def _debug_exec(self, code):
        ns = self._debug_namespace()
        ns["_result"] = None
        exec(code, ns)
        r = ns.get("_result")
        return repr(r) if r is not None else "OK (no _result set)"

    def _debug_call_tool(self, tool_name, arguments):
        if not tool_name:
            return {"error": "Missing 'tool' parameter"}
        result = execute_on_main_thread(
            self._execute_tool_on_main, tool_name, arguments,
            timeout=_PROCESS_TIMEOUT)
        return result

    def _debug_trigger(self, command):
        from plugin.main import _modules, get_services
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

    def _debug_namespace(self):
        """Build a namespace for eval/exec with useful references."""
        import plugin.main as main_mod
        ns = {
            "services": self.services,
            "tools": self.tool_registry,
            "events": self.event_bus,
            "modules": getattr(main_mod, "_modules", []),
            "log": log,
        }
        try:
            import uno
            ns["uno"] = uno
            ns["ctx"] = uno.getComponentContext()
        except ImportError:
            pass
        return ns

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
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Access-Control-Allow-Methods",
                            "GET, POST, DELETE, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers",
                            "Content-Type, Authorization, Mcp-Session-Id")
        handler.send_header("Access-Control-Expose-Headers",
                            "Mcp-Session-Id")
