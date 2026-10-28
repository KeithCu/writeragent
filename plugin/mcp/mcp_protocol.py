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

from __future__ import annotations

import datetime
import hmac
import json
import logging
import os
import secrets
import select
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import unquote

from plugin.chatbot.agent_manual import writer_topic_list_text
from plugin.framework.uno_context import get_runtime_uid, normalize_doc_url
from plugin.version import EXTENSION_VERSION
from plugin.framework.tool import ToolContext
from plugin.framework.queue_executor import QueueExecutor
from plugin.framework.errors import resolve_exception_message, format_error_payload, make_tool_error
import plugin.mcp.document_mutation_gate as document_mutation_gate
import plugin.mcp.mcp_session as mcp_session
from plugin.mcp.http_trace import log_mcp_transport_entry, log_unsupported_protocol_version
from plugin.mcp.server import (
    forget_sse_keepalive,
    note_sse_keepalive,
    read_json_body,
    try_reserve_sse,
    write_http_empty,
    write_http_json,
)
from plugin.mcp import wire_types

log = logging.getLogger("writeragent.mcp.protocol")

# Longest an SSE keepalive wait goes without checking HttpServer.stop's flag.
_SSE_STOP_POLL_SEC = 0.5

_SUPPORTED_HTTP_PROTOCOL_VERSIONS = frozenset({wire_types.MCP_PROTOCOL_VERSION, "2024-11-05", "2025-06-18", "2025-03-26"})


def _is_long_running(tool: Any) -> bool:
    fn = getattr(tool, "is_async", None) if tool is not None else None
    return fn() is True if callable(fn) else False


def _send_405(handler: Any, allow: str) -> None:
    def _headers(h: Any) -> None:
        mcp_session._send_mcp_response_headers(h)
        h.send_header("Allow", allow)

    write_http_empty(handler, 405, extra_headers=_headers)


def _document_echo_payload(doc: Any) -> dict[str, Any] | None:
    """{name, uid} of the resolved target document, or None. Reads UNO properties — call ONLY
    where UNO access is legal (the main thread); worker-thread callers must precompute this.

    Best-effort: a disposed document or a thread-guard failure must not fail the tool
    result. The exception is logged so that failure is visible in writeragent_debug.log.
    """
    if doc is None:
        return None
    try:
        url = str(getattr(doc, "URL", "") or "")
        name = unquote(os.path.basename(url)) if url else "Untitled"
        return {"name": name, "uid": get_runtime_uid(doc)}
    except Exception:
        # Echo is diagnostic. Returning None with no log hid a thread-guard or
        # proxy regression as a missing "document" field on an otherwise successful call.
        log.exception("MCP document echo failed")
        return None


# Chat keeps specialized + mcp off the default list (_DEFAULT_EXCLUDE_TIERS in tool.py).
# MCP advertise policy is forked: keep mcp-tier tools; hide specialized except in direct_flat.
# tier="chat" (send_peer_work / send_peer_result) stays off both MCP lists.
MCP_DELEGATE_EXCLUDE_TIERS = frozenset({"specialized", "specialized_control", "chat"})
MCP_DIRECT_FLAT_EXCLUDE_TIERS = frozenset({"specialized_control", "chat"})


def drop_unavailable_domains(schemas: Any, registry: Any, ctx: Any) -> Any:
    """Remove tools whose specialized domain cannot run on this install.

    The discovery catalog has always hidden such a domain; without this the flat tool list
    advertised its tools anyway, so the same install offered a capability in one exposure mode and
    not the other. Only domains with a known prerequisite are affected — everything else passes
    through untouched.
    """
    if ctx is None:
        return schemas
    from plugin.vision.vision_availability import specialized_domain_available

    kept = []
    for schema in schemas:
        name = schema.get("name") if isinstance(schema, dict) else None
        domain = None
        if name:
            tool = registry.get(name)
            domain = getattr(tool, "specialized_domain", None) if tool is not None else None
        if domain and not specialized_domain_available(str(domain), ctx):
            continue
        kept.append(schema)
    return kept


@dataclass
class _PreparedMcpCall:
    """Main-thread document resolve + ToolContext. Safe to hand to a worker with precomputed echo."""

    tool: object
    context: ToolContext
    doc: object
    doc_key: str
    needs_gate: bool
    echo: dict[str, Any] | None



# Pointer to the on-demand manual (T4 / G2 consolidation). The full how-to — editing, editing-html,
# review-modes, search, navigation, images, concurrency — is served per topic by get_guidance(topic), so the model
# pulls one section when needed instead of front-loading a manual here (Claude Desktop doesn't read
# `instructions`; Claude Code truncates it). The topic texts are the shared prompt pieces in
# constants.py (single source with the sidebar prompt), mapped per doc type by agent_manual.py.
_MCP_GUIDANCE_POINTER = (
    " HOW TO USE THESE TOOLS: call get_guidance(topic) for the manual — topics follow the open "
    f"document's type (for Writer: {writer_topic_list_text()}); call get_guidance() for the current list. "
    "Confirm edits by the tool result's structured fields, and in Writer note tracked changes are "
    "the user's to accept/reject, not yours."
)


def _format_mcp_clock_context(now: datetime.datetime | None = None) -> str:
    """Return connection-time local clock context for an MCP host's model prompt.

    Emits the local wall clock in Calc's accepted ISO shape (no offset / ``Z``).
    Weekday and timezone *name* follow the process locale / OS tzname; the numeric
    stamp itself stays locale-independent so models can copy it into ``write_formula_range``.
    """
    local_now = now.astimezone() if now is not None else datetime.datetime.now().astimezone()
    # Drop tzinfo so isoformat() cannot emit +HH:MM / Z — Calc serials are timezone-less.
    wall = local_now.replace(tzinfo=None)
    weekday = local_now.strftime("%A")
    timezone_name = local_now.tzname()
    timezone_suffix = f" ({timezone_name})" if timezone_name else ""
    return f"Current local date and time: {weekday}, {wall.isoformat(timespec='seconds')}{timezone_suffix}."


# Clock stamp is already offset-free; remind models not to re-add Z/offsets from other sources.
_MCP_CALC_DATETIME_HINT = " When writing Calc date/time cells, use the same offset-free ISO as the clock above (YYYY-MM-DD, HH:MM[:SS], YYYY-MM-DDTHH:MM[:SS]) or PTnHnMnS for elapsed values; do not append a timezone offset or Z."


def build_initialize_instructions(mode: str, *, now: datetime.datetime | None = None) -> str:
    """Assemble the MCP initialize `instructions` string for a tool-exposure mode.

    Pure function (no server/UNO) so the wording is unit-testable. `mode` is one of
    'direct_flat', 'direct_discovery', or anything else (treated as the delegate default)."""
    # Tool-choice only: targeting + type filter. Edit/nav/bulk stay in get_guidance — not a second manual.
    base = "WriterAgent MCP — AI document workspace. WORKFLOW: 1) With more than one document open, call list_open_documents and pass document_url (url or uid) on later tools; do not assume focus is stable. 2) tools/list is filtered by active document type (writer/calc/draw)."
    if mode == "direct_flat":
        mode_hint = " Specialized tools are listed directly in tools/list; call them by name. No WriterAgent LLM endpoint is required."
    elif mode == "direct_discovery":
        mode_hint = " Call find_tools with no arguments for the full specialized domain catalog, then find_tools(domain=…) for tool schemas in that area; call tools by name. No WriterAgent LLM endpoint is required."
    else:
        mode_hint = " For specialized capabilities, call delegate_to_specialized_*_toolset with domain and task (requires a WriterAgent chat endpoint for the inner agent)."
    return _format_mcp_clock_context(now) + " " + base + mode_hint + _MCP_CALC_DATETIME_HINT + _MCP_GUIDANCE_POINTER


def _validate_http_protocol_version(handler: Any) -> tuple[int, dict[str, Any]] | None:
    """Return (status, jsonrpc_body) when the HTTP Mcp-Protocol-Version header is unsupported."""
    value = handler.headers.get("Mcp-Protocol-Version")
    requested = value.strip() if value else None
    if requested is None or requested in _SUPPORTED_HTTP_PROTOCOL_VERSIONS:
        return None
    log_unsupported_protocol_version(handler, requested)
    return (400, wire_types.jsonrpc_failure(None, wire_types.INVALID_REQUEST, "Unsupported MCP-Protocol-Version: %s" % requested))


# Backpressure — one fast MCP tool body at a time (_execute_with_backpressure).
# The semaphore covers prepare and, for a read-only tool, the body. A mutating
# fast call releases it before the per-document gate and takes it again only
# for the body, so a wait on one document does not 429 other documents.
# Long-running tools skip the semaphore and take only the gate.
# See docs/framework/threading.md § MCP tool execution paths.
_tool_semaphore = threading.Semaphore(1)
_WAIT_TIMEOUT = 5.0
_BUSY_MESSAGE = "LibreOffice is busy processing another tool call. Please wait a moment and retry."
_PROCESS_TIMEOUT = 60.0

_ACTIVE_DOCUMENT_SENTINEL = "__active_document__"


def _doc_titles(docs: list[Any]) -> list[str]:
    """Human-readable names for an error message; never raises."""
    titles = []
    for d in docs:
        try:
            titles.append(str(d.getTitle() or "") or "(untitled)")
        except Exception:
            titles.append("(unknown)")
    return titles


def _real_active_document(doc_svc: Any) -> Any:
    """The active document, or None when no real document is open.

    LibreOffice's Start Center is a live component but not a document, so
    get_active_document() returns it when nothing is open. A real document -- even an
    unsupported type like Math/Base -- supports ``com.sun.star.document.OfficeDocument``;
    the Start Center does not. So only the Start Center is normalized to None (real but
    unsupported docs are left to fail with the clearer "unsupported document" error). This
    keeps the MCP layer (NO_DOCUMENT_OPEN, find_tools' no-doc catalog, direct_flat's no-doc
    broadening) consistent in the real "no document open" state.
    """
    doc = doc_svc.get_active_document()
    if doc is None:
        return None
    # supportsService on the active component does not fail. The old except
    # kept the component anyway, including a disposed one.
    if not doc.supportsService("com.sun.star.document.OfficeDocument"):
        return None
    return doc


def _resolve_mcp_doc_key(document_url: str | None, doc: Any) -> str:
    """Stable per-document key for the mutation gate, derived from the RESOLVED document (``doc``).

    Keying off the resolved document — not the raw request handle — is what makes addressing the
    SAME document by its file URL OR by its RuntimeUID map to ONE gate, so two concurrent mutating
    calls on that document serialize instead of racing. RuntimeUID is preferred because it
    is stable for the document's whole session (it survives Save As, where the URL changes).

    Falls back to the normalized request URL only when the document couldn't be resolved, and to
    _ACTIVE_DOCUMENT_SENTINEL when there is neither — "target the active document" today.
    """
    if doc is not None:
        # A failed uid or URL read must not fall through to the request URL.
        # That second key lets two calls on the same document run at once.
        uid = get_runtime_uid(doc)
        if uid:
            return "uid:%s" % uid
        url = normalize_doc_url(doc.getURL())
        if url:
            return "url:%s" % url
    # No resolved doc (or one with neither uid nor URL): best-effort key off the raw request URL,
    # namespaced ("url:") so it can never collide with a resolved "uid:"/"url:" key; else the
    # active-document sentinel.
    if document_url:
        return "url:%s" % normalize_doc_url(document_url)
    return _ACTIVE_DOCUMENT_SENTINEL


def _echoable_jsonrpc_id(msg: Any) -> str | int | None:
    """Request id to copy onto a JSON-RPC error, or None when it cannot be detected.

    JSON-RPC 2.0 uses null when the id itself is missing or not a string or
    integer. A bool is an int subclass and is not a valid id.
    """
    if not isinstance(msg, dict):
        return None
    req_id = msg.get("id")
    if isinstance(req_id, bool) or not isinstance(req_id, (str, int)):
        return None
    return req_id


def _mcp_unknown_tool_result(message: str) -> dict[str, Any]:
    """tools/call content block for a tool this exposure mode will not run.

    Same shape as the old hand-built dict: text JSON of status/code/message,
    isError true. make_tool_error omits empty details.
    """
    payload = make_tool_error(message, code="UNKNOWN_TOOL")
    return wire_types.call_tool_result(json.dumps(payload, ensure_ascii=False), is_error=True)


class _DebugHandlerAdapter:
    """Adapter presenting a handler-like interface from GET /debug arguments."""

    def __init__(self, body: Any, headers: Any):
        self.body: Any = body
        self.headers: Any = headers or {}
        addr = getattr(headers, "_client_address", None) or getattr(headers, "client_address", None)
        self.client_address: Any = addr if addr is not None else ("127.0.0.1", 0)


class MCPProtocolHandler:
    """MCP JSON-RPC protocol — route handlers for the HTTP server."""

    services: Any
    queue_executor: Any
    tool_registry: Any
    event_bus: Any
    version: str
    debug_token: str

    def __init__(self, services: Any, debug_token: str | None = None) -> None:
        self.services = services
        self.queue_executor = services.get("main_thread") or QueueExecutor(ctx=services.get("uno") if services else None)
        self.tool_registry = services.tools
        self.event_bus = getattr(services, "events", None)
        self._cancelled_requests: set[tuple[Any, str | int]] = set()
        self._in_flight_requests: dict[tuple[Any, str | int], int] = {}
        self._requests_lock: threading.Lock = threading.Lock()
        # plugin.version is part of the extension. A missing module is a
        # packaging bug, not a runtime fallback to "unknown".
        self.version = EXTENSION_VERSION
        self.debug_token = debug_token or secrets.token_urlsafe(32)

    # ── Raw handlers (receive GenericRequestHandler) ─────────────────

    def _extract_session(self, handler: Any) -> Any:
        """Cancellation key: echoed session header plus client IP.

        The header is the process-wide Mcp-Session-Id when the client sends it
        (every spec client after initialize). X-Session-Id is used only when that
        header is absent. The peer is the IP, not the source port: a cancel is a
        second HTTP request and cannot be paired with the in-flight tools/call by
        TCP port. Loopback clients share one Mcp-Session-Id and one IP, so they
        share this key. That is the one-session contract, not a second session id.
        """
        if handler is None:
            return None
        session_id = mcp_session._get_request_session_id(handler)
        if not session_id and hasattr(handler, "headers") and handler.headers:
            session_id = handler.headers.get("X-Session-Id") or handler.headers.get("X-Session-ID")
        addr = getattr(handler, "client_address", None)
        peer = addr[0] if isinstance(addr, tuple) and addr else addr
        if session_id and peer:
            return f"{session_id}@{peer}"
        return session_id or peer

    def handle_mcp_post(self, handler: Any, transport: str = "mcp") -> None:
        """POST /mcp — MCP streamable-http (JSON-RPC 2.0)."""
        log_mcp_transport_entry(handler, transport)
        version_error = _validate_http_protocol_version(handler)
        if version_error is not None:
            status, response = version_error
            self._send_json(handler, status, response)
            return
        body = self._read_body(handler)
        if body is None:
            return
        document_url = handler.headers.get("X-Document-URL") or None

        # POST threads are short-lived daemons. Registering them via
        # note_sse_keepalive makes HttpServer.stop() join them. A POST waiting
        # on the main-thread queue while stop() runs on the main thread can
        # stall or deadlock. Only long-lived SSE streams need keepalive
        # tracking and shutdown.
        self._handle_mcp(body, handler, document_url=document_url)

    def _open_sse_stream(
        self,
        handler: Any,
        *,
        transport: str | None = None,
        check_accept: bool = False,
        extra_headers: tuple[tuple[str, str], ...] = (),
        open_log: bool = False,
        disconnect_log: bool = False,
    ) -> None:
        if transport:
            log_mcp_transport_entry(handler, transport)
        if mcp_session._reject_stale_session(handler):
            return
        if check_accept:
            accept = handler.headers.get("Accept", "")
            if "text/event-stream" not in accept:
                self._send_json(handler, 406, {"error": "Not Acceptable: must Accept text/event-stream"})
                return
        tcp_server = getattr(handler, "server", None)
        sock = getattr(handler, "connection", None)
        stop_event = try_reserve_sse(tcp_server, sock)
        if stop_event is None:
            _send_405(handler, "POST, DELETE, OPTIONS")
            return
        reserved = True
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-cache")
            for name, value in extra_headers:
                handler.send_header(name, value)
            mcp_session._send_mcp_response_headers(handler)
            handler.end_headers()
            if open_log:
                log.info("[SSE] GET stream opened")
            reserved = False
            self._run_sse_keepalive_loop(handler, stop_event=stop_event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            if disconnect_log:
                log.info("[SSE] GET stream disconnected")
        finally:
            if reserved:
                forget_sse_keepalive(tcp_server, sock)

    def handle_mcp_sse(self, handler: Any) -> None:
        """GET /mcp — SSE notification stream (keepalive)."""
        self._open_sse_stream(handler, transport="mcp-sse", check_accept=True)

    def handle_mcp_delete(self, handler: Any) -> None:
        """DELETE /mcp — not supported: one process-wide session must stay alive."""
        # Nelson a3d69e68 / GitHub #38. Streamable HTTP lets a client DELETE
        # the session URL to end it. WriterAgent has one session id for the
        # whole soffice process, shared by every client. Returning 200 claimed
        # the session ended when it did not; clearing the id would cut every
        # other client off. 405 tells spec clients the session is still here.
        # They recover on 404 (stale id after restart), not 409 or 200.
        log_mcp_transport_entry(handler, "mcp")
        _send_405(handler, "GET, POST, OPTIONS")

    def handle_sse_stream(self, handler: Any) -> None:
        """GET /sse — legacy SSE transport (keepalive only)."""
        self._open_sse_stream(
            handler,
            extra_headers=(("Connection", "keep-alive"), ("X-Accel-Buffering", "no")),
            open_log=True,
            disconnect_log=True,
        )

    def _run_sse_keepalive_loop(self, handler: Any, interval: float = 15, stop_event: threading.Event | None = None) -> None:
        """Keep an SSE stream alive until the client drops or the HTTP server stops.

        This loop runs on the ThreadingMixIn request thread until the socket
        errors. HttpServer.stop() only ends serve_forever(), so each toggle
        would leave another daemon thread in select() for up to ``interval``
        seconds, including across restart. Register the socket, watch this
        generation's stop event, and leave when stop() shuts the socket down
        (that wakes select).
        """
        sock = handler.connection
        tcp_server = getattr(handler, "server", None)
        if stop_event is None:
            stop_event = note_sse_keepalive(tcp_server, sock)
        if stop_event is None:
            log.info("[SSE] GET stream closed")
            return
        try:
            while not stop_event.is_set():
                try:
                    handler.wfile.write(b": keepalive\n\n")
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break

                # Wait for client disconnect, the keepalive interval, or
                # stop() shutting this socket down (readable / error).
                # Sliced: on Windows a shutdown()/close() from another thread
                # does not wake select, so the stop flag is re-checked each
                # slice (GHA 37401464435: thread still alive after stop).
                readable: list[Any] = []
                wait_until = time.monotonic() + interval
                try:
                    while not stop_event.is_set():
                        left = wait_until - time.monotonic()
                        if left <= 0:
                            break
                        readable, _unused, _unused2 = select.select([sock], [], [], min(left, _SSE_STOP_POLL_SEC))
                        if readable:
                            break
                except (OSError, ValueError):
                    break
                if stop_event.is_set():
                    break
                if not readable:
                    continue
                try:
                    # What was wrong: EOF was sock.recv(1, MSG_PEEK). After the TLS
                    # handshake, ssl.SSLSocket.recv raises ValueError for any non-zero
                    # flags. use_ssl wraps the accepted socket with
                    # do_handshake_on_connect=False, but the request headers are read
                    # before this loop, so the handshake is already done. ValueError is
                    # not ConnectionResetError or OSError, so the outer except logged
                    # "SSE keepalive loop exception" and closed the stream instead of
                    # treating b"" as EOF. A readable TLS socket that still had bytes
                    # was closed the same way; the plain-socket path drained them.
                    # How: select already reported the socket readable. One recv is
                    # both the EOF check and the drain. Nothing after this loop reads
                    # those bytes.
                    # Why: recv with no flags works on a plain socket and on SSLSocket.
                    data = sock.recv(4096)
                    if not data:
                        break
                except (ConnectionResetError, OSError):
                    break
        except Exception:
            # Socket disconnects are handled above. Anything else closed the
            # client stream; debug text hid the traceback.
            log.exception("SSE keepalive loop exception")
        finally:
            forget_sse_keepalive(tcp_server, sock)
            log.info("[SSE] GET stream closed")

    def handle_sse_post(self, handler: Any) -> None:
        """POST /sse or /messages — streamable HTTP (same as /mcp)."""
        # Shares the same body as handle_mcp_post to include keepalive logic.
        # The transport label keeps /sse and /messages distinct in [MCP-HTTP] logs.
        self.handle_mcp_post(handler, transport="sse")

    def _debug_forbidden(self, handler: Any) -> str | None:
        """Refuse unless peer is loopback, debug token matches, and Origin is absent."""
        addr = getattr(handler, "client_address", None)
        client_ip = addr[0] if (isinstance(addr, (list, tuple)) and len(addr) > 0) else getattr(handler, "client_ip", "127.0.0.1")
        if client_ip not in ("127.0.0.1", "::1", "localhost"):
            return "Forbidden: Debug actions restricted to localhost"

        headers = getattr(handler, "headers", None)
        if headers is None:
            headers = {}
        # Reject Origin to block any cross-origin requests on /debug.
        origin = headers.get("Origin") or headers.get("origin")
        if origin:
            return "Forbidden: Origin header not allowed on /debug"

        token = headers.get("X-WriterAgent-Debug-Token") or headers.get("x-writeragent-debug-token")
        if not token or not self.debug_token or not hmac.compare_digest(token, self.debug_token):
            return "Forbidden: Invalid or missing debug token"

        return None

    # ── Simple handlers (body, headers, query) -> (status, dict) ─────

    def handle_debug_info(self, body_or_handler: Any, headers: Any = None, query: Any = None) -> tuple[int, dict[str, Any]]:
        """GET /debug — show available debug actions."""
        # What was wrong: GET /debug was registered as raw=True, sending protocol headers
        # and writing JSON itself rather than using the server's non-raw JSON handler.
        # How it happened: MCP_ROUTE_TABLE table consolidation mistakenly marked it raw=True.
        # Why this change: return (status, dict) so HttpServer formats and sends the response.
        if headers is None and hasattr(body_or_handler, "headers"):
            handler = body_or_handler
        else:
            handler = _DebugHandlerAdapter(body_or_handler, headers)

        forbidden = self._debug_forbidden(handler)
        if forbidden:
            return (403, {"error": forbidden})

        tools = list(self.tool_registry.tool_names) if self.tool_registry else []
        return (
            200,
            {
                "debug": True,
                "usage": "POST /debug with JSON body",
                "actions": {
                    "call_tool": {"description": "Call a registered tool", "body": {"action": "call_tool", "tool": "get_document_info", "args": {}}},
                    "trigger": {"description": "Simulate a menu trigger command", "body": {"action": "trigger", "command": "settings"}},
                    "services": {"description": "List registered services", "body": {"action": "services"}},
                    "config": {"description": "Get/set config values", "body": {"action": "config", "key": "mcp.port", "value": None}},
                },
                "tools": tools,
            },
        )

    def handle_debug_post(self, handler: Any) -> None:
        """POST /debug — execute debug actions."""
        forbidden = self._debug_forbidden(handler)
        if forbidden:
            log.warning("Blocked /debug access: %s", forbidden)
            self._send_json(handler, 403, {"error": forbidden})
            return

        body = self._read_body(handler)
        if body is None:
            return
        # What was wrong: a JSON array or scalar is a successful read_json_body
        # result (a list is a legal MCP batch on POST /mcp). body.get ran before
        # the try, so the error left this function.
        # How: GenericRequestHandler._dispatch turned that AttributeError into HTTP 500.
        # Why: /debug actions are objects. 400 here. read_json_body must still return lists.
        if not isinstance(body, dict):
            self._send_json(handler, 400, {"error": "JSON body must be an object"})
            return
        action = body.get("action", "")
        try:
            if action == "call_tool":
                document_url = handler.headers.get("X-Document-URL") or None
                result = self._debug_call_tool(body.get("tool", ""), body.get("args", {}), document_url=document_url)
            elif action == "trigger":
                result = self._debug_trigger(body.get("command", ""))
            elif action == "services":
                result = self._debug_services()
            elif action == "config":
                result = self._debug_config(body.get("key"), body.get("value", "__NOSET__"))
            else:
                result = {"error": "Unknown action: %s" % action}
            self._send_json(handler, 200, {"ok": True, "result": result})
        except Exception as e:
            log.exception("Debug %s error", action)
            self._send_json(handler, 500, format_error_payload(e))

    # ── MCP protocol handler ─────────────────────────────────────────

    def _handle_mcp(self, msg: Any, handler: Any, document_url: str | None = None, session: Any = None) -> None:
        """Route MCP JSON-RPC request(s) — single or batch."""
        if session is None:
            session = self._extract_session(handler)
        method = msg.get("method", "?") if isinstance(msg, dict) else "batch"
        req_id = msg.get("id") if isinstance(msg, dict) else None
        log.info("[MCP] <<< %s (id=%s)", method, req_id)

        if mcp_session._reject_stale_session(handler, msg):
            return

        is_initialize = isinstance(msg, dict) and msg.get("method") == "initialize"

        # Batch request
        if isinstance(msg, list):
            if not msg:
                self._send_json(handler, 400, wire_types.jsonrpc_failure(None, wire_types.INVALID_REQUEST, "Empty batch"))
                return

            if any(isinstance(item, dict) and item.get("method") == "initialize" for item in msg):
                self._send_json(
                    handler,
                    400,
                    wire_types.jsonrpc_failure(None, wire_types.INVALID_REQUEST, "initialize must not be part of a JSON-RPC batch"),
                )
                return

            responses = []
            for item in msg:
                result = self._process_jsonrpc(item, document_url=document_url, session=session)
                if result is not None:
                    _status, response = result
                    responses.append(response)

            if responses:
                self._send_json(handler, 200, responses)
            else:
                self._send_empty(handler, 202)
            return

        # Single request
        result = self._process_jsonrpc(msg, document_url=document_url, session=session)
        if result is None:
            self._send_empty(handler, 202)
            return
        status, response = result

        if is_initialize and status == 200:
            mcp_session._mint_session_id_once()

        log.info("[MCP] >>> %s (id=%s) -> %d", method, req_id, status)
        self._send_json(handler, status, response)

    # ── MCP method handlers ──────────────────────────────────────────

    def _mcp_initialize(self, params: Any) -> Any:
        # We echo the client's version only when it is in
        # _SUPPORTED_HTTP_PROTOCOL_VERSIONS, not any string it sends, because
        # the client then sends that value as Mcp-Protocol-Version and
        # _validate_http_protocol_version answers 400 to every later request.
        client_version = params.get("protocolVersion", wire_types.MCP_PROTOCOL_VERSION)
        if not isinstance(client_version, str):
            # protocolVersion must be a string. A non-string raises TypeError
            # on set membership and becomes HTTP 500 instead of a JSON-RPC
            # INVALID_PARAMS error.
            raise ValueError("protocolVersion must be a string")
        if client_version not in _SUPPORTED_HTTP_PROTOCOL_VERSIONS:
            client_version = wire_types.MCP_PROTOCOL_VERSION
        return wire_types.initialize_result(protocol_version=wire_types.MCP_PROTOCOL_VERSION, client_protocol_version=client_version, server_version=self.version, instructions=build_initialize_instructions(self._tool_exposure_mode()))

    def _mcp_ping(self, params: Any) -> Any:
        return wire_types.ping_result()

    def _tool_exposure_mode(self) -> str:
        """Read mcp.tool_exposure_mode (delegate | direct_flat | direct_discovery)."""
        # ConfigService.get already returns the caller default on ConfigError.
        # The except turned any other failure into the delegate catalog.
        return self.services.config.get("mcp.tool_exposure_mode", "delegate") or "delegate"

    def _mcp_tools_list(self, params: Any, document_url: str | None = None) -> Any:
        mode = self._tool_exposure_mode()
        # Direct modes (direct_flat / direct_discovery) intentionally skip the delegate
        # sub-agent so MCP hosts work without a WriterAgent LLM endpoint configured.
        # The host model orchestrates specialized tools itself. Future enhancement:
        # optional live context injection (Calc snapshot, shapes canvas, open-docs list)
        # like specialized_base.py does for delegated runs.
        # direct_flat advertises the specialized tools directly (control tools stay hidden --
        # they only make sense inside an active delegated domain). Every other mode keeps
        # today's core-only list (specialized tools are still callable by name -- via the
        # delegate gateway, or the find_tools discovery tool in direct_discovery).
        if mode == "direct_flat":
            exclude_tiers = MCP_DIRECT_FLAT_EXCLUDE_TIERS
        else:
            exclude_tiers = MCP_DELEGATE_EXCLUDE_TIERS

        def _resolve_and_filter() -> list[dict[str, Any]]:
            # Runs on the main (VCL) thread. Resolving the document AND filtering tools by
            # doc type both touch UNO -- get_schemas() -> supports_doc() calls
            # doc.supportsService() -- so the WHOLE block must be marshaled, not just the
            # doc lookup. Doing the doc-type filtering on the MCP request thread trips the
            # UNO thread guard and fails tools/list with a 500.
            doc_svc = self.services.document
            if document_url:
                doc, _unused = doc_svc.resolve_document_by_url(document_url)
            else:
                doc = _real_active_document(doc_svc)

            # In direct_flat with no target at all (no active doc AND no document_url), don't
            # filter by doc type, or app-specific tools would be dropped with no find_tools
            # fallback. An unresolvable document_url is a DOCUMENT_NOT_FOUND case, not a
            # no-target broaden, so it keeps normal filtering -- as do delegate (byte-for-byte
            # unchanged) and direct_discovery (find_tools has its own no-doc catalog).
            broaden = mode == "direct_flat" and doc is None and not document_url
            doc_filter = {"filter_doc_type": False} if broaden else {}
            doc_type = None
            uno_services: frozenset[str] = frozenset()
            if doc is not None:
                doc_type = self.services.document.detect_doc_type(doc)
                from plugin.doc.doc_type import uno_services_for_document

                uno_services = uno_services_for_document(doc, doc_type)
            schemas = self.tool_registry.get_schemas("mcp", doc_type=doc_type, uno_services_supported=uno_services, exclude_tiers=exclude_tiers, **doc_filter)

            # Filtering by the ACTIVE document alone made the Writer tools vanish
            # whenever a spreadsheet happened to have focus, with a Writer document
            # open right beside it -- the most reported WriterAgent failure by far
            # ("no editing tools in this session"), and indistinguishable from the
            # extension being down. Every tool takes document_url, so the catalog
            # covers all OPEN document types; the active one still comes first, and
            # a call with no document_url still targets it.
            broadened: dict[str, Any] = {}
            if doc is not None and not document_url:
                schemas, broadened = self._add_other_open_doc_schemas(schemas, doc_type, exclude_tiers)

            # A domain whose backend is not configured is hidden from the discovery catalog; the
            # flat list has to agree, or the same install advertises a capability in one exposure
            # mode and not the other. This block already runs on the main thread, which get_ctx
            # requires.
            from plugin.framework.uno_context import get_ctx

            # This block runs on the main thread. get_ctx returns None when
            # there is no context; an exception is a thread-guard violation
            # and must not be turned into "advertise every domain".
            uno_ctx = get_ctx()
            schemas = drop_unavailable_domains(schemas, self.tool_registry, uno_ctx)

            if mode == "direct_flat":
                # Keep Writer sidebar-only flows (brainstorming, writing_plan) out of the flat
                # list -- they need bespoke session orchestration the direct modes don't give.
                from plugin.doc.find_tools_tool import sidebar_only_tool_names

                sidebar_only = sidebar_only_tool_names(self.tool_registry, doc, doc_type=doc_type, uno_services_supported=uno_services)
                # The sidebar-only set is per document type, so it has to cover the
                # types the catalog was broadened to as well -- otherwise broadening
                # past an active Calc document smuggled Writer's sidebar-only flows
                # (brainstorming, writing_plan) into the flat list.
                for other_type, other_doc in broadened.items():
                    from plugin.doc.doc_type import uno_services_for_document

                    sidebar_only = sidebar_only | sidebar_only_tool_names(self.tool_registry, other_doc, doc_type=other_type, uno_services_supported=uno_services_for_document(other_doc, other_type))
                if sidebar_only:
                    schemas = [s for s in schemas if s.get("name") not in sidebar_only]
            return schemas

        schemas = self.queue_executor.execute(_resolve_and_filter, timeout=10.0)

        # find_tools is the discovery search tool, useful only in direct_discovery mode
        # (small core list + on-demand search). delegate advertises the gateway and
        # direct_flat already lists everything, so hide it by name in those modes.
        # Pure name filtering -- no UNO -- so it stays off the main thread.
        if mode != "direct_discovery":
            schemas = [s for s in schemas if s.get("name") != "find_tools"]

        return wire_types.list_tools_result(schemas)

    def _mcp_resources_list(self, params: Any) -> Any:
        return wire_types.empty_resources_result()

    def _mcp_prompts_list(self, params: Any) -> Any:
        return wire_types.empty_prompts_result()

    def _mcp_tools_call(self, params: Any, document_url: str | None = None, req_id: Any = None, session: Any = None) -> Any:
        call_params = wire_types.CallToolRequestParams.from_params(params)
        tool_name = call_params.name
        arguments = dict(call_params.arguments)

        tool = self.tool_registry.get(tool_name)
        tool_params = tool.get_parameters() if tool else {}
        tool_props = tool_params.get("properties", {}) if tool_params else {}

        if "document_url" not in tool_props:
            arg_document_url = arguments.pop("document_url", None)
        else:
            arg_document_url = arguments.get("document_url", None)

        if arg_document_url:
            document_url = arg_document_url

        # find_tools is the discovery search tool; it is only advertised in
        # direct_discovery mode, so reject calling it by name in other modes -- otherwise
        # the default (delegate) behavior would not really be unchanged.
        mode = self._tool_exposure_mode()
        if tool_name == "find_tools" and mode != "direct_discovery":
            return _mcp_unknown_tool_result("Tool 'find_tools' is only available when mcp.tool_exposure_mode is 'direct_discovery'.")

        if tool:
            tier = getattr(tool, "tier", "core")
            # direct_discovery advertises specialized tools via find_tools, so
            # invoking them must work. MCP_DELEGATE_EXCLUDE_TIERS excludes
            # "specialized". Both direct modes use MCP_DIRECT_FLAT_EXCLUDE_TIERS
            # (specialized_control and chat only).
            if mode in ("direct_flat", "direct_discovery"):
                exclude_tiers = MCP_DIRECT_FLAT_EXCLUDE_TIERS
            else:
                exclude_tiers = MCP_DELEGATE_EXCLUDE_TIERS

            if tier in exclude_tiers:
                return _mcp_unknown_tool_result(f"Tool '{tool_name}' is not available in the current exposure mode.")

        # One off-thread path: a long-running tool is is_async() exactly True
        # (positive timeout checked in _prepare_mcp_execution) and runs through
        # execute_safe. The long_running attribute alone must not select a second
        # path. MagicMock is_async() is not exactly True, so it stays on backpressure.
        is_long_running = _is_long_running(tool)

        log.debug("*** tools/call: %s, event_bus=%s ***", tool_name, self.event_bus)
        event_bus = self.event_bus
        if event_bus is not None:
            event_bus.emit("mcp:request", tool=tool_name, args=arguments, method="tools/call", req_id=req_id)

        try:
            exec_kwargs: dict[str, Any] = {"document_url": document_url, "req_id": req_id}
            if session is not None:
                exec_kwargs["session"] = session
            if is_long_running is True:
                res = self._execute_long_running(tool_name, arguments, **exec_kwargs)
            else:
                res = self._execute_with_backpressure(tool_name, arguments, **exec_kwargs)
        except document_mutation_gate.BusyError:
            raise
        except TimeoutError:
            # We map TimeoutError to 504, not a tool error, because only QueueExecutor/gate timeouts reach here; ToolBase.execute_safe already turns a tool's own exceptions (including TimeoutError) into error dicts.
            raise
        except Exception as e:
            # Tool failures must be MCP tool results (isError), not JSON-RPC
            # INTERNAL_ERROR. Clients treat HTTP 500 as transient and retry
            # (Hermes retried apply_style ~150× in 0.5s). BusyError/TimeoutError
            # stay 429/504 above. WriterAgentException used to re-raise into
            # _process_jsonrpc as HTTP 500 — that is the retryable path.
            log.exception("MCP tool %s raised unexpectedly", tool_name)
            code = getattr(e, "code", None) or "TOOL_EXECUTION_ERROR"
            if code == "INTERNAL_ERROR":
                code = "TOOL_EXECUTION_ERROR"
            res = make_tool_error(resolve_exception_message(e), code=code, tool_name=tool_name, error_type=type(e).__name__)

        if event_bus is not None:
            snippet = str(res)[:100] if res else ""
            event_bus.emit("mcp:result", tool=tool_name, result_snippet=snippet, args=arguments, req_id=req_id)

        # A tool may return an image: {"_mcp_image": {"data": <b64>, "mimeType": ...}} ->
        # emit a native MCP image content block (get_image) instead of base64-as-text.
        is_error = isinstance(res, dict) and res.get("status") == "error"
        img = res.get("_mcp_image") if isinstance(res, dict) else None
        if isinstance(img, dict) and img.get("data"):
            return wire_types.call_tool_result_image(img["data"], img.get("mimeType", "image/png"), is_error=is_error)
        return wire_types.call_tool_result(json.dumps(res, ensure_ascii=False, default=str), is_error=is_error)

    # ── JSON-RPC processing ──────────────────────────────────────────

    def _process_jsonrpc(self, msg: Any, document_url: str | None = None, session: Any = None) -> Any:
        """Process a JSON-RPC message.

        Returns (http_status, response_dict) or None for notifications (no ``id``).
        """
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return (400, wire_types.jsonrpc_failure(_echoable_jsonrpc_id(msg), wire_types.INVALID_REQUEST, "Invalid JSON-RPC 2.0 request"))

        # Handle cancellation notifications globally.
        # Key cancellations by (session, requestId). An id alone lets one
        # client cancel another client's request that reused the same id.
        if msg.get("method") == "notifications/cancelled":
            params = msg.get("params")
            req_id_to_cancel = params.get("requestId") if isinstance(params, dict) else None
            if isinstance(req_id_to_cancel, (str, int)) and not isinstance(req_id_to_cancel, bool):
                cancel_target = (session, req_id_to_cancel)
                with self._requests_lock:
                    if cancel_target in self._in_flight_requests:
                        self._cancelled_requests.add(cancel_target)

        # Notifications must not receive a JSON-RPC response (HTTP 202, empty body).
        if wire_types.is_jsonrpc_notification(msg):
            return None

        parsed = wire_types.parse_jsonrpc_request(msg)
        if isinstance(parsed, wire_types.JsonRpcParseError):
            return (400, wire_types.jsonrpc_failure(_echoable_jsonrpc_id(msg), parsed.code, parsed.message))

        method = parsed.method
        params = parsed.params
        req_id = parsed.req_id

        log.debug(f"*** MCP INCOMING METHOD: {method} (id={req_id}) ***")

        # tools/list and tools/call take document_url. A mixed dict is an
        # unknown callable to mypy, so only the one-argument methods live here.
        one_arg: dict[str, Callable[[Any], Any]] = {"initialize": self._mcp_initialize, "ping": self._mcp_ping, "resources/list": self._mcp_resources_list, "prompts/list": self._mcp_prompts_list}
        if method not in one_arg and method not in ("tools/list", "tools/call"):
            return (400, wire_types.jsonrpc_failure(req_id, wire_types.METHOD_NOT_FOUND, "Unknown method: %s" % method))

        try:
            if method == "tools/list":
                result = self._mcp_tools_list(params, document_url=document_url)
            elif method == "tools/call":
                cancel_key = (session, req_id) if req_id is not None else None
                if cancel_key is not None:
                    with self._requests_lock:
                        count = self._in_flight_requests.get(cancel_key, 0)
                        if count == 0:
                            self._cancelled_requests.discard(cancel_key)
                        self._in_flight_requests[cancel_key] = count + 1
                try:
                    result = self._mcp_tools_call(params, document_url=document_url, req_id=req_id, session=session)
                finally:
                    if cancel_key is not None:
                        with self._requests_lock:
                            count = self._in_flight_requests.get(cancel_key, 0) - 1
                            if count <= 0:
                                self._in_flight_requests.pop(cancel_key, None)
                                self._cancelled_requests.discard(cancel_key)
                            else:
                                self._in_flight_requests[cancel_key] = count
            else:
                result = one_arg[method](params)
            if log.isEnabledFor(logging.DEBUG):
                preview = str(result)
                cap = 2000 if (isinstance(result, dict) and result.get("isError")) else 100
                log.debug("*** MCP RESULT: %s ***", preview[:cap])
            if result is None:
                return (500, wire_types.jsonrpc_failure(req_id, wire_types.INTERNAL_ERROR, "No result from MCP handler"))
            return (200, wire_types.jsonrpc_success(req_id, result))
        except ValueError as e:
            return (400, wire_types.jsonrpc_failure(req_id, wire_types.INVALID_PARAMS, str(e)))
        except document_mutation_gate.BusyError as e:
            log.warning("MCP %s: busy (%s)", method, e)
            return (429, wire_types.jsonrpc_failure(req_id, wire_types.SERVER_BUSY, str(e), {"retryable": True}))
        except TimeoutError as e:
            log.exception("MCP %s timeout", method)
            return (504, wire_types.jsonrpc_failure(req_id, wire_types.EXECUTION_TIMEOUT, str(e)))
        except Exception as e:
            log.exception("MCP %s error", method)
            return (500, wire_types.jsonrpc_failure(req_id, wire_types.INTERNAL_ERROR, str(e), data=format_error_payload(e)))

    # ── Backpressure execution ───────────────────────────────────────

    def _execute_with_backpressure(self, tool_name: str, arguments: Any, document_url: str | None = None, req_id: Any = None, session: Any = None) -> Any:
        """Execute a fast tool with backpressure.

        Document resolve and the tool body are marshalled to the VCL main thread.
        UNO still runs only on that thread. The per-document gate is acquired on
        this HTTP worker.

        What was wrong: the semaphore was held across the gate wait (up to 30s).
        A long-running mutator on document A made every other fast tools/call,
        including one for document B, time out as BusyError.
        How: prepare still runs under the semaphore. A mutating call releases it
        before the gate and acquires it again only for the body. Read-only calls
        never take the gate, so they keep the semaphore straight through.
        Why: long-running tools take only the gate, so releasing before the wait
        cannot deadlock, and one slow edit no longer 429s unrelated fast calls.
        """
        acquired = _tool_semaphore.acquire(timeout=_WAIT_TIMEOUT)
        if not acquired:
            raise document_mutation_gate.BusyError(_BUSY_MESSAGE)
        holding = True
        try:
            from plugin.framework.send_cancellation import get_current_send_cancellation

            send_cancellation = get_current_send_cancellation()
            prepared = self.queue_executor.execute(self._prepare_mcp_execution, tool_name, arguments, document_url, req_id, timeout=10.0, bound_scope=send_cancellation, send_cancellation=send_cancellation, session=session)
            if not isinstance(prepared, _PreparedMcpCall):
                return prepared
            if not prepared.needs_gate:
                return self.queue_executor.execute(self._invoke_prepared_mcp_tool, prepared, tool_name, arguments, timeout=_PROCESS_TIMEOUT)
            # Gate key is known only after prepare. Drop the process-wide permit
            # before waiting so other documents can take it.
            _tool_semaphore.release()
            holding = False
            with document_mutation_gate._document_mutation_gate(prepared.doc_key, enabled=True):
                acquired = _tool_semaphore.acquire(timeout=_WAIT_TIMEOUT)
                if not acquired:
                    raise document_mutation_gate.BusyError(_BUSY_MESSAGE)
                holding = True
                try:
                    return self.queue_executor.execute(self._invoke_prepared_mcp_tool, prepared, tool_name, arguments, timeout=_PROCESS_TIMEOUT)
                finally:
                    # TimeoutError means the body never started. A body that did
                    # start is not timed out, so this release is not under a live
                    # main-thread tool.
                    _tool_semaphore.release()
                    holding = False
        finally:
            if holding:
                _tool_semaphore.release()

    def _add_other_open_doc_schemas(self, schemas: list[dict[str, Any]], active_doc_type: str | None, exclude_tiers: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Append tools for the other open document types, active type first.

        Returns ``(schemas, {doc_type: doc})`` for the types that were added, so the
        caller can apply the per-doc-type filters (sidebar-only flows) to them too.
        Main-thread only (touches UNO). open_documents already skips a bad
        component. A failure here propagates so tools/list does not omit a family.
        """
        # open_documents already skips a component whose supportsService fails.
        # A raise here is a catalog bug; swallowing it hid a document type.
        by_type = self.services.document.open_documents_by_type()
        if not isinstance(by_type, dict):
            return schemas, {}
        others = {k: v for k, v in by_type.items() if k != active_doc_type}
        if not others:
            return schemas, {}
        from plugin.doc.doc_type import uno_services_for_document

        seen = {sch.get("name") for sch in schemas}
        for other_type, other_doc in others.items():
            # A get_schemas failure is a bug. Skipping the type hid that tool family.
            extra = self.tool_registry.get_schemas("mcp", doc_type=other_type, uno_services_supported=uno_services_for_document(other_doc, other_type), exclude_tiers=exclude_tiers)
            for sch in extra:
                name = sch.get("name")
                if name and name not in seen:
                    seen.add(name)
                    schemas.append(sch)
        log.debug("tools/list broadened past the active %s document to also cover: %s", active_doc_type, ", ".join(sorted(others)))
        return schemas, others

    def _prepare_mcp_execution(self, tool_name: str, arguments: Any, document_url: str | None = None, req_id: Any = None, *, send_cancellation: Any, session: Any = None) -> Any:
        """Main-thread only: unknown-tool check, document resolve, ToolContext, precomputed echo.

        Returns ``_PreparedMcpCall`` or a structured error dict.

        Callers pass the scope captured on the worker, including None.
        None means that worker had no scope. This method does not read the main thread's send contextvar.
        """
        tool = self.tool_registry.get(tool_name)
        if tool is None:
            return {"status": "error", "code": "UNKNOWN_TOOL", "message": "No tool named '%s'. Check tools/list for the exact name (tools are filtered by the open document's type)." % tool_name}

        doc = None
        doc_type = "writer"
        # resolve_document_by_url returns (None, None) when nothing matches.
        # An exception is a disposed document or a real failure. Mapping it to
        # DOCUMENT_NOT_FOUND dropped the message (only the type name was logged).
        doc_svc = self.services.document
        if document_url:
            doc, doc_type = doc_svc.resolve_document_by_url(document_url)
        else:
            doc = _real_active_document(doc_svc)
            if doc:
                doc_type = doc_svc.detect_doc_type(doc)

        if doc is None and document_url:
            return {"status": "error", "code": "DOCUMENT_NOT_FOUND", "message": ("No open document matches document_url '%s'. Call list_open_documents and retry with one of the returned url or uid values." % document_url), "details": {"document_url": document_url}}
        if doc is None and getattr(tool, "requires_document", True):
            # "No document open" used to be decided by the ACTIVE document alone, so a
            # call landing while focus sat on the Start Center (or on another app)
            # answered "LibreOffice isn't open" one second after list_open_documents
            # had listed the very document the caller meant -- reported twice as an
            # intermittent, unreproducible failure. Fall back to what is actually
            # open: unambiguous when there is one document, and when there are several
            # say so and name them instead of denying they exist.
            # Count DOCUMENTS, not types: with two Writer documents open and neither active,
            # a per-type view has one entry and would silently pick the first -- possibly the
            # wrong petition. Only a single open document is unambiguous.
            doc_svc = self.services.document
            # open_documents already returns [] when desktop enumeration fails.
            # Catching here turned a later bug into a false NO_DOCUMENT_OPEN.
            open_docs = doc_svc.open_documents()
            if not isinstance(open_docs, list):
                open_docs = []  # stubbed/unavailable service -> behave as before
            if len(open_docs) == 1:
                doc = open_docs[0]
                doc_type = doc_svc.detect_doc_type(doc)
                log.debug("no active document; falling back to the single open %s document", doc_type)
            elif open_docs:
                return {
                    "status": "error",
                    "code": "NO_ACTIVE_DOCUMENT",
                    "message": ("No document is active in LibreOffice (its window may not have focus), but %d are open: %s. Call list_open_documents and pass document_url (url or uid) to say which one you mean." % (len(open_docs), ", ".join(_doc_titles(open_docs)))),
                }
            else:
                return {"status": "error", "code": "NO_DOCUMENT_OPEN", "message": ("No document open in LibreOffice. Ask the user to open or create a document; list_open_documents works in this state to check what is open.")}

        from plugin.doc.doc_type import uno_services_for_document
        from plugin.framework.uno_context import get_ctx

        ctx = get_ctx()
        uno_services = uno_services_for_document(doc, doc_type)
        active_page_idx = None
        if doc_type in ("draw", "impress"):
            # get_active_page_index returns 0 for a missing page and re-raises
            # disposal. DrawBridge raises RuntimeError when the doc has no page
            # container. The old except turned both into active_page_index None.
            from plugin.draw.bridge import DrawBridge

            active_page_idx = DrawBridge(doc).get_active_page_index()

        def stop_checker() -> bool:
            if req_id is not None:
                cancel_key = (session, req_id)
                with self._requests_lock:
                    if cancel_key in self._cancelled_requests:
                        return True
            if send_cancellation is not None and send_cancellation.is_cancelled():
                return True
            return False

        # MagicMock tools (and any non-bool is_async result) are not async. A
        # non-numeric timeout is not a positive timeout: comparing MagicMock
        # to int raises TypeError and aborts preparation before ToolContext.
        _is_async = _is_long_running(tool)
        _timeout = getattr(tool, "timeout", None)
        if _is_async and _timeout is not None and not (isinstance(_timeout, (int, float)) and _timeout > 0):
            return {"status": "error", "code": "TOOL_EXECUTION_ERROR", "message": "Async tools must declare a positive timeout to run off-thread."}

        context = ToolContext(doc=doc, ctx=ctx, doc_type=doc_type, services=self.services, caller="mcp", active_page_index=active_page_idx, uno_services_supported=uno_services, send_cancellation=send_cancellation, stop_checker=stop_checker)
        return _PreparedMcpCall(tool=tool, context=context, doc=doc, doc_key=_resolve_mcp_doc_key(document_url, doc), needs_gate=document_mutation_gate._tool_needs_document_mutation_gate(tool, arguments), echo=_document_echo_payload(doc))

    def _invoke_prepared_mcp_tool(self, prepared: _PreparedMcpCall, tool_name: str, arguments: Any) -> Any:
        """Registry execute + elapsed/echo. Caller holds the mutation gate when the tool needs it.

        ``prepared.echo`` must already be computed on the main thread.
        Client ``bypass_thread_guard`` is dropped and the keyword is not passed,
        so ``ToolRegistry.execute`` keeps ``execute_safe``. Do not call ``tool.execute``.
        A latched Stop is refused here so the fast path and the long-running path share one pre-execute check.
        """
        stop_checker = prepared.context.stop_checker
        if callable(stop_checker) and stop_checker() is True:
            # What was wrong: the fast path used to skip the Stop check, so a tool
            # cancelled during prepare or the gate wait still ran.
            # How: both paths call this method before execute.
            # Why: the checker only reads the cancel set and the send scope.
            return {"status": "error", "code": "USER_STOPPED", "message": "Stopped by user"}
        # Security: keyword-only bypass flag must never come from a client
        safe_args = {k: v for k, v in arguments.items() if k != "bypass_thread_guard"}
        t0 = time.perf_counter()

        result = self.tool_registry.execute(tool_name, prepared.context, **safe_args)

        elapsed = time.perf_counter() - t0
        if isinstance(result, dict):
            result["_elapsed_ms"] = round(elapsed * 1000, 1)
            if prepared.echo and "document" not in result:
                result["document"] = prepared.echo
        return result

    def _execute_long_running(self, tool_name: str, arguments: Any, document_url: str | None = None, req_id: Any = None, session: Any = None) -> Any:
        """Execute a long-running tool on the current background HTTP thread.

        Context resolution runs on the main thread. Mutating tools hold the same
        per-document gate as backpressure, acquired here on the worker;
        read-only tools skip it. is_async tools (the long-running path) run
        here through execute_safe; the registry marshals sync tools back to
        the main thread. Client arguments cannot set bypass_thread_guard, and
        this method does not pass that keyword.
        """
        from plugin.framework.send_cancellation import get_current_send_cancellation
        send_cancellation = get_current_send_cancellation()
        prepared = self.queue_executor.execute(self._prepare_mcp_execution, tool_name, arguments, document_url, req_id, timeout=10.0, bound_scope=send_cancellation, send_cancellation=send_cancellation, session=session)
        if not isinstance(prepared, _PreparedMcpCall):
            return prepared
        with document_mutation_gate._document_mutation_gate(prepared.doc_key, enabled=prepared.needs_gate):
            return self._invoke_prepared_mcp_tool(prepared, tool_name, arguments)

    # ── Debug helpers ────────────────────────────────────────────────

    def _debug_call_tool(self, tool_name: str, arguments: Any, document_url: str | None = None) -> Any:
        if not tool_name:
            return {"error": "Missing 'tool' parameter"}
        if not isinstance(arguments, dict):
            return {"error": "'args' must be a dictionary"}

        args_copy = dict(arguments)

        if document_url is None:
            document_url = args_copy.pop("document_url", None)

        result = self._execute_with_backpressure(tool_name, args_copy, document_url=document_url)
        return result

    def _debug_trigger(self, command: str) -> Any:
        from plugin.main import get_services

        if command == "settings":
            from plugin.chatbot.dialog_views import settings_box

            registry = get_services()
            if registry is None:
                return {"error": "Services not initialized"}
            if registry.get("config") is None:
                return {"error": "No config service"}
            from plugin.framework.uno_context import get_ctx

            ctx = get_ctx()
            self.queue_executor.execute(settings_box, ctx, timeout=120.0)
            return "Settings dialog shown"
        return {"triggered": command, "note": "Use menu for UI commands"}

    def _debug_services(self) -> Any:
        if not self.services:
            return []
        return list(self.services._services.keys())

    def _debug_config(self, key: Any, value: Any) -> Any:
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

    def _read_body(self, handler: Any) -> Any:
        """Read and parse JSON body from an HTTP handler.

        Same cap, chunked rejection, and socket-timeout failure as ``GenericRequestHandler._read_body``.
        The two entry points used to each call ``rfile.read(content_length)``.
        """
        data, rejected = read_json_body(handler)
        if rejected is not None:
            status, err = rejected
            self._send_json(handler, status, format_error_payload(err))
            return None
        return data

    def _send_empty(self, handler: Any, status: int) -> None:
        """Send an empty response via an HTTP handler."""
        write_http_empty(handler, status, extra_headers=lambda h: mcp_session._send_mcp_response_headers(h, session_id=mcp_session._mcp_session_id))

    def _send_json(self, handler: Any, status: int, data: Any) -> None:
        """Send a JSON response via an HTTP handler."""
        write_http_json(handler, status, data, extra_headers=lambda h: mcp_session._send_mcp_response_headers(h, session_id=mcp_session._mcp_session_id))
