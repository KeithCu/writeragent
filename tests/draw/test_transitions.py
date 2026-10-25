# WriterAgent — unit tests for Impress layout helpers
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import MagicMock, patch

import pytest

from plugin.draw.transform_schema import AUTOLAYOUT_BY_NAME, AUTOLAYOUT_ID
from plugin.draw.transitions import (
    GetSlideLayout,
    GetSlideTransition,
    SetSlideLayout,
    SetSlideTransition,
    _FADE_EFFECTS,
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


def test_get_slide_transition_defaults_when_properties_missing():
    ctx = MagicMock()
    page = MagicMock()
    page.getPropertyValue.side_effect = Exception("Property missing")
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page):
        out = GetSlideTransition().execute(ctx, page=0)
    assert out == {
        "status": "ok",
        "page": 0,
        "effect": "none",
        "speed": "medium",
        "duration": 0,
        "transition_duration": None,
        "advance": "on_click",
    }


def test_get_slide_transition_defaults_only_the_failed_property():
    ctx = MagicMock()
    page = MagicMock()
    effect_val = MagicMock()
    effect_val.value = "DISSOLVE"
    speed_val = MagicMock()
    speed_val.value = "SLOW"

    def get_property(name: str):
        if name == "Effect":
            return effect_val
        if name == "Speed":
            return speed_val
        if name == "Duration":
            return 3
        if name == "TransitionDuration":
            raise Exception("TransitionDuration missing")
        if name == "Change":
            return 2
        raise Exception(f"Unexpected {name}")

    page.getPropertyValue.side_effect = get_property
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page):
        out = GetSlideTransition().execute(ctx, page=1)
    assert out == {
        "status": "ok",
        "page": 1,
        "effect": "dissolve",
        "speed": "slow",
        "duration": 3,
        "transition_duration": None,
        "advance": "semi_auto",
    }


def test_set_slide_transition_effect_and_speed():
    ctx = MagicMock()
    page = MagicMock()

    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page), \
         patch("plugin.draw.transitions._pres_enum", side_effect=lambda type_name, member: (type_name, member)):
        out = SetSlideTransition().execute(ctx, page=0, effect="dissolve", speed="fast", duration=5, advance="auto")

    assert out == {"status": "ok", "page": 0, "updated": ["effect", "speed", "duration", "advance"]}
    page.setPropertyValue.assert_any_call("Effect", ("FadeEffect", "DISSOLVE"))
    page.setPropertyValue.assert_any_call("Speed", ("AnimationSpeed", "FAST"))
    page.setPropertyValue.assert_any_call("Duration", 5)
    page.setPropertyValue.assert_any_call("Change", 1)


def test_set_slide_transition_unknown_effect():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page):
        out = SetSlideTransition().execute(ctx, page=0, effect="not_a_real_effect")

    assert out["status"] == "error"
    assert "Unknown effect" in out["message"]
    assert "dissolve" in out["details"]["available"]


def test_set_slide_transition_enum_import_error_is_a_tool_error():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page), \
         patch("plugin.draw.transitions._pres_enum", side_effect=ImportError("no uno")):
        out = SetSlideTransition().execute(ctx, page=0, effect="dissolve")

    assert out["status"] == "error"
    assert "Could not update slide transition" in out["message"]
    page.setPropertyValue.assert_not_called()


def test_set_slide_transition_accepts_every_effect_key():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page), \
         patch("plugin.draw.transitions._pres_enum", return_value="enum"):
        for key in _FADE_EFFECTS:
            out = SetSlideTransition().execute(ctx, page=0, effect=key)
            assert out["status"] == "ok"
            assert "effect" in out["updated"]


def test_set_slide_transition_unknown_effect_never_looks_up_enum():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page), \
         patch("plugin.draw.transitions._pres_enum", side_effect=AssertionError("enum lookup")):
        out = SetSlideTransition().execute(ctx, page=0, effect="bogus")

    assert out["status"] == "error"
    assert "Unknown effect" in out["message"]


def test_set_slide_transition_unknown_speed():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page):
        out = SetSlideTransition().execute(ctx, page=0, speed="supersonic")

    assert out["status"] == "error"
    assert "Unknown speed" in out["message"]
    assert out["details"]["available"] == ["fast", "medium", "slow"]


def test_set_slide_transition_unknown_advance():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page):
        out = SetSlideTransition().execute(ctx, page=0, advance="telepathic")

    assert out["status"] == "error"
    assert "Unknown advance" in out["message"]
    assert out["details"]["available"] == ["auto", "on_click", "semi_auto"]


def test_set_slide_transition_accepts_semi_auto():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page):
        out = SetSlideTransition().execute(ctx, page=0, advance="semi_auto")
    assert out["status"] == "ok"
    page.setPropertyValue.assert_any_call("Change", 2)


def test_get_slide_transition_uses_ctx_active_page_index():
    ctx = MagicMock()
    ctx.active_page_index = 2
    page = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page) as mock_get_slide:
        out = GetSlideTransition().execute(ctx)
    assert out["page"] == 2
    mock_get_slide.assert_called_once_with(ctx.doc, 2)


def test_explicit_page_wins_over_ctx_active_page_index():
    ctx = MagicMock()
    ctx.active_page_index = 2
    page = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page) as mock_get_slide:
        out = GetSlideTransition().execute(ctx, page=0)
    assert out["page"] == 0
    mock_get_slide.assert_called_once_with(ctx.doc, 0)


def test_set_slide_layout_uses_ctx_active_page_index():
    ctx = MagicMock()
    ctx.active_page_index = 1
    page = MagicMock()
    page.Layout = 20
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page) as mock_get_slide:
        out = SetSlideLayout().execute(ctx, layout="text")
    assert out["page"] == 1
    assert page.Layout == 1
    mock_get_slide.assert_called_once_with(ctx.doc, 1)


def test_set_slide_transition_invalid_speed_writes_nothing():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page):
        out = SetSlideTransition().execute(ctx, page=0, effect="dissolve", speed="supersonic")
    assert out["status"] == "error"
    assert "Unknown speed" in out["message"]
    page.setPropertyValue.assert_not_called()


def test_set_slide_transition_invalid_advance_writes_nothing():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page):
        out = SetSlideTransition().execute(ctx, page=0, effect="dissolve", duration=5, advance="telepathic")
    assert out["status"] == "error"
    page.setPropertyValue.assert_not_called()


def test_set_slide_transition_bad_duration_type():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page):
        out = SetSlideTransition().execute(ctx, page=0, duration="five")
    assert out["status"] == "error"
    assert "Invalid duration" in out["message"]
    page.setPropertyValue.assert_not_called()


def test_set_slide_transition_bad_transition_duration_type():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page):
        out = SetSlideTransition().execute(ctx, page=0, transition_duration="long")
    assert out["status"] == "error"
    assert "Invalid transition_duration" in out["message"]
    page.setPropertyValue.assert_not_called()


def test_set_slide_transition_write_failure_is_a_tool_error():
    ctx = MagicMock()
    page = MagicMock()
    page.setPropertyValue.side_effect = RuntimeError("boom")
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page):
        out = SetSlideTransition().execute(ctx, page=0, effect="dissolve")
    assert out["status"] == "error"
    assert "Could not update slide transition" in out["message"]


def test_set_slide_transition_disposal_still_raises():
    ctx = MagicMock()
    page = MagicMock()
    page.setPropertyValue.side_effect = RuntimeError("boom")
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=page), \
         patch("plugin.draw.transitions.is_real_disposal", return_value=True):
        with pytest.raises(RuntimeError):
            SetSlideTransition().execute(ctx, page=0, effect="dissolve")

