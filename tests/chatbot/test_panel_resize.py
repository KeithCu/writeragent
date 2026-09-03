# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Unit tests for plugin.chatbot.panel_resize."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

from plugin.chatbot.panel_resize import (
    _PanelResizeListener,
    compute_chat_panel_layout,
)


def _mock_control(x, y, width, height):
    ctrl = MagicMock()
    pos = SimpleNamespace(X=x, Y=y, Width=width, Height=height)

    def set_pos_size(nx, ny, nw, nh, _flags):
        pos.X, pos.Y, pos.Width, pos.Height = nx, ny, nw, nh

    ctrl.getPosSize.return_value = pos
    ctrl.setPosSize.side_effect = set_pos_size
    return ctrl


def _xdl_snapshot():
    """Positions from extension/Dialogs/ChatPanelDialog.xdl."""
    return {
        "btn_settings": (4, 2, 16, 12),
        "btn_python": (22, 2, 16, 12),
        "btn_latex": (40, 2, 16, 12),
        "btn_search": (58, 2, 16, 12),
        "btn_hamburger": (76, 2, 16, 12),
        "response": (4, 16, 142, 110),
        "status": (4, 128, 142, 10),
        "query_label": (4, 140, 142, 10),
        "query": (4, 152, 142, 30),
        "send": (4, 186, 50, 15),
        "stop": (56, 186, 50, 15),
        "clear": (108, 186, 50, 15),
        "chat_mode_selector": (4, 203, 142, 14),
        "model_label": (4, 217, 142, 10),
        "model_selector": (4, 229, 142, 14),
        "image_model_selector": (4, 217, 142, 14),
        "base_size_label": (4, 231, 20, 10),
        "base_size_input": (25, 229, 40, 14),
        "aspect_ratio_selector": (70, 229, 102, 14),
    }


class TestComputeChatPanelLayout:
    def test_transcript_fills_space_above_bottom_band(self):
        layouts = compute_chat_panel_layout(900, 500, _xdl_snapshot())
        response = layouts["response"]
        status = layouts["status"]

        assert response.y == 16
        assert status.y > response.y + response.height
        assert status.y > 300
        assert response.height > 200

    def test_inflated_response_snapshot_height_is_ignored(self):
        snapshot = _xdl_snapshot()
        snapshot["response"] = (4, 16, 142, 400)
        layouts = compute_chat_panel_layout(900, 500, snapshot)
        response = layouts["response"]
        status = layouts["status"]

        assert response.height > 200
        assert status.y > response.y + response.height - 20

    def test_tall_panel_gives_larger_transcript(self):
        short = compute_chat_panel_layout(900, 373, _xdl_snapshot())["response"].height
        tall = compute_chat_panel_layout(900, 900, _xdl_snapshot())["response"].height
        assert tall > short

    def test_short_panel_keeps_minimum_transcript_and_visible_bottom(self):
        layouts = compute_chat_panel_layout(900, 220, _xdl_snapshot())
        response = layouts["response"]
        status = layouts["status"]

        assert response.height >= 30
        assert status.y + status.height <= 220

    def test_stretch_controls_fill_column(self):
        layouts = compute_chat_panel_layout(900, 500, _xdl_snapshot())
        right = 900 - 4
        for name in ("status", "query", "chat_mode_selector", "model_selector"):
            rect = layouts[name]
            assert rect.x + rect.width == right
        assert layouts["response"].x + layouts["response"].width == right
        assert layouts["chat_mode_selector"].width == layouts["model_selector"].width

    def test_narrow_panel_no_child_overflows(self):
        layouts = compute_chat_panel_layout(180, 500, _xdl_snapshot())
        right = 180 - 4
        for name, rect in layouts.items():
            assert rect.x + rect.width <= right, name


    def test_hidpi_mapped_positions_do_not_overflow(self):
        # 3x AppFont mapping: Clear X sits past a 400px column unless we move X.
        snapshot = {
            name: (x * 3, y * 3, w * 3, h * 3) for name, (x, y, w, h) in _xdl_snapshot().items()
        }
        layouts = compute_chat_panel_layout(400, 900, snapshot)
        right = 400 - 4
        for name, rect in layouts.items():
            assert rect.x >= 0, name
            assert rect.x + rect.width <= right, name

class TestPanelResizeListenerIntegration:
    def test_listener_applies_layout_and_syncs_rich_control(self):
        controls = {
            name: _mock_control(x, y, w, h)
            for name, (x, y, w, h) in _xdl_snapshot().items()
        }
        rich = _mock_control(12, 24, 120, 90)
        controls["response_rich"] = rich
        root = MagicMock()
        root.getPosSize.return_value = SimpleNamespace(Width=900, Height=500)
        root.getControl.side_effect = lambda name: controls.get(name)

        listener = _PanelResizeListener(controls)
        listener._width_negotiated = True
        with patch("plugin.chatbot.rich_text_control.get_control_text_length", return_value=0):
            listener.relayout_now(root)

        expected = compute_chat_panel_layout(900, 500, _xdl_snapshot())
        for name, rect in expected.items():
            ps = controls[name].getPosSize()
            assert ps.X == rect.x
            assert ps.Y == rect.y
            assert ps.Width == rect.width
            assert ps.Height == rect.height

        assert listener.last_response_rect is not None
        _rx, _ry, _rw, rh = listener.last_response_rect
        assert rh == expected["response"].height
        assert rich.getPosSize().Height == rh - 16

    def test_listener_syncs_rich_control_bounds_when_non_empty(self):
        controls = {
            name: _mock_control(x, y, w, h)
            for name, (x, y, w, h) in _xdl_snapshot().items()
        }
        rich = _mock_control(12, 24, 120, 90)
        controls["response_rich"] = rich
        root = MagicMock()
        root.getPosSize.return_value = SimpleNamespace(Width=900, Height=500)
        root.getControl.side_effect = lambda name: controls.get(name)

        listener = _PanelResizeListener(controls)
        listener._width_negotiated = True
        with patch("plugin.chatbot.rich_text_control.get_control_text_length", return_value=10):
            listener.relayout_now(root)

        expected = compute_chat_panel_layout(900, 500, _xdl_snapshot())
        _rx, _ry, _rw, rh = listener.last_response_rect
        assert rh == expected["response"].height
        assert rich.getPosSize().Height == rh - 16

    def test_slash_popup_overlay_is_not_laid_out(self):
        snapshot = _xdl_snapshot()
        snapshot["slash_popup"] = (4, 80, 142, 60)
        layouts = compute_chat_panel_layout(900, 500, snapshot)
        assert "slash_popup" not in layouts
        assert "query" in layouts

    def test_narrow_panel_stretches_response_to_margin(self):
        layouts = compute_chat_panel_layout(180, 500, _xdl_snapshot())
        response = layouts["response"]
        assert response.x + response.width <= 180 - 4

    def test_create_time_overflow_is_clamped_before_negotiation(self):
        # Keith: FIRST LAYOUT root=320 max_child_right=1087 overflow=YES
        snapshot = {
            name: (x * 3, y * 3, w * 3, h * 3) for name, (x, y, w, h) in _xdl_snapshot().items()
        }
        controls = {
            name: _mock_control(x, y, w, h) for name, (x, y, w, h) in snapshot.items()
        }
        parent = MagicMock()
        parent.getPosSize.return_value = SimpleNamespace(Width=1115, Height=1684)
        root = MagicMock()
        root.getPosSize.return_value = SimpleNamespace(Width=320, Height=400)
        listener = _PanelResizeListener(controls)
        listener._parent_window = parent
        listener.relayout_now(root)
        right = 320 - 4
        for name, ctrl in controls.items():
            ps = ctrl.getPosSize()
            assert ps.X + ps.Width <= right, name
        parent.setPosSize.assert_not_called()

    def test_create_gtk_jump_before_hfw_does_not_become_the_column(self):
        # 1x H8 log: FIRST LAYOUT 320 then windowResized 383 before hfw.
        # Filling 383 is how the default H-bar gets its extra width.
        snapshot = _xdl_snapshot()
        controls = {
            name: _mock_control(x, y, w, h) for name, (x, y, w, h) in snapshot.items()
        }
        root = MagicMock()
        root.getPosSize.return_value = SimpleNamespace(Width=320, Height=400)
        listener = _PanelResizeListener(controls)
        listener.relayout_now(root)
        root.getPosSize.return_value = SimpleNamespace(Width=383, Height=485)
        listener.on_window_resized(SimpleNamespace(Source=root))
        root.setPosSize.assert_not_called()
        right = 320 - 4
        for name, ctrl in controls.items():
            ps = ctrl.getPosSize()
            assert ps.X + ps.Width <= right, name

    def test_window_resized_grow_layouts_to_viewport_not_gtk_inflation(self):
        # Keith 2026-08-28: query_text then windowResized 995→1019, no hfw.
        # Do not setPosSize the dialog (that fights a widen drag). Layout to 995.
        snapshot = _xdl_snapshot()
        controls = {
            name: _mock_control(x, y, w, h) for name, (x, y, w, h) in snapshot.items()
        }
        root = MagicMock()
        root.getPosSize.return_value = SimpleNamespace(Width=1019, Height=2488)
        listener = _PanelResizeListener(controls)
        listener.note_width_negotiated(995)
        listener.on_window_resized(SimpleNamespace(Source=root))
        root.setPosSize.assert_not_called()
        right = 995 - 4
        for name, ctrl in controls.items():
            ps = ctrl.getPosSize()
            assert ps.X + ps.Width <= right, name

    def test_window_resized_shrink_trusts_the_window(self):
        snapshot = _xdl_snapshot()
        controls = {
            name: _mock_control(x, y, w, h) for name, (x, y, w, h) in snapshot.items()
        }
        root = MagicMock()
        root.getPosSize.return_value = SimpleNamespace(Width=800, Height=500)
        listener = _PanelResizeListener(controls)
        listener.note_width_negotiated(995)
        listener.on_window_resized(SimpleNamespace(Source=root))
        root.setPosSize.assert_not_called()
        q = controls["query"].getPosSize()
        assert q.X + q.Width <= 800 - 4
        assert q.Width > 700


class TestSidebarHeaderButtonListeners:
    def test_settings_button_listener(self):
        from plugin.chatbot.panel import SettingsButtonListener

        mock_handler = MagicMock()
        with patch("plugin.framework.main_shared.get_action_handler", return_value=mock_handler):
            listener = SettingsButtonListener()
            listener.on_action_performed(MagicMock())
            mock_handler.assert_called_once()

    def test_python_button_listener(self):
        from plugin.chatbot.panel import PythonButtonListener

        mock_handler = MagicMock()
        with patch("plugin.framework.main_shared.get_action_handler", return_value=mock_handler):
            listener = PythonButtonListener()
            listener.on_action_performed(MagicMock())
            mock_handler.assert_called_once()

    def test_latex_button_listener(self):
        from plugin.chatbot.panel import LatexButtonListener

        mock_handler = MagicMock()
        with patch("plugin.framework.main_shared.get_action_handler", return_value=mock_handler):
            listener = LatexButtonListener()
            listener.on_action_performed(MagicMock())
            mock_handler.assert_called_once()

    def test_search_button_listener(self):
        from plugin.chatbot.panel import SearchButtonListener

        mock_handler = MagicMock()
        with patch("plugin.framework.main_shared.get_action_handler", return_value=mock_handler):
            listener = SearchButtonListener()
            listener.on_action_performed(MagicMock())
            mock_handler.assert_called_once()

    def test_python_cell_button_listener(self):
        from plugin.chatbot.panel import PythonCellButtonListener

        mock_handler = MagicMock()
        with patch("plugin.framework.main_shared.get_action_handler", return_value=mock_handler):
            listener = PythonCellButtonListener()
            listener.on_action_performed(MagicMock())
            mock_handler.assert_called_once()

    def test_hamburger_button_listener(self):
        from plugin.chatbot.panel import HamburgerButtonListener

        with patch("plugin.chatbot.hamburger_menu.show_hamburger_menu") as mock_show:
            listener = HamburgerButtonListener()
            listener.on_action_performed(MagicMock())
            mock_show.assert_called_once()


