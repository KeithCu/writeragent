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
