# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for LibrePy Python sidebar helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugin.librepy.python_sidebar import (
    _CALC_ONLY_IDS,
    _MIN_FLEX_HEIGHT,
    _PanelResizeListener,
    compute_python_sidebar_layout,
    format_runtime_status,
    workbook_key_for_doc,
)


def _xdl_snapshot():
    """Positions from extension/Dialogs/PythonSidebarDialog.xdl."""
    return {
        "btn_hdr_settings": (4, 2, 16, 12),
        "btn_python": (22, 2, 16, 12),
        "btn_latex": (40, 2, 16, 12),
        "btn_hamburger": (58, 2, 16, 12),
        "status_label": (4, 18, 172, 10),
        "status": (4, 28, 172, 28),
        "btn_refresh": (4, 60, 54, 14),
        "btn_edit_cell": (62, 60, 54, 14),
        "btn_run_script": (120, 60, 56, 14),
        "cells_label": (4, 78, 172, 10),
        "cells_list": (4, 90, 172, 70),
        "filter_label": (4, 164, 40, 10),
        "filter_combo": (44, 162, 132, 14),
        "diag_label": (4, 180, 172, 10),
        "diag_list": (4, 192, 172, 50),
        "diag_detail": (4, 246, 172, 70),
        "btn_edit_init": (4, 322, 84, 14),
        "btn_reset": (92, 322, 84, 14),
        "btn_settings": (4, 340, 172, 14),
    }


def _mock_control(x, y, width, height):
    ctrl = MagicMock()
    pos = SimpleNamespace(X=x, Y=y, Width=width, Height=height)

    def set_pos_size(nx, ny, nw, nh, _flags):
        pos.X, pos.Y, pos.Width, pos.Height = nx, ny, nw, nh

    ctrl.getPosSize.return_value = pos
    ctrl.setPosSize.side_effect = set_pos_size
    return ctrl


def test_format_runtime_status_isolated_embedded():
    ctx = MagicMock()
    with (
        patch("plugin.librepy.python_sidebar.python_session_mode", return_value="isolated"),
        patch("plugin.librepy.python_sidebar.get_config_str", return_value=""),
    ):
        text = format_runtime_status(ctx, None)
    assert "Isolated" in text
    assert "embedded" in text.lower() or "LibreOffice" in text


def test_format_runtime_status_shared_with_venv():
    ctx = MagicMock()
    with (
        patch("plugin.librepy.python_sidebar.python_session_mode", return_value="shared"),
        patch("plugin.librepy.python_sidebar.get_config_str", return_value="/tmp/myvenv"),
        patch("plugin.librepy.python_sidebar.resolve_venv_python", return_value="/tmp/myvenv/bin/python"),
    ):
        text = format_runtime_status(ctx, None)
    assert "Shared" in text
    assert "/tmp/myvenv" in text


def test_workbook_key_for_doc_uses_session_id():
    doc = MagicMock()
    with patch(
        "plugin.librepy.python_sidebar.calc_workbook_base_session_id",
        return_value="calc:file:///tmp/a.ods",
    ):
        assert workbook_key_for_doc(doc) == "calc:file:///tmp/a.ods"


def test_workbook_key_unknown_on_none():
    assert workbook_key_for_doc(None) == "unknown"


def test_python_sidebar_xdl_uses_menulist_not_listbox():
    """LibreOffice dialog.dtd has dlg:menulist only; dlg:listbox breaks createContainerWindow
    and aborts soffice with 'pure virtual method called' when the Calc sidebar opens."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    # Repo: extension/Dialogs/; make release bundle: Dialogs/ at OXT root.
    candidates = (
        root / "extension" / "Dialogs" / "PythonSidebarDialog.xdl",
        root / "Dialogs" / "PythonSidebarDialog.xdl",
    )
    xdl = next((p for p in candidates if p.is_file()), None)
    assert xdl is not None, f"PythonSidebarDialog.xdl not found under {root} (tried {[str(p) for p in candidates]})"
    text = xdl.read_text(encoding="utf-8")
    assert "dlg:listbox" not in text
    assert 'dlg:id="cells_list"' in text and "dlg:menulist" in text
    assert 'dlg:id="diag_list"' in text
    assert 'dlg:id="btn_hdr_settings"' in text
    assert 'dlg:id="btn_hamburger"' in text
    assert 'dlg:id="btn_search"' not in text


def test_activation_listener_schedules_refresh():
    """Switching sheets fires _schedule_refresh via the activation listener."""
    from plugin.librepy.python_sidebar import PythonSidebarController

    ctrl = PythonSidebarController.__new__(PythonSidebarController)
    refresh_calls = []
    ctrl._schedule_refresh = lambda *_: refresh_calls.append(1)

    # _Activation is a private class; import and instantiate it directly.
    from plugin.librepy.python_sidebar import _Activation  # type: ignore[attr-defined]

    listener = _Activation(ctrl._schedule_refresh)
    listener.activeSpreadsheetChanged(object())
    assert refresh_calls, "_schedule_refresh should be called when active sheet changes via activeSpreadsheetChanged"


def test_sidebar_prefers_frame_document_over_desktop():
    from plugin.librepy.python_sidebar import PythonSidebarController

    frame = MagicMock()
    model = MagicMock()
    frame.getController.return_value.getModel.return_value = model
    ctrl = PythonSidebarController.__new__(PythonSidebarController)
    ctrl.ctx = MagicMock()
    ctrl.frame = frame
    with (
        patch("plugin.librepy.python_sidebar.is_calc", return_value=True),
        patch("plugin.framework.thread_guard.guard_uno", side_effect=lambda m: m),
        patch("plugin.librepy.python_sidebar.get_calc_document_from_ctx") as fallback,
    ):
        assert ctrl._calc_document() is model
    fallback.assert_not_called()


def test_layout_identity_at_xdl_height():
    snapshot = _xdl_snapshot()
    layouts = compute_python_sidebar_layout(180, 376, snapshot)
    for rect in layouts.values():
        assert rect[0] + rect[2] <= 176


def test_layout_taller_grows_flex_and_stays_in_width():
    snapshot = _xdl_snapshot()
    layouts = compute_python_sidebar_layout(180, 500, snapshot)
    flex = {"status", "cells_list", "diag_list", "diag_detail"}
    for name, (ox, oy, ow, oh) in snapshot.items():
        nx, ny, nw, nh = layouts[name]
        assert nx + nw <= 176
        if name in flex:
            assert nh > oh
            assert ny >= oy
        else:
            assert nh == oh


def test_layout_wide_panel_scales_all_controls():
    snapshot = _xdl_snapshot()
    layouts = compute_python_sidebar_layout(500, 360, snapshot)
    for rect in layouts.values():
        assert rect[0] + rect[2] <= 496
    assert layouts["btn_refresh"][2] > 54
    assert layouts["btn_edit_init"][2] > 84
    assert layouts["btn_settings"][2] > 172


def test_layout_narrow_panel_does_not_overflow():
    snapshot = _xdl_snapshot()
    layouts = compute_python_sidebar_layout(150, 360, snapshot)
    for rect in layouts.values():
        assert rect[0] + rect[2] <= 146
    assert layouts["btn_refresh"][2] < 54
    assert layouts["btn_refresh"][2] >= 20


def test_layout_preserves_xdl_gaps_and_grows_flex_by_snapshot_ratio():
    snapshot = _xdl_snapshot()
    layouts = compute_python_sidebar_layout(180, 500, snapshot)
    status = layouts["status"]
    refresh = layouts["btn_refresh"]
    assert refresh[1] - (status[1] + status[3]) == 4
    leftover = 500 - 20 - (354 - 218)
    assert layouts["status"][3] == leftover * 28 // 218
    assert layouts["cells_list"][3] == leftover * 70 // 218
    assert layouts["diag_list"][3] == leftover * 50 // 218
    assigned = layouts["status"][3] + layouts["cells_list"][3] + layouts["diag_list"][3]
    assert layouts["diag_detail"][3] == leftover - assigned
    short = compute_python_sidebar_layout(180, 376, snapshot)
    tall = compute_python_sidebar_layout(180, 700, snapshot)
    for name in ("status", "cells_list", "diag_list", "diag_detail"):
        assert tall[name][3] > short[name][3]


def test_layout_short_panel_keeps_minimum_flex_height():
    layouts = compute_python_sidebar_layout(180, 200, _xdl_snapshot())
    for name in ("status", "cells_list", "diag_list", "diag_detail"):
        assert layouts[name][3] >= _MIN_FLEX_HEIGHT


def test_resize_listener_applies_layout():
    snapshot = _xdl_snapshot()
    controls = {name: _mock_control(x, y, w, h) for name, (x, y, w, h) in snapshot.items()}
    root = MagicMock()
    root.getPosSize.return_value = SimpleNamespace(Width=180, Height=500)
    listener = _PanelResizeListener(controls)
    listener.relayout_now(root)
    expected = compute_python_sidebar_layout(180, 500, snapshot)
    for name, (ex, ey, ew, eh) in expected.items():
        ps = controls[name].getPosSize()
        assert (ps.X, ps.Y, ps.Width, ps.Height) == (ex, ey, ew, eh)


def test_resize_listener_applies_wide_layout():
    snapshot = _xdl_snapshot()
    controls = {name: _mock_control(x, y, w, h) for name, (x, y, w, h) in snapshot.items()}
    root = MagicMock()
    root.getPosSize.return_value = SimpleNamespace(Width=600, Height=360)
    listener = _PanelResizeListener(controls)
    listener.relayout_now(root)
    ps = controls["btn_settings"].getPosSize()
    assert ps.X + ps.Width <= 588
    assert controls["btn_refresh"].getPosSize().Width > 54


def test_resize_listener_applies_narrow_layout():
    snapshot = _xdl_snapshot()
    controls = {name: _mock_control(x, y, w, h) for name, (x, y, w, h) in snapshot.items()}
    root = MagicMock()
    root.getPosSize.return_value = SimpleNamespace(Width=150, Height=360)
    listener = _PanelResizeListener(controls)
    listener.relayout_now(root)
    for ctrl in controls.values():
        ps = ctrl.getPosSize()
        assert ps.X + ps.Width <= 138
    assert controls["btn_refresh"].getPosSize().Width < 54


def test_layout_button_rows_and_gaps_at_arbitrary_widths():
    snapshot = _xdl_snapshot()
    for w in (200, 350, 500, 750, 1000):
        layouts = compute_python_sidebar_layout(w, 400, snapshot)
        # All controls stay within bounds (width - 12)
        for name, rect in layouts.items():
            assert rect[0] >= 4, f"{name} left edge {rect[0]} < 4 at width {w}"
            assert rect[0] + rect[2] <= w - 12, f"{name} right edge {rect[0] + rect[2]} > {w - 12} at width {w}"

        for hdr in ("btn_hdr_settings", "btn_python", "btn_latex", "btn_hamburger"):
            assert layouts[hdr][2] == 16
            assert layouts[hdr][3] == 12

        # 3-button row: refresh, edit_cell, run_script
        r_ref = layouts["btn_refresh"]
        r_edit = layouts["btn_edit_cell"]
        r_run = layouts["btn_run_script"]
        assert r_ref[0] == 4
        assert r_edit[0] == r_ref[0] + r_ref[2] + 4
        assert r_run[0] == r_edit[0] + r_edit[2] + 4
        assert r_run[0] + r_run[2] == w - 12

        # 2-button row: edit_init, reset
        r_init = layouts["btn_edit_init"]
        r_rst = layouts["btn_reset"]
        assert r_init[0] == 4
        assert r_rst[0] == r_init[0] + r_init[2] + 4
        assert r_rst[0] + r_rst[2] == w - 12

        # Filter combo stretches to the right edge
        r_flab = layouts["filter_label"]
        r_fcom = layouts["filter_combo"]
        assert r_flab[0] == 4
        assert r_fcom[0] == r_flab[0] + r_flab[2] + 4
        assert r_fcom[0] + r_fcom[2] == w - 12


def test_python_tool_panel_get_height_for_width_handles_all_sizes():
    from plugin.librepy.panel_factory import PythonToolPanel

    panel_win = MagicMock()
    panel_win.getPosSize.return_value = SimpleNamespace(Width=300, Height=400)
    parent_win = MagicMock()
    parent_win.getPosSize.return_value = SimpleNamespace(Width=300, Height=400)
    ctx = MagicMock()

    panel = PythonToolPanel(panel_win, parent_win, ctx)
    listener = MagicMock()
    panel.resize_listener = listener

    # deck_hint is the viewport; ChildFrame request is synced to it (width only)
    panel.getHeightForWidth(350)
    panel_win.setPosSize.assert_called_with(0, 0, 350, 400, 15)
    parent_win.setPosSize.assert_not_called()
    listener.relayout_now.assert_called_with(panel_win)

    # Wide column width (> 500)
    panel_win.reset_mock()
    listener.reset_mock()
    parent_win.getPosSize.return_value = SimpleNamespace(Width=750, Height=400)
    panel.getHeightForWidth(750)
    panel_win.setPosSize.assert_called_with(0, 0, 750, 400, 15)
    listener.relayout_now.assert_called_with(panel_win)

    # Frame-sized query must fill the docked column, not the document frame
    panel_win.reset_mock()
    listener.reset_mock()
    panel_win.getPosSize.return_value = SimpleNamespace(Width=300, Height=400)
    parent_win.getPosSize.return_value = SimpleNamespace(Width=300, Height=400)
    panel.getHeightForWidth(1262)
    panel_win.setPosSize.assert_called_with(0, 0, 300, 400, 15)
    listener.relayout_now.assert_called_with(panel_win)


def _writer_snapshot():
    full = _xdl_snapshot()
    return {k: v for k, v in full.items() if k not in _CALC_ONLY_IDS}


def test_writer_layout_flexes_status_and_full_width_reset():
    snapshot = _writer_snapshot()
    layouts = compute_python_sidebar_layout(180, 376, snapshot)
    assert "cells_list" not in layouts
    assert layouts["btn_reset"][0] == 4
    assert layouts["btn_reset"][0] + layouts["btn_reset"][2] == 168
    tall = compute_python_sidebar_layout(180, 500, snapshot)
    assert tall["status"][3] > layouts["status"][3]


def test_writer_does_not_use_desktop_calc_document():
    from plugin.librepy.python_sidebar import PythonSidebarController

    ctrl = PythonSidebarController.__new__(PythonSidebarController)
    ctrl.ctx = MagicMock()
    ctrl.frame = MagicMock()
    ctrl._calc_panel = False
    with patch("plugin.librepy.python_sidebar.get_calc_document_from_ctx") as fallback:
        assert ctrl._calc_document() is None
    fallback.assert_not_called()


def test_sidebar_xcu_registers_writer():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    paths = (
        root / "extension-core" / "registry" / "org" / "openoffice" / "Office" / "UI" / "Sidebar.xcu",
        root / "extension" / "registry" / "org" / "openoffice" / "Office" / "UI" / "Sidebar.xcu",
    )
    if not all(path.is_file() for path in paths):
        pytest.skip("extension trees are not copied into the stripped make release tree")
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "LibrePyDeck" in text
        assert "com.sun.star.text.TextDocument" in text


def test_hide_calc_only_controls_calls_set_visible():
    from plugin.librepy.python_sidebar import PythonSidebarController

    ctrl = PythonSidebarController.__new__(PythonSidebarController)
    hidden: list[str] = []

    def fake_ctrl(name):
        return name

    ctrl._ctrl = fake_ctrl  # type: ignore[method-assign]
    with patch(
        "plugin.librepy.python_sidebar.set_control_visible",
        side_effect=lambda c, vis: hidden.append(c) if not vis else None,
    ):
        ctrl._hide_calc_only_controls()
    assert set(hidden) == set(_CALC_ONLY_IDS)

