# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Impress slide transition and layout tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.draw.base import ToolDrawSlideLayoutBase, ToolDrawSlideTransitionsBase
from plugin.draw.bridge import DrawBridge, resolve_page_and_bridge
from plugin.draw.transform_schema import AUTOLAYOUT_ID
from plugin.framework.errors import is_real_disposal, uno_read

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


# Named FadeEffect values for agent convenience. The UNO member is key.upper().
_FADE_EFFECTS = frozenset({
    "none",
    "fade_from_left", "fade_from_top", "fade_from_right", "fade_from_bottom",
    "fade_to_center", "fade_from_center",
    "move_from_left", "move_from_top", "move_from_right", "move_from_bottom",
    "roll_from_left", "roll_from_top", "roll_from_right", "roll_from_bottom",
    "uncover_to_left", "uncover_to_top", "uncover_to_right", "uncover_to_bottom",
    "open_vertical", "open_horizontal", "close_vertical", "close_horizontal",
    "dissolve", "random",
})

_ADVANCE_MODES: dict[int, str] = {0: "on_click", 1: "auto", 2: "semi_auto"}
_ADVANCE_CHANGES: dict[str, int] = {name: change for change, name in _ADVANCE_MODES.items()}
_SPEED_ENUM: tuple[str, ...] = ("slow", "medium", "fast")


def _pres_enum(type_name: str, member: str) -> Any:
    """One com.sun.star.presentation enum member."""
    # uno.Enum works in every PyUNO; importing FadeEffect/AnimationSpeed from
    # com.sun.star.presentation broke in 788b91a31 (#1996).
    import uno

    return uno.Enum("com.sun.star.presentation.%s" % type_name, member)

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
    return _LAYOUTS.get(_canonical_layout_name(name))


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


def _slide_for_tool(ctx: ToolContext, kwargs: dict[str, Any]) -> tuple[Any, int]:
    """Explicit page, else ctx.active_page_index, else the controller."""
    bridge, page_idx = resolve_page_and_bridge(ctx, kwargs)
    return DrawBridge.get_slide_for_tool(bridge.doc, page_idx), page_idx


class GetSlideTransition(ToolDrawSlideTransitionsBase):
    """Read the current transition settings from a slide."""

    name: str | None = "get_slide_transition"
    intent: str | None = "navigate"
    description: str = "Get the transition effect, speed, duration, and advance mode for an Impress slide."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page, page_idx = _slide_for_tool(ctx, kwargs)

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
        "Advance: on_click, auto, semi_auto (set duration for auto-advance)."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."},
            "effect": {"type": "string", "description": ("Transition effect name (e.g. 'dissolve', 'fade_from_left', 'random', 'none'). Case-insensitive.")},
            "speed": {"type": "string", "enum": list(_SPEED_ENUM), "description": "Transition speed (default: medium)."},
            "duration": {"type": "integer", "description": "Auto-advance duration in seconds (0 = manual advance)."},
            "transition_duration": {"type": "number", "description": "Transition animation time in seconds (e.g. 1.5)."},
            "advance": {"type": "string", "enum": list(_ADVANCE_CHANGES), "description": "How to advance: on_click (default), auto, or semi_auto (object effects run automatically, click still changes the page)."},
        },
        "required": [],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page, page_idx = _slide_for_tool(ctx, kwargs)
        # Values are plain data until the write loop. _pres_enum stays inside
        # the try so ImportError is still "Could not update slide transition".
        ops: list[tuple[str, str, Any]] = []

        effect = kwargs.get("effect")
        if effect is not None:
            effect_key = str(effect).strip().lower()
            if effect_key not in _FADE_EFFECTS:
                return self._tool_error("Unknown effect: %s" % effect, available=sorted(_FADE_EFFECTS))
            ops.append(("effect", "Effect", effect_key.upper()))

        # Speed and TransitionDuration are one LibreOffice double.
        # unopage.cxx WID_PAGE_SPEED writes setTransitionDuration (slow 3s,
        # medium 2s, fast 1s) and the getter derives speed from that double.
        # Setting both used to leave the duration and report the overwritten speed.
        td = kwargs.get("transition_duration")
        speed = kwargs.get("speed")
        if td is not None:
            try:
                ops.append(("transition_duration", "TransitionDuration", float(td)))
            except (TypeError, ValueError):
                return self._tool_error("Invalid transition_duration: %r" % (td,))
        elif speed is not None:
            speed_key = str(speed).lower()
            if speed_key not in _SPEED_ENUM:
                return self._tool_error("Unknown speed: %s" % speed, available=sorted(_SPEED_ENUM))
            ops.append(("speed", "Speed", speed_key.upper()))

        duration = kwargs.get("duration")
        if duration is not None:
            try:
                ops.append(("duration", "Duration", int(duration)))
            except (TypeError, ValueError):
                return self._tool_error("Invalid duration: %r" % (duration,))

        advance = kwargs.get("advance")
        if advance is not None:
            change = _ADVANCE_CHANGES.get(advance)
            if change is None:
                return self._tool_error("Unknown advance: %s" % advance, available=sorted(_ADVANCE_CHANGES))
            ops.append(("advance", "Change", change))

        updated: list[str] = []
        try:
            for label, prop, val in ops:
                if prop == "Effect":
                    val = _pres_enum("FadeEffect", val)
                elif prop == "Speed":
                    val = _pres_enum("AnimationSpeed", val)
                page.setPropertyValue(prop, val)
                updated.append(label)
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            return self._tool_error("Could not update slide transition: %s" % exc)

        return {"status": "ok", "page": page_idx, "updated": updated}


class GetSlideLayout(ToolDrawSlideLayoutBase):
    """Get the layout of an Impress slide."""

    name: str | None = "get_slide_layout"
    intent: str | None = "navigate"
    description: str = "Get the layout type of an Impress slide. Returns the layout ID and a human-readable name."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page, page_idx = _slide_for_tool(ctx, kwargs)
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
        page, page_idx = _slide_for_tool(ctx, kwargs)
        applied = apply_slide_layout(page, layout_name)
        return {"status": "ok", "page": page_idx, "layout": applied}
