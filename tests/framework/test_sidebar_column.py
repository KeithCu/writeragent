# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for plugin.framework.sidebar_column."""

from plugin.framework.sidebar_column import sidebar_column_width


def test_fills_deck_hint():
    assert sidebar_column_width(350, 300) == 350
    assert sidebar_column_width(600, 600) == 600


def test_trusts_deck_hint_when_parent_is_slightly_smaller():
    # Old min() used 265 and left a 16px fight. deck_hint is the viewport.
    assert sidebar_column_width(281, 265) == 281


def test_does_not_wedge_to_minimal_hint():
    # Chat live: nWidth 180 (getMinimalWidth) parent 312 wedged the panel.
    assert sidebar_column_width(180, 312) == 312
    # 1115 is a stuck ChildFrame, not a column parent.
    assert sidebar_column_width(180, 1115) == 180


def test_shrink_does_not_keep_stale_wide_parent():
    # Keith HiDPI 2026-08-27: parent_after stuck at 992, deck_hint 806.
    assert sidebar_column_width(806, 992) == 806
    assert sidebar_column_width(305, 397, min_w=300) == 305
    assert sidebar_column_width(320, 397, min_w=320) == 320


def test_grow_after_childframe_sync():
    # After we set the ChildFrame request to 806, grow must use nWidth.
    assert sidebar_column_width(900, 806) == 900


def test_ignores_frame_hint_when_parent_is_column():
    assert sidebar_column_width(1262, 300) == 300
    assert sidebar_column_width(1170, 280) == 280


def test_agreed_wide_values_are_the_column():
    assert sidebar_column_width(1170, 1170) == 1170
    assert sidebar_column_width(1170, 1170, min_w=220) == 1170


def test_hidpi_column_is_not_a_frame():
    assert sidebar_column_width(900, 900) == 900
    assert sidebar_column_width(1100, 1100) == 1100
    # 1600 vs 900 is 1.78× > 1.5: treat as document frame.
    assert sidebar_column_width(1600, 900) == 900


def test_zero_hint_falls_back():
    assert sidebar_column_width(0, 280) == 280
    assert sidebar_column_width(0, 0) == 180
    assert sidebar_column_width(0, 0, min_w=220) == 220
