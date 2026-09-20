"""Unit tests for A1 send_peer_work / send_peer_result (no live soffice)."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

from plugin.doc.peer_message import (
    PEER_QUEUE_CAP,
    PEER_RESULT_TOOL_NAME,
    PEER_SPECIALIZED_DOMAIN,
    PEER_TOOL_NAMES,
    PEER_WORK_DELIVERY_FOOTER,
    PEER_WORK_TOOL_NAME,
    PeerPendingTurn,
    SendPeerResult,
    SendPeerWork,
    drop_listener_queue,
    enqueue_peer_turn,
    filter_peer_message_schemas,
    filter_peer_tools_for_specialized,
    format_peer_envelope,
    identity_from_doc,
    is_v1_peer_model,
    kick_pending_peer_starts,
    listener_queue_len,
    peer_send_caller_allowed,
    reset_peer_queues,
    resolve_peer_target,
    schedule_peer_turn,
    v1_peer_type_label,
)
from plugin.framework.async_drain_guard import drain_owner_scope, reset_sentry_state
from plugin.framework.tool import ToolContext, ToolRegistry


class _Listener:
    """Weakref-capable stand-in for SendButtonListener."""

    def __init__(self):
        self.session = MagicMock()
        self.session.messages = []
        self.appended = []
        self.started = []
        self.sidebar_state = MagicMock()
        self.sidebar_state.send.is_busy = False
        self._active_q = None
        self._send_cancellation = None
        self.chat_mode_selector = None

    def _append_response(self, text, role="assistant"):
        self.appended.append((text, role))

    def start_extracted_peer_send(self, query_text, *, already_appended):
        self.started.append((query_text, already_appended))
        return True


def setup_function():
    reset_peer_queues()
    reset_sentry_state()


def teardown_function():
    reset_peer_queues()
    reset_sentry_state()


def _ctx(caller="chat", doc=None):
    if doc is None:
        doc = MagicMock()
        doc.getURL.return_value = "file:///tmp/self.odt"
        doc.getTitle.return_value = "self.odt"
        doc.getRuntimeUID.return_value = "self-uid"
        doc.RuntimeUID = "self-uid"
        doc.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    return ToolContext(doc=doc, ctx=object(), doc_type="writer", services=None, caller=caller)


def test_peer_message_module_does_not_import_panel_factory():
    src = Path("plugin/doc/peer_message.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert "plugin.chatbot.panel_factory" not in imported
    assert "plugin.chatbot.panel" not in imported
    assert not any(name.endswith("panel_factory") for name in imported)


def test_envelope_from_ctx_doc():
    doc = MagicMock()
    doc.getURL.return_value = "file:///tmp/Budget%202026.ods"
    doc.getTitle.return_value = "Budget 2026.ods"
    doc.getRuntimeUID.return_value = "uid-budget"
    doc.RuntimeUID = "uid-budget"
    with patch("plugin.doc.document_research._system_path_from_url", return_value="/tmp/Budget 2026.ods"):
        ident = identity_from_doc(doc)
    assert ident["name"] == "Budget 2026.ods"
    assert ident["uid"] == "uid-budget"
    assert ident["url"] == "file:///tmp/Budget%202026.ods"
    wrapped = format_peer_envelope(
        name=ident["name"],
        uid=ident["uid"],
        url=ident["url"],
        message="Compute Q4.",
    )
    assert wrapped.startswith("[Peer work from: Budget 2026.ods | uid=uid-budget |")
    assert "peer_ask_id" not in wrapped
    assert "Compute Q4." in wrapped
    assert wrapped.endswith(PEER_WORK_DELIVERY_FOOTER)
    assert "send_peer_result" in PEER_WORK_DELIVERY_FOOTER


def test_envelope_untitled_url_empty():
    doc = MagicMock()
    doc.getURL.return_value = ""
    doc.getTitle.return_value = "Untitled 1"
    doc.getRuntimeUID.return_value = "uid-untitled"
    doc.RuntimeUID = "uid-untitled"
    ident = identity_from_doc(doc)
    assert ident["url"] == ""
    assert ident["uid"] == "uid-untitled"
    assert ident["name"] == "Untitled 1"


def test_impress_is_distinct_v1_peer_not_draw():
    """Impress is a v1 peer, but must not collapse into Draw in the catalog."""
    impress = MagicMock()
    impress.supportsService.side_effect = lambda s: s in (
        "com.sun.star.drawing.DrawingDocument",
        "com.sun.star.presentation.PresentationDocument",
    )
    assert is_v1_peer_model(impress) is True
    assert v1_peer_type_label(impress) == "impress"

    draw = MagicMock()
    draw.supportsService.side_effect = lambda s: s == "com.sun.star.drawing.DrawingDocument"
    assert is_v1_peer_model(draw) is True
    assert v1_peer_type_label(draw) == "draw"


def test_list_v1_peers_includes_impress_type():
    """Research catalog may say draw; list_v1_peers still emits type=impress."""
    from plugin.doc.peer_message import list_v1_peers

    self_doc = MagicMock()
    impress = MagicMock()
    impress.supportsService.side_effect = lambda s: s in (
        "com.sun.star.drawing.DrawingDocument",
        "com.sun.star.presentation.PresentationDocument",
    )
    catalog = [{"name": "Deck.odp", "uid": "impress-uid", "url": "file:///tmp/Deck.odp", "doc_type": "draw"}]

    with patch("plugin.framework.thread_guard.assert_main_thread"):
        with patch("plugin.framework.uno_context.get_runtime_uid", side_effect=lambda m: "self" if m is self_doc else "impress-uid"):
            with patch("plugin.doc.document_research.get_open_documents", return_value=catalog):
                with patch("plugin.framework.uno_context.resolve_document_by_url", return_value=(impress, "draw")):
                    peers = list_v1_peers(object(), self_doc)
    assert peers == [
        {"name": "Deck.odp", "uid": "impress-uid", "url": "file:///tmp/Deck.odp", "type": "impress"}
    ]


def test_addressing_impress_accepted():
    """resolve_peer_target accepts PresentationDocument; type stays impress."""
    self_doc = MagicMock()
    self_doc.getRuntimeUID.return_value = "self"
    impress = MagicMock()
    impress.getRuntimeUID.return_value = "impress-uid"
    impress.supportsService.side_effect = lambda s: s in (
        "com.sun.star.drawing.DrawingDocument",
        "com.sun.star.presentation.PresentationDocument",
    )

    with patch("plugin.framework.uno_context.resolve_document_by_url", return_value=(impress, "draw")):
        with patch(
            "plugin.framework.uno_context.get_runtime_uid",
            side_effect=lambda m: "self" if m is self_doc else "impress-uid",
        ):
            model, code, _msg = resolve_peer_target(object(), self_doc, "impress-uid")
    assert code is None
    assert model is impress
    assert v1_peer_type_label(model) == "impress"


def test_addressing_unsupported_unknown_model():
    """PEER_UNSUPPORTED is for non-v1 apps, not Impress."""
    self_doc = MagicMock()
    self_doc.getRuntimeUID.return_value = "self"
    other = MagicMock()
    other.getRuntimeUID.return_value = "other-uid"
    other.supportsService.return_value = False

    with patch("plugin.framework.uno_context.resolve_document_by_url", return_value=(other, "unknown")):
        with patch(
            "plugin.framework.uno_context.get_runtime_uid",
            side_effect=lambda m: "self" if m is self_doc else "other-uid",
        ):
            model, code, msg = resolve_peer_target(object(), self_doc, "other-uid")
    assert model is None
    assert code == "PEER_UNSUPPORTED"
    assert "not a Writer, Calc, Draw, or Impress document" in msg


def test_list_v1_peers_magicmock_ctx_does_not_hang():
    """Regression: prompt/schema catalog with ctx=MagicMock() must not spin."""
    from plugin.doc.peer_message import list_v1_peers
    from plugin.framework.prompts import get_chat_system_prompt_for_document

    ctx = MagicMock()
    doc = MagicMock()
    doc.getRuntimeUID.return_value = "self"
    assert list_v1_peers(ctx, doc) == []
    model = MagicMock()
    model.supportsService.return_value = False
    prompt = get_chat_system_prompt_for_document(model, ctx=ctx)
    assert isinstance(prompt, str)
    assert "send_peer_work" not in prompt
    assert "send_peer_result" not in prompt


def test_outer_prompt_with_peers_has_no_peer_send_tools():
    from plugin.framework.prompts import format_peer_outer_delegate_hint, get_chat_system_prompt_for_document

    model = MagicMock()
    model.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    peers = [{"name": "Budget.ods", "uid": "u2", "url": "", "type": "calc"}]
    with patch("plugin.doc.peer_message.list_v1_peers", return_value=peers):
        prompt = get_chat_system_prompt_for_document(model, ctx=MagicMock())
    outer = format_peer_outer_delegate_hint(model)
    assert outer in prompt
    assert "delegate_to_specialized_writer_toolset" in prompt
    assert "send_peer_work" not in prompt  # outer does not ask; only names result delivery
    assert "send_peer_result" in prompt  # deliver via send_peer_result after local work
    assert "PEER SIDEBARS" not in prompt
    assert "PEER vs READ" not in prompt
    assert "ASK vs REPLY" not in prompt
    assert "[Peer work from:" in prompt or "[Peer from:" in prompt
    assert "peer_ask_id" not in prompt



def test_visibility_filter_alone_hides_tool():
    schemas = [
        {"type": "function", "function": {"name": PEER_WORK_TOOL_NAME, "description": "base"}},
        {"type": "function", "function": {"name": "undo", "description": "u"}},
    ]
    out = filter_peer_message_schemas(schemas, ctx=object(), doc=object())
    names = [s["function"]["name"] for s in out]
    assert PEER_WORK_TOOL_NAME not in names
    assert "undo" in names


def test_visibility_filter_two_writer_hides_on_main():
    """Experiment: outer chat never advertises peer send tools, even with peers."""
    schemas = [
        {"type": "function", "function": {"name": PEER_WORK_TOOL_NAME, "description": "base"}},
        {"type": "function", "function": {"name": "undo", "description": "u"}},
    ]
    peers = [{"name": "Other.odt", "uid": "u2", "url": "file:///tmp/Other.odt", "type": "writer"}]
    with patch("plugin.doc.peer_message.list_v1_peers", return_value=peers):
        out = filter_peer_message_schemas(schemas, ctx=object(), doc=object())
    names = [s["function"]["name"] for s in out]
    assert PEER_WORK_TOOL_NAME not in names
    assert "undo" in names


def test_visibility_filter_two_writer_shows_catalog_on_specialized():
    schemas = [
        {"type": "function", "function": {"name": PEER_WORK_TOOL_NAME, "description": "base"}},
        {"type": "function", "function": {"name": PEER_RESULT_TOOL_NAME, "description": "base"}},
    ]
    peers = [{"name": "Other.odt", "uid": "u2", "url": "file:///tmp/Other.odt", "type": "writer"}]
    with patch("plugin.doc.peer_message.list_v1_peers", return_value=peers):
        out = filter_peer_message_schemas(
            schemas, ctx=object(), doc=object(), active_domain=PEER_SPECIALIZED_DOMAIN
        )
    assert len(out) == 2
    names = {s["function"]["name"] for s in out}
    assert names == PEER_TOOL_NAMES
    for schema in out:
        desc = schema["function"]["description"]
        assert "Other.odt" in desc
        assert "uid=u2" in desc
        assert "type=writer" in desc


def test_visibility_filter_specialized_hides_when_alone():
    schemas = [
        {"type": "function", "function": {"name": PEER_WORK_TOOL_NAME, "description": "base"}},
        {"type": "function", "function": {"name": PEER_RESULT_TOOL_NAME, "description": "base"}},
    ]
    with patch("plugin.doc.peer_message.list_v1_peers", return_value=[]):
        out = filter_peer_message_schemas(
            schemas, ctx=object(), doc=object(), active_domain=PEER_SPECIALIZED_DOMAIN
        )
    assert out == []


def test_addressing_self_rejected():
    self_doc = MagicMock()
    self_doc.getRuntimeUID.return_value = "uid-self"
    self_doc.RuntimeUID = "uid-self"
    peer = MagicMock()
    peer.getRuntimeUID.return_value = "uid-self"
    peer.RuntimeUID = "uid-self"
    peer.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    with patch("plugin.framework.uno_context.resolve_document_by_url", return_value=(peer, "writer")):
        with patch("plugin.framework.uno_context.get_runtime_uid", return_value="uid-self"):
            model, code, _msg = resolve_peer_target(object(), self_doc, "uid-self")
    assert model is None
    assert code == "PEER_SELF"


def test_addressing_missing():
    self_doc = MagicMock()
    with patch("plugin.framework.uno_context.resolve_document_by_url", return_value=(None, None)):
        with patch("plugin.doc.peer_message.list_v1_peers", return_value=[]):
            model, code, _msg = resolve_peer_target(object(), self_doc, "file:///missing.odt")
    assert model is None
    assert code == "PEER_NOT_FOUND"


def test_addressing_ambiguous_name():
    self_doc = MagicMock()
    peers = [
        {"name": "Budget.ods", "uid": "a", "url": "", "type": "calc"},
        {"name": "Budget.ods", "uid": "b", "url": "", "type": "calc"},
    ]
    with patch("plugin.framework.uno_context.resolve_document_by_url", return_value=(None, None)):
        with patch("plugin.doc.peer_message.list_v1_peers", return_value=peers):
            model, code, _msg = resolve_peer_target(object(), self_doc, "Budget.ods")
    assert model is None
    assert code == "PEER_AMBIGUOUS"


def test_addressing_unique_name():
    self_doc = MagicMock()
    self_doc.getRuntimeUID.return_value = "self"
    peer = MagicMock()
    peer.getRuntimeUID.return_value = "peer-uid"
    peer.supportsService.side_effect = lambda s: s == "com.sun.star.sheet.SpreadsheetDocument"
    peers = [{"name": "Budget.ods", "uid": "peer-uid", "url": "", "type": "calc"}]

    def _resolve(_ctx, target):
        if target == "peer-uid":
            return peer, "calc"
        return None, None

    with patch("plugin.framework.uno_context.resolve_document_by_url", side_effect=_resolve):
        with patch("plugin.doc.peer_message.list_v1_peers", return_value=peers):
            with patch("plugin.framework.uno_context.get_runtime_uid", side_effect=lambda m: "self" if m is self_doc else "peer-uid"):
                model, code, _msg = resolve_peer_target(object(), self_doc, "Budget.ods")
    assert code is None
    assert model is peer


def test_queue_fifo_cap_overflow_and_stop_drops():
    listener = _Listener()
    turns = [PeerPendingTurn(f"t{i}", False) for i in range(PEER_QUEUE_CAP)]
    for turn in turns:
        assert enqueue_peer_turn(listener, turn) is None
    assert listener_queue_len(listener) == PEER_QUEUE_CAP
    overflow = enqueue_peer_turn(listener, PeerPendingTurn("extra", False))
    assert overflow == "PEER_QUEUE_FULL"
    drop_listener_queue(listener)
    assert listener_queue_len(listener) == 0


def test_schedule_does_not_start_while_drain_owned():
    listener = _Listener()
    turn = PeerPendingTurn("wrapped", True)
    with drain_owner_scope("stream"):
        err = schedule_peer_turn(listener, turn)
        assert err is None
        assert listener_queue_len(listener) == 1
        assert listener.started == []
    # idle callback may schedule but testing post is deferred; kick explicitly
    kick_pending_peer_starts()
    assert listener.started == [("wrapped", True)]
    assert listener_queue_len(listener) == 0


def test_user_busy_wins_over_queued_inject():
    listener = _Listener()
    listener.sidebar_state.send.is_busy = True
    turn = PeerPendingTurn("wrapped", False)
    assert schedule_peer_turn(listener, turn) is None
    assert listener.started == []
    assert listener_queue_len(listener) == 1
    listener.sidebar_state.send.is_busy = False
    kick_pending_peer_starts()
    assert listener.started == [("wrapped", False)]


def test_p3_busy_then_queue_reply():
    """Packet P: inbound reply queues while the peer is busy; starts after idle."""
    listener = _Listener()
    listener.sidebar_state.send.is_busy = True
    wrapped = (
        "[Peer result from: BudgetPeer.ods | uid=calc-uid | url=]\n\n"
        "Total row written at A4:B4."
    )
    turn = PeerPendingTurn(wrapped, False)
    assert schedule_peer_turn(listener, turn) is None
    assert listener.started == []
    assert listener_queue_len(listener) == 1
    listener.sidebar_state.send.is_busy = False
    kick_pending_peer_starts()
    assert listener.started == [(wrapped, False)]
    assert listener_queue_len(listener) == 0


def test_p3_execute_queues_reply_while_writer_busy():
    """Calc send_peer_result while Writer is rambling: accept, no inject, start later."""
    tool = SendPeerResult()
    ctx = _ctx()
    peer = MagicMock()
    listener = _Listener()
    listener.sidebar_state.send.is_busy = True
    panel = MagicMock()
    panel.send_listener = listener
    with patch("plugin.doc.peer_message.resolve_peer_target", return_value=(peer, None, "")):
        with patch("plugin.framework.uno_context.get_runtime_uid", return_value="peer-uid"):
            with patch("plugin.doc.live_panels.get_live_panel", return_value=panel):
                result = tool.execute(
                    ctx,
                    document_url="peer-uid",
                    message="Total row written at A4:B4.",
                )
    assert result["status"] == "ok"
    assert result["accepted"] is True
    assert listener.appended == []
    listener.session.add_user_message.assert_not_called()
    assert listener.started == []
    assert listener_queue_len(listener) == 1
    listener.sidebar_state.send.is_busy = False
    kick_pending_peer_starts()
    assert listener.started and listener.started[0][1] is False


def test_execute_status_ok_accepted():
    tool = SendPeerWork()
    ctx = _ctx()
    peer = MagicMock()
    listener = _Listener()
    panel = MagicMock()
    panel.send_listener = listener
    with patch("plugin.doc.peer_message.resolve_peer_target", return_value=(peer, None, "")):
        with patch("plugin.framework.uno_context.get_runtime_uid", return_value="peer-uid"):
            with patch("plugin.doc.live_panels.get_live_panel", return_value=panel):
                with drain_owner_scope("stream"):
                    result = tool.execute(ctx, document_url="peer-uid", message="Do the thing")
    assert result["status"] == "ok"
    assert result["accepted"] is True
    assert result.get("envelope_kind") == "work"
    assert "peer_ask_id" not in result
    assert "specialized_workflow_finished immediately" in result["message"]
    assert listener.session.add_user_message.called
    assert listener.appended


def test_execute_refuses_mcp_caller():
    tool = SendPeerWork()
    ctx = _ctx(caller="mcp")
    result = tool.execute(ctx, document_url="x", message="hi")
    assert result["status"] == "error"
    assert result["code"] == "PEER_CHAT_ONLY"


def test_execute_allows_specialized_document_research_caller():
    """Subagent ToolContext may not be caller=chat; domain must still inject."""
    tool = SendPeerWork()
    ctx = _ctx(caller="")
    ctx.active_domain = PEER_SPECIALIZED_DOMAIN
    peer = MagicMock()
    listener = _Listener()
    panel = MagicMock()
    panel.send_listener = listener
    with patch("plugin.doc.peer_message.resolve_peer_target", return_value=(peer, None, "")):
        with patch("plugin.framework.uno_context.get_runtime_uid", return_value="peer-uid"):
            with patch("plugin.doc.live_panels.get_live_panel", return_value=panel):
                with drain_owner_scope("stream"):
                    result = tool.execute(ctx, document_url="peer-uid", message="Do the thing")
    assert result["status"] == "ok"
    assert result["accepted"] is True
    assert listener.session.add_user_message.called


def test_peer_send_caller_allowed_mcp_blocked_specialized_ok():
    chat = _ctx(caller="chat")
    mcp = _ctx(caller="mcp")
    spec = _ctx(caller="")
    spec.active_domain = PEER_SPECIALIZED_DOMAIN
    script = _ctx(caller="script")
    assert peer_send_caller_allowed(chat) is True
    assert peer_send_caller_allowed(mcp) is False
    assert peer_send_caller_allowed(spec) is True
    assert peer_send_caller_allowed(script) is False


def test_execute_missing_sidebar():
    tool = SendPeerWork()
    ctx = _ctx()
    peer = MagicMock()
    with patch("plugin.doc.peer_message.resolve_peer_target", return_value=(peer, None, "")):
        with patch("plugin.framework.uno_context.get_runtime_uid", return_value="peer-uid"):
            with patch("plugin.doc.live_panels.get_live_panel", return_value=None):
                result = tool.execute(ctx, document_url="peer-uid", message="hi")
    assert result["status"] == "error"
    assert result["code"] == "PEER_SIDEBAR_NOT_OPEN"
    assert "sidebar" in result["message"].lower()


def test_execute_queue_full():
    tool = SendPeerWork()
    ctx = _ctx()
    peer = MagicMock()
    listener = _Listener()
    panel = MagicMock()
    panel.send_listener = listener
    for i in range(PEER_QUEUE_CAP):
        enqueue_peer_turn(listener, PeerPendingTurn(f"t{i}", False))
    with patch("plugin.doc.peer_message.resolve_peer_target", return_value=(peer, None, "")):
        with patch("plugin.framework.uno_context.get_runtime_uid", return_value="peer-uid"):
            with patch("plugin.doc.live_panels.get_live_panel", return_value=panel):
                with drain_owner_scope("stream"):
                    result = tool.execute(ctx, document_url="peer-uid", message="one more")
    assert result["status"] == "error"
    assert result["code"] == "PEER_QUEUE_FULL"


def test_registry_chat_tier_on_default_list():
    registry = ToolRegistry(services=None)
    registry.register(SendPeerWork())
    registry.register(SendPeerResult())
    names = {t.name for t in registry.get_tools(doc_type="writer")}
    assert PEER_TOOL_NAMES <= names
    mcp_names = {t.name for t in registry.get_tools(exclude_tiers=frozenset({"specialized", "specialized_control", "chat"}))}
    assert not (PEER_TOOL_NAMES & mcp_names)


def test_schemas_main_hides_specialized_shows_when_peers_open():
    registry = ToolRegistry(services=None)
    registry.register(SendPeerWork())
    registry.register(SendPeerResult())
    peers = [{"name": "Budget.ods", "uid": "u2", "url": "file:///tmp/Budget.ods", "type": "calc"}]
    with patch("plugin.doc.peer_message.list_v1_peers", return_value=peers):
        main = registry.get_schemas("openai", doc_type="writer", ctx=object(), doc=object())
        inner = registry.get_schemas(
            "openai",
            doc_type="writer",
            ctx=object(),
            doc=object(),
            active_domain=PEER_SPECIALIZED_DOMAIN,
        )
        mcp = registry.get_schemas(
            "mcp",
            doc_type="writer",
            ctx=object(),
            doc=object(),
            active_domain=PEER_SPECIALIZED_DOMAIN,
        )
    main_names = [s["function"]["name"] for s in main]
    inner_names = [s["function"]["name"] for s in inner]
    mcp_names = [s.get("name") for s in mcp]
    assert not (PEER_TOOL_NAMES & set(main_names))
    assert PEER_TOOL_NAMES <= set(inner_names)
    assert not (PEER_TOOL_NAMES & set(mcp_names))
    for name in PEER_TOOL_NAMES:
        inner_desc = next(s["function"]["description"] for s in inner if s["function"]["name"] == name)
        assert "Budget.ods" in inner_desc
        assert "uid=u2" in inner_desc


def test_schemas_impress_doc_type_sees_peer_tool_on_specialized():
    """Impress sidebar caches PresentationDocument only — tools must list that service."""
    registry = ToolRegistry(services=None)
    registry.register(SendPeerWork())
    registry.register(SendPeerResult())
    peers = [{"name": "Memo.odt", "uid": "u2", "url": "", "type": "writer"}]
    with patch("plugin.doc.peer_message.list_v1_peers", return_value=peers):
        inner = registry.get_schemas(
            "openai",
            doc_type="impress",
            ctx=object(),
            doc=object(),
            active_domain=PEER_SPECIALIZED_DOMAIN,
        )
        draw_inner = registry.get_schemas(
            "openai",
            doc_type="draw",
            ctx=object(),
            doc=object(),
            active_domain=PEER_SPECIALIZED_DOMAIN,
        )
    assert PEER_TOOL_NAMES <= {s["function"]["name"] for s in inner}
    assert PEER_TOOL_NAMES <= {s["function"]["name"] for s in draw_inner}


def test_specialized_get_tools_includes_peer_when_peers_open():
    registry = ToolRegistry(services=None)
    registry.register(SendPeerWork())
    registry.register(SendPeerResult())
    tools = registry.get_tools(doc_type="writer", active_domain=PEER_SPECIALIZED_DOMAIN, exclude_tiers=())
    assert PEER_TOOL_NAMES <= {t.name for t in tools}
    peers = [{"name": "Budget.ods", "uid": "u2", "url": "", "type": "calc"}]
    with patch("plugin.doc.peer_message.list_v1_peers", return_value=peers):
        kept = filter_peer_tools_for_specialized(tools, uno_ctx=object(), doc=object())
    assert PEER_TOOL_NAMES <= {t.name for t in kept}
    with patch("plugin.doc.peer_message.list_v1_peers", return_value=[]):
        dropped = filter_peer_tools_for_specialized(tools, uno_ctx=object(), doc=object())
    assert not (PEER_TOOL_NAMES & {t.name for t in dropped})


def test_is_mutation_false_and_sync():
    for tool in (SendPeerWork(), SendPeerResult()):
        assert tool.detects_mutation() is False
        assert tool.is_async() is False
        assert tool.tier == "chat"
        assert "com.sun.star.presentation.PresentationDocument" in tool.uno_services
        assert "com.sun.star.drawing.DrawingDocument" in tool.uno_services
    assert SendPeerWork().name == "send_peer_work"
    assert SendPeerResult().name == "send_peer_result"
    assert "peer_ask_id" not in SendPeerWork.parameters["properties"]
    assert "peer_ask_id" not in SendPeerResult.parameters["properties"]


def test_prompts_outer_thin_inner_choice():
    from plugin.framework.prompts import (
        PEER_INNER_CHOICE_RULES,
        PEER_OUTER_DELEGATE_HINT,
        format_peer_outer_delegate_hint,
        get_peer_inner_choice_block,
        get_peer_messaging_prompt_block,
        peer_outer_delegate_tool_name,
    )

    assert "send_peer_work" not in PEER_OUTER_DELEGATE_HINT
    assert "send_peer_result" in PEER_OUTER_DELEGATE_HINT
    assert "PEER SIDEBARS" not in PEER_OUTER_DELEGATE_HINT
    assert "document_research" in PEER_OUTER_DELEGATE_HINT
    assert "[Peer work from:" in PEER_OUTER_DELEGATE_HINT
    assert "[Peer result from:" in PEER_OUTER_DELEGATE_HINT
    assert "send_peer_work" in PEER_INNER_CHOICE_RULES
    assert "send_peer_result" in PEER_INNER_CHOICE_RULES
    assert "delegate_read_document" in PEER_INNER_CHOICE_RULES
    assert "specialized_workflow_finished immediately" in PEER_INNER_CHOICE_RULES
    assert "you MUST send_peer_result" in PEER_INNER_CHOICE_RULES
    assert "one HTML/result string" in PEER_INNER_CHOICE_RULES
    assert "peer_ask_id" not in PEER_INNER_CHOICE_RULES
    assert "before specialized_workflow_finished" in PEER_INNER_CHOICE_RULES
    assert "peer sidebar never sees it" in PEER_INNER_CHOICE_RULES
    assert "message argument to send_peer_result" in PEER_INNER_CHOICE_RULES
    assert "other result text in the task" in PEER_INNER_CHOICE_RULES
    assert "answer from the task alone" in PEER_INNER_CHOICE_RULES
    assert "tool side effect" in PEER_INNER_CHOICE_RULES
    # Ask vs reply polarity: only [Peer work from:] selects reply; ask never send_peer_result.
    assert "ASK vs REPLY" in PEER_INNER_CHOICE_RULES
    assert "ASK PATH" in PEER_INNER_CHOICE_RULES
    assert "REPLY PATH" in PEER_INNER_CHOICE_RULES
    assert "does not make this the reply path" in PEER_INNER_CHOICE_RULES
    assert "only that envelope does" in PEER_INNER_CHOICE_RULES
    assert "NEVER delegate_read_document on that open peer" in PEER_INNER_CHOICE_RULES
    assert "NEVER send_peer_result from the asker" in PEER_INNER_CHOICE_RULES
    assert "answer must come back as a peer result" in PEER_INNER_CHOICE_RULES
    assert "stamps [Peer result" in PEER_INNER_CHOICE_RULES
    assert "On the reply path only" in PEER_INNER_CHOICE_RULES
    # Open-peer hard fork: matching catalog entry → send_peer_work, not silent read.
    assert "about an Open peer file" in PEER_INNER_CHOICE_RULES
    assert "that sidebar is live" in PEER_INNER_CHOICE_RULES
    assert "duplicates work" in PEER_INNER_CHOICE_RULES
    assert "races the peer reply" in PEER_INNER_CHOICE_RULES
    assert "only when the file is not in Open peers" in PEER_INNER_CHOICE_RULES
    assert "nearby on disk" in PEER_INNER_CHOICE_RULES
    assert "no live sidebar" in PEER_INNER_CHOICE_RULES
    # Ask polarity: peer does the edit; do not request a content dump for local fill.
    assert "that peer's own document" in PEER_INNER_CHOICE_RULES
    assert "perform the edit/fill" in PEER_INNER_CHOICE_RULES
    assert "values/facts to write" in PEER_INNER_CHOICE_RULES
    assert "owns the write tools" in PEER_INNER_CHOICE_RULES
    assert "dump of blank/current content" in PEER_INNER_CHOICE_RULES
    assert "skips the peer write path" in PEER_INNER_CHOICE_RULES
    assert "silent file fact" not in PEER_INNER_CHOICE_RULES
    assert "must change, compute, write" not in PEER_INNER_CHOICE_RULES
    assert "not a JSON array" in PEER_OUTER_DELEGATE_HINT
    assert "PEER SIDEBARS" not in PEER_INNER_CHOICE_RULES
    # Tool descriptions keep the same ask/reply polarity.
    assert "ASK PATH only" in SendPeerWork.description
    assert "REPLY PATH only" in SendPeerResult.description
    assert "merely mentions send_peer_result" in SendPeerResult.description
    # Idle-after-send: outer must Ready, not keep tooling in the same turn.
    assert "Stop tool use and Ready" in PEER_OUTER_DELEGATE_HINT
    assert "later user turn" in PEER_OUTER_DELEGATE_HINT
    assert "document_research, python, or query" in PEER_OUTER_DELEGATE_HINT
    # Work envelopes must re-delegate a reply; data/result inbound is local only.
    assert "you MUST Do {delegate}(domain=\"document_research\")" in PEER_OUTER_DELEGATE_HINT
    assert "peer_ask_id" not in PEER_OUTER_DELEGATE_HINT
    assert "deliver via send_peer_result" in PEER_OUTER_DELEGATE_HINT
    assert "local sidebar answer never reaches" in PEER_OUTER_DELEGATE_HINT
    assert "Do not delegate an ack specialize" in PEER_OUTER_DELEGATE_HINT
    assert "not a new work request" in PEER_OUTER_DELEGATE_HINT
    assert SendPeerWork.parameters["properties"]["message"]["type"] == "string"

    writer = MagicMock()
    writer.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    calc = MagicMock()
    calc.supportsService.side_effect = lambda s: s == "com.sun.star.sheet.SpreadsheetDocument"
    draw = MagicMock()
    draw.supportsService.side_effect = lambda s: s == "com.sun.star.drawing.DrawingDocument"
    assert peer_outer_delegate_tool_name(writer) == "delegate_to_specialized_writer_toolset"
    assert peer_outer_delegate_tool_name(calc) == "delegate_to_specialized_calc_toolset"
    assert peer_outer_delegate_tool_name(draw) == "delegate_to_specialized_draw_toolset"

    peers = [{"name": "Budget.ods", "uid": "u2", "url": "", "type": "calc"}]
    with patch("plugin.doc.peer_message.list_v1_peers", return_value=peers):
        outer = get_peer_messaging_prompt_block(calc, ctx=object())
        inner = get_peer_inner_choice_block(object(), doc=object())
    assert outer == format_peer_outer_delegate_hint(calc)
    assert "delegate_to_specialized_calc_toolset" in outer
    assert "send_peer_work" not in outer
    assert "send_peer_result" in outer
    assert "Budget.ods" in inner
    assert "uid=u2" in inner
    assert PEER_INNER_CHOICE_RULES in inner
    assert "Stop tool use and Ready" in outer
    assert "you MUST Do" in outer and "document_research" in outer
    assert "local sidebar answer never reaches" in outer
    assert "Do not delegate an ack specialize" in outer
    assert "[Peer work from:" in outer
    assert "[Peer result from:" in outer
    assert "send_peer_work" not in outer
    assert "send_peer_result" in outer


def test_outer_hint_idle_after_send_and_conditional_reply():
    """Outer hint encodes idle-after-send and MUST-reply after work envelopes."""
    from plugin.framework.prompts import (
        PEER_OUTER_DELEGATE_HINT,
        PEER_OUTER_IDLE_AFTER_SEND,
        annotate_outer_peer_wait,
        looks_like_peer_wait_outcome,
    )

    assert PEER_OUTER_IDLE_AFTER_SEND in PEER_OUTER_DELEGATE_HINT
    assert "send_peer_work" not in PEER_OUTER_IDLE_AFTER_SEND
    assert "send_peer_result" not in PEER_OUTER_IDLE_AFTER_SEND
    assert looks_like_peer_wait_outcome("Message sent to the peer. Waiting for reply.")
    assert looks_like_peer_wait_outcome('{"status": "ok", "accepted": true}')
    assert looks_like_peer_wait_outcome("waiting for a peer reply")
    assert not looks_like_peer_wait_outcome("Q4 revenue is 12 in Sheet1.A1")

    sent = annotate_outer_peer_wait(
        {"status": "ok", "message": "Specialized task (document_research) completed.", "result": "Message sent to the peer."},
    )
    assert PEER_OUTER_IDLE_AFTER_SEND in sent["message"]
    assert PEER_OUTER_IDLE_AFTER_SEND in sent["result"]

    invoked = annotate_outer_peer_wait(
        {"status": "ok", "message": "Specialized task (document_research) completed.", "result": "Asked Calc for KPIs."},
        peer_send_invoked=True,
    )
    assert PEER_OUTER_IDLE_AFTER_SEND in invoked["message"]

    research = annotate_outer_peer_wait(
        {"status": "ok", "message": "Specialized task (document_research) completed.", "result": "Q4 revenue is 12."},
    )
    assert research["result"] == "Q4 revenue is 12."
    assert PEER_OUTER_IDLE_AFTER_SEND not in research["message"]

    err = annotate_outer_peer_wait({"status": "error", "message": "Message sent to the peer."})
    assert err["message"] == "Message sent to the peer."
    assert "Stop tool use" not in err["message"]

    already = annotate_outer_peer_wait(
        {
            "status": "ok",
            "message": "done " + PEER_OUTER_IDLE_AFTER_SEND,
            "result": "Message sent to the peer.",
        }
    )
    assert already["message"].count(PEER_OUTER_IDLE_AFTER_SEND) == 1


def test_summarize_peer_tool_on_wire():
    from plugin.doc.peer_message import log_peer_tool_on_wire, summarize_peer_tool_on_wire

    assert summarize_peer_tool_on_wire([{"function": {"name": "undo"}}]) == (False, 0)
    schemas = [
        {
            "function": {
                "name": PEER_WORK_TOOL_NAME,
                "description": "base Open peers: Budget.ods (uid=u2, url=, type=calc).",
            }
        }
    ]
    assert summarize_peer_tool_on_wire(schemas) == (True, 1)
    log_peer_tool_on_wire(schemas)


def test_chat_tier_excluded_from_mcp_frozensets():
    from plugin.mcp.mcp_protocol import MCP_DELEGATE_EXCLUDE_TIERS, MCP_DIRECT_FLAT_EXCLUDE_TIERS

    assert "chat" in MCP_DELEGATE_EXCLUDE_TIERS
    assert "chat" in MCP_DIRECT_FLAT_EXCLUDE_TIERS


def test_format_peer_envelope_work_vs_result():
    from plugin.doc.peer_message import PEER_WORK_DELIVERY_FOOTER, format_peer_envelope

    work = format_peer_envelope(
        name="A.ods", uid="u1", url="", message="do thing", kind="work"
    )
    result = format_peer_envelope(
        name="A.ods", uid="u1", url="", message="<table/>", kind="result"
    )
    assert work.startswith("[Peer work from: A.ods |")
    assert result.startswith("[Peer result from: A.ods |")
    assert "peer_ask_id" not in work and "peer_ask_id" not in result
    assert work == (
        "[Peer work from: A.ods | uid=u1 | url=]\n\ndo thing\n\n"
        + PEER_WORK_DELIVERY_FOOTER
    )
    assert result == "[Peer result from: A.ods | uid=u1 | url=]\n\n<table/>"
    assert PEER_WORK_DELIVERY_FOOTER in work
    assert PEER_WORK_DELIVERY_FOOTER not in result
    assert 'domain="document_research"' in PEER_WORK_DELIVERY_FOOTER
    assert "send_peer_result" in PEER_WORK_DELIVERY_FOOTER
    assert "envelope header" in PEER_WORK_DELIVERY_FOOTER
    assert "never reaches the asking peer" in PEER_WORK_DELIVERY_FOOTER
