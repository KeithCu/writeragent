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
import json
import logging
import select
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator

from plugin.framework.uno_context import get_runtime_uid, normalize_doc_url
from plugin.framework.queue_executor import QueueExecutor
from plugin.framework.errors import (
    WriterAgentException,
    _resolve_exception_message,
    format_error_payload,
    make_tool_error,
    safe_json_loads,
)
from plugin.mcp.cors import send_cors_headers
from plugin.mcp.http_trace import log_mcp_transport_entry, log_unsupported_protocol_version
from plugin.mcp.server import write_http_empty, write_http_json
from plugin.mcp.mcp_state import MCPState, MCPStateStr, EventKind, MCPEvent, ParseRequestEffect, ExecuteToolEffect, StreamResponseEffect, SendErrorEffect, next_state
from plugin.mcp import wire_types

log = logging.getLogger("writeragent.mcp.protocol")

# Local binding for headers/handlers; canonical constant is wire_types.MCP_PROTOCOL_VERSION.
MCP_PROTOCOL_VERSION = wire_types.MCP_PROTOCOL_VERSION
_SUPPORTED_HTTP_PROTOCOL_VERSIONS = frozenset({MCP_PROTOCOL_VERSION, "2024-11-05"})

def _document_echo_payload(doc: Any) -> dict[str, Any] | None:
    """{name, uid} of the resolved target document, or None. Reads UNO properties — call ONLY
    where UNO access is legal (the main thread); worker-thread callers must precompute this."""
    if doc is None:
        return None
    try:
        import os
        from urllib.parse import unquote

        url = str(getattr(doc, "URL", "") or "")
        name = unquote(os.path.basename(url)) if url else "Untitled"
        return {"name": name, "uid": get_runtime_uid(doc)}
    except Exception:
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
    context: object
    doc: object
    doc_key: str
    needs_gate: bool
    echo: dict | None


def _attach_precomputed_echo(result: Any, echo: dict[str, Any] | None) -> None:
    if isinstance(result, dict) and echo and "document" not in result:
        result["document"] = echo


def _attach_document_echo(result: Any, doc: Any) -> None:  # pyright: ignore[reportUnusedFunction]
    """Echo the resolved target document ({name, uid}) in a tool result. Without an explicit
    document_url the target follows the USER'S window focus and can change between two calls —
    the echo lets the agent detect that instead of silently editing the wrong document.
    MAIN-THREAD callers only (see _document_echo_payload). Imported by tests.
    """
    _attach_precomputed_echo(result, _document_echo_payload(doc))


# Pointer to the on-demand manual (T4 / G2 consolidation). The full how-to — editing, editing-html,
# review-modes, search, navigation, images, concurrency — is served per topic by get_guidance(topic), so the model
# pulls one section when needed instead of front-loading a manual here (Claude Desktop doesn't read
# `instructions`; Claude Code truncates it). The topic texts are the shared prompt pieces in
# constants.py (single source with the sidebar prompt), mapped per doc type by agent_manual.py.
_MCP_GUIDANCE_POINTER = (
    " HOW TO USE THESE TOOLS: call get_guidance(topic) for the manual — topics follow the open "
    "document's type (for Writer: editing, editing-html, review-modes, search, navigation, images, "
    "concurrency); call get_guidance() for the current list. "
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
_MCP_CALC_DATETIME_HINT = (
    " When writing Calc date/time cells, use the same offset-free ISO as the clock above "
    "(YYYY-MM-DD, HH:MM[:SS], YYYY-MM-DDTHH:MM[:SS]) or PTnHnMnS for elapsed values; "
    "do not append a timezone offset or Z."
)


def build_initialize_instructions(mode: str, *, now: datetime.datetime | None = None) -> str:
    """Assemble the MCP initialize `instructions` string for a tool-exposure mode.

    Pure function (no server/UNO) so the wording is unit-testable. `mode` is one of
    'direct_flat', 'direct_discovery', or anything else (treated as the delegate default)."""
    # Tool-choice only: targeting + type filter. Edit/nav/bulk stay in get_guidance — not a second manual.
    base = (
        "WriterAgent MCP — AI document workspace. WORKFLOW: "
        "1) With more than one document open, call list_open_documents and pass document_url "
        "(url or uid) on later tools; do not assume focus is stable. "
        "2) tools/list is filtered by active document type (writer/calc/draw)."
    )
    if mode == "direct_flat":
        mode_hint = (
            " Specialized tools are listed directly in tools/list; call them by name. "
            "No WriterAgent LLM endpoint is required."
        )
    elif mode == "direct_discovery":
        mode_hint = (
            " Call find_tools with no arguments for the full specialized domain catalog, "
            "then find_tools(domain=…) for tool schemas in that area; call tools by name. "
            "No WriterAgent LLM endpoint is required."
        )
    else:
        mode_hint = (
            " For specialized capabilities, call delegate_to_specialized_*_toolset with domain and task "
            "(requires a WriterAgent chat endpoint for the inner agent)."
        )
    return _format_mcp_clock_context(now) + " " + base + mode_hint + _MCP_CALC_DATETIME_HINT + _MCP_GUIDANCE_POINTER


def _get_request_protocol_version(handler: Any) -> str | None:
    for name in ("Mcp-Protocol-Version", "mcp-protocol-version", "MCP-Protocol-Version"):
        value = handler.headers.get(name)
        if value:
            return value.strip()
    return None


def _get_request_session_id(handler: Any) -> str | None:
    for name in ("Mcp-Session-Id", "mcp-session-id", "MCP-Session-Id"):
        value = handler.headers.get(name)
        if value:
            return value.strip()
    return None


def _validate_http_protocol_version(handler: Any) -> tuple[int, dict[str, Any]] | None:
    """Return (status, jsonrpc_body) when the HTTP Mcp-Protocol-Version header is unsupported."""
    requested = _get_request_protocol_version(handler)
    if requested is None or requested in _SUPPORTED_HTTP_PROTOCOL_VERSIONS:
        return None
    log_unsupported_protocol_version(handler, requested)
    return (400, wire_types.jsonrpc_failure(None, wire_types.INVALID_REQUEST, "Unsupported MCP-Protocol-Version: %s" % requested))


def _send_mcp_response_headers(handler: Any, *, session_id: str | None = None) -> None:
    """CORS plus streamable-HTTP MCP headers on every MCP transport response."""
    send_cors_headers(handler, preflight=False)
    handler.send_header("Mcp-Protocol-Version", MCP_PROTOCOL_VERSION)
    if session_id:
        handler.send_header("Mcp-Session-Id", session_id)


# Backpressure — one fast MCP tool at a time on the main thread (_execute_with_backpressure).
# Long-running tools skip this; see docs/framework/threading.md § MCP tool execution paths.
_tool_semaphore = threading.Semaphore(1)
_WAIT_TIMEOUT = 5.0
_PROCESS_TIMEOUT = 60.0

_ACTIVE_DOCUMENT_SENTINEL = "__active_document__"

_doc_gates: dict[str, threading.Lock] = {}
_doc_gates_guard = threading.Lock()


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
    try:
        if not doc.supportsService("com.sun.star.document.OfficeDocument"):
            return None
    except Exception:
        pass  # can't introspect -> keep it (don't break a real document)
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
        try:
            uid = get_runtime_uid(doc)
            if uid:
                return "uid:%s" % uid
            url = normalize_doc_url(doc.getURL())
            if url:
                return "url:%s" % url
        except Exception:
            log.debug("Could not resolve document key for the mutation gate", exc_info=True)
    # No resolved doc (or one with neither uid nor URL): best-effort key off the raw request URL,
    # namespaced ("url:") so it can never collide with a resolved "uid:"/"url:" key; else the
    # active-document sentinel.
    if document_url:
        return "url:%s" % normalize_doc_url(document_url)
    return _ACTIVE_DOCUMENT_SENTINEL


def _get_document_mutation_gate(doc_key: str) -> threading.Lock:
    # Future: prune _doc_gates[doc_key] on document OnUnload if a long-lived MCP server
    # opens enough unique URLs that this dict becomes measurable overhead.
    with _doc_gates_guard:
        gate = _doc_gates.get(doc_key)
        if gate is None:
            gate = threading.Lock()
            _doc_gates[doc_key] = gate
        return gate


def _tool_needs_document_mutation_gate(tool: Any, arguments: Any = None) -> bool:
    if tool is None:
        return True  # unknown tool -> be safe
    try:
        return bool(tool.requires_document_lock(arguments))
    except Exception:
        return bool(tool.detects_mutation())


@contextmanager
def _document_mutation_gate(doc_key: str, *, enabled: bool, timeout: float = 30.0) -> Generator[None, None, None]:
    if not enabled:
        yield
        return
    gate = _get_document_mutation_gate(doc_key)
    acquired = gate.acquire(timeout=timeout)
    if not acquired:
        log.warning("MCP _document_mutation_gate timed out after %ss waiting for %s", timeout, doc_key)
        raise BusyError(f"Timed out waiting for document mutation lock ({doc_key})")
    try:
        yield
    finally:
        gate.release()


class BusyError(WriterAgentException):
    """The VCL main thread is already processing another tool call."""

    code: str = "SERVER_BUSY"


# One session id for the whole soffice process, shared by every MCP client.
# Minted on first successful initialize; never rotated; never cleared on DELETE.
_mcp_session_id = None
_mcp_session_lock = threading.Lock()
_SESSION_EXPIRED_MSG = "Session expired (server restarted). Call initialize again."


def _mint_session_id_once() -> str:
    """Assign uuid4 on first successful initialize; later calls keep that id."""
    global _mcp_session_id
    with _mcp_session_lock:
        if _mcp_session_id is None:
            _mcp_session_id = str(uuid.uuid4())
        return _mcp_session_id


def _reject_stale_session(handler: Any, msg: Any = None) -> bool:
    """Write HTTP 404 when Mcp-Session-Id is present and not the process id.

    Spec clients re-initialize on 404, not 409 or silent success. No header is
    allowed (CLI / first contact). A single ``initialize`` is always allowed —
    that is recovery after restart. A stale header on a batch 404s the whole
    request so we never process some items.
    """
    incoming = _get_request_session_id(handler)
    if incoming is None:
        return False
    if isinstance(msg, dict) and msg.get("method") == "initialize":
        return False
    if incoming == _mcp_session_id:
        return False
    req_id = msg.get("id") if isinstance(msg, dict) else None
    log.info("[MCP] stale session id %r (current=%r) — 404", incoming, _mcp_session_id)
    write_http_json(
        handler,
        404,
        wire_types.jsonrpc_failure(req_id, wire_types.INVALID_REQUEST, _SESSION_EXPIRED_MSG),
        extra_headers=lambda h: _send_mcp_response_headers(h, session_id=_mcp_session_id),
    )
    return True


class MCPProtocolHandler:
    """MCP JSON-RPC protocol — route handlers for the HTTP server."""

    def __init__(self, services: Any) -> None:
        self.services = services
        self.queue_executor = services.get("main_thread") or QueueExecutor(ctx=services.get("uno") if services else None)
        self.tool_registry = services.tools
        self.event_bus = getattr(services, "events", None)
        self.version = "unknown"
        try:
            from plugin.version import EXTENSION_VERSION

            self.version = EXTENSION_VERSION
        except ImportError:
            pass

    # ── Raw handlers (receive GenericRequestHandler) ─────────────────

    def handle_mcp_post(self, handler: Any) -> None:
        """POST /mcp — MCP streamable-http (JSON-RPC 2.0)."""
        log_mcp_transport_entry(handler, "mcp")
        version_error = _validate_http_protocol_version(handler)
        if version_error is not None:
            status, response = version_error
            self._send_json(handler, status, response)
            return
        body = self._read_body(handler)
        if body is None:
            return
        document_url = handler.headers.get("X-Document-URL") or None
        self._handle_mcp(body, handler, document_url=document_url)

    def handle_mcp_sse(self, handler: Any) -> None:
        """GET /mcp — SSE notification stream (keepalive)."""
        log_mcp_transport_entry(handler, "mcp-sse")
        if _reject_stale_session(handler):
            return
        accept = handler.headers.get("Accept", "")
        if "text/event-stream" not in accept:
            self._send_json(handler, 406, {"error": "Not Acceptable: must Accept text/event-stream"})
            return
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        _send_mcp_response_headers(handler)
        handler.end_headers()
        self._run_sse_keepalive_loop(handler)

    def handle_mcp_delete(self, handler: Any) -> None:
        """DELETE /mcp — not supported: one process-wide session must stay alive."""
        # Bugfix: Nelson a3d69e68 / GitHub #38. Streamable HTTP lets a client
        # DELETE the session URL to end it. WriterAgent has one session id for
        # the whole soffice process, shared by every client. Returning 200
        # claimed the session ended when it did not; clearing the id would cut
        # every other client off. 405 tells spec clients the session is still
        # here. They recover on 404 (stale id after restart), not 409 or 200.
        log_mcp_transport_entry(handler, "mcp")

        def _headers(h: Any) -> None:
            _send_mcp_response_headers(h)
            h.send_header("Allow", "GET, POST, OPTIONS")

        write_http_empty(handler, 405, extra_headers=_headers)

    def handle_sse_stream(self, handler: Any) -> None:
        """GET /sse — legacy SSE transport (keepalive only)."""
        if _reject_stale_session(handler):
            return
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-cache")
            handler.send_header("Connection", "keep-alive")
            handler.send_header("X-Accel-Buffering", "no")
            _send_mcp_response_headers(handler)
            handler.end_headers()
            log.info("[SSE] GET stream opened")
            self._run_sse_keepalive_loop(handler)
        except (BrokenPipeError, ConnectionResetError, OSError):
            log.info("[SSE] GET stream disconnected")

    def _run_sse_keepalive_loop(self, handler: Any, interval: float = 15) -> None:
        """Run a keepalive loop for an SSE stream without blocking the worker thread
        longer than necessary on disconnect.
        """
        sock = handler.connection
        try:
            while True:
                try:
                    handler.wfile.write(b": keepalive\n\n")
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break

                # Wait for either activity on the socket (client disconnect or data)
                # or the timeout to send the next keepalive.
                # select.select on the socket returns if it's readable, which
                # for a client that only receives means EOF (disconnect).
                r, _unused, _unused2 = select.select([sock], [], [], interval)
                if r:
                    try:
                        # Peek at the data to see if it's EOF (empty byte)
                        peek = sock.recv(1, socket.MSG_PEEK)
                        if not peek:
                            # Client closed connection
                            break
                        # If there was actual data (unexpected for SSE GET),
                        # we consume it to avoid immediate re-triggering of select.
                        sock.recv(4096)
                    except (ConnectionResetError, OSError):
                        break
        except Exception as e:
            log.debug("SSE keepalive loop exception: %s", e)
        finally:
            log.info("[SSE] GET stream closed")

    def handle_sse_post(self, handler: Any) -> None:
        """POST /sse or /messages — streamable HTTP (same as /mcp)."""
        log_mcp_transport_entry(handler, "sse")
        version_error = _validate_http_protocol_version(handler)
        if version_error is not None:
            status, response = version_error
            self._send_json(handler, status, response)
            return
        body = self._read_body(handler)
        if body is None:
            return
        document_url = handler.headers.get("X-Document-URL") or None
        self._handle_mcp(body, handler, document_url=document_url)

    # ── Simple handlers (body, headers, query) -> (status, dict) ─────

    def handle_debug_info(self, body: Any, headers: Any, query: Any) -> tuple[int, dict[str, Any]]:
        """GET /debug — show available debug actions."""
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
            from plugin.framework.errors import format_error_payload

            log.exception("Debug %s error", action)
            self._send_json(handler, 500, format_error_payload(e))

    # ── MCP protocol handler ─────────────────────────────────────────

    def _handle_mcp(self, msg: Any, handler: Any, document_url: str | None = None) -> None:
        """Route MCP JSON-RPC request(s) — single or batch."""
        method = msg.get("method", "?") if isinstance(msg, dict) else "batch"
        req_id = msg.get("id") if isinstance(msg, dict) else None
        log.info("[MCP] <<< %s (id=%s)", method, req_id)

        if _reject_stale_session(handler, msg):
            return

        is_initialize = isinstance(msg, dict) and msg.get("method") == "initialize"

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
                write_http_empty(handler, 202, extra_headers=_send_mcp_response_headers)
            return

        # Single request
        result = self._process_jsonrpc(msg, document_url=document_url)
        if result is None:
            write_http_empty(handler, 202, extra_headers=lambda h: _send_mcp_response_headers(h, session_id=_mcp_session_id))
            return
        status, response = result

        if is_initialize and status == 200:
            _mint_session_id_once()

        log.info("[MCP] >>> %s (id=%s) -> %d", method, req_id, status)
        write_http_json(
            handler,
            status,
            response,
            extra_headers=lambda h: _send_mcp_response_headers(h, session_id=_mcp_session_id),
            indent=2,
        )

    # ── MCP method handlers ──────────────────────────────────────────

    def _mcp_initialize(self, params: Any) -> Any:
        client_version = params.get("protocolVersion", MCP_PROTOCOL_VERSION)
        return wire_types.initialize_result(
            protocol_version=MCP_PROTOCOL_VERSION,
            client_protocol_version=client_version,
            server_version=self.version,
            instructions=build_initialize_instructions(self._tool_exposure_mode()),
        )

    def _mcp_ping(self, params: Any) -> Any:
        return wire_types.ping_result()

    def _tool_exposure_mode(self) -> str:
        """Read mcp.tool_exposure_mode (delegate | direct_flat | direct_discovery)."""
        try:
            return self.services.config.get("mcp.tool_exposure_mode", "delegate") or "delegate"
        except Exception:
            return "delegate"

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
            schemas = self.tool_registry.get_schemas(
                "mcp",
                doc_type=doc_type,
                uno_services_supported=uno_services,
                exclude_tiers=exclude_tiers,
                **doc_filter,
            )

            # A domain whose backend is not configured is hidden from the discovery catalog; the
            # flat list has to agree, or the same install advertises a capability in one exposure
            # mode and not the other. This block already runs on the main thread, which get_ctx
            # requires.
            from plugin.framework.uno_context import get_ctx

            try:
                uno_ctx = get_ctx()
            except Exception:
                uno_ctx = None  # no context to ask -> advertise, same as the catalog does
            schemas = drop_unavailable_domains(schemas, self.tool_registry, uno_ctx)

            if mode == "direct_flat":
                # Keep Writer sidebar-only flows (brainstorming, writing_plan) out of the flat
                # list -- they need bespoke session orchestration the direct modes don't give.
                from plugin.doc.find_tools_tool import sidebar_only_tool_names
                sidebar_only = sidebar_only_tool_names(
                    self.tool_registry,
                    doc,
                    doc_type=doc_type,
                    uno_services_supported=uno_services,
                )
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

    def _mcp_tools_call(self, params: Any, document_url: str | None = None) -> Any:
        state = MCPState(status=MCPStateStr.IDLE)

        call_params = wire_types.CallToolRequestParams.from_params(params)
        tool_name = call_params.name
        arguments = dict(call_params.arguments)
        arg_document_url = arguments.pop("document_url", None)
        if arg_document_url:
            document_url = arg_document_url

        # find_tools is the discovery search tool; it is only advertised in
        # direct_discovery mode, so reject calling it by name in other modes -- otherwise
        # the default (delegate) behavior would not really be unchanged.
        if tool_name == "find_tools" and self._tool_exposure_mode() != "direct_discovery":
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "status": "error", "code": "UNKNOWN_TOOL",
                    "message": "Tool 'find_tools' is only available when mcp.tool_exposure_mode is 'direct_discovery'.",
                }, ensure_ascii=False)}],
                "isError": True,
            }

        tool = self.tool_registry.get(tool_name)
        is_long_running = getattr(tool, "long_running", False) if tool else False

        initial_event = MCPEvent(kind=EventKind.REQUEST_RECEIVED, data={"tool_name": tool_name, "arguments": arguments, "document_url": document_url, "is_long_running": is_long_running})

        # State machine runner
        events_to_process = [initial_event]
        final_result = None

        while events_to_process:
            event = events_to_process.pop(0)
            tr = next_state(state, event)
            state = tr.state
            effects = tr.effects

            for effect in effects:
                if isinstance(effect, ParseRequestEffect):
                    log.debug(f"*** tools/call: {state.tool_name}, event_bus={self.event_bus} ***")
                    event_bus = getattr(self, "event_bus", None)
                    if event_bus is not None:
                        event_bus.emit("mcp:request", tool=state.tool_name, args=state.arguments, method="tools/call")

                elif isinstance(effect, ExecuteToolEffect):
                    try:
                        if effect.is_long_running:
                            res = self._execute_long_running(effect.tool_name, effect.arguments, document_url=effect.document_url)
                        else:
                            res = self._execute_with_backpressure(effect.tool_name, effect.arguments, document_url=effect.document_url)
                        events_to_process.append(MCPEvent(kind=EventKind.TOOL_COMPLETED, data={"result": res}))
                    except BusyError:
                        raise
                    except TimeoutError:
                        raise
                    except Exception as e:
                        # Tool failures must be MCP tool results (isError), not JSON-RPC
                        # INTERNAL_ERROR. Clients treat HTTP 500 as transient and retry
                        # (Hermes retried apply_style ~150× in 0.5s). BusyError/TimeoutError
                        # stay 429/504 above. WriterAgentException used to re-raise into
                        # _process_jsonrpc as HTTP 500 — that is the retryable path.
                        log.exception("MCP tool %s raised unexpectedly", effect.tool_name)
                        code = getattr(e, "code", None) or "TOOL_EXECUTION_ERROR"
                        if code == "INTERNAL_ERROR":
                            code = "TOOL_EXECUTION_ERROR"
                        events_to_process.append(MCPEvent(
                            kind=EventKind.TOOL_COMPLETED,
                            data={"result": make_tool_error(
                                _resolve_exception_message(e),
                                code=code,
                                tool_name=effect.tool_name,
                                error_type=type(e).__name__,
                            )},
                        ))

                elif isinstance(effect, StreamResponseEffect):
                    event_bus = getattr(self, "event_bus", None)
                    if event_bus is not None:
                        snippet = str(effect.result)[:100] if effect.result else ""
                        event_bus.emit("mcp:result", tool=state.tool_name, result_snippet=snippet, args=state.arguments)

                    # A tool may return an image: {"_mcp_image": {"data": <b64>, "mimeType": ...}} ->
                    # emit a native MCP image content block (get_image) instead of base64-as-text.
                    res = effect.result
                    img = res.get("_mcp_image") if isinstance(res, dict) else None
                    if isinstance(img, dict) and img.get("data"):
                        final_result = wire_types.call_tool_result_image(
                            img["data"], img.get("mimeType", "image/png"), is_error=effect.is_error,
                        )
                    else:
                        final_result = wire_types.call_tool_result(
                            json.dumps(res, ensure_ascii=False, default=str),
                            is_error=effect.is_error,
                        )

                elif isinstance(effect, SendErrorEffect):
                    raise ValueError(effect.message)

        return final_result

    # ── JSON-RPC processing ──────────────────────────────────────────

    def _process_jsonrpc(self, msg: Any, document_url: str | None = None) -> Any:
        """Process a JSON-RPC message.

        Returns (http_status, response_dict) or None for notifications (no ``id``).
        """
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return (400, wire_types.jsonrpc_failure(None, wire_types.INVALID_REQUEST, "Invalid JSON-RPC 2.0 request"))

        # Notifications must not receive a JSON-RPC response (HTTP 202, empty body).
        if wire_types.is_jsonrpc_notification(msg):
            return None

        parsed = wire_types.parse_jsonrpc_request(msg)
        if isinstance(parsed, wire_types.JsonRpcParseError):
            return (400, wire_types.jsonrpc_failure(None, parsed.code, parsed.message))

        method = parsed.method
        params = parsed.params
        req_id = parsed.req_id

        handler = {"initialize": self._mcp_initialize, "ping": self._mcp_ping, "tools/list": self._mcp_tools_list, "tools/call": self._mcp_tools_call, "resources/list": self._mcp_resources_list, "prompts/list": self._mcp_prompts_list}.get(method)

        log.debug(f"*** MCP INCOMING METHOD: {method} (id={req_id}) ***")

        if handler is None:
            return (400, wire_types.jsonrpc_failure(req_id, wire_types.METHOD_NOT_FOUND, "Unknown method: %s" % method))

        try:
            if method == "tools/list":
                result = self._mcp_tools_list(params, document_url=document_url)
            elif method == "tools/call":
                result = self._mcp_tools_call(params, document_url=document_url)
            else:
                result = handler(params)
            preview = str(result)
            cap = 2000 if (isinstance(result, dict) and result.get("isError")) else 100
            log.debug("*** MCP RESULT: %s ***", preview[:cap])
            if result is None:
                return (500, wire_types.jsonrpc_failure(req_id, wire_types.INTERNAL_ERROR, "No result from MCP handler"))
            return (200, wire_types.jsonrpc_success(req_id, result))
        except ValueError as e:
            return (400, wire_types.jsonrpc_failure(req_id, wire_types.INVALID_PARAMS, str(e)))
        except BusyError as e:
            log.warning("MCP %s: busy (%s)", method, e)
            return (429, wire_types.jsonrpc_failure(req_id, wire_types.SERVER_BUSY, str(e), {"retryable": True}))
        except TimeoutError as e:
            log.exception("MCP %s timeout", method)
            return (504, wire_types.jsonrpc_failure(req_id, wire_types.EXECUTION_TIMEOUT, str(e)))
        except WriterAgentException as e:
            log.exception("MCP %s error", method)
            return (500, wire_types.jsonrpc_failure(req_id, wire_types.INTERNAL_ERROR, e.message, data=format_error_payload(e)))
        except Exception as e:
            log.exception("MCP %s error", method)
            return (500, wire_types.jsonrpc_failure(req_id, wire_types.INTERNAL_ERROR, str(e), data=format_error_payload(e)))

    # ── Backpressure execution ───────────────────────────────────────

    def _execute_with_backpressure(self, tool_name: str, arguments: Any, document_url: str | None = None) -> Any:
        """Execute a tool on the VCL main thread with backpressure.

        Acquires _tool_semaphore then _execute_tool_on_main (which holds the per-doc
        mutation gate for mutating tools). UNO runs on the main thread only.
        """
        acquired = _tool_semaphore.acquire(timeout=_WAIT_TIMEOUT)
        if not acquired:
            raise BusyError("LibreOffice is busy processing another tool call. Please wait a moment and retry.")
        try:
            return self.queue_executor.execute(self._execute_tool_on_main, tool_name, arguments, document_url, timeout=_PROCESS_TIMEOUT)
        finally:
            _tool_semaphore.release()

    def _prepare_mcp_execution(self, tool_name: str, arguments: Any, document_url: str | None = None) -> Any:
        """Main-thread only: unknown-tool check, document resolve, ToolContext, precomputed echo.

        Returns ``_PreparedMcpCall`` or a structured error dict.
        """
        tool = self.tool_registry.get(tool_name)
        if tool is None:
            return {"status": "error", "code": "UNKNOWN_TOOL",
                    "message": "No tool named '%s'. Check tools/list for the exact name (tools are filtered by the open document's type)." % tool_name}

        doc = None
        doc_type = "writer"
        try:
            doc_svc = self.services.document
            if document_url:
                doc, doc_type = doc_svc.resolve_document_by_url(document_url)
            else:
                doc = _real_active_document(doc_svc)
                if doc:
                    doc_type = doc_svc.detect_doc_type(doc)
        except Exception as e:
            log.warning("Error resolving context in execution: %s", type(e).__name__)
            doc = None

        if doc is None and document_url:
            return {"status": "error", "code": "DOCUMENT_NOT_FOUND",
                    "message": ("No open document matches document_url '%s'. Call list_open_documents and retry "
                                "with one of the returned url or uid values." % document_url),
                    "details": {"document_url": document_url}}
        if doc is None and getattr(tool, "requires_document", True):
            return {"status": "error", "code": "NO_DOCUMENT_OPEN",
                    "message": ("No document open in LibreOffice. Ask the user to open or create a document; "
                                "list_open_documents works in this state to check what is open.")}

        from plugin.doc.doc_type import uno_services_for_document
        from plugin.framework.tool import ToolContext
        from plugin.framework.uno_context import get_ctx

        ctx = get_ctx()
        uno_services = uno_services_for_document(doc, doc_type)
        active_page_idx = None
        if doc_type in ("draw", "impress"):
            try:
                from plugin.draw.bridge import DrawBridge
                active_page_idx = DrawBridge(doc).get_active_page_index()
            except Exception:
                pass

        context = ToolContext(
            doc=doc,
            ctx=ctx,
            doc_type=doc_type,
            services=self.services,
            caller="mcp",
            active_page_index=active_page_idx,
            uno_services_supported=uno_services,
        )
        return _PreparedMcpCall(
            tool=tool,
            context=context,
            doc=doc,
            doc_key=_resolve_mcp_doc_key(document_url, doc),
            needs_gate=_tool_needs_document_mutation_gate(tool, arguments),
            echo=_document_echo_payload(doc),
        )

    def _run_prepared_mcp_execute(self, prepared: _PreparedMcpCall, tool_name: str, arguments: Any) -> Any:
        """Gate + registry execute + elapsed/echo. ``prepared.echo`` must already be computed on main."""
        with _document_mutation_gate(prepared.doc_key, enabled=prepared.needs_gate):
            t0 = time.perf_counter()
            result = self.tool_registry.execute(tool_name, prepared.context, **arguments)
            elapsed = time.perf_counter() - t0
        if isinstance(result, dict):
            result["_elapsed_ms"] = round(elapsed * 1000, 1)
            _attach_precomputed_echo(result, prepared.echo)
        return result

    def _execute_long_running(self, tool_name: str, arguments: Any, document_url: str | None = None) -> Any:
        """Execute a long-running tool on the current background HTTP thread.

        Context resolution runs on the main thread. Mutating tools hold the same
        per-document gate as _execute_tool_on_main; read-only tools skip it.
        Tool bodies run on the HTTP worker; UNO inside tools uses execute_on_main_thread.
        """
        prepared = self.queue_executor.execute(self._prepare_mcp_execution, tool_name, arguments, document_url, timeout=10.0)
        if not isinstance(prepared, _PreparedMcpCall):
            return prepared
        return self._run_prepared_mcp_execute(prepared, tool_name, arguments)

    def _execute_tool_on_main(self, tool_name: str, arguments: Any, document_url: str | None = None) -> Any:
        """Run a backpressure tool on the main thread; shares _document_mutation_gate with long-running path."""
        prepared = self._prepare_mcp_execution(tool_name, arguments, document_url)
        if not isinstance(prepared, _PreparedMcpCall):
            return prepared
        return self._run_prepared_mcp_execute(prepared, tool_name, arguments)

    # ── Debug helpers ────────────────────────────────────────────────

    def _debug_call_tool(self, tool_name: str, arguments: Any, document_url: str | None = None) -> Any:
        if not tool_name:
            return {"error": "Missing 'tool' parameter"}
        result = self._execute_with_backpressure(tool_name, arguments, document_url=document_url)
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

    def _detect_active_doc_type(self) -> Any:
        try:
            doc_svc = self.services.document
            doc = _real_active_document(doc_svc)
            if doc:
                return doc_svc.detect_doc_type(doc)
        except Exception as e:
            log.warning("Error detecting doc type: %s", type(e).__name__)
            pass
        return None

    def _read_body(self, handler: Any) -> Any:
        """Read and parse JSON body from an HTTP handler."""
        content_length = int(handler.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        raw = handler.rfile.read(content_length).decode("utf-8")
        data = safe_json_loads(raw, default=None, strict=True)
        if data is None and raw.strip():
            log.warning("Invalid JSON body: %s", raw[:200])
            from plugin.framework.errors import AgentParsingError, format_error_payload

            err = AgentParsingError("Invalid JSON body in HTTP request", details={"raw": raw[:200]})
            self._send_json(handler, 400, format_error_payload(err))
            return None
        return data if data is not None else {}

    def _send_json(self, handler: Any, status: int, data: Any) -> None:
        """Send a JSON response via an HTTP handler."""
        write_http_json(handler, status, data, extra_headers=_send_mcp_response_headers)
