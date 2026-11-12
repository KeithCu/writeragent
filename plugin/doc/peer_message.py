# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A1 peer send: ``send_peer_work`` / ``send_peer_result``.

Queues a user-equivalent turn on another already-open Writer/Calc/Draw/Impress
sidebar and returns immediately. Experiment: advertised on the
document_research specialized loop only (not outer chat, not MCP).
Kind is which tool was called (work vs result). See ``docs/chat/peer-messaging.md``.
Pending turns live in ``peer_queue`` (one ``PeerQueue``); a target must implement ``PeerListener``.

This module must not import ``plugin.chatbot.panel`` or
``plugin.chatbot.panel_factory`` (cycle: CommonModule → tool → panel →
``get_tools()``).
"""

from __future__ import annotations

import copy
import logging
import os
import unicodedata
import weakref
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Protocol

from plugin.framework.doc_type import (
    DOCUMENT_SERVICES,
    DocumentType,
    doc_type_label_for_enum,
    get_document_type,
)
from plugin.framework.async_drain_guard import add_drain_idle_callback, get_drain_owner
from plugin.framework.tool import ToolBase, ToolContext

log = logging.getLogger("writeragent.doc.peer_message")

PEER_WORK_TOOL_NAME = "send_peer_work"
PEER_RESULT_TOOL_NAME = "send_peer_result"
PEER_TOOL_NAMES = frozenset({PEER_WORK_TOOL_NAME, PEER_RESULT_TOOL_NAME})
PEER_QUEUE_CAP = 8
# Only the title is capped: uid/url are addresses the model sends back.
PEER_NAME_MAX_CHARS = 200
PEER_SPECIALIZED_DOMAIN = "document_research"
# MCP / script / venv RPC must never inject a sidebar turn. Chat main and
# document_research specialized inherit or set caller="chat"; also allow a
# specialized ToolContext that only sets active_domain.
_PEER_SEND_BLOCKED_CALLERS = frozenset({"mcp", "script", "ppt_master_venv"})

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


@dataclass(eq=False)
class PeerPendingTurn:
    """One queued extracted send on a listener.

    Turns compare by identity (eq=False). The queue removes turns by identity
    so value equality does not inadvertently drop a distinct turn with identical
    text during busy re-queue or rollback.
    """

    wrapped_text: str
    already_appended: bool


class PeerListener(Protocol):
    """What a sidebar panel provides so this module can queue and start a peer turn on it.

    SendButtonListener (plugin/chatbot/panel.py) implements this; the unit and
    UNO fakes implement the same four methods. Listeners must be weak-referenceable.
    """

    def is_busy(self) -> bool: ...
    def in_chat_mode(self) -> bool: ...
    def inject_user_turn(self, text: str) -> bool: ...
    def start_extracted_peer_send(self, query_text: str, *, already_appended: bool) -> bool: ...


class PeerQueue:
    """Pending peer turns for every sidebar, oldest first, in one deque of (weakref, turn).

    Turns never leave the deque until they start, so Stop, dispose, and the queue cap
    always observe all pending entries even while a kick or send pump is in flight.
    """

    def __init__(self) -> None:
        self._items: deque[tuple[weakref.ref[Any], PeerPendingTurn]] = deque()

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, turn: object) -> bool:
        """True if turn is currently queued (by identity)."""
        return any(item[1] is turn for item in self._items)

    def entries(self) -> list[tuple[PeerListener, PeerPendingTurn]]:
        """Snapshot of live (listener, turn) pairs, oldest first."""
        result: list[tuple[PeerListener, PeerPendingTurn]] = []
        for ref, turn in self._items:
            listener = ref()
            if listener is not None:
                result.append((listener, turn))
        return result

    def pending(self, listener: object) -> int:
        """Count live pending turns for this listener (used for cap check and test assertions)."""
        return sum(1 for item in self._items if item[0]() is listener)

    def append(self, listener: PeerListener, turn: PeerPendingTurn) -> None:
        self._items.append((weakref.ref(listener), turn))

    def _keep(self, pred: Callable[[Any, PeerPendingTurn], bool]) -> None:
        kept: list[tuple[weakref.ref[Any], PeerPendingTurn]] = []
        for ref, turn in self._items:
            listener = ref()
            if listener is not None and pred(listener, turn):
                kept.append((ref, turn))
        self._items.clear()
        self._items.extend(kept)

    def remove(self, turn: PeerPendingTurn) -> None:
        """Drop turn by identity; also purges dead listener references."""
        self._keep(lambda listener_obj, item_turn: item_turn is not turn)

    def drop_listener(self, listener: object) -> None:
        """Drop all turns for listener; also purges dead listener references."""
        self._keep(lambda listener_obj, item_turn: listener_obj is not listener)


peer_queue: PeerQueue = PeerQueue()


def v1_peer_type_label(model: Any) -> str | None:
    """``writer`` / ``calc`` / ``draw`` / ``impress``, or None if unsupported."""
    dt = get_document_type(model)
    return None if dt == DocumentType.UNKNOWN else doc_type_label_for_enum(dt)


def identity_from_doc(doc: Any) -> dict[str, str]:
    """Sender fields from ``ToolContext.doc`` (not authored in ``message``)."""
    from plugin.framework.uno_context import get_runtime_uid

    if doc is None:
        return {"name": "Untitled", "uid": "", "url": ""}
    # Do not catch. get_runtime_uid and get_document_path are @main_thread_only;
    # an off-main RuntimeError must propagate and must not fall through to getTitle().
    # get_document_path already catches Exception in its body and returns None.
    uid = get_runtime_uid(doc) or ""
    url = str(doc.getURL() or "")
    name = ""
    if url:
        from plugin.doc.text_helpers import get_document_path

        path = get_document_path(doc) or ""
        name = os.path.basename(path) if path else ""
    if not name:
        name = str(doc.getTitle() or "") or "Untitled"
    return {"name": name, "uid": uid, "url": url}


# Fixed footer on work envelopes only. Outer PEER_OUTER_DELEGATE_HINT already
# says to re-delegate for send_peer_result, but models ignore it when the body
# is a short list/read ask and Ready with a chat-only answer — the asker never
# gets a peer result. Result envelopes must stay body-only (no delivery footer).
PEER_WORK_DELIVERY_FOOTER = (
    "When the local work is done, you MUST Do domain=\"document_research\" to deliver "
    "via send_peer_result(document_url=<uid or url from this envelope header>, "
    "message=<one HTML/result string>). "
    "Why: nested specialize done is not peer delivery; a chat-only answer in this "
    "sidebar never reaches the asking peer."
)


def _sanitize_peer_field(value: str) -> str:
    """Flatten one name/uid/url field so it cannot forge the header line or a catalog row."""
    return "".join(
        " " if (ch in "|[]\u2028\u2029" or unicodedata.category(ch) == "Cc") else ch
        for ch in str(value or "")
    )


def format_peer_envelope(
    *,
    name: str,
    uid: str,
    url: str,
    message: str | None,
    kind: str = "work",
) -> str:
    """Code-inserted wrapper. ``message`` is the body only.

    ``kind`` is which tool was called: ``"work"`` for ``send_peer_work``,
    ``"result"`` for ``send_peer_result``. Models must not invent the header.
    Work envelopes append ``PEER_WORK_DELIVERY_FOOTER``; result envelopes do not.
    """
    label = "Peer result from" if kind == "result" else "Peer work from"
    header = (
        f"[{label}: {_sanitize_peer_field(name)[:PEER_NAME_MAX_CHARS]} | "
        f"uid={_sanitize_peer_field(uid)} | url={_sanitize_peer_field(url)}]"
    )
    # None message becomes an empty body, not the literal "None".
    body = "" if message is None else message.rstrip("\n")
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
            f"{_sanitize_peer_field(p.get('name') or 'Untitled')[:PEER_NAME_MAX_CHARS]} "
            f"(uid={_sanitize_peer_field(p.get('uid') or '')}, "
            f"url={_sanitize_peer_field(p.get('url') or '')}, "
            f"type={_sanitize_peer_field(p.get('type') or '')})"
        )
    return "Open peers: " + "; ".join(parts) + "."


def _resolve_open_model(uno_ctx: Any, key: str) -> Any | None:
    from plugin.framework.uno_context import resolve_document_by_url

    try:
        model, _unused_type = resolve_document_by_url(uno_ctx, key)
    except Exception:
        return None
    return model


def list_v1_peers(uno_ctx: Any, self_doc: Any) -> list[dict[str, str]]:
    """Resolvable other v1 peers (distinct uid, supported model, addressable).

    Desktop catalog + RuntimeUID. Callers marshal to the main thread
    (specialized scaffolding / ``get_peer_inner_choice_block``) or this asserts.
    """
    from plugin.doc.document_research import get_open_documents
    from plugin.framework.thread_guard import assert_main_thread
    from plugin.framework.uno_context import get_runtime_uid, uno_same

    assert_main_thread("peer_message.list_v1_peers")
    self_uid = ""
    if self_doc is not None:
        self_uid = get_runtime_uid(self_doc) or ""
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
        model = _resolve_open_model(uno_ctx, uid)
        if model is None and url:
            model = _resolve_open_model(uno_ctx, url)
        label = v1_peer_type_label(model)
        if label is None:
            continue
        if not self_uid and self_doc is not None and uno_same(self_doc, model):
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
    from plugin.framework.uno_context import get_runtime_uid, uno_same

    target = (document_url or "").strip()
    if not target:
        return None, "VALIDATION_ERROR", "document_url is required."

    self_uid = ""
    if self_doc is not None:
        self_uid = get_runtime_uid(self_doc) or ""

    model = _resolve_open_model(uno_ctx, target)

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
            model = _resolve_open_model(uno_ctx, name_hits[0]["uid"])

    if model is None:
        # list_v1_peers omits self; match identity_from_doc so the caller does not get PEER_NOT_FOUND.
        if self_doc is not None and target == identity_from_doc(self_doc)["name"]:
            return None, "PEER_SELF", "Cannot send a peer message to the same document."
        return None, "PEER_NOT_FOUND", f"No open Writer, Calc, Draw, or Impress peer matches {target!r}."

    peer_uid = get_runtime_uid(model) or ""
    if self_uid and peer_uid and self_uid == peer_uid:
        return None, "PEER_SELF", "Cannot send a peer message to the same document."
    if self_doc is not None and (not self_uid or not peer_uid) and uno_same(self_doc, model):
        return None, "PEER_SELF", "Cannot send a peer message to the same document."

    if v1_peer_type_label(model) is None:
        return None, "PEER_UNSUPPORTED", "Target is not a Writer, Calc, Draw, or Impress document."
    return model, None, ""


def enqueue_peer_turn(listener: PeerListener, turn: PeerPendingTurn) -> str | None:
    """Queue *turn*. Returns ``PEER_QUEUE_FULL`` or None."""
    from plugin.framework.thread_guard import assert_main_thread

    assert_main_thread("peer_message.enqueue_peer_turn")
    if peer_queue.pending(listener) >= PEER_QUEUE_CAP:
        return "PEER_QUEUE_FULL"
    peer_queue.append(listener, turn)
    return None


def drop_listener_queue(listener: PeerListener | None) -> None:
    """Stop / dispose: drop that listener's pending injects only."""
    if listener is None:
        return
    from plugin.framework.thread_guard import assert_main_thread

    assert_main_thread("peer_message.drop_listener_queue")
    peer_queue.drop_listener(listener)


def kick_pending_peer_starts() -> None:
    """Start at most one queued extracted send when the process drain is idle."""
    from plugin.framework.thread_guard import assert_main_thread

    assert_main_thread("peer_message.kick_pending_peer_starts")
    if get_drain_owner() is not None:
        return
    for listener, turn in peer_queue.entries():
        if turn not in peer_queue:
            continue
        if listener.is_busy() or get_drain_owner() is not None:
            continue
        try:
            res = listener.start_extracted_peer_send(
                turn.wrapped_text, already_appended=turn.already_appended
            )
        except Exception:
            log.exception("kick_pending_peer_starts: start_extracted_peer_send raised")
            # If the send took the turn (listener busy or live drain owner),
            # the in-flight send owns the envelope: remove it so a second
            # start after the drain does not deliver it twice.
            if listener.is_busy() or get_drain_owner() is not None:
                peer_queue.remove(turn)
                break
            continue
        # send_state refuses EXTRACTED_SEND while the hands-free mic records,
        # so start returns False. Leave the turn in the queue to retry when idle.
        if res is False:
            continue
        peer_queue.remove(turn)
        break


def _on_drain_idle() -> None:
    """Schedule a kick after the current stack unwinds.

    Starting the peer drain inside ``drain_owner_scope``'s ``finally`` would
    run it before the caller’s ``SEND_COMPLETED`` (Ready). ``MainThread.post``
    is inline under ``Policy.test_inline``, so we never start here — only
    mark that a kick is due. Production posts to the next VCL tick. Mock-sidebar
    Packet P dispatches ``KICK_PEERS`` after Writer Ready (do not force-marshal
    from this callback: that starts Calc during Writer wrapup and sticks Stop).
    """
    if get_drain_owner() is not None:
        return
    if not peer_queue:
        return
    try:
        from plugin.framework.queue_executor import Policy, main

        if main.policy is not Policy.test_inline:
            main.post(kick_pending_peer_starts)
    except Exception:
        log.debug("peer drain-idle schedule failed", exc_info=True)


add_drain_idle_callback(_on_drain_idle)


def schedule_peer_turn(listener: PeerListener, turn: PeerPendingTurn) -> str | None:
    """Enqueue, inject on idle listener, and kick if drain owner is unset.

    enqueue_peer_turn enforces the queue cap before any transcript write.
    already_appended tracks whether the session write completed.
    kick is last so an immediate start sees already_appended.
    """
    err = enqueue_peer_turn(listener, turn)
    if err:
        return err
    if not listener.is_busy():
        try:
            turn.already_appended = listener.inject_user_turn(turn.wrapped_text)
        except Exception:
            peer_queue.remove(turn)
            raise
    if get_drain_owner() is None and not listener.is_busy():
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
        return True, desc.count("(uid=")
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


def _list_v1_peers_or_empty(uno_ctx: Any, doc: Any, where: str) -> list[dict[str, str]]:
    try:
        return list_v1_peers(uno_ctx, doc)
    except RuntimeError:
        # assert_main_thread failures must propagate, not masquerade as an empty peer list.
        raise
    except Exception:
        log.debug("%s: catalog failed", where, exc_info=True)
        return []


def filter_peer_tools_for_specialized(tools: list[Any], uno_ctx: Any, doc: Any) -> list[Any]:
    """Keep both peer tools on document_research only when a v1 peer is open."""
    if not any(getattr(t, "name", None) in PEER_TOOL_NAMES for t in tools):
        return tools
    peers: list[dict[str, str]] = []
    if uno_ctx is not None:
        peers = _list_v1_peers_or_empty(uno_ctx, doc, "filter_peer_tools_for_specialized")
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
    # Outer get_schemas strips peer tools unless active_domain is document_research—desktop walks stay there.
    if not _document_research_domain(active_domain):
        return [s for s in schemas if _schema_function_name(s) not in PEER_TOOL_NAMES]
    peers: list[dict[str, str]] = []
    if ctx is not None and doc is not None:
        peers = _list_v1_peers_or_empty(ctx, doc, "filter_peer_message_schemas")
    if not peers:
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


def _peer_in_chat_mode(listener: PeerListener) -> bool:
    """True when the target sidebar is in Chat mode (not Librarian, Image, or a sub-agent)."""
    try:
        return listener.in_chat_mode()
    except Exception:
        log.debug("peer mode probe failed; treating as chat", exc_info=True)
        return True


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

    name: str | None = ""  # subclasses set
    description: str = ""
    envelope_kind: ClassVar[str] = "work"
    tier: str = "chat"
    # Domain membership: inner document_research sees this; outer chat does not
    # advertise it (filter_peer_message_schemas). Cross-cutting so Writer/Calc/
    # Draw/Impress document_research toolsets all get the same tools.
    specialized_domain: ClassVar[str | None] = PEER_SPECIALIZED_DOMAIN
    specialized_cross_cutting: ClassVar[bool] = True
    is_mutation: bool | None = False
    # PresentationDocument is required when the Impress sidebar caches
    # doc_type="impress" (services map is PresentationDocument only).
    # DrawingDocument still matches Draw-only; do not treat Impress as Draw
    # in the peer catalog (see v1_peer_type_label).
    uno_services: list[str] | None = list(DOCUMENT_SERVICES)
    parameters: dict[str, Any] | None = _PEER_PARAMETERS

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
        listener: PeerListener | None = panel.send_listener if panel is not None else None
        if listener is None:
            return self._tool_error(
                "Open the peer sidebar once so a live chat panel exists. "
                "Peer messaging cannot start a second agent loop.",
                code="PEER_SIDEBAR_NOT_OPEN",
            )
        if not _peer_in_chat_mode(listener):
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

        # The slot is reserved inside schedule_peer_turn before the transcript write.
        turn = PeerPendingTurn(
            wrapped_text=wrapped,
            already_appended=False,
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

    name: str | None = PEER_WORK_TOOL_NAME
    description: str = _WORK_DESCRIPTION
    envelope_kind: ClassVar[str] = "work"


class SendPeerResult(_SendPeerBase):
    """Queue a result/reply on another live sidebar (document_research specialized)."""

    name: str | None = PEER_RESULT_TOOL_NAME
    description: str = _RESULT_DESCRIPTION
    envelope_kind: ClassVar[str] = "result"
