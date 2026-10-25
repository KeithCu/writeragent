# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO tests for ChatPanelDialog resize and layout constraints."""

from __future__ import annotations

from typing import Any

from plugin.chatbot.dialogs import get_optional, load_writeragent_dialog
from plugin.chatbot.panel_resize import (
    _BOTTOM_CLUSTER,
    _PanelResizeListener,
    column_right_margin,
    compute_chat_panel_layout,
)
from plugin.testing_runner import native_test


@native_test
def test_chat_panel_resize_native(ctx: Any) -> None:
    """Verify dialog layout, bounds clamping, and bottom cluster anchoring under UNO."""
    dialog = load_writeragent_dialog("ChatPanelDialog", ctx=ctx)
    assert dialog is not None, "Failed to load ChatPanelDialog"

    control_names = (
        "btn_settings",
        "btn_python",
        "btn_latex",
        "btn_search",
        "btn_hamburger",
        "backend_indicator",
        "response",
        "status",
        "query_label",
        "query",
        "send",
        "stop",
        "clear",
        "chk_voice",
        "chat_mode_selector",
        "model_label",
        "model_selector",
        "image_model_selector",
        "base_size_label",
        "base_size_input",
        "aspect_ratio_selector",
    )
    controls: dict[str, Any] = {}
    for name in control_names:
        ctrl = get_optional(dialog, name)
        if ctrl is not None:
            controls[name] = ctrl

    listener = _PanelResizeListener(controls)
    listener.set_windows(dialog, None)

    initial_pos = dialog.getPosSize()
    cur_w = int(initial_pos.Width) or 320
    cur_h = int(initial_pos.Height) or 500

    test_widths = [cur_w, 240, 900]
    for w in test_widths:
        dialog.setPosSize(0, 0, w, cur_h, 15)
        listener.relayout_now(dialog, viewport_w=w)

        snapshot: dict[str, tuple[int, int, int, int]] = {}
        for name, ctrl in controls.items():
            pos = ctrl.getPosSize()
            snapshot[name] = (int(pos.X), int(pos.Y), int(pos.Width), int(pos.Height))

        layouts = compute_chat_panel_layout(w, cur_h, snapshot)
        margin = column_right_margin(snapshot)

        # Assert no laid-out child's right edge passes the column
        for name, rect in layouts.items():
            assert rect.x + rect.width <= w, f"{name} right edge {rect.x + rect.width} exceeds width {w}"

        # Assert response_rich is not part of compute_chat_panel_layout's result
        assert "response_rich" not in layouts

        # Assert response height > 0
        resp = layouts.get("response")
        assert resp is not None and resp.height > 0, "Response height must be > 0"

        # Assert bottom cluster controls are still inside the window
        for b_name in ("send", "query", "status"):
            b_rect = layouts.get(b_name)
            if b_rect is not None:
                assert b_rect.y + b_rect.height <= cur_h, f"{b_name} bottom passes window height {cur_h}"
