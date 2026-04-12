# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Impress slide transition and layout tools."""

from plugin.framework.errors import ToolExecutionError
import logging

from plugin.framework.tool_base import ToolBase

log = logging.getLogger("nelson.draw")


def _get_slide(doc, page_index=None):
    """Resolve a slide by index or active."""
    pages = doc.getDrawPages()
    if page_index is not None:
        if page_index < 0 or page_index >= pages.getCount():
            raise ToolExecutionError("Page index %d out of range." % page_index)
        return pages.getByIndex(page_index)
    controller = doc.getCurrentController()
    if hasattr(controller, "getCurrentPage"):
        return controller.getCurrentPage()
    return pages.getByIndex(0)


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

# Layout name → short value mapping (from PpSlideLayout).
_LAYOUTS = {
    "title": 0,
    "text": 1,
    "two_column_text": 2,
    "table": 3,
    "text_and_chart": 4,
    "chart_and_text": 5,
    "org_chart": 6,
    "chart": 7,
    "text_and_clipart": 8,
    "clipart_and_text": 9,
    "title_only": 10,
    "blank": 11,
    "text_and_object": 12,
    "object_and_text": 13,
    "large_object": 14,
    "object": 15,
    "text_and_media": 16,
    "media_and_text": 17,
    "object_over_text": 18,
    "text_over_object": 19,
    "two_column_and_object": 20,
    "object_and_two_column": 21,
    "two_objects_over_text": 22,
    "four_objects": 23,
    "vertical_text": 24,
    "vertical_title_and_text": 25,
    "vertical_title_and_text_over_chart": 26,
    "two_objects": 27,
    "object_and_two_objects": 28,
    "two_objects_and_object": 29,
}

# Reverse lookup for display.
_LAYOUT_NAMES = {v: k for k, v in _LAYOUTS.items()}


class GetSlideTransition(ToolBase):
    """Read the current transition settings from a slide."""

    name = "get_slide_transition"
    intent = "navigate"
    description = (
        "Get the transition effect, speed, duration, and advance mode "
        "for an Impress slide."
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "0-based slide index (active slide if omitted).",
            },
        },
        "required": [],
    }
    uno_services = ["com.sun.star.presentation.PresentationDocument"]

    def execute(self, ctx, **kwargs):
        page = _get_slide(ctx.doc, kwargs.get("page_index"))

        # FadeEffect
        effect = "none"
        try:
            fe = page.getPropertyValue("Effect")
            effect = fe.value.lower() if hasattr(fe, "value") else str(fe).lower()
        except Exception:
            pass

        # Speed
        speed = "medium"
        try:
            sp = page.getPropertyValue("Speed")
            speed = sp.value.lower() if hasattr(sp, "value") else str(sp).lower()
        except Exception:
            pass

        # Duration (auto-advance)
        duration = 0
        try:
            duration = page.getPropertyValue("Duration")
        except Exception:
            pass

        # TransitionDuration (transition animation time)
        transition_duration = None
        try:
            transition_duration = page.getPropertyValue("TransitionDuration")
        except Exception:
            pass

        # Change mode: 0=click, 1=auto, 2=semi-auto
        change = 0
        try:
            change = page.getPropertyValue("Change")
        except Exception:
            pass

        return {
            "status": "ok",
            "page_index": kwargs.get("page_index"),
            "effect": effect,
            "speed": speed,
            "duration": duration,
            "transition_duration": transition_duration,
            "advance": {0: "on_click", 1: "auto", 2: "semi_auto"}.get(change, "on_click"),
        }
class SetSlideTransition(ToolBase):
    """Set the transition effect on a slide."""

    name = "set_slide_transition"
    intent = "edit"
    description = (
        "Set the transition effect on an Impress slide. "
        "Effects: none, fade_from_left/top/right/bottom, "
        "move_from_left/top/right/bottom, dissolve, random, "
        "open_vertical/horizontal, close_vertical/horizontal, "
        "roll_from_left/top/right/bottom, "
        "uncover_to_left/top/right/bottom, fade_to_center, fade_from_center. "
        "Speed: slow, medium, fast. "
        "Advance: on_click, auto (set duration for auto-advance)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "0-based slide index (active slide if omitted).",
            },
            "effect": {
                "type": "string",
                "description": (
                    "Transition effect name (e.g. 'dissolve', 'fade_from_left', "
                    "'random', 'none'). Case-insensitive."
                ),
            },
            "speed": {
                "type": "string",
                "enum": ["slow", "medium", "fast"],
                "description": "Transition speed (default: medium).",
            },
            "duration": {
                "type": "integer",
                "description": "Auto-advance duration in seconds (0 = manual advance).",
            },
            "transition_duration": {
                "type": "number",
                "description": "Transition animation time in seconds (e.g. 1.5).",
            },
            "advance": {
                "type": "string",
                "enum": ["on_click", "auto"],
                "description": "How to advance: on_click (default) or auto.",
            },
        },
        "required": [],
    }
    uno_services = ["com.sun.star.presentation.PresentationDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        page = _get_slide(ctx.doc, kwargs.get("page_index"))
        updated = []

        # Effect
        effect_name = kwargs.get("effect")
        if effect_name is not None:
            effect_key = effect_name.strip().lower()
            uno_name = _FADE_EFFECTS.get(effect_key, effect_key.upper())
            try:
                from com.sun.star.presentation.FadeEffect import (
                    NONE, FADE_FROM_LEFT, FADE_FROM_TOP, FADE_FROM_RIGHT,
                    FADE_FROM_BOTTOM, FADE_TO_CENTER, FADE_FROM_CENTER,
                    MOVE_FROM_LEFT, MOVE_FROM_TOP, MOVE_FROM_RIGHT,
                    MOVE_FROM_BOTTOM, ROLL_FROM_LEFT, ROLL_FROM_TOP,
                    ROLL_FROM_RIGHT, ROLL_FROM_BOTTOM,
                    UNCOVER_TO_LEFT, UNCOVER_TO_TOP, UNCOVER_TO_RIGHT,
                    UNCOVER_TO_BOTTOM, OPEN_VERTICAL, OPEN_HORIZONTAL,
                    CLOSE_VERTICAL, CLOSE_HORIZONTAL, DISSOLVE, RANDOM,
                )
                effects_map = {
                    "NONE": NONE,
                    "FADE_FROM_LEFT": FADE_FROM_LEFT,
                    "FADE_FROM_TOP": FADE_FROM_TOP,
                    "FADE_FROM_RIGHT": FADE_FROM_RIGHT,
                    "FADE_FROM_BOTTOM": FADE_FROM_BOTTOM,
                    "FADE_TO_CENTER": FADE_TO_CENTER,
                    "FADE_FROM_CENTER": FADE_FROM_CENTER,
                    "MOVE_FROM_LEFT": MOVE_FROM_LEFT,
                    "MOVE_FROM_TOP": MOVE_FROM_TOP,
                    "MOVE_FROM_RIGHT": MOVE_FROM_RIGHT,
                    "MOVE_FROM_BOTTOM": MOVE_FROM_BOTTOM,
                    "ROLL_FROM_LEFT": ROLL_FROM_LEFT,
                    "ROLL_FROM_TOP": ROLL_FROM_TOP,
                    "ROLL_FROM_RIGHT": ROLL_FROM_RIGHT,
                    "ROLL_FROM_BOTTOM": ROLL_FROM_BOTTOM,
                    "UNCOVER_TO_LEFT": UNCOVER_TO_LEFT,
                    "UNCOVER_TO_TOP": UNCOVER_TO_TOP,
                    "UNCOVER_TO_RIGHT": UNCOVER_TO_RIGHT,
                    "UNCOVER_TO_BOTTOM": UNCOVER_TO_BOTTOM,
                    "OPEN_VERTICAL": OPEN_VERTICAL,
                    "OPEN_HORIZONTAL": OPEN_HORIZONTAL,
                    "CLOSE_VERTICAL": CLOSE_VERTICAL,
                    "CLOSE_HORIZONTAL": CLOSE_HORIZONTAL,
                    "DISSOLVE": DISSOLVE,
                    "RANDOM": RANDOM,
                }
                if uno_name in effects_map:
                    page.setPropertyValue("Effect", effects_map[uno_name])
                    updated.append("effect")
                else:
                    return self._tool_error(
                        "Unknown effect: %s" % effect_name,
                        available=sorted(_FADE_EFFECTS.keys()),
                    )
            except ImportError:
                return self._tool_error("FadeEffect enum not available.")

        # Speed
        speed = kwargs.get("speed")
        if speed is not None:
            from com.sun.star.presentation.AnimationSpeed import (
                SLOW, MEDIUM, FAST,
            )
            speed_map = {"slow": SLOW, "medium": MEDIUM, "fast": FAST}
            if speed.lower() in speed_map:
                page.setPropertyValue("Speed", speed_map[speed.lower()])
                updated.append("speed")

        # Transition animation duration
        td = kwargs.get("transition_duration")
        if td is not None:
            try:
                page.setPropertyValue("TransitionDuration", float(td))
                updated.append("transition_duration")
            except Exception:
                pass

        # Auto-advance duration
        duration = kwargs.get("duration")
        if duration is not None:
            page.setPropertyValue("Duration", int(duration))
            updated.append("duration")

        # Advance mode
        advance = kwargs.get("advance")
        if advance is not None:
            change = 0 if advance == "on_click" else 1
            page.setPropertyValue("Change", change)
            updated.append("advance")

        return {
            "status": "ok",
            "page_index": kwargs.get("page_index"),
            "updated": updated,
        }
class GetSlideLayout(ToolBase):
    """Get the layout of an Impress slide."""

    name = "get_slide_layout"
    intent = "navigate"
    description = (
        "Get the layout type of an Impress slide. "
        "Returns the layout ID and a human-readable name."
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "0-based slide index (active slide if omitted).",
            },
        },
        "required": [],
    }
    uno_services = ["com.sun.star.presentation.PresentationDocument"]

    def execute(self, ctx, **kwargs):
        page = _get_slide(ctx.doc, kwargs.get("page_index"))
        layout_id = page.Layout
        layout_name = _LAYOUT_NAMES.get(layout_id, "unknown_%d" % layout_id)
        return {
            "status": "ok",
            "page_index": kwargs.get("page_index"),
            "layout_id": layout_id,
            "layout_name": layout_name,
            "available_layouts": sorted(_LAYOUTS.keys()),
        }
class SetSlideLayout(ToolBase):
    """Set the layout of an Impress slide."""

    name = "set_slide_layout"
    intent = "edit"
    description = (
        "Set the layout of an Impress slide. "
        "Layouts: blank, title, text, title_only, two_column_text, "
        "text_and_chart, chart, text_and_object, object, "
        "text_and_clipart, large_object, four_objects, "
        "vertical_text, two_objects, and more. "
        "Use get_slide_layout to see all available layout names."
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "0-based slide index (active slide if omitted).",
            },
            "layout": {
                "type": "string",
                "description": "Layout name (e.g. 'blank', 'title', 'text_and_object').",
            },
        },
        "required": ["layout"],
    }
    uno_services = ["com.sun.star.presentation.PresentationDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        layout_name = kwargs.get("layout", "").strip().lower()
        if layout_name not in _LAYOUTS:
            return self._tool_error(
                "Unknown layout: %s" % layout_name,
                available=sorted(_LAYOUTS.keys()),
            )
        page = _get_slide(ctx.doc, kwargs.get("page_index"))
        page.Layout = _LAYOUTS[layout_name]
        return {
            "status": "ok",
            "page_index": kwargs.get("page_index"),
            "layout": layout_name,
        }
