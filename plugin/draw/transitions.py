# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Impress slide transition and layout tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.draw.base import ToolDrawSlideLayoutBase, ToolDrawSlideTransitionsBase
from plugin.draw.bridge import DrawBridge
from plugin.draw.layouts import available_layout_names, layout_id, layout_name
from plugin.framework.errors import available_miss_details, is_disposal, uno_read

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
    return layout_name(lid)


def _transition_enum_name(value: Any) -> str:
    """Lowercase a UNO enum `.value`, or `str(value)` when the object has no `.value`."""
    return value.value.lower() if hasattr(value, "value") else str(value).lower()


class GetSlideTransition(ToolDrawSlideTransitionsBase):
    """Read the current transition settings from a slide."""

    name: str | None = "slide_transition_get"
    description: str = "Get the transition effect, speed, duration, and advance mode for an Impress slide."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page, page_idx = DrawBridge(ctx.doc).resolve_page(kwargs.get("page"), active_page_index=ctx.active_page_index)

        # Missing Effect/Speed/Duration/TransitionDuration/Change properties get per-field
        # defaults via uno_read ("none", "medium", 0, None, on_click). Disposal still propagates.
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

    name: str | None = "slide_transition_set"
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
        page, page_idx = DrawBridge(ctx.doc).resolve_page(kwargs.get("page"), active_page_index=ctx.active_page_index)
        # Values are plain data until the write loop. _pres_enum stays inside
        # the try so ImportError is still "Could not update slide transition".
        ops: list[tuple[str, str, Any]] = []

        effect = kwargs.get("effect")
        if effect is not None:
            effect_key = str(effect).strip().lower()
            if effect_key not in _FADE_EFFECTS:
                avail = sorted(_FADE_EFFECTS)
                return self._tool_error(
                    "Unknown effect: %s" % effect,
                    available=avail,
                    **available_miss_details(str(effect), avail),
                )
            ops.append(("effect", "Effect", effect_key.upper()))

        # Speed and TransitionDuration are one LibreOffice double.
        # unopage.cxx WID_PAGE_SPEED writes setTransitionDuration (slow 3s,
        # medium 2s, fast 1s) and the getter derives speed from that double.
        # LibreOffice maps Speed and TransitionDuration to one double; setting both overwrites speed.
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
                avail = sorted(_SPEED_ENUM)
                return self._tool_error(
                    "Unknown speed: %s" % speed,
                    available=avail,
                    **available_miss_details(str(speed), avail),
                )
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
                avail = sorted(_ADVANCE_CHANGES)
                return self._tool_error(
                    "Unknown advance: %s" % advance,
                    available=avail,
                    **available_miss_details(str(advance), avail),
                )
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
            if is_disposal(exc):
                raise
            return self._tool_error("Could not update slide transition: %s" % exc)

        return {"status": "ok", "page": page_idx, "updated": updated}


class GetSlideLayout(ToolDrawSlideLayoutBase):
    """Get the layout of an Impress slide."""

    name: str | None = "slide_layout_get"
    description: str = "Get the layout type of an Impress slide. Returns the layout ID and a human-readable name."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page, page_idx = DrawBridge(ctx.doc).resolve_page(kwargs.get("page"), active_page_index=ctx.active_page_index)
        current_id = page.Layout
        layout_name_str = layout_name(current_id)
        return {"status": "ok", "page": page_idx, "layout_id": current_id, "layout_name": layout_name_str, "available_layouts": available_layout_names()}


class SetSlideLayout(ToolDrawSlideLayoutBase):
    """Set the layout of an Impress slide."""

    name: str | None = "slide_layout_set"
    description: str = (
        "Set the layout of an Impress slide. Layouts: blank, title, text, title_only, two_column_text, text_and_chart, chart, text_and_object, object, text_and_clipart, large_object, four_objects, vertical_text, two_objects, and more. Use slide_layout_get to see all available layout names."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}, "layout": {"type": "string", "description": "Layout name (e.g. 'blank', 'title', 'text_and_object')."}}, "required": ["layout"]}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        layout_name = kwargs["layout"].strip().lower()
        if layout_id(layout_name) is None:
            avail = available_layout_names()
            return self._tool_error(
                "Unknown layout: %s" % layout_name,
                available=avail,
                **available_miss_details(str(layout_name), avail),
            )
        page, page_idx = DrawBridge(ctx.doc).resolve_page(kwargs.get("page"), active_page_index=ctx.active_page_index)
        applied = apply_slide_layout(page, layout_name)
        return {"status": "ok", "page": page_idx, "layout": applied}
