# WriterAgent — unit tests for Impress layout helpers
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import MagicMock, patch

import pytest

from plugin.draw.transform_schema import AUTOLAYOUT_BY_NAME, AUTOLAYOUT_ID
from plugin.draw.transitions import (
    GetSlideLayout,
    SetSlideLayout,
    _LAYOUTS,
    apply_slide_layout,
    layout_id,
)


def test_layout_ids_match_libreoffice_autolayout():
    """Friendly names must be AutoLayout ids, not PpSlideLayout minus one."""
    assert layout_id("title") == AUTOLAYOUT_BY_NAME["AUTOLAYOUT_TITLE"] == 0
    assert layout_id("text") == AUTOLAYOUT_BY_NAME["AUTOLAYOUT_TITLE_CONTENT"] == 1
    assert layout_id("two_column_text") == AUTOLAYOUT_BY_NAME["AUTOLAYOUT_TITLE_2CONTENT"] == 3
    assert layout_id("title_only") == AUTOLAYOUT_BY_NAME["AUTOLAYOUT_TITLE_ONLY"] == 19
    assert layout_id("blank") == AUTOLAYOUT_BY_NAME["AUTOLAYOUT_NONE"] == 20
    assert layout_id("four_objects") == AUTOLAYOUT_BY_NAME["AUTOLAYOUT_TITLE_4CONTENT"] == 18
    assert layout_id("vertical_text") == AUTOLAYOUT_BY_NAME["AUTOLAYOUT_TITLE_VCONTENT"] == 29
    assert layout_id("six_objects") == AUTOLAYOUT_BY_NAME["AUTOLAYOUT_TITLE_6CONTENT"] == 34
    assert layout_id("chart") == AUTOLAYOUT_ID["AUTOLAYOUT_CHART"] == 2
    assert layout_id("table") == AUTOLAYOUT_ID["AUTOLAYOUT_TAB"] == 8
    assert layout_id("object") == AUTOLAYOUT_ID["AUTOLAYOUT_OBJ"] == 11
    # The old VBA table wrote these onto page.Layout and picked another layout.
    assert layout_id("title_only") != 10
    assert layout_id("blank") != 11
    assert layout_id("two_column_text") != 2
    assert layout_id("four_objects") != 23
    assert len(set(_LAYOUTS.values())) == len(_LAYOUTS)


def test_layout_id_blank_and_none_alias():
    assert layout_id("blank") == 20
    assert layout_id("none") == 20
    assert layout_id("NONE") == 20
    assert layout_id("large_object") == layout_id("object") == 11
    assert layout_id("two_objects") == layout_id("two_column_text") == 3
    assert layout_id("unknown") is None
    assert layout_id("") is None
    # No AutoLayout constant; the old ids were handout pages.
    assert layout_id("text_and_media") is None
    assert layout_id("media_and_text") is None


def test_apply_slide_layout_sets_page_and_returns_canonical():
    page = MagicMock()
    page.Layout = 20
    assert apply_slide_layout(page, "text") == "text"
    assert page.Layout == 1
    assert apply_slide_layout(page, "none") == "blank"
    assert page.Layout == 20
    assert apply_slide_layout(page, "title_only") == "title_only"
    assert page.Layout == 19
    assert apply_slide_layout(page, "two_objects") == "two_column_text"
    assert page.Layout == 3


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
    assert page.Layout == 20


def test_get_slide_layout_labels_autolayout_not_powerpoint_ids():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page):
        page.Layout = 19
        title_only = GetSlideLayout().execute(ctx, page=0)
        page.Layout = 20
        blank = GetSlideLayout().execute(ctx, page=0)
        page.Layout = 18
        four = GetSlideLayout().execute(ctx, page=0)
        # Old four_objects id is AUTOLAYOUT_HANDOUT2 and must not keep that label.
        page.Layout = 23
        handout = GetSlideLayout().execute(ctx, page=0)
    assert title_only["layout_name"] == "title_only"
    assert title_only["layout_id"] == 19
    assert blank["layout_name"] == "blank"
    assert blank["layout_id"] == 20
    assert four["layout_name"] == "four_objects"
    assert four["layout_id"] == 18
    assert handout["layout_name"] == "unknown_23"
    assert "blank" in blank["available_layouts"]
    assert "none" in blank["available_layouts"]
    assert "title_only" in blank["available_layouts"]
