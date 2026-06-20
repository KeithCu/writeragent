# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for shared LO appearance / theme detection (used by chat + Monaco editor)."""

from unittest.mock import MagicMock

import pytest

from plugin.framework import appearance


def test_get_monaco_theme_info_dark_from_field_color():
    """Dark when FieldColor luminance < 128."""
    win = MagicMock()
    # A fairly dark color (e.g. ~0x2d2d2d)
    win.StyleSettings.FieldColor = 0x2D2D2D
    win.StyleSettings.DialogColor = 0x1E1E1E
    info = appearance.get_monaco_theme_info(style_window=win)
    assert info["is_dark"] is True
    assert info["monaco"] == "vs-dark"
    assert isinstance(info["bg"], int)


def test_get_monaco_theme_info_light_from_field_color():
    """Light otherwise; uses darkened DialogColor for bg if present."""
    win = MagicMock()
    win.StyleSettings.FieldColor = 0xFFFFFF  # bright
    win.StyleSettings.DialogColor = 0xF0F0F0
    info = appearance.get_monaco_theme_info(style_window=win)
    assert info["is_dark"] is False
    assert info["monaco"] == "vs"
    # bg should be a darkened variant of dialog or fallback
    assert info["bg"] != 0xFFFFFF


def test_get_monaco_theme_info_fallback_on_missing():
    """Safe light fallback when no StyleSettings."""
    info = appearance.get_monaco_theme_info(style_window=None, doc=None, ctx=None)
    assert info["monaco"] == "vs"
    assert info["is_dark"] is False


def test_get_theme_colors_uses_shared_logic():
    """get_theme_colors (used by chat) should still work and be consistent with is_dark."""
    win = MagicMock()
    win.StyleSettings.FieldColor = 0x222222
    win.StyleSettings.DialogColor = 0x111111
    bg, user, assistant = appearance.get_theme_colors(style_window=win)
    assert isinstance(bg, int)
    # For dark we return field_color as first
    assert bg == 0x222222


def test_get_style_window_prefers_explicit():
    explicit = object()
    assert appearance.get_style_window(style_window=explicit) is explicit
