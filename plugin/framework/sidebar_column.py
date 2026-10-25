# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sidebar column width: fill the deck viewport.

Deck.cxx ScrolledWindow H-policy is AUTOMATIC. getHeightForWidth is called
with rContentBox.GetWidth() (the viewport). The AWT parent is CreateChildFrame
inside that box.

Trust nWidth (the viewport) except:
  - 180: XDL AppFont leak, use parent
  - nWidth > 1.5× parent: document-frame leak, use parent

Do not setPosSize the ChildFrame. GtkSalFrame::SetPosSize on a SYSTEMCHILD
is gtk_widget_set_size_request (a minimum). Kids can grow past it.
Experiments: docs/chat/sidebar-hscroll-experiments.md
"""

from __future__ import annotations

import logging
from typing import Any
import uno

from plugin.framework.errors import suppress_disposed

log = logging.getLogger(__name__)

_POS_SIZE_FLAGS = 15
_LAYOUT_MIN, _LAYOUT_MAX, _LAYOUT_PREFERRED = 100, -1, 400
_XDL_APPFONT_LEAK_PX = 180
# (180, 312) is a real column parent. (180, 1115) is a stuck ChildFrame.
_XDL_LEAK_PARENT_MAX = _XDL_APPFONT_LEAK_PX * 2
_FRAME_VS_COLUMN = 1.5


def sidebar_column_width(n_width: int, parent_w: int, min_w: int = 180) -> int:
    """Pixel width the panel window and ChildFrame must fill."""
    # XDL dlg:width="180" is AppFont, not pixels.
    if n_width == _XDL_APPFONT_LEAK_PX and _XDL_APPFONT_LEAK_PX < parent_w <= _XDL_LEAK_PARENT_MAX:
        return parent_w
    # Document frame is several times the column. A grow with a lagging
    # ChildFrame request is only a little larger (Keith 900 vs 806).
    if n_width > 0 and parent_w > 0 and n_width > int(parent_w * _FRAME_VS_COLUMN):
        return parent_w
    if n_width > 0:
        return n_width
    if parent_w > 0:
        return parent_w
    return min_w


def apply_sidebar_height_for_width(
    panel_window: Any,
    parent_window: Any,
    n_width: int,
    min_w: int = 180,
) -> tuple[tuple[int, int], Any]:
    """Negotiate sidebar width, apply height fallback, and size the panel window."""
    eff_w = min_w
    current_h = _LAYOUT_PREFERRED
    layout_size = uno.createUnoStruct("com.sun.star.ui.LayoutSize", _LAYOUT_MIN, _LAYOUT_MAX, _LAYOUT_PREFERRED)
    with suppress_disposed("apply_sidebar_height_for_width", logger=log):
        parent_w = 0
        parent_h = 0
        if parent_window is not None and hasattr(parent_window, "getPosSize"):
            pr = parent_window.getPosSize()
            if pr is not None:
                parent_w = getattr(pr, "Width", 0) or 0
                parent_h = getattr(pr, "Height", 0) or 0

        panel_h = 0
        if panel_window is not None and hasattr(panel_window, "getPosSize"):
            before = panel_window.getPosSize()
            if before is not None:
                panel_h = getattr(before, "Height", 0) or 0

        current_h = panel_h if panel_h > 0 else (parent_h if parent_h > 0 else _LAYOUT_PREFERRED)
        eff_w = sidebar_column_width(n_width, parent_w, min_w=min_w)

        if panel_window is not None and hasattr(panel_window, "setPosSize"):
            panel_window.setPosSize(0, 0, eff_w, current_h, _POS_SIZE_FLAGS)

    return (eff_w, current_h), layout_size


