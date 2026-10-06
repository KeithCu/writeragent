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


from plugin.chatbot.panel_resize import (
    _PanelResizeListener,
    column_right_margin,
    compute_chat_panel_layout,
    fit_snapshot_min_heights,
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
        "chk_voice": (154, 138, 22, 14),
        "chat_mode_selector": (4, 203, 142, 14),
        "model_label": (4, 217, 142, 10),
        "model_selector": (4, 229, 142, 14),
        "image_model_selector": (4, 217, 142, 14),
        "base_size_label": (4, 233, 20, 10),
        "base_size_input": (25, 231, 40, 14),
        "aspect_ratio_selector": (70, 231, 102, 14),
    }


class TestComputeChatPanelLayout:
    def test_transcript_fills_space_above_bottom_band(self):
        layouts = compute_chat_panel_layout(900, 500, _xdl_snapshot())
        response = layouts["response"]
        status = layouts["status"]
        label = layouts["query_label"]
        chk_voice = layouts["chk_voice"]

        assert response.y == 16
        assert status.y > response.y + response.height
        assert status.y > 300
        # Label stays at its left edge. Checkbox keeps its row and is centered.
        # Label width is the space before the checkbox, not a fixed string width.
        assert label.x == 4
        assert chk_voice.y == label.y - 2
        assert chk_voice.height == 14
        assert chk_voice.x == (900 - chk_voice.width) // 2
        assert label.x + label.width + chk_voice.height // 2 <= chk_voice.x
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
        right = 900 - column_right_margin(_xdl_snapshot())
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
    def test_disposing_runs_callback_without_removing_listener(self):
        # VCL disposing means the listener is already going away. Removing it
        # from here used to be the only teardown, and it never cancelled the send.
        calls: list[str] = []
        listener = _PanelResizeListener({}, on_dispose=lambda: calls.append("dispose"))
        root = MagicMock()
        listener._root_window = root

        listener.disposing(root)

        assert calls == ["dispose"]
        root.removeWindowListener.assert_not_called()
        assert listener._root_window is None
        listener.disposing(root)
        assert calls == ["dispose"]

    def test_one_arg_constructor_disposing_is_safe(self):
        listener = _PanelResizeListener({})
        listener.disposing(None)

    def test_disposing_logs_callback_failure(self):
        def boom() -> None:
            raise RuntimeError("deck")

        listener = _PanelResizeListener({}, on_dispose=boom)
        listener.disposing(None)

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

    def test_relayout_caps_height_against_parent_window(self):
        # Calc infinite resize loop fix: cap height against parent window height
        snapshot = _xdl_snapshot()
        controls = {
            name: _mock_control(x, y, w, h) for name, (x, y, w, h) in snapshot.items()
        }
        parent = MagicMock()
        parent.getPosSize.return_value = SimpleNamespace(Width=320, Height=600)
        root = MagicMock()
        root.getPosSize.return_value = SimpleNamespace(Width=320, Height=1200)
        listener = _PanelResizeListener(controls)
        listener._parent_window = parent
        listener.relayout_now(root)
        send = controls["send"].getPosSize()
        assert send.Y < 600

    def test_relayout_caps_height_at_maximum_limit(self):
        # Unbounded resize loop ceiling: cap at 3000px max
        snapshot = _xdl_snapshot()
        controls = {
            name: _mock_control(x, y, w, h) for name, (x, y, w, h) in snapshot.items()
        }
        root = MagicMock()
        root.getPosSize.return_value = SimpleNamespace(Width=320, Height=5000)
        listener = _PanelResizeListener(controls)
        listener.relayout_now(root)
        send = controls["send"].getPosSize()
        assert send.Y < 3000


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


def test_image_mode_bottom_row_fits_at_1x():
    """Scrolly QA: at 1x the Image-mode bottom row (Base, aspect) was clipped.

    The row overlapped image_model_selector by 2 units in the XDL and sat 10px
    from the panel edge, which GTK's taller 1x combo boxes overran.
    """
    from plugin.chatbot.panel_resize import compute_chat_panel_layout

    height = 500
    layouts = compute_chat_panel_layout(300, height, _xdl_snapshot())
    image_model = layouts["image_model_selector"]
    for name in ("base_size_input", "aspect_ratio_selector"):
        rect = layouts[name]
        assert rect.y >= image_model.y + image_model.height
        assert rect.y + rect.height == height - 20


# Snapshots captured from the real sidebar (LibreOffice 25.2, gen VCL) at
# SAL_FORCEDPI=96 and 192: ChatPanelDialog.xdl after AppFont mapping.
_SNAPSHOT_1X = {
    "btn_settings": (7, 3, 28, 20), "btn_python": (39, 3, 28, 20), "btn_latex": (71, 3, 28, 20),
    "btn_search": (103, 3, 28, 20), "btn_hamburger": (135, 3, 28, 20), "backend_indicator": (213, 7, 99, 16),
    "response": (7, 26, 252, 179), "status": (7, 208, 252, 16), "query_label": (7, 228, 252, 16),
    "query": (7, 247, 252, 49), "send": (7, 302, 89, 24), "stop": (99, 302, 89, 24), "clear": (192, 302, 78, 24),
    "chk_voice": (273, 224, 39, 23), "chat_mode_selector": (7, 330, 252, 23), "model_label": (7, 353, 252, 16),
    "model_selector": (7, 372, 252, 23), "image_model_selector": (7, 353, 252, 23),
    "base_size_label": (7, 379, 36, 16), "base_size_input": (44, 375, 71, 23), "aspect_ratio_selector": (124, 375, 181, 23),
}
_SNAPSHOT_2X = {
    "btn_settings": (13, 6, 52, 38), "btn_python": (72, 6, 52, 38), "btn_latex": (131, 6, 52, 38),
    "btn_search": (190, 6, 52, 38), "btn_hamburger": (249, 6, 52, 38), "backend_indicator": (393, 13, 183, 31),
    "response": (13, 50, 465, 344), "status": (13, 400, 465, 31), "query_label": (13, 438, 465, 31),
    "query": (13, 475, 465, 94), "send": (13, 581, 164, 47), "stop": (183, 581, 164, 47), "clear": (354, 581, 144, 47),
    "chk_voice": (504, 431, 72, 44), "chat_mode_selector": (13, 634, 465, 44), "model_label": (13, 678, 465, 31),
    "model_selector": (13, 716, 465, 44), "image_model_selector": (13, 678, 465, 44),
    "base_size_label": (13, 728, 66, 31), "base_size_input": (82, 722, 131, 44), "aspect_ratio_selector": (229, 722, 334, 44),
}


class TestColumnFitsAtBothScales:
    def test_right_inset_mirrors_left_inset(self):
        assert column_right_margin(_SNAPSHOT_1X) == 8
        assert column_right_margin(_SNAPSHOT_2X) == 16

    def test_panel_request_fits_default_column(self):
        # The panel asks the deck for max_child_right + left inset (+1 at 2x).
        # With a 4px right margin that was 262 > 259 (1x) and 305 > 296 (2x):
        # a horizontal scrollbar at the default width.
        for snapshot, width in ((_SNAPSHOT_1X, 259), (_SNAPSHOT_1X, 320), (_SNAPSHOT_2X, 235), (_SNAPSHOT_2X, 296)):
            layouts = compute_chat_panel_layout(width, 900, snapshot)
            left = snapshot["response"][0]
            max_right = max(r.x + r.width for r in layouts.values())
            assert max_right + left + 1 <= width, (width, max_right)

    def test_button_row_shares_width_at_2x(self):
        width = 296
        layouts = compute_chat_panel_layout(width, 900, _SNAPSHOT_2X)
        send, stop, clear = layouts["send"], layouts["stop"], layouts["clear"]
        assert send.x == 13
        assert send.x + send.width < stop.x
        assert stop.x + stop.width < clear.x
        # Measured minimum widths of Record/Stop/Clear at 2x are 71/67/72.
        for rect in (send, stop, clear):
            assert rect.width >= 72
        assert clear.x + clear.width == layouts["query"].x + layouts["query"].width

    def test_button_row_lines_up_with_boxes_at_1x(self):
        layouts = compute_chat_panel_layout(259, 900, _SNAPSHOT_1X)
        assert layouts["clear"].x + layouts["clear"].width == layouts["query"].x + layouts["query"].width
        # Measured minimum widths at 1x are 49/47/51.
        assert min(layouts[n].width for n in ("send", "stop", "clear")) >= 51

    def test_checkbox_follows_measured_label(self):
        for snapshot, label_w, width in ((_SNAPSHOT_2X, 128, 296), (_SNAPSHOT_1X, 67, 259), (_SNAPSHOT_2X, 128, 600)):
            layouts = compute_chat_panel_layout(width, 900, snapshot, preferred={"query_label": (label_w, 25)})
            label, voice = layouts["query_label"], layouts["chk_voice"]
            assert label.width >= label_w, width
            assert voice.x > label.x + label.width
            assert voice.x - (label.x + label_w) <= voice.height, width
            assert voice.x + voice.width <= width - column_right_margin(snapshot)

    def test_checkbox_squeezes_label_only_when_column_is_too_narrow(self):
        layouts = compute_chat_panel_layout(120, 900, _SNAPSHOT_2X, preferred={"query_label": (128, 25)})
        label, voice = layouts["query_label"], layouts["chk_voice"]
        assert voice.x + voice.width <= 120 - column_right_margin(_SNAPSHOT_2X)
        assert label.x + label.width < voice.x

    def test_status_grows_to_minimum_height_and_pushes_band_down(self):
        fitted = fit_snapshot_min_heights(_SNAPSHOT_1X, {"status": 21})
        assert fitted["status"] == (7, 208, 252, 21)
        assert fitted["query_label"][1] == 228 + 5
        assert fitted["send"][1] == 302 + 5
        assert fitted["response"] == _SNAPSHOT_1X["response"]
        layouts = compute_chat_panel_layout(259, 600, fitted)
        status = layouts["status"]
        assert status.height == 21
        assert status.y + status.height <= layouts["query_label"].y
        assert layouts["response"].y + layouts["response"].height <= status.y

    def test_status_already_tall_enough_is_unchanged(self):
        assert fit_snapshot_min_heights(_SNAPSHOT_2X, {"status": 31}) == _SNAPSHOT_2X

    def test_listener_measures_status_and_label_from_peer(self):
        controls = {name: _mock_control(*rect) for name, rect in _SNAPSHOT_1X.items()}
        controls["status"].getMinimumSize.return_value = SimpleNamespace(Width=38, Height=21)
        controls["query_label"].getPreferredSize.return_value = SimpleNamespace(Width=67, Height=13)
        root = MagicMock()
        root.getPosSize.return_value = SimpleNamespace(Width=259, Height=600)
        listener = _PanelResizeListener(controls)
        listener.relayout_now(root)
        assert controls["status"].getPosSize().Height == 21
        label = controls["query_label"].getPosSize()
        voice = controls["chk_voice"].getPosSize()
        assert label.Width >= 67
        assert voice.X > label.X + label.Width

    def test_image_size_box_uses_measured_width_at_2x(self):
        # Measured preferred width of the "1024" size box at 2x is 68px; its
        # XDL width is 131px, which left the aspect box ~60px ("Squa").
        layouts = compute_chat_panel_layout(296, 900, _SNAPSHOT_2X, preferred={"base_size_input": (68, 41)})
        base, aspect = layouts["base_size_input"], layouts["aspect_ratio_selector"]
        assert base.width == 68
        assert aspect.x == base.x + base.width + 16
        assert aspect.width >= 100
        assert aspect.x + aspect.width == 296 - column_right_margin(_SNAPSHOT_2X)

    def test_image_size_box_never_grows_past_xdl_width(self):
        layouts = compute_chat_panel_layout(259, 900, _SNAPSHOT_1X, preferred={"base_size_input": (500, 29)})
        assert layouts["base_size_input"].width == 71
