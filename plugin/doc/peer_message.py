# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A1 peer send: ``send_peer_work`` / ``send_peer_result``.

Queues a user-equivalent turn on another already-open Writer/Calc/Draw/Impress
sidebar and returns immediately. Experiment: advertised on the
document_research specialized loop only (not outer chat, not MCP).
Kind is which tool was called (work vs result). See ``docs/chat/peer-messaging.md``.

This module must not import ``plugin.chatbot.panel`` or
``plugin.chatbot.panel_factory`` (cycle: CommonModule → tool → panel →
``get_tools()``).
"""

from __future__ import annotations

import copy
import logging
import os
import weakref
from collections import deque
from dataclasses import dataclass
from typing import Any, ClassVar
from weakref import WeakKeyDictionary

from plugin.framework.async_drain_guard import add_drain_idle_callback, get_drain_owner
from plugin.framework.tool import ToolBase, ToolContext

log = logging.getLogger("writeragent.doc.peer_message")

PEER_WORK_TOOL_NAME = "send_peer_work"
PEER_RESULT_TOOL_NAME = "send_peer_result"
PEER_TOOL_NAMES = frozenset({PEER_WORK_TOOL_NAME, PEER_RESULT_TOOL_NAME})
PEER_QUEUE_CAP = 8
PEER_SPECIALIZED_DOMAIN = "document_research"
# MCP / script / venv RPC must never inject a sidebar turn. Chat main and
# document_research specialized inherit or set caller="chat"; also allow a
# specialized ToolContext that only sets active_domain.
_PEER_SEND_BLOCKED_CALLERS = frozenset({"mcp", "script", "ppt_master_venv"})

_TEXT_SERVICE = "com.sun.star.text.TextDocument"
_CALC_SERVICE = "com.sun.star.sheet.SpreadsheetDocument"
_DRAW_SERVICE = "com.sun.star.drawing.DrawingDocument"
_IMPRESS_SERVICE = "com.sun.star.presentation.PresentationDocument"

_WORK_DESCRIPTION = (
    "ASK PATH only: send a new work request to another already-open Writer, Calc, Draw, "
    "or Impress sidebar. Use when the task is about an Open peer and has no "
    "[Peer work from: …] envelope. Do not call send_peer_result on this path — the peer "
    "replies later. Returns immediately {status: ok, accepted: true}. "
    "After ok/accepted you MUST call specialized_workflow_finished "
    "immediately — the peer runs after this loop exits; waiting deadlocks the reply. "
    "document_url is the one target argument: a file URL, RuntimeUID, or a "
    "display name that matches exactly one open peer. Required on every call. "
    "Never put your own path, uid, or URL in message — the gateway inserts "
    "[Peer work from: name | uid=… | url=…]. Never invent the other app's write tools."
)

_RESULT_DESCRIPTION = (
    "REPLY PATH only: deliver a result/reply when the task contains a "
    "[Peer work from: …] envelope. Do not use this tool on an ask task that merely "
    "mentions send_peer_result or reply back — only that envelope selects this path. "
    "Returns immediately {status: ok, accepted: true}. "
    "After ok/accepted you MUST call specialized_workflow_finished "
    "immediately — the peer runs after this loop exits; waiting deadlocks the reply. "
    "document_url is the one target argument: a file URL, RuntimeUID, or a "
    "display name that matches exactly one open peer (copy uid/url from the inbound "
    "[Peer work from: …] envelope). Required on every call. "
    "Never put your own path, uid, or URL in message — the gateway inserts "
    "[Peer result from: name | uid=… | url=…]. Never invent the other app's write tools."
)

# Reinforces the prompt: specialized agents must exit after accepted.
PEER_ACCEPTED_FINISH_HINT = (
    "Queued. You MUST call specialized_workflow_finished immediately. "
    "Why: the reply arrives later as a follow-up user turn on the caller sidebar; "
    "waiting in this loop blocks the peer."
)


@dataclass
class PeerPendingTurn:
    """One queued extracted send on a listener."""

    wrapped_text: str
    already_appended: bool


_listener_queues: WeakKeyDictionary[Any, deque[PeerPendingTurn]] = WeakKeyDictionary()
_global_fifo: deque[tuple[weakref.ref[Any], PeerPendingTurn]] = deque()
_idle_kick_scheduled = False


def _supports_service(model: Any, service: str) -> bool:
    try:
        supports = getattr(model, "supportsService", None)
        if not callable(supports):
            return False
        return bool(supports(service))
    except Exception:
        return False


def is_v1_peer_model(model: Any) -> bool:
    """True for Writer / Calc / Draw / Impress.

    Impress also supports DrawingDocument, so a Draw-only check already
    matches it. PresentationDocument is listed so a model that only
    reports the Impress service still counts.
    """
    if model is None:
        return False
    return (
        _supports_service(model, _TEXT_SERVICE)
        or _supports_service(model, _CALC_SERVICE)
        or _supports_service(model, _DRAW_SERVICE)
        or _supports_service(model, _IMPRESS_SERVICE)
    )


def v1_peer_type_label(model: Any) -> str | None:
    """``writer`` / ``calc`` / ``draw`` / ``impress``, or None if unsupported.

    Check PresentationDocument before DrawingDocument. Impress supports both,
    and research catalog ``doc_type: "draw"`` (impress_as_draw) would otherwise
    collapse the peer type. Draw-only models stay ``draw``.
    """
    if model is None:
        return None
    if _supports_service(model, _TEXT_SERVICE):
        return "writer"
    if _supports_service(model, _CALC_SERVICE):
        return "calc"
    if _supports_service(model, _IMPRESS_SERVICE):
        return "impress"
    if _supports_service(model, _DRAW_SERVICE):
        return "draw"
    return None


def identity_from_doc(doc: Any) -> dict[str, str]:
    """Sender fields from ``ToolContext.doc`` (not authored in ``message``)."""
    from plugin.framework.uno_context import get_runtime_uid

    uid = ""
    url = ""
    name = "Untitled"
    if doc is None:
        return {"name": name, "uid": uid, "url": url}
    try:
        uid = get_runtime_uid(doc) or ""
    except Exception:
        uid = ""
    try:
        raw_url = doc.getURL() if hasattr(doc, "getURL") else ""
        url = str(raw_url or "")
    except Exception:
        url = ""
    if url:
        try:
            from plugin.doc.document_research import _system_path_from_url

            path = _system_path_from_url(url) or ""
            base = os.path.basename(path) if path else ""
            if base:
                name = base
        except Exception:
            pass
    if name == "Untitled":
        try:
            title = doc.getTitle() if hasattr(doc, "getTitle") else None
            if title:
                name = str(title)
        except Exception:
            pass
    return {"name": name, "uid": uid, "url": url}


# Fixed footer on work envelopes only. Outer PEER_OUTER_DELEGATE_HINT already
# says to re-delegate for send_peer_result, but models ignore it when the body
# is a short list/read ask and Ready with a chat-only answer — the asker never
# gets a peer result. Result envelopes must stay body-only (no delivery footer).
PEER_WORK_DELIVERY_FOOTER = (
    "When the local work is done, you MUST Do domain=\"document_research\" to deliver "
    "via send_peer_result(document_url=<uid or url from this envelope header>, "
    "message=<one HTML/result string>). "
    "Why: a chat-only answer in this sidebar never reaches the asking peer."
)


def format_peer_envelope(
    *,
    name: str,
    uid: str,
    url: str,
    message: str,
    kind: str = "work",
) -> str:
    """Code-inserted wrapper. ``message`` is the body only.

    ``kind`` is which tool was called: ``"work"`` for ``send_peer_work``,
    ``"result"`` for ``send_peer_result``. Models must not invent the header.
    Work envelopes append ``PEER_WORK_DELIVERY_FOOTER``; result envelopes do not.
    """
    label = "Peer result from" if kind == "result" else "Peer work from"
    header = f"[{label}: {name} | uid={uid} | url={url}]"
    body = message.rstrip("\n") if message else message
    if kind == "result":
        return f"{header}\n\n{body}"
    return f"{header}\n\n{body}\n\n{PEER_WORK_DELIVERY_FOOTER}"


def format_peer_catalog(peers: list[dict[str, str]]) -> str:
    """Short open-peer list for the tool description / prompt block."""
    if not peers:
        return ""
    parts = []
    for p in peers:
        parts.append(
            f"{p.get('name') or 'Untitled'} (uid={p.get('uid') or ''}, "
            f"url={p.get('url') or ''}, type={p.get('type') or ''})"
        )
    return "Open peers: " + "; ".join(parts) + "."


def list_v1_peers(uno_ctx: Any, self_doc: Any) -> list[dict[str, str]]:
    """Resolvable other v1 peers (distinct uid, supported model, addressable).

    Desktop catalog + RuntimeUID. Callers marshal to the main thread
    (specialized scaffolding / ``get_peer_inner_choice_block``) or this asserts.
    """
    from plugin.doc.document_research import get_open_documents
    from plugin.framework.thread_guard import assert_main_thread
    from plugin.framework.uno_context import get_runtime_uid, resolve_document_by_url

    assert_main_thread("peer_message.list_v1_peers")
    self_uid = ""
    if self_doc is not None:
        try:
            self_uid = get_runtime_uid(self_doc) or ""
        except Exception:
            self_uid = ""
    peers: list[dict[str, str]] = []
    try:
        catalog = get_open_documents(uno_ctx, self_doc)
    except Exception:
        log.debug("list_v1_peers: get_open_documents failed", exc_info=True)
        return []
    seen: set[str] = set()
    for rec in catalog:
        uid = str(rec.get("uid") or "")
        url = str(rec.get("url") or "")
        if not uid or uid == self_uid or uid in seen:
            continue
        model = None
        try:
            model, _unused_type = resolve_document_by_url(uno_ctx, uid)
        except Exception:
            model = None
        if model is None and url:
            try:
                model, _unused_type = resolve_document_by_url(uno_ctx, url)
            except Exception:
                model = None
        label = v1_peer_type_label(model)
        if label is None:
            continue
        seen.add(uid)
        peers.append(
            {
                "name": str(rec.get("name") or "Untitled"),
                "uid": uid,
                "url": url,
                "type": label,
            }
        )
    return peers


def resolve_peer_target(
    uno_ctx: Any,
    self_doc: Any,
    document_url: str,
) -> tuple[Any | None, str | None, str]:
    """Resolve *document_url* as file URL, RuntimeUID, or unique display name.

    Returns ``(model, error_code, error_message)``. Success: ``(model, None, "")``.
    """
    from plugin.framework.uno_context import get_runtime_uid, resolve_document_by_url

    target = (document_url or "").strip()
    if not target:
        return None, "VALIDATION_ERROR", "document_url is required."

    self_uid = ""
    if self_doc is not None:
        try:
            self_uid = get_runtime_uid(self_doc) or ""
        except Exception:
            self_uid = ""

    model = None
    try:
        model, _unused_type = resolve_document_by_url(uno_ctx, target)
    except Exception:
        model = None

    if model is None:
        peers = list_v1_peers(uno_ctx, self_doc)
        name_hits = [p for p in peers if p.get("name") == target]
        if len(name_hits) > 1:
            return (
                None,
                "PEER_AMBIGUOUS",
                f"document_url {target!r} matches more than one open peer; use a uid or file URL.",
            )
        if len(name_hits) == 1:
            try:
                model, _unused_type = resolve_document_by_url(uno_ctx, name_hits[0]["uid"])
            except Exception:
                model = None

    if model is None:
        return None, "PEER_NOT_FOUND", f"No open Writer, Calc, Draw, or Impress peer matches {target!r}."

    peer_uid = ""
    try:
        peer_uid = get_runtime_uid(model) or ""
    except Exception:
        peer_uid = ""
    if self_uid and peer_uid and self_uid == peer_uid:
        return None, "PEER_SELF", "Cannot send a peer message to the same document."

    if not is_v1_peer_model(model):
        return None, "PEER_UNSUPPORTED", "Target is not a Writer, Calc, Draw, or Impress document."
    return model, None, ""


def listener_is_busy(listener: Any) -> bool:
    """Busy if the send FSM, active stream queue, or cancellation scope is live."""
    if listener is None:
        return True
    state = getattr(listener, "sidebar_state", None)
    send = getattr(state, "send", None) if state is not None else None
    if send is not None and bool(getattr(send, "is_busy", False)):
        return True
    if getattr(listener, "_active_q", None) is not None:
        return True
    if getattr(listener, "_send_cancellation", None) is not None:
        return True
    return False


def listener_queue_len(listener: Any) -> int:
    q = _listener_queues.get(listener)
    return len(q) if q is not None else 0


def enqueue_peer_turn(listener: Any, turn: PeerPendingTurn) -> str | None:
    """Queue *turn*. Returns ``PEER_QUEUE_FULL`` or None."""
    q = _listener_queues.get(listener)
    if q is None:
        q = deque()
        _listener_queues[listener] = q
    if len(q) >= PEER_QUEUE_CAP:
        return "PEER_QUEUE_FULL"
    q.append(turn)
    _global_fifo.append((weakref.ref(listener), turn))
    return None


def drop_listener_queue(listener: Any) -> None:
    """Stop / dispose: drop that listener's pending injects only."""
    global _global_fifo
    if listener is None:
        return
    q = _listener_queues.pop(listener, None)
    if q is not None:
        q.clear()
    _global_fifo = deque(item for item in _global_fifo if item[0]() is not listener)


def reset_peer_queues() -> None:
    """Test hook: clear all pending turns."""
    global _global_fifo, _idle_kick_scheduled
    _listener_queues.clear()
    _global_fifo.clear()
    _idle_kick_scheduled = False


def kick_pending_peer_starts() -> None:
    """Start at most one queued extracted send when the process drain is idle."""
    global _idle_kick_scheduled
    _idle_kick_scheduled = False
    if get_drain_owner() is not None:
        return
    skipped: list[tuple[weakref.ref[Any], PeerPendingTurn]] = []
    started = False
    while _global_fifo and not started:
        ref, turn = _global_fifo.popleft()
        listener = ref()
        if listener is None:
            continue
        q = _listener_queues.get(listener)
        if q is None:
            continue
        try:
            q.remove(turn)
        except ValueError:
            continue
        if listener_is_busy(listener) or get_drain_owner() is not None:
            q.appendleft(turn)
            skipped.append((ref, turn))
            continue
        start_fn = getattr(listener, "start_extracted_peer_send", None)
        if not callable(start_fn):
            continue
        started = True
        start_fn(turn.wrapped_text, already_appended=turn.already_appended)
    for item in reversed(skipped):
        _global_fifo.appendleft(item)


def _on_drain_idle() -> None:
    """Schedule a kick after the current stack unwinds.

    Starting the peer drain inside ``drain_owner_scope``'s ``finally`` would
    run it before the caller’s ``SEND_COMPLETED`` (Ready). ``QueueExecutor.post``
    is inline under ``WRITERAGENT_TESTING=1``, so we never start here — only
    mark that a kick is due. Production posts to the next VCL tick. Mock-sidebar
    Packet P dispatches ``KICK_PEERS`` after Writer Ready (do not force-marshal
    from this callback: that starts Calc during Writer wrapup and sticks Stop).
    """
    global _idle_kick_scheduled
    if get_drain_owner() is not None:
        return
    if not _global_fifo:
        return
    _idle_kick_scheduled = True
    try:
        from plugin.framework.queue_executor import default_executor

        if not default_executor._should_run_inline():
            default_executor.post(kick_pending_peer_starts)
    except Exception:
        log.debug("peer drain-idle schedule failed", exc_info=True)


add_drain_idle_callback(_on_drain_idle)


def schedule_peer_turn(listener: Any, turn: PeerPendingTurn) -> str | None:
    """Enqueue and start now only when the process drain owner is unset."""
    err = enqueue_peer_turn(listener, turn)
    if err:
        return err
    # Never start from caller execute() while a drain owns the pump (accidental A2).
    if get_drain_owner() is None and not listener_is_busy(listener):
        kick_pending_peer_starts()
    return None


def _schema_function_name(schema: dict[str, Any]) -> str:
    fn = schema.get("function")
    if isinstance(fn, dict):
        return str(fn.get("name") or "")
    return str(schema.get("name") or "")


def summarize_peer_tool_on_wire(schemas: list[dict[str, Any]]) -> tuple[bool, int]:
    """Whether either peer tool is advertised, plus Open-peers count from the catalog."""
    for schema in schemas:
        if _schema_function_name(schema) not in PEER_TOOL_NAMES:
            continue
        fn = schema.get("function")
        desc = str(fn.get("description") or "") if isinstance(fn, dict) else str(schema.get("description") or "")
        return True, desc.count("uid=")
    return False, 0


def log_peer_tool_on_wire(schemas: list[dict[str, Any]]) -> None:
    """Headed dig: prove peer tools were on the chat wire (vs the model not calling them)."""
    on_wire, peer_count = summarize_peer_tool_on_wire(schemas)
    names = sorted(
        n for n in (_schema_function_name(s) for s in schemas) if n in PEER_TOOL_NAMES
    )
    log.info(
        "peer tools: on_wire=%s names=%s peer_count=%d",
        on_wire,
        ",".join(names) or "-",
        peer_count,
    )


def _document_research_domain(active_domain: str | None) -> bool:
    base = (active_domain or "").split(":")[0]
    return base == PEER_SPECIALIZED_DOMAIN


def peer_send_caller_allowed(ctx: Any) -> bool:
    """True for sidebar chat and document_research specialized; MCP stays out.

    Specialized smol loops reuse the parent ``ToolContext`` (``caller="chat"``)
    or set ``active_domain="document_research"``. A chat-only check would be
    enough today, but the domain clause keeps execute working if a specialized
    caller is not tagged ``chat``.
    """
    caller = str(getattr(ctx, "caller", "") or "")
    if caller in _PEER_SEND_BLOCKED_CALLERS:
        return False
    if caller == "chat":
        return True
    return _document_research_domain(getattr(ctx, "active_domain", None))


def peer_message_visible_on_specialized(
    *,
    active_domain: str | None,
    peers: list[dict[str, str]],
) -> bool:
    """Advertise only on the document_research inner wire when a v1 peer exists."""
    return bool(peers) and _document_research_domain(active_domain)


def filter_peer_tools_for_specialized(tools: list[Any], uno_ctx: Any, doc: Any) -> list[Any]:
    """Keep both peer tools on document_research only when a v1 peer is open."""
    if not any(getattr(t, "name", None) in PEER_TOOL_NAMES for t in tools):
        return tools
    peers: list[dict[str, str]] = []
    if uno_ctx is not None:
        try:
            peers = list_v1_peers(uno_ctx, doc)
        except Exception:
            log.debug("filter_peer_tools_for_specialized: catalog failed", exc_info=True)
            peers = []
    if not peers:
        return [t for t in tools if getattr(t, "name", None) not in PEER_TOOL_NAMES]
    return tools


def filter_peer_message_schemas(
    schemas: list[dict[str, Any]],
    ctx: Any,
    doc: Any = None,
    active_domain: str | None = None,
) -> list[dict[str, Any]]:
    """Hide peer send tools on the outer chat wire.

    Master/#672 advertised this on main chat when a peer was open. This
    experiment shows both tools only on ``document_research`` schemas, and only
    when a resolvable other v1 peer exists. Catalog is baked into each description.
    """
    if not any(_schema_function_name(s) in PEER_TOOL_NAMES for s in schemas):
        return schemas
    peers: list[dict[str, str]] = []
    if ctx is not None and doc is not None:
        try:
            peers = list_v1_peers(ctx, doc)
        except Exception:
            log.debug("filter_peer_message_schemas: catalog failed", exc_info=True)
            peers = []
    if not peer_message_visible_on_specialized(active_domain=active_domain, peers=peers):
        return [s for s in schemas if _schema_function_name(s) not in PEER_TOOL_NAMES]
    catalog = format_peer_catalog(peers)
    out: list[dict[str, Any]] = []
    for schema in schemas:
        if _schema_function_name(schema) not in PEER_TOOL_NAMES:
            out.append(schema)
            continue
        enriched = copy.deepcopy(schema)
        fn = enriched.get("function")
        if isinstance(fn, dict):
            desc = str(fn.get("description") or "")
            fn["description"] = (desc + " " + catalog).strip()
        elif "description" in enriched:
            enriched["description"] = (str(enriched.get("description") or "") + " " + catalog).strip()
        out.append(enriched)
    return out


def _peer_not_chat_mode(listener: Any) -> bool:
    """True when the target sidebar is not in Chat mode (do not flip librarian/image)."""
    try:
        from plugin.chatbot.chat_sidebar_mode import (
            CHAT_MODE_CHAT,
            SidebarModeFlags,
            mode_from_selector_with_flags,
        )

        flags = getattr(listener, "sidebar_mode_flags", None)
        if not isinstance(flags, SidebarModeFlags):
            flags = SidebarModeFlags()
        selector = getattr(listener, "chat_mode_selector", None)
        if selector is None:
            return False
        mode = mode_from_selector_with_flags(selector, flags)
        return mode != CHAT_MODE_CHAT
    except Exception:
        return False


def _inject_on_listener(listener: Any, wrapped: str) -> None:
    session = getattr(listener, "session", None)
    if session is not None and hasattr(session, "add_user_message"):
        session.add_user_message(wrapped)
    append = getattr(listener, "_append_response", None)
    if callable(append):
        append(wrapped, role="user")


_PEER_PARAMETERS = {
    "type": "object",
    "properties": {
        "document_url": {
            "type": "string",
            "description": (
                "The one target argument: file URL, RuntimeUID, or a display name "
                "that matches exactly one open peer. Required on every call. "
                "Never omit it; never put your own identity here."
            ),
        },
        "message": {
            "type": "string",
            "description": (
                "One string: natural-language task or reply body (an HTML table is one string, "
                "not a JSON array). Do not paste your own path, uid, or URL — "
                "the gateway inserts the [Peer work from: …] or [Peer result from: …] envelope."
            ),
        },
    },
    "required": ["document_url", "message"],
}


class _SendPeerBase(ToolBase):
    """Shared inject/queue/Ready path for work and result peer sends."""

    name = ""  # subclasses set
    description = ""
    envelope_kind: ClassVar[str] = "work"
    tier = "chat"
    # Domain membership: inner document_research sees this; outer chat does not
    # advertise it (filter_peer_message_schemas). Cross-cutting so Writer/Calc/
    # Draw/Impress document_research toolsets all get the same tools.
    specialized_domain: ClassVar[str | None] = PEER_SPECIALIZED_DOMAIN
    specialized_cross_cutting: ClassVar[bool] = True
    is_mutation = False
    # PresentationDocument is required when the Impress sidebar caches
    # doc_type="impress" (services map is PresentationDocument only).
    # DrawingDocument still matches Draw-only; do not treat Impress as Draw
    # in the peer catalog (see v1_peer_type_label).
    uno_services = [_TEXT_SERVICE, _CALC_SERVICE, _DRAW_SERVICE, _IMPRESS_SERVICE]
    parameters = _PEER_PARAMETERS

    def is_async(self) -> bool:
        return False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        if not peer_send_caller_allowed(ctx):
            return self._tool_error(
                f"{self.name} is sidebar chat / document_research only.",
                code="PEER_CHAT_ONLY",
            )
        document_url = str(kwargs.get("document_url") or "").strip()
        message = str(kwargs.get("message") or "")
        if not document_url:
            return self._tool_error("document_url is required.", code="VALIDATION_ERROR")
        if not message.strip():
            return self._tool_error("message is required.", code="VALIDATION_ERROR")

        envelope_kind = self.envelope_kind

        model, err_code, err_msg = resolve_peer_target(ctx.ctx, ctx.doc, document_url)
        if err_code or model is None:
            return self._tool_error(err_msg, code=err_code or "PEER_NOT_FOUND")

        from plugin.doc.live_panels import get_live_panel
        from plugin.framework.uno_context import get_runtime_uid

        uid = get_runtime_uid(model) or ""
        if not uid:
            return self._tool_error(
                "Peer document has no RuntimeUID.",
                code="PEER_NOT_FOUND",
            )
        panel = get_live_panel(uid)
        listener = getattr(panel, "send_listener", None) if panel is not None else None
        if listener is None:
            return self._tool_error(
                "Open the peer sidebar once so a live chat panel exists. "
                "Peer messaging cannot start a second agent loop.",
                code="PEER_SIDEBAR_NOT_OPEN",
            )
        if _peer_not_chat_mode(listener):
            return self._tool_error(
                "The peer sidebar must be in Chat mode (not Librarian, Image, or a sub-agent).",
                code="PEER_NOT_CHAT_MODE",
            )

        sender = identity_from_doc(ctx.doc)
        wrapped = format_peer_envelope(
            name=sender["name"],
            uid=sender["uid"],
            url=sender["url"],
            message=message,
            kind=envelope_kind,
        )

        busy = listener_is_busy(listener)
        already_appended = not busy
        if already_appended:
            _inject_on_listener(listener, wrapped)

        turn = PeerPendingTurn(
            wrapped_text=wrapped,
            already_appended=already_appended,
        )
        overflow = schedule_peer_turn(listener, turn)
        if overflow:
            return self._tool_error(
                f"Peer sidebar queue is full (max {PEER_QUEUE_CAP} pending turns).",
                code="PEER_QUEUE_FULL",
            )
        return {
            "status": "ok",
            "accepted": True,
            "envelope_kind": envelope_kind,
            "message": PEER_ACCEPTED_FINISH_HINT,
        }


class SendPeerWork(_SendPeerBase):
    """Queue a new work request on another live sidebar (document_research specialized)."""

    name = PEER_WORK_TOOL_NAME
    description = _WORK_DESCRIPTION
    envelope_kind: ClassVar[str] = "work"


class SendPeerResult(_SendPeerBase):
    """Queue a result/reply on another live sidebar (document_research specialized)."""

    name = PEER_RESULT_TOOL_NAME
    description = _RESULT_DESCRIPTION
    envelope_kind: ClassVar[str] = "result"
