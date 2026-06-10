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
    """Positions from extension/WriterAgentDialogs/ChatPanelDialog.xdl."""
    return {
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

    def test_content_edge_matches_clear_button_row(self):
        layouts = compute_chat_panel_layout(900, 500, _xdl_snapshot())
        clear_right = layouts["clear"].x + layouts["clear"].width
        for name in ("status", "query", "model_selector", "image_model_selector", "aspect_ratio_selector"):
            rect = layouts[name]
            assert rect.x + rect.width <= clear_right
            assert rect.width < 200


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

    def test_listener_preserves_non_empty_rich_control_bounds(self):
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

        ps = rich.getPosSize()
        assert (ps.X, ps.Y, ps.Width, ps.Height) == (12, 24, 120, 90)

    def test_narrow_panel_stretches_response_to_margin(self):
        layouts = compute_chat_panel_layout(180, 500, _xdl_snapshot())
        response = layouts["response"]
        assert response.x + response.width <= 180 - 4
