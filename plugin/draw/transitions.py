# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Impress slide transition and layout tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.draw.base import ToolDrawSlideLayoutBase, ToolDrawSlideTransitionsBase
from plugin.draw.bridge import DrawBridge
from plugin.draw.transform_schema import AUTOLAYOUT_ID
from plugin.framework.errors import uno_read

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


# Named FadeEffect values for agent convenience.
_FADE_EFFECTS = {
    "none": "NONE",
    "fade_from_left": "FADE_FROM_LEFT",
    "fade_from_top": "FADE_FROM_TOP",
    "fade_from_right": "FADE_FROM_RIGHT",
    "fade_from_bottom": "FADE_FROM_BOTTOM",
    "fade_to_center": "FADE_TO_CENTER",
    "fade_from_center": "FADE_FROM_CENTER",
    "move_from_left": "MOVE_FROM_LEFT",
    "move_from_top": "MOVE_FROM_TOP",
    "move_from_right": "MOVE_FROM_RIGHT",
    "move_from_bottom": "MOVE_FROM_BOTTOM",
    "roll_from_left": "ROLL_FROM_LEFT",
    "roll_from_top": "ROLL_FROM_TOP",
    "roll_from_right": "ROLL_FROM_RIGHT",
    "roll_from_bottom": "ROLL_FROM_BOTTOM",
    "uncover_to_left": "UNCOVER_TO_LEFT",
    "uncover_to_top": "UNCOVER_TO_TOP",
    "uncover_to_right": "UNCOVER_TO_RIGHT",
    "uncover_to_bottom": "UNCOVER_TO_BOTTOM",
    "open_vertical": "OPEN_VERTICAL",
    "open_horizontal": "OPEN_HORIZONTAL",
    "close_vertical": "CLOSE_VERTICAL",
    "close_horizontal": "CLOSE_HORIZONTAL",
    "dissolve": "DISSOLVE",
    "random": "RANDOM",
}

_ADVANCE_MODES: dict[int, str] = {0: "on_click", 1: "auto", 2: "semi_auto"}
_SPEED_ENUM: dict[str, str] = {"slow": "SLOW", "medium": "MEDIUM", "fast": "FAST"}


# What was wrong: In live PyUNO, com.sun.star.presentation.FadeEffect and AnimationSpeed
#   are UNO enum modules/types and not top-level attributes of com.sun.star.presentation,
#   so importing FadeEffect or AnimationSpeed directly from com.sun.star.presentation raised ImportError.
# How it happened: 788b91a31 (#1996) changed the imports to `from com.sun.star.presentation import ...`.
# Why this change: Catch (ImportError, AttributeError) and construct the uno.Enum instance via
#   uno.Enum("com.sun.star.presentation....", ...) when direct attribute lookup is unavailable.
def _fade_effect(enum_name: str) -> Any:
    try:
        from com.sun.star.presentation import FadeEffect

        return getattr(FadeEffect, enum_name)
    except (ImportError, AttributeError):
        import uno

        return uno.Enum("com.sun.star.presentation.FadeEffect", enum_name)


def _speed_enum(speed_key: str) -> Any:
    try:
        from com.sun.star.presentation import AnimationSpeed

        return getattr(AnimationSpeed, _SPEED_ENUM[speed_key])
    except (ImportError, AttributeError):
        import uno

        return uno.Enum("com.sun.star.presentation.AnimationSpeed", _SPEED_ENUM[speed_key])

# Friendly name → AutoLayout constant in AUTOLAYOUT_ID.
# Impress ``page.Layout`` is the LibreOffice AutoLayout enum
# (include/xmloff/autolayout.hxx), not PowerPoint PpSlideLayout
# minus one (title_only=10, blank=11, four_objects=23, …). Those
# numbers select a different layout — title-only becomes TEXTOBJ,
# blank becomes an OLE object, four objects becomes a handout
# page. Ids are looked up from the shared table so set/get cannot
# drift from transform_engine.
_LAYOUT_AUTOLAYOUT = {
    "title": "AUTOLAYOUT_TITLE",
    "text": "AUTOLAYOUT_TITLE_CONTENT",
    "chart": "AUTOLAYOUT_CHART",
    "two_column_text": "AUTOLAYOUT_TITLE_2CONTENT",
    "text_and_chart": "AUTOLAYOUT_TEXTCHART",
    "org_chart": "AUTOLAYOUT_ORG",
    "text_and_clipart": "AUTOLAYOUT_TEXTCLIP",
    "chart_and_text": "AUTOLAYOUT_CHARTTEXT",
    "table": "AUTOLAYOUT_TAB",
    "clipart_and_text": "AUTOLAYOUT_CLIPTEXT",
    "text_and_object": "AUTOLAYOUT_TEXTOBJ",
    "object": "AUTOLAYOUT_OBJ",
    "two_column_and_object": "AUTOLAYOUT_TITLE_CONTENT_2CONTENT",
    "object_and_text": "AUTOLAYOUT_OBJTEXT",
    "object_over_text": "AUTOLAYOUT_TITLE_CONTENT_OVER_CONTENT",
    "object_and_two_column": "AUTOLAYOUT_TITLE_2CONTENT_CONTENT",
    "two_objects_over_text": "AUTOLAYOUT_TITLE_2CONTENT_OVER_CONTENT",
    "text_over_object": "AUTOLAYOUT_TEXTOVEROBJ",
    "four_objects": "AUTOLAYOUT_TITLE_4CONTENT",
    "title_only": "AUTOLAYOUT_TITLE_ONLY",
    "blank": "AUTOLAYOUT_NONE",
    "vertical_title_and_text_over_chart": "AUTOLAYOUT_VTITLE_VCONTENT_OVER_VCONTENT",
    "vertical_title_and_text": "AUTOLAYOUT_VTITLE_VCONTENT",
    "vertical_text": "AUTOLAYOUT_TITLE_VCONTENT",
    "title_two_vertical_text": "AUTOLAYOUT_TITLE_2VTEXT",
    "only_text": "AUTOLAYOUT_ONLY_TEXT",
    "six_objects": "AUTOLAYOUT_TITLE_6CONTENT",
}

# Canonical name → AutoLayout id. One name per id so get_slide_layout is stable.
_LAYOUTS = {name: AUTOLAYOUT_ID[auto] for name, auto in _LAYOUT_AUTOLAYOUT.items()}
_LAYOUT_NAMES = {lid: name for name, lid in _LAYOUTS.items()}

# Names that share a canonical layout. "none" is the empty-page hatch.
# two_objects is the two-box content layout (not the old id 27, which is a
# vertical title layout). large_object is the OLE object layout.
# text_and_media / media_and_text had no AutoLayout constant.
_LAYOUT_ALIASES = {
    "none": "blank",
    "centered_text": "only_text",
    "large_object": "object",
    "two_objects": "two_column_text",
    "object_and_two_objects": "two_column_and_object",
    "two_objects_and_object": "object_and_two_column",
}


def _canonical_layout_name(name: str) -> str:
    return _LAYOUT_ALIASES.get(name.strip().lower(), name.strip().lower())


def available_layout_names() -> list[str]:
    """Canonical names plus aliases accepted by set_slide_layout / add_slide."""
    return sorted(set(_LAYOUTS) | set(_LAYOUT_ALIASES))


def layout_id(name: str) -> int | None:
    """Return the LibreOffice AutoLayout id for a layout name, or None.

    ``none`` aliases ``blank`` (``AUTOLAYOUT_NONE``, 20) so add_slide can
    keep an empty page. Ids come from ``AUTOLAYOUT_ID``, not PpSlideLayout.
    """
    if not isinstance(name, str):
        return None
    key = _canonical_layout_name(name)
    if not key:
        return None
    return _LAYOUTS.get(key)


def apply_slide_layout(page: Any, name: str) -> str:
    """Set ``page.Layout`` from a named layout. Returns the canonical name.

    Impress instantiates placeholders synchronously on this assignment — no
    ``processEvents`` refresh. Shared by AddSlide and SetSlideLayout so the
    two cannot drift.
    """
    lid = layout_id(name)
    if lid is None:
        raise ValueError("Unknown layout: %s" % name)
    page.Layout = lid
    return _canonical_layout_name(name)


def _transition_enum_name(value: Any) -> str:
    """Lowercase a UNO enum `.value`, or `str(value)` when the object has no `.value`."""
    return value.value.lower() if hasattr(value, "value") else str(value).lower()


class GetSlideTransition(ToolDrawSlideTransitionsBase):
    """Read the current transition settings from a slide."""

    name: str | None = "get_slide_transition"
    intent: str | None = "navigate"
    description: str = "Get the transition effect, speed, duration, and advance mode for an Impress slide."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page_idx = kwargs.get("page")
        page = DrawBridge.get_slide_for_tool(ctx.doc, page_idx)

        # Bugfix: 130c136d6 dropped the per-property fallbacks while removing
        # try/except. A missing Effect/Speed/Duration/TransitionDuration/Change
        # used to return "none", "medium", 0, None, and Change 0 (advance
        # "on_click"). Direct getPropertyValue now fails the tool. uno_read
        # restores each fallback on its own and still raises on a disposed document.
        effect = uno_read(lambda: _transition_enum_name(page.getPropertyValue("Effect")), "none")
        speed = uno_read(lambda: _transition_enum_name(page.getPropertyValue("Speed")), "medium")
        duration = uno_read(lambda: page.getPropertyValue("Duration"), 0)
        transition_duration = uno_read(lambda: page.getPropertyValue("TransitionDuration"), None)
        change = uno_read(lambda: page.getPropertyValue("Change"), 0)

        return {
            "status": "ok",
            "page": page_idx,
            "effect": effect,
            "speed": speed,
            "duration": duration,
            "transition_duration": transition_duration,
            "advance": _ADVANCE_MODES.get(change, "on_click"),
        }


class SetSlideTransition(ToolDrawSlideTransitionsBase):
    """Set the transition effect on a slide."""

    name: str | None = "set_slide_transition"
    intent: str | None = "edit"
    description: str = (
        "Set the transition effect on an Impress slide. "
        "Effects: none, fade_from_left/top/right/bottom, "
        "move_from_left/top/right/bottom, dissolve, random, "
        "open_vertical/horizontal, close_vertical/horizontal, "
        "roll_from_left/top/right/bottom, "
        "uncover_to_left/top/right/bottom, fade_to_center, fade_from_center. "
        "Speed: slow, medium, fast. "
        "Advance: on_click, auto (set duration for auto-advance)."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."},
            "effect": {"type": "string", "description": ("Transition effect name (e.g. 'dissolve', 'fade_from_left', 'random', 'none'). Case-insensitive.")},
            "speed": {"type": "string", "enum": list(_SPEED_ENUM), "description": "Transition speed (default: medium)."},
            "duration": {"type": "integer", "description": "Auto-advance duration in seconds (0 = manual advance)."},
            "transition_duration": {"type": "number", "description": "Transition animation time in seconds (e.g. 1.5)."},
            "advance": {"type": "string", "enum": ["on_click", "auto"], "description": "How to advance: on_click (default) or auto."},
        },
        "required": [],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page_idx = kwargs.get("page")
        page = DrawBridge.get_slide_for_tool(ctx.doc, page_idx)
        updated = []

        # Effect
        effect_name = kwargs.get("effect")
        if effect_name is not None:
            effect_key = effect_name.strip().lower()
            try:
                _fade_effect("NONE")
            except ImportError:
                return self._tool_error("FadeEffect enum not available.")

            if effect_key in _FADE_EFFECTS:
                page.setPropertyValue("Effect", _fade_effect(_FADE_EFFECTS[effect_key]))
                updated.append("effect")
            else:
                return self._tool_error("Unknown effect: %s" % effect_name, available=sorted(_FADE_EFFECTS.keys()))

        # Speed and TransitionDuration are one LibreOffice double.
        # unopage.cxx WID_PAGE_SPEED writes setTransitionDuration (slow 3s,
        # medium 2s, fast 1s) and the getter derives speed from that double.
        # Setting both used to leave the duration and report the overwritten speed.
        speed = kwargs.get("speed")
        td = kwargs.get("transition_duration")
        if td is not None:
            try:
                page.setPropertyValue("TransitionDuration", float(td))
                updated.append("transition_duration")
            except Exception as exc:
                from plugin.framework.errors import is_disposed_exception

                if is_disposed_exception(exc):
                    raise
                return self._tool_error("Could not set transition_duration: %s" % exc)
        elif speed is not None:
            speed_key = str(speed).lower()
            if speed_key not in _SPEED_ENUM:
                return self._tool_error("Unknown speed: %s" % speed, available=sorted(_SPEED_ENUM.keys()))
            page.setPropertyValue("Speed", _speed_enum(speed_key))
            updated.append("speed")

        # Auto-advance duration
        duration = kwargs.get("duration")
        if duration is not None:
            page.setPropertyValue("Duration", int(duration))
            updated.append("duration")

        # Advance mode
        advance = kwargs.get("advance")
        if advance is not None:
            if advance not in ("on_click", "auto"):
                return self._tool_error("Unknown advance: %s" % advance, available=["on_click", "auto"])
            change = 0 if advance == "on_click" else 1
            page.setPropertyValue("Change", change)
            updated.append("advance")

        return {"status": "ok", "page": page_idx, "updated": updated}


class GetSlideLayout(ToolDrawSlideLayoutBase):
    """Get the layout of an Impress slide."""

    name: str | None = "get_slide_layout"
    intent: str | None = "navigate"
    description: str = "Get the layout type of an Impress slide. Returns the layout ID and a human-readable name."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page_idx = kwargs.get("page")
        page = DrawBridge.get_slide_for_tool(ctx.doc, page_idx)
        current_id = page.Layout
        layout_name = _LAYOUT_NAMES.get(current_id, "unknown_%d" % current_id)
        return {"status": "ok", "page": page_idx, "layout_id": current_id, "layout_name": layout_name, "available_layouts": available_layout_names()}


class SetSlideLayout(ToolDrawSlideLayoutBase):
    """Set the layout of an Impress slide."""

    name: str | None = "set_slide_layout"
    intent: str | None = "edit"
    description: str = (
        "Set the layout of an Impress slide. Layouts: blank, title, text, title_only, two_column_text, text_and_chart, chart, text_and_object, object, text_and_clipart, large_object, four_objects, vertical_text, two_objects, and more. Use get_slide_layout to see all available layout names."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}, "layout": {"type": "string", "description": "Layout name (e.g. 'blank', 'title', 'text_and_object')."}}, "required": ["layout"]}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        layout_name = kwargs["layout"].strip().lower()
        if layout_id(layout_name) is None:
            return self._tool_error("Unknown layout: %s" % layout_name, available=available_layout_names())
        page_idx = kwargs.get("page")
        page = DrawBridge.get_slide_for_tool(ctx.doc, page_idx)
        applied = apply_slide_layout(page, layout_name)
        return {"status": "ok", "page": page_idx, "layout": applied}
