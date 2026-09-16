# WriterAgent — unit tests for Impress layout helpers
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import MagicMock, patch

import pytest

from plugin.draw.transitions import apply_slide_layout, layout_id, SetSlideLayout


def test_layout_id_text_is_title_plus_content_not_title_slide():
    assert layout_id("text") == 1
    assert layout_id("title") == 0
    assert layout_id("title_only") == 10


def test_layout_id_blank_and_none_alias():
    assert layout_id("blank") == 11
    assert layout_id("none") == 11
    assert layout_id("NONE") == 11
    assert layout_id("unknown") is None
    assert layout_id("") is None


def test_apply_slide_layout_sets_page_and_returns_canonical():
    page = MagicMock()
    page.Layout = 20
    assert apply_slide_layout(page, "text") == "text"
    assert page.Layout == 1
    assert apply_slide_layout(page, "none") == "blank"
    assert page.Layout == 11


def test_apply_slide_layout_unknown_raises():
    with pytest.raises(ValueError, match="Unknown layout"):
        apply_slide_layout(MagicMock(), "not_a_layout")


def test_set_slide_layout_uses_shared_helper():
    ctx = MagicMock()
    page = MagicMock()
    page.Layout = 20
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page):
        out = SetSlideLayout().execute(ctx, layout="text", page=0)
    assert out["status"] == "ok"
    assert out["layout"] == "text"
    assert page.Layout == 1


def test_set_slide_layout_none_alias():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page):
        out = SetSlideLayout().execute(ctx, layout="none", page=0)
    assert out["status"] == "ok"
    assert out["layout"] == "blank"
    assert page.Layout == 11
