"""Annotation lock for panel_wiring leftovers (reportMissingParameterType)."""

from __future__ import annotations

import logging
from typing import Any, get_type_hints
from unittest.mock import MagicMock

import pytest

from plugin.chatbot.panel_wiring import _install_frame_session_listeners, _wireControls


def test_wire_controls_annotates_self() -> None:
    """Module-level `_wireControls(self, ...)` is not a method; `self` still needs a type."""
    hints = get_type_hints(_wireControls)
    assert hints["self"] is Any
    assert hints["root_window"] is Any
    assert hints["has_recording"] is bool


def test_install_thread_violation_propagates() -> None:
    """FrameSession.install re-raises a UNO thread violation; wiring must not swallow it."""
    session = MagicMock()
    session.install.side_effect = RuntimeError("UNO thread violation: addFocusListener")
    query = object()
    with pytest.raises(RuntimeError, match="UNO thread violation"):
        _install_frame_session_listeners(session, object(), query, ())
    session.set_focus_pin.assert_called_once_with(query)


def test_install_other_errors_stay_a_debug_log(caplog: pytest.LogCaptureFixture) -> None:
    """A normal attach failure is still logged and does not leave wiring."""
    # What was wrong: logger.debug calls are stripped in release builds.
    # Why: skip when running against stripped release bundle.
    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("log.debug stripped in release bundle")
    session = MagicMock()
    session.install.side_effect = RuntimeError("focus attach failed")
    with caplog.at_level(logging.DEBUG, logger="plugin.chatbot.panel_wiring"):
        _install_frame_session_listeners(session, object(), object(), ())
    assert any("frame session focus install" in record.message for record in caplog.records)


def test_measure_send_width_restores_label_when_pos_size_fails() -> None:
    """A width read must not leave the button on the last candidate label."""
    from plugin.chatbot.panel_wiring import _measure_aux_button_max_width, _measure_send_button_max_width

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
    assert _measure_send_button_max_width(send, has_recording=False) is None
    assert send.getModel.return_value.Label == "Keep send"

    aux = _ctrl("Keep aux")
    assert _measure_aux_button_max_width(aux, ["Stop", "Change"]) is None
    assert aux.getModel.return_value.Label == "Keep aux"


def test_mode_ui_failure_still_passes_mode_flags_and_toggles_image_controls() -> None:
    """A raised mode-UI wire must not hand None into button wiring."""
    from unittest.mock import MagicMock, patch

    from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

    class _Host:
        ctx = object()
        send_listener = None
        frame_session = None
        toolpanel = None
        m_panelRootWindow = None

        def _get_document_model(self) -> object:
            return object()

        def _wire_model_selectors(self, *_args: object) -> None:
            return None

        def _wire_chat_mode_ui(self, *_args: object) -> tuple[str, SidebarModeFlags, object]:
            raise RuntimeError("mode ui failed")

        def _setup_sessions(self, *_args: object) -> None:
            return None

        def _wire_buttons(self, controls: object, model: object, initial_mode: str, mode_flags: object, toggle_image_ui: object) -> None:
            self.captured = (controls, model, initial_mode, mode_flags, toggle_image_ui)

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
        patch("plugin.chatbot.panel_wiring.global_event_bus.subscribe"),
        patch("plugin.chatbot.panel_wiring.init_logging", side_effect=RuntimeError("skip update schedule")),
        patch("plugin.framework.config.get_config_bool_safe", return_value=False),
        patch("plugin.embeddings.embeddings_periodic.schedule_periodic_embeddings_indexer_once"),
    ):
        _wireControls(host, root, False, lambda _ctx: None)

    _controls, _model, initial_mode, mode_flags, toggle = host.captured
    assert initial_mode == "chat"
    assert isinstance(mode_flags, SidebarModeFlags)
    assert mode_flags == SidebarModeFlags()
    toggle(False)
    named["image_model_selector"].setVisible.assert_called_with(False)
    named["model_selector"].setVisible.assert_called_with(True)
    named["aspect_ratio_selector"].setVisible.assert_called_with(False)
    named["base_size_input"].setVisible.assert_called_with(False)
    named["base_size_label"].setVisible.assert_called_with(False)
    named["model_label"].setVisible.assert_called_with(True)


def test_model_selector_failure_still_wires_chat_mode_ui() -> None:
    """A failure in _wire_model_selectors must not skip _wire_chat_mode_ui."""
    from unittest.mock import MagicMock, patch

    from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

    custom_flags = SidebarModeFlags(include_brainstorming=True)
    custom_toggle = MagicMock()
    mode_ui_called = False

    class _Host:
        ctx = object()
        send_listener = None
        frame_session = None
        toolpanel = None
        m_panelRootWindow = None

        def _get_document_model(self) -> object:
            return object()

        def _wire_model_selectors(self, *_args: object) -> None:
            raise RuntimeError("model selector failed")

        def _wire_chat_mode_ui(self, *_args: object, **_kwargs: object) -> tuple[str, SidebarModeFlags, object]:
            nonlocal mode_ui_called
            mode_ui_called = True
            return ("web", custom_flags, custom_toggle)

        def _setup_sessions(self, *_args: object) -> None:
            return None

        def _wire_buttons(self, controls: object, model: object, initial_mode: str, mode_flags: object, toggle_image_ui: object) -> None:
            self.captured = (controls, model, initial_mode, mode_flags, toggle_image_ui)

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
        patch("plugin.chatbot.panel_wiring.global_event_bus.subscribe"),
        patch("plugin.chatbot.panel_wiring.init_logging", side_effect=RuntimeError("skip update schedule")),
        patch("plugin.framework.config.get_config_bool_safe", return_value=False),
        patch("plugin.embeddings.embeddings_periodic.schedule_periodic_embeddings_indexer_once"),
    ):
        _wireControls(host, root, False, lambda _ctx: None)

    assert mode_ui_called is True
    _controls, _model, initial_mode, mode_flags, toggle = host.captured
    assert initial_mode == "web"
    assert mode_flags is custom_flags
    assert toggle is custom_toggle


def test_make_toggle_image_ui_swaps_visibility_and_relayouts() -> None:
    """make_toggle_image_ui toggles text vs image controls and invokes resize relayout."""
    from plugin.chatbot.panel_wiring import make_toggle_image_ui

    panel = MagicMock()
    root = MagicMock()
    panel.m_panelRootWindow = root
    panel.toolpanel = MagicMock()
    rl = MagicMock()
    panel.toolpanel.resize_listener = rl

    controls = {
        "model_label": MagicMock(),
        "model_selector": MagicMock(),
        "image_model_selector": MagicMock(),
        "aspect_ratio_selector": MagicMock(),
        "base_size_input": MagicMock(),
        "base_size_label": MagicMock(),
    }

    toggle = make_toggle_image_ui(panel, controls)

    # Image mode = True
    toggle(True)
    controls["model_label"].setVisible.assert_called_with(False)
    controls["model_selector"].setVisible.assert_called_with(False)
    controls["image_model_selector"].setVisible.assert_called_with(True)
    controls["aspect_ratio_selector"].setVisible.assert_called_with(True)
    controls["base_size_input"].setVisible.assert_called_with(True)
    controls["base_size_label"].setVisible.assert_called_with(True)
    rl.relayout_now.assert_called_with(root)

    # Image mode = False
    rl.relayout_now.reset_mock()
    toggle(False)
    controls["model_label"].setVisible.assert_called_with(True)
    controls["model_selector"].setVisible.assert_called_with(True)
    controls["image_model_selector"].setVisible.assert_called_with(False)
    controls["aspect_ratio_selector"].setVisible.assert_called_with(False)
    controls["base_size_input"].setVisible.assert_called_with(False)
    controls["base_size_label"].setVisible.assert_called_with(False)
    rl.relayout_now.assert_called_with(root)

