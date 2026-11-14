# WriterAgent — unit tests for Impress layout helpers
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import MagicMock, patch

import pytest

from plugin.draw.layouts import LAYOUT_TABLE, layout_id, layout_name
from plugin.draw.transitions import (
    GetSlideLayout,
    GetSlideTransition,
    SetSlideLayout,
    SetSlideTransition,
    _FADE_EFFECTS,
    apply_slide_layout,
)


def test_layout_table_friendly_names_unique():
    """Friendly names in LAYOUT_TABLE must be unique (id pins live in test_transitions_uno.py)."""
    friendly_entries = [row[0] for row in LAYOUT_TABLE if row[0] is not None]
    assert len(set(friendly_entries)) == len(friendly_entries)


def test_layout_id_blank_and_none_alias():
    assert layout_id("none") == layout_id("blank") == 20
    assert layout_id("NONE") == 20
    assert layout_id("unknown") is None
    assert layout_id("") is None


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
    with pytest.raises(ValueError, match="Unknown layout"):
        apply_slide_layout(MagicMock(), "999")


def test_layout_name_fallback_and_numeric_id():
    assert layout_name(21) == "autolayout_notes"
    assert layout_name(99) == "unknown_99"
    assert apply_slide_layout(MagicMock(), "21") == "autolayout_notes"


def test_set_slide_layout_uses_shared_helper():
    ctx = MagicMock()
    page = MagicMock()
    page.Layout = 20
    with patch("plugin.draw.transitions.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = SetSlideLayout().execute(ctx, layout="text", page=0)
    assert out["status"] == "ok"
    assert out["layout"] == "text"
    assert page.Layout == 1


def test_set_slide_layout_none_alias():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.transitions.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = SetSlideLayout().execute(ctx, layout="none", page=0)
    assert out["status"] == "ok"
    assert out["layout"] == "blank"
    assert page.Layout == 20


def test_get_slide_layout_labels_autolayout_not_powerpoint_ids():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.transitions.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        page.Layout = 19
        title_only = GetSlideLayout().execute(ctx, page=0)
        page.Layout = 20
        blank = GetSlideLayout().execute(ctx, page=0)
        page.Layout = 18
        four = GetSlideLayout().execute(ctx, page=0)
        # Old four_objects id is AUTOLAYOUT_HANDOUT2 and must not keep that label.
        page.Layout = 23
        handout = GetSlideLayout().execute(ctx, page=0)
        page.Layout = 99
        unknown = GetSlideLayout().execute(ctx, page=0)
    assert title_only["layout_name"] == "title_only"
    assert title_only["layout_id"] == 19
    assert blank["layout_name"] == "blank"
    assert blank["layout_id"] == 20
    assert four["layout_name"] == "four_objects"
    assert four["layout_id"] == 18
    assert handout["layout_name"] == "autolayout_handout2"
    assert unknown["layout_name"] == "unknown_99"


def test_get_slide_transition_defaults_when_properties_missing():
    ctx = MagicMock()
    page = MagicMock()
    page.getPropertyValue.side_effect = Exception("Property missing")
    with patch("plugin.draw.transitions.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
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
    with patch("plugin.draw.transitions.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 1)
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

    with patch("plugin.draw.transitions.DrawBridge") as bridge_cls, \
         patch("plugin.draw.transitions._pres_enum", side_effect=lambda type_name, member: (type_name, member)):
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = SetSlideTransition().execute(ctx, page=0, effect="dissolve", speed="fast", duration=5, advance="auto")

    assert out == {"status": "ok", "page": 0, "updated": ["effect", "speed", "duration", "advance"]}
    page.setPropertyValue.assert_any_call("Effect", ("FadeEffect", "DISSOLVE"))
    page.setPropertyValue.assert_any_call("Speed", ("AnimationSpeed", "FAST"))
    page.setPropertyValue.assert_any_call("Duration", 5)
    page.setPropertyValue.assert_any_call("Change", 1)


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"effect": "not_a_real_effect"}, id="unknown_effect"),
        pytest.param({"speed": "supersonic"}, id="unknown_speed"),
        pytest.param({"advance": "telepathic"}, id="unknown_advance"),
        pytest.param({"effect": "dissolve", "speed": "supersonic"}, id="invalid_speed_with_effect"),
        pytest.param({"effect": "dissolve", "duration": 5, "advance": "telepathic"}, id="invalid_advance_with_effect"),
        pytest.param({"duration": "five"}, id="bad_duration_type"),
        pytest.param({"transition_duration": "long"}, id="bad_transition_duration_type"),
    ],
)
def test_set_slide_transition_invalid_args_write_nothing(kwargs):
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.transitions.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = SetSlideTransition().execute(ctx, page=0, **kwargs)

    assert out["status"] == "error"
    page.setPropertyValue.assert_not_called()


def test_set_slide_transition_enum_import_error_is_a_tool_error():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.transitions.DrawBridge") as bridge_cls, \
         patch("plugin.draw.transitions._pres_enum", side_effect=ImportError("no uno")):
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = SetSlideTransition().execute(ctx, page=0, effect="dissolve")

    assert out["status"] == "error"
    page.setPropertyValue.assert_not_called()


def test_set_slide_transition_accepts_every_effect_key():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.transitions.DrawBridge") as bridge_cls, \
         patch("plugin.draw.transitions._pres_enum", return_value="enum"):
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        for key in _FADE_EFFECTS:
            out = SetSlideTransition().execute(ctx, page=0, effect=key)
            assert out["status"] == "ok"
            assert "effect" in out["updated"]


def test_set_slide_transition_unknown_effect_never_looks_up_enum():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.transitions.DrawBridge") as bridge_cls, \
         patch("plugin.draw.transitions._pres_enum", side_effect=AssertionError("enum lookup")):
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = SetSlideTransition().execute(ctx, page=0, effect="bogus")

    assert out["status"] == "error"
    page.setPropertyValue.assert_not_called()


def test_set_slide_transition_accepts_semi_auto():
    ctx = MagicMock()
    page = MagicMock()
    with patch("plugin.draw.transitions.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = SetSlideTransition().execute(ctx, page=0, advance="semi_auto")
    assert out["status"] == "ok"
    page.setPropertyValue.assert_any_call("Change", 2)


def test_get_slide_transition_uses_ctx_active_page_index():
    ctx = MagicMock()
    ctx.active_page_index = 2
    page = MagicMock()
    with patch("plugin.draw.transitions.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 2)
        out = GetSlideTransition().execute(ctx)
    assert out["page"] == 2
    bridge_cls.return_value.resolve_page.assert_called_once_with(None, active_page_index=2)


def test_explicit_page_wins_over_ctx_active_page_index():
    ctx = MagicMock()
    ctx.active_page_index = 2
    page = MagicMock()
    with patch("plugin.draw.transitions.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = GetSlideTransition().execute(ctx, page=0)
    assert out["page"] == 0
    bridge_cls.return_value.resolve_page.assert_called_once_with(0, active_page_index=2)


def test_set_slide_layout_uses_ctx_active_page_index():
    ctx = MagicMock()
    ctx.active_page_index = 1
    page = MagicMock()
    page.Layout = 20
    with patch("plugin.draw.transitions.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 1)
        out = SetSlideLayout().execute(ctx, layout="text")
    assert out["page"] == 1
    assert page.Layout == 1
    bridge_cls.return_value.resolve_page.assert_called_once_with(None, active_page_index=1)


def test_set_slide_transition_write_failure_is_a_tool_error():
    ctx = MagicMock()
    page = MagicMock()
    page.setPropertyValue.side_effect = RuntimeError("boom")
    with patch("plugin.draw.transitions.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        out = SetSlideTransition().execute(ctx, page=0, effect="dissolve")
    assert out["status"] == "error"
    assert "Could not update slide transition" in out["message"]


def test_set_slide_transition_disposal_still_raises():
    ctx = MagicMock()
    page = MagicMock()
    page.setPropertyValue.side_effect = RuntimeError("boom")
    with patch("plugin.draw.transitions.DrawBridge") as bridge_cls, \
         patch("plugin.draw.transitions.is_disposal", return_value=True):
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        with pytest.raises(RuntimeError):
            SetSlideTransition().execute(ctx, page=0, effect="dissolve")

