from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from plugin.chatbot.panel_events import PanelEventsController, _relabel_button
from plugin.chatbot.send_state import ButtonAction
from plugin.chatbot.tool_loop_actions import TurnController


class _ButtonModel:
    def __init__(self, label: str = "", enabled: bool = True) -> None:
        self.Label = label
        self.Enabled = enabled


class _ButtonPeer:
    def __init__(self, label: str = "") -> None:
        self.label = label

    def getProperty(self, name: str) -> Any:
        return self.label

    def setProperty(self, name: str, value: Any) -> None:
        self.label = value


class _MockButton:
    def __init__(self, label: str = "", enabled: bool = True) -> None:
        self.model = _ButtonModel(label, enabled)
        self.peer = _ButtonPeer(label)
        self.rect = (0, 0, 100, 30)

    def getModel(self) -> Any:
        return self.model

    def getPeer(self) -> Any:
        return self.peer

    def getPosSize(self) -> Any:
        x, y, w, h = self.rect
        return SimpleNamespace(X=x, Y=y, Width=w, Height=h)

    def setPosSize(self, x: int, y: int, w: int, h: int, flags: int) -> None:
        self.rect = (x, y, w, h)


class _MockStatusControl:
    def __init__(self, text: str = "") -> None:
        self.text = text

    def getText(self) -> str:
        return self.text

    def setText(self, text: str) -> None:
        self.text = text


class DummyEventsPanel:
    def __init__(self) -> None:
        self.ctx: Any = MagicMock()
        self.frame: Any = MagicMock()
        self._panel_teardown = False
        self._send_busy = False
        self._mcp_event_bus = None
        self._mcp_turn_lock = threading.Lock()
        self._last_mcp_turn: dict[str, Any] = {}
        self._approval_event: Any = None
        self._approval_query_for_engine: str | None = None
        self._approval_ui_backup: dict[str, Any] | None = None

        self.send_control = _MockButton("Send", True)
        self.stop_control = _MockButton("Stop", False)
        self.clear_control = _MockButton("Clear", True)
        self.status_control = _MockStatusControl("Ready")
        self._button_action = ButtonAction.RECORD
        self._stop_button_action = ButtonAction.IGNORE
        self._clear_button_action = ButtonAction.IGNORE
        self._button_mnemonics: dict[str, str] = {}

        self._set_status_calls: list[str] = []
        self._append_response_calls: list[str] = []
        self._events: Any = None
        self._subscriptions: list[Any] = []

    def _mnemonic_cache(self) -> dict[str, str]:
        return self._button_mnemonics

    def _set_status(self, text: str) -> None:
        self._set_status_calls.append(text)
        self.status_control.setText(text)

    def _append_response(self, text: str) -> None:
        self._append_response_calls.append(text)

    def _subscribe(self, bus: Any, event_name: str, handler: Any) -> None:
        bus.subscribe(event_name, handler, weak=True)
        if not hasattr(self, "_subscriptions"):
            self._subscriptions = []
        self._subscriptions.append((bus, event_name, handler))

    def _finish_inline_web_approval(self, approved: bool, query_override: str | None = None) -> None:
        if hasattr(self, "_events"):
            self._events._finish_inline_web_approval(approved, query_override=query_override)

    def _on_mcp_request(self, *args: Any, **kwargs: Any) -> None:
        if hasattr(self, "_events"):
            self._events._on_mcp_request(*args, **kwargs)

    def _on_mcp_result(self, *args: Any, **kwargs: Any) -> None:
        if hasattr(self, "_events"):
            self._events._on_mcp_result(*args, **kwargs)

    def _on_http_server_started(self, *args: Any, **kwargs: Any) -> None:
        if hasattr(self, "_events"):
            self._events._on_http_server_started(*args, **kwargs)

    def _on_http_server_stopped(self, *args: Any, **kwargs: Any) -> None:
        if hasattr(self, "_events"):
            self._events._on_http_server_stopped(*args, **kwargs)

    def _post_server_status(self, text: str) -> None:
        if hasattr(self, "_events"):
            self._events._post_server_status(text)


def test_relabel_button_preserves_rect():
    btn = _MockButton("Old", True)
    cache: dict[str, str] = {}
    _relabel_button(btn, "New", cache)
    assert btn.model.Label == "New"
    assert btn.rect == (0, 0, 100, 30)


def test_mcp_subscribe_must_defer():
    with patch("plugin.framework.queue_executor.MainThread.on_python_main_thread", return_value=True):
        with patch("plugin.app.bootstrap_complete", return_value=True):
            assert PanelEventsController._mcp_subscribe_must_defer() is False

    with patch("plugin.framework.queue_executor.MainThread.on_python_main_thread", return_value=False):
        with patch("plugin.app.bootstrap_complete", return_value=False):
            assert PanelEventsController._mcp_subscribe_must_defer() is True


def test_subscribe_mcp_events():
    panel = DummyEventsPanel()
    controller = PanelEventsController(panel)
    panel._events = controller

    bus = MagicMock()
    tools = MagicMock()
    tools._services = SimpleNamespace(events=bus)

    with patch("plugin.app.get_tools", return_value=tools):
        controller._subscribe_mcp_events()

    assert panel._mcp_event_bus is bus
    bus.subscribe.assert_any_call("mcp:request", controller._on_mcp_request, weak=True)
    bus.subscribe.assert_any_call("mcp:result", controller._on_mcp_result, weak=True)


def test_independent_ui_restore_for_clear():
    panel = DummyEventsPanel()
    controller = PanelEventsController(panel)
    panel._events = controller
    panel.clear_control.model.Label = "Reject"
    panel._approval_event = threading.Event()
    panel._approval_ui_backup = {
        "actions": (ButtonAction.RECORD, ButtonAction.IGNORE, ButtonAction.IGNORE),
        "controls": {"clear": ("Clear", True)},
    }
    controller._finish_inline_web_approval(False)
    assert panel.clear_control.model.Label == "Clear"


def test_inline_web_approval_lifecycle():
    panel = DummyEventsPanel()
    controller = PanelEventsController(panel)
    panel._events = controller

    ev = threading.Event()
    controller.begin_inline_web_approval("python tutorial", "web_search", ev)

    assert panel._approval_event is ev
    assert panel._approval_query_for_engine == "python tutorial"
    assert panel.send_control.model.Label == "Accept"
    assert panel.stop_control.model.Label == "Change"
    assert panel.clear_control.model.Label == "Reject"

    controller._finish_inline_web_approval(True, query_override="python 3 tutorial")
    assert getattr(ev, "approved", None) is True
    assert getattr(ev, "query_override", None) == "python 3 tutorial"
    assert ev.is_set()
    assert panel._approval_event is None
    assert panel.send_control.model.Label == "Send"
    assert panel.stop_control.model.Label == "Stop"
    assert panel.clear_control.model.Label == "Clear"


def test_open_web_search_change_dialog():
    panel = DummyEventsPanel()
    controller = PanelEventsController(panel)
    panel._events = controller

    ev = threading.Event()
    panel._approval_event = ev
    panel._approval_query_for_engine = "original"
    panel._approval_ui_backup = {
        "actions": (ButtonAction.RECORD, ButtonAction.IGNORE, ButtonAction.IGNORE),
        "controls": {"send": ("Send", True)},
    }

    with patch("plugin.chatbot.dialogs.show_web_search_query_edit_dialog", return_value="edited"):
        controller._open_web_search_change_dialog()

    assert getattr(ev, "approved", None) is True
    assert getattr(ev, "query_override", None) == "edited"
    assert ev.is_set()


def test_http_server_status():
    panel = DummyEventsPanel()
    controller = PanelEventsController(panel)
    panel._events = controller

    with patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn: fn()):
        controller._on_http_server_started("http://localhost:8000")
        assert any("MCP server started http://localhost:8000" in s for s in panel._set_status_calls)

        controller._on_http_server_stopped()
        assert any("MCP server stopped" in s for s in panel._set_status_calls)


def test_mcp_request_and_result():
    panel = DummyEventsPanel()
    controller = PanelEventsController(panel)

    turn = TurnController(session=MagicMock())
    turn.alive = True

    with patch("plugin.chatbot.tool_loop_actions.current_turn", return_value=turn):
        controller._on_mcp_request(tool="calculator", args={"expr": "2+2"}, req_id="req1")

    assert "req1" in panel._last_mcp_turn
    assert panel._last_mcp_turn["req1"] is turn

    with (
        patch("plugin.chatbot.tool_loop_actions.current_turn", return_value=turn),
        patch("plugin.framework.queue_executor.main.is_current", return_value=True),
        patch("plugin.framework.thread_guard.get_background_task_name", return_value=None),
    ):
        controller._on_mcp_result(tool="calculator", result_snippet="4", req_id="req1")

    assert "req1" not in panel._last_mcp_turn
    assert len(panel._append_response_calls) == 1
    assert "[MCP Result]" in panel._append_response_calls[0]


def test_mcp_result_preserves_concurrent_requests():
    """Verify that earlier concurrent MCP requests are not dropped by later request IDs."""
    from tests.conftest import make_mock_send_button_listener

    listener = make_mock_send_button_listener()

    turn_mock = MagicMock(spec=TurnController)
    turn_mock.alive = True

    append_mock = MagicMock()
    listener._append_response = append_mock

    with patch("plugin.chatbot.tool_loop_actions.current_turn", return_value=turn_mock):
        # Two requests arrive concurrently
        listener._events._on_mcp_request(tool="test1", req_id="request_1")
        listener._events._on_mcp_request(tool="test2", req_id="request_2")

        # Result for request_1 arrives after request_2 was registered
        listener._events._on_mcp_result(tool="test1", result_snippet="snip1", req_id="request_1")
        # Result for request_2 arrives
        listener._events._on_mcp_result(tool="test2", result_snippet="snip2", req_id="request_2")

    # Both results must be appended
    assert append_mock.call_count == 2
    assert "test1" in append_mock.call_args_list[0][0][0]
    assert "test2" in append_mock.call_args_list[1][0][0]
