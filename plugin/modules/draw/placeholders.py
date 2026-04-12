# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Impress/Draw slide placeholder tools.

Presentation placeholders (title, subtitle, body, etc.) are shapes on the
slide with specific presentation types. These tools provide direct access
to placeholders by role rather than shape index.
"""

from plugin.framework.errors import ToolExecutionError
import logging

from plugin.framework.tool_base import ToolBase

log = logging.getLogger("nelson.draw")

# Presentation object types (from com.sun.star.presentation.PresentationObjectType)
# Shapes on Impress slides have a "PresObj" property or can be identified
# by checking the "IsEmptyPresentationObject" and class properties.
# In practice, the simplest approach is to iterate shapes and check
# their "PresObj" or "ClassName" property.

_PLACEHOLDER_ROLES = {
    "title": ["Title", "TitleText"],
    "subtitle": ["SubTitle", "Subtitle"],
    "body": ["Outline", "Text", "Body"],
    "notes": ["Notes"],
}


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


def _find_placeholder(page, role):
    """Find a placeholder shape by role name.

    Tries multiple identification strategies:
    1. Check ClassName property (Impress presentation objects)
    2. Check shape name patterns
    3. Fall back to positional heuristic (first text shape = title, etc.)
    """
    role_lower = role.lower()
    candidates = _PLACEHOLDER_ROLES.get(role_lower, [role])

    # Strategy 1: match by ClassName (presentation object type)
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        try:
            class_name = ""
            if hasattr(shape, "ClassName"):
                class_name = shape.ClassName
            elif hasattr(shape, "getShapeType"):
                # Check if it's a presentation shape
                try:
                    class_name = shape.getPropertyValue("ClassName")
                except Exception:
                    pass
            if class_name:
                for cand in candidates:
                    if cand.lower() in class_name.lower():
                        return shape, i
        except Exception:
            pass

    # Strategy 2: match by shape Name
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        try:
            name = shape.Name if hasattr(shape, "Name") else ""
            if name:
                for cand in candidates:
                    if cand.lower() in name.lower():
                        return shape, i
        except Exception:
            pass

    # Strategy 3: positional heuristic for common roles
    text_shapes = []
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        if hasattr(shape, "getString"):
            text_shapes.append((shape, i))

    if role_lower == "title" and len(text_shapes) >= 1:
        return text_shapes[0]
    if role_lower in ("subtitle", "body") and len(text_shapes) >= 2:
        return text_shapes[1]

    return None, None


def _list_placeholders(page):
    """List all text-capable shapes on a page with role detection."""
    result = []
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        if not hasattr(shape, "getString"):
            continue
        entry = {
            "shape_index": i,
            "text": shape.getString(),
        }
        try:
            if hasattr(shape, "Name") and shape.Name:
                entry["name"] = shape.Name
        except Exception:
            pass
        try:
            if hasattr(shape, "ClassName") and shape.ClassName:
                entry["class"] = shape.ClassName
        except Exception:
            try:
                cn = shape.getPropertyValue("ClassName")
                if cn:
                    entry["class"] = cn
            except Exception:
                pass
        # Detect role from class
        cls = str(entry.get("class", "")).lower()
        for role, patterns in _PLACEHOLDER_ROLES.items():
            for p in patterns:
                if p.lower() in cls:
                    entry["role"] = role
                    break
            if "role" in entry:
                break
        result.append(entry)
    return result


class ListPlaceholders(ToolBase):
    """List all text placeholders on a slide."""

    name = "list_placeholders"
    intent = "navigate"
    description = (
        "List all text placeholders on a slide with their role "
        "(title, subtitle, body), text content, and shape index."
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
    uno_services = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]

    def execute(self, ctx, **kwargs):
        page = _get_slide(ctx.doc, kwargs.get("page_index"))
        placeholders = _list_placeholders(page)
        return {
            "status": "ok",
            "page_index": kwargs.get("page_index"),
            "placeholders": placeholders,
            "count": len(placeholders),
        }
class GetPlaceholderText(ToolBase):
    """Get text from a slide placeholder by role or shape index."""

    name = "get_placeholder_text"
    intent = "navigate"
    description = (
        "Get text from a slide placeholder. "
        "Specify role ('title', 'subtitle', 'body') or shape_index. "
        "Use list_placeholders to see available placeholders."
    )
    parameters = {
        "type": "object",
        "properties": {
            "role": {
                "type": "string",
                "description": "Placeholder role: 'title', 'subtitle', or 'body'.",
            },
            "shape_index": {
                "type": "integer",
                "description": "Shape index (from list_placeholders).",
            },
            "page_index": {
                "type": "integer",
                "description": "0-based slide index (active slide if omitted).",
            },
        },
        "required": [],
    }
    uno_services = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]

    def execute(self, ctx, **kwargs):
        page = _get_slide(ctx.doc, kwargs.get("page_index"))
        role = kwargs.get("role")
        shape_index = kwargs.get("shape_index")

        if shape_index is not None:
            if shape_index < 0 or shape_index >= page.getCount():
                return self._tool_error("Shape index out of range.")
            shape = page.getByIndex(shape_index)
        elif role:
            shape, _ = _find_placeholder(page, role)
            if shape is None:
                return self._tool_error(
                    "Placeholder '%s' not found." % role,
                    available=_list_placeholders(page),
                )
        else:
            return self._tool_error("Specify role or shape_index.")

        if not hasattr(shape, "getString"):
            return self._tool_error("Shape has no text.")

        return {
            "status": "ok",
            "text": shape.getString(),
            "role": role,
            "shape_index": shape_index,
        }
class SetPlaceholderText(ToolBase):
    """Set text on a slide placeholder by role or shape index."""

    name = "set_placeholder_text"
    intent = "edit"
    description = (
        "Set text on a slide placeholder. "
        "Specify role ('title', 'subtitle', 'body') or shape_index. "
        "Use list_placeholders to see available placeholders."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Text to set on the placeholder.",
            },
            "role": {
                "type": "string",
                "description": "Placeholder role: 'title', 'subtitle', or 'body'.",
            },
            "shape_index": {
                "type": "integer",
                "description": "Shape index (from list_placeholders).",
            },
            "page_index": {
                "type": "integer",
                "description": "0-based slide index (active slide if omitted).",
            },
        },
        "required": ["text"],
    }
    uno_services = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        page = _get_slide(ctx.doc, kwargs.get("page_index"))
        text = kwargs["text"]
        role = kwargs.get("role")
        shape_index = kwargs.get("shape_index")

        if shape_index is not None:
            if shape_index < 0 or shape_index >= page.getCount():
                return self._tool_error("Shape index out of range.")
            shape = page.getByIndex(shape_index)
        elif role:
            shape, shape_index = _find_placeholder(page, role)
            if shape is None:
                return self._tool_error(
                    "Placeholder '%s' not found." % role,
                    available=_list_placeholders(page),
                )
        else:
            return self._tool_error("Specify role or shape_index.")

        if not hasattr(shape, "setString"):
            return self._tool_error("Shape does not support text.")

        shape.setString(text)
        return {
            "status": "ok",
            "text": text,
            "role": role,
            "shape_index": shape_index,
        }
