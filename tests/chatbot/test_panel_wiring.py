"""Tests for panel wiring listener installation and control wiring."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from plugin.chatbot.panel_wiring import _install_frame_session_listeners, _wireControls


def test_install_thread_violation_propagates() -> None:
    """FrameSession.install re-raises a UNO thread violation; wiring must not swallow it."""
    session = MagicMock()
    session.install.side_effect = RuntimeError("UNO thread violation: addFocusListener")
    query = object()
    with pytest.raises(RuntimeError, match="UNO thread violation"):
        _install_frame_session_listeners(session, {"query": query})
    session.set_focus_pin.assert_called_once_with(query)


def test_install_other_errors_do_not_propagate() -> None:
    """A normal attach failure is swallowed and does not leave wiring."""
    session = MagicMock()
    session.install.side_effect = RuntimeError("focus attach failed")
    query = object()
    _install_frame_session_listeners(session, {"query": query})
    session.set_focus_pin.assert_called_once_with(query)


def test_measure_send_width_restores_label_when_pos_size_fails() -> None:
    """A width read must not leave the button on the last candidate label."""
    from plugin.chatbot.panel_wiring import _pin_button_width

    def _ctrl(original: str) -> MagicMock:
        ctrl = MagicMock()
        ctrl.getModel.return_value.Label = original
        calls = {"n": 0}

        def _pos() -> MagicMock:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("pos")
            rect = MagicMock()
            rect.Width = 40
            return rect

        ctrl.getPosSize.side_effect = _pos
        return ctrl

    send = _ctrl("Keep send")
    _pin_button_width(send, ["Send", "Accept"])
    assert send.getModel.return_value.Label == "Keep send"
    send.setPosSize.assert_not_called()

    aux = _ctrl("Keep aux")
    _pin_button_width(aux, ["Stop", "Change"])
    assert aux.getModel.return_value.Label == "Keep aux"
    aux.setPosSize.assert_not_called()


def _run_wire_controls(
    *,
    frame_session: Any = None,
    wire_model_selectors: Any = None,
    wire_chat_mode_ui: Any = None,
) -> Any:
    """Invoke ``_wireControls`` on a minimal host with mocked UNO controls and logging."""
    from unittest.mock import patch

    from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

    class _Host:
        ctx = object()
        send_listener = None
        toolpanel = None
        m_panelRootWindow = None
        xParentWindow = None
        _rich_control_listener = None

        def __init__(self) -> None:
            self.frame_session = frame_session
            self.captured: tuple[Any, Any, str, Any] = (None, None, "", None)

        def _get_document_model(self) -> object:
            return object()

        def _wire_model_selectors(self, *args: object) -> None:
            if wire_model_selectors is not None:
                wire_model_selectors(*args)

        def _wire_chat_mode_ui(self, *args: object, **kwargs: object) -> tuple[str, SidebarModeFlags]:
            if wire_chat_mode_ui is not None:
                return wire_chat_mode_ui(*args, **kwargs)
            return ("chat", SidebarModeFlags())

        def _setup_sessions(self, *_args: object) -> None:
            return None

        def _wire_buttons(self, controls: Any, model: Any, initial_mode: str, mode_flags: Any) -> None:
            self.captured = (controls, model, initial_mode, mode_flags)

        def _update_backend_indicator(self, _root: object) -> None:
            return None

        def _on_config_changed(self, *_args: object) -> None:
            return None

    named: dict[str, MagicMock] = {}

    def _ctrl(name: str) -> MagicMock:
        if name not in named:
            ctrl = MagicMock(name=name)
            rect = MagicMock()
            rect.X = 0
            rect.Y = 0
            rect.Width = 10
            rect.Height = 10
            ctrl.getPosSize.return_value = rect
            named[name] = ctrl
        return named[name]

    root = MagicMock()
    root_rect = MagicMock()
    root_rect.Width = 400
    root_rect.Height = 600
    root.getPosSize.return_value = root_rect
    root.getControl.side_effect = _ctrl

    host = _Host()
    with (
        patch("plugin.chatbot.panel_wiring.translate_dialog"),
        patch("plugin.chatbot.panel_wiring.get_optional_control", side_effect=lambda _root, name: _ctrl(name)),
        patch("plugin.chatbot.panel_wiring.get_config", return_value=""),
        patch("plugin.chatbot.panel_wiring._PanelResizeListener", return_value=MagicMock()),
        patch("plugin.chatbot.panel_wiring.get_event_bus"),
        patch("plugin.chatbot.panel_wiring.init_logging", side_effect=RuntimeError("skip update schedule")),
        patch("plugin.framework.config.get_config", return_value=False),
        patch("plugin.embeddings.embeddings_periodic.schedule_periodic_embeddings_indexer_once"),
    ):
        _wireControls(host, root)
    return host


def test_mode_ui_failure_still_passes_mode_flags_and_toggles_image_controls() -> None:
    """A raised mode-UI wire must not hand None into button wiring; fallback flags are passed instead."""
    from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

    def _raise_mode_ui(*_args: object, **_kwargs: object) -> tuple[str, SidebarModeFlags]:
        raise RuntimeError("mode ui failed")

    host = _run_wire_controls(wire_chat_mode_ui=_raise_mode_ui)
    _controls, _model, initial_mode, mode_flags = host.captured
    assert initial_mode == "chat"
    assert isinstance(mode_flags, SidebarModeFlags)
    assert mode_flags == SidebarModeFlags()


def test_model_selector_failure_still_wires_chat_mode_ui() -> None:
    """A failure in model-selector wiring must not prevent chat-mode UI wiring from running."""
    from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

    custom_flags = SidebarModeFlags(include_brainstorming=True)
    mode_ui_called = False

    def _fail_model_selectors(*_args: object) -> None:
        raise RuntimeError("model selector failed")

    def _mode_ui(*_args: object, **_kwargs: object) -> tuple[str, SidebarModeFlags]:
        nonlocal mode_ui_called
        mode_ui_called = True
        return ("web", custom_flags)

    host = _run_wire_controls(
        wire_model_selectors=_fail_model_selectors,
        wire_chat_mode_ui=_mode_ui,
    )
    _controls, _model, initial_mode, mode_flags = host.captured
    assert mode_ui_called is True
    assert initial_mode == "web"
    assert mode_flags is custom_flags


def test_make_toggle_image_ui_swaps_visibility_and_relayouts() -> None:
    """ChatPanelElement._toggle_image_ui toggles text vs image controls and invokes resize relayout."""
    from plugin.chatbot.panel_factory import ChatPanelElement

    panel = MagicMock()
    root = MagicMock()
    panel.m_panelRootWindow = root
    panel.toolpanel = MagicMock()
    rl = MagicMock()
    panel.toolpanel.resize_listener = rl

    controls = {
        "model_label": MagicMock(),
        "model_selector": MagicMock(),
        "reasoning_effort_selector": MagicMock(),
        "image_model_selector": MagicMock(),
        "aspect_ratio_selector": MagicMock(),
        "base_size_input": MagicMock(),
        "base_size_label": MagicMock(),
    }
    root.getControl.side_effect = lambda n: controls.get(n)

    # Image mode = True
    ChatPanelElement._toggle_image_ui(panel, True)
    controls["model_label"].setVisible.assert_called_with(False)
    controls["model_selector"].setVisible.assert_called_with(False)
    controls["reasoning_effort_selector"].setVisible.assert_called_with(False)
    controls["image_model_selector"].setVisible.assert_called_with(True)
    controls["aspect_ratio_selector"].setVisible.assert_called_with(True)
    controls["base_size_input"].setVisible.assert_called_with(True)
    controls["base_size_label"].setVisible.assert_called_with(True)
    rl.relayout_now.assert_called_with(root)

    # Image mode = False
    rl.relayout_now.reset_mock()
    ChatPanelElement._toggle_image_ui(panel, False)
    controls["model_label"].setVisible.assert_called_with(True)
    controls["model_selector"].setVisible.assert_called_with(True)
    controls["reasoning_effort_selector"].setVisible.assert_called_with(True)
    controls["image_model_selector"].setVisible.assert_called_with(False)
    controls["aspect_ratio_selector"].setVisible.assert_called_with(False)
    controls["base_size_input"].setVisible.assert_called_with(False)
    controls["base_size_label"].setVisible.assert_called_with(False)
    rl.relayout_now.assert_called_with(root)


def test_wire_controls_installs_frame_session_when_rich_text_disabled() -> None:
    """Frame session listeners are installed even when rich text control is off."""
    frame_session = MagicMock()
    frame_session.install.return_value = None
    frame_session.set_focus_pin.return_value = None

    host = _run_wire_controls(frame_session=frame_session)

    host.frame_session.set_focus_pin.assert_called_once()
    host.frame_session.install.assert_called_once()


def test_measure_aux_button_width_uses_translated_labels(monkeypatch):
    from plugin.chatbot.panel_wiring import _pin_button_width

    class _Pos:
        X = 10
        Y = 20
        Height = 30

        def __init__(self, width: int) -> None:
            self.Width = width

    class _Model:
        Label = "Stop"

    model = _Model()

    class _Ctrl:
        pos_size_args = None

        def getModel(self):
            return model

        def getPosSize(self):
            return _Pos(len(model.Label))

        def setPosSize(self, x, y, width, height, flags):
            self.pos_size_args = (x, y, width, height, flags)

    ctrl = _Ctrl()
    monkeypatch.setattr("plugin.chatbot.panel_wiring._", lambda text: text + "XX")
    _pin_button_width(ctrl, ["Stop", "Change"])
    assert model.Label == "Stop"
    assert ctrl.pos_size_args is not None
    assert ctrl.pos_size_args[2] == len("ChangeXX")
