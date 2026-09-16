# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Impress/Draw slide placeholder tools.

Presentation placeholders (title, subtitle, body, etc.) are shapes on the
slide with specific presentation types. These tools provide direct access
to placeholders by role rather than shape index.
"""

import logging

from plugin.framework.tool import ToolBase

log = logging.getLogger("nelson.draw")

# Presentation object types (from com.sun.star.presentation.PresentationObjectType)
# Shapes on Impress slides have a "PresObj" property or can be identified
# by checking the "IsEmptyPresentationObject" and class properties.
# In practice, the simplest approach is to iterate shapes and check
# their "PresObj" or "ClassName" property.

from plugin.draw.bridge import DrawBridge

# Specific tokens first so matching is not shape-order / substring-any.
# "Text" used to be a body pattern: "text" in "titletextshape" returned the
# title as body. "title" is also a substring of "subtitle" — SubTitle first.
_CLASS_ROLE_PRIORITY = (
    ("TitleText", "title"),
    ("SubTitle", "subtitle"),
    ("Subtitle", "subtitle"),
    ("Outliner", "body"),
    ("Outline", "body"),
    ("Title", "title"),
    ("Body", "body"),
    ("Notes", "notes"),
)


def _role_from_label(label):
    """Map a ClassName or shape Name to a role. First matching token wins."""
    if not label:
        return None
    lower = str(label).lower()
    for token, role in _CLASS_ROLE_PRIORITY:
        if token.lower() in lower:
            return role
    return None


def _shape_class_name(shape):
    try:
        if hasattr(shape, "ClassName") and shape.ClassName:
            return str(shape.ClassName)
    except Exception:
        pass
    try:
        cn = shape.getPropertyValue("ClassName")
        if cn:
            return str(cn)
    except Exception:
        pass
    return ""


def _find_placeholder(page, role):
    """Find a placeholder shape by role name.

    Tries multiple identification strategies:
    1. ClassName via the priority class→role map (TitleText→title, …)
    2. Shape Name via the same map
    3. Positional heuristic (first text shape = title, second = body)
    """
    role_lower = role.lower()

    # Strategy 1: class → role (deterministic; not substring-any / shape-order)
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        try:
            if _role_from_label(_shape_class_name(shape)) == role_lower:
                return shape, i
        except Exception:
            pass

    # Strategy 2: match by shape Name using the same map
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        try:
            name = shape.Name if hasattr(shape, "Name") else ""
            if name and _role_from_label(name) == role_lower:
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
        entry = {"index": i, "text": shape.getString()}
        try:
            if hasattr(shape, "Name") and shape.Name:
                entry["name"] = shape.Name
        except Exception:
            pass
        class_name = _shape_class_name(shape)
        if class_name:
            entry["class"] = class_name
        role = _role_from_label(class_name)
        if role:
            entry["role"] = role
        result.append(entry)
    return result


class ListPlaceholders(ToolBase):
    """List all text placeholders on a slide."""

    name = "list_placeholders"
    intent = "navigate"
    description = "List all text placeholders on a slide with their role (title, subtitle, body), text content, and index."
    parameters = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}}, "required": []}
    uno_services = ["com.sun.star.presentation.PresentationDocument"]

    def execute(self, ctx, **kwargs):
        page_idx = kwargs.get("page")
        page = DrawBridge.get_slide_for_tool(ctx.doc, page_idx)
        placeholders = _list_placeholders(page)
        return {"status": "ok", "page": page_idx, "placeholders": placeholders, "count": len(placeholders)}


class GetPlaceholderText(ToolBase):
    """Get text from a slide placeholder by role or shape index."""

    name = "get_placeholder_text"
    intent = "navigate"
    description = "Get text from a slide placeholder. Specify role ('title', 'subtitle', 'body') or index. Use list_placeholders to see available placeholders."
    parameters = {
        "type": "object",
        "properties": {
            "role": {"type": "string", "description": "Placeholder role: 'title', 'subtitle', or 'body'."},
            "index": {"type": "integer", "description": "Shape index (from list_placeholders)."},
            "page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."},
        },
        "required": [],
    }
    uno_services = ["com.sun.star.presentation.PresentationDocument"]

    def execute(self, ctx, **kwargs):
        page_idx = kwargs.get("page")
        page = DrawBridge.get_slide_for_tool(ctx.doc, page_idx)
        role = kwargs.get("role")
        shape_index = kwargs.get("index")

        if shape_index is not None:
            if shape_index < 0 or shape_index >= page.getCount():
                return self._tool_error("Shape index out of range.")
            shape = page.getByIndex(shape_index)
        elif role:
            shape, _unused = _find_placeholder(page, role)
            if shape is None:
                return self._tool_error("Placeholder '%s' not found." % role, available=_list_placeholders(page))
        else:
            return self._tool_error("Specify role or index.")

        if not hasattr(shape, "getString"):
            return self._tool_error("Shape has no text.")

        return {"status": "ok", "text": shape.getString(), "role": role, "index": shape_index}


class SetPlaceholderText(ToolBase):
    """Set text on a slide placeholder by role or shape index."""

    name = "set_placeholder_text"
    intent = "edit"
    description = "Set text on a slide placeholder. Specify role ('title', 'subtitle', 'body') or index. Use list_placeholders to see available placeholders."
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to set on the placeholder."},
            "role": {"type": "string", "description": "Placeholder role: 'title', 'subtitle', or 'body'."},
            "index": {"type": "integer", "description": "Shape index (from list_placeholders)."},
            "page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."},
        },
        "required": ["text"],
    }
    uno_services = ["com.sun.star.presentation.PresentationDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        page_idx = kwargs.get("page")
        page = DrawBridge.get_slide_for_tool(ctx.doc, page_idx)
        text = kwargs["text"]
        role = kwargs.get("role")
        shape_index = kwargs.get("index")

        if shape_index is not None:
            if shape_index < 0 or shape_index >= page.getCount():
                return self._tool_error("Shape index out of range.")
            shape = page.getByIndex(shape_index)
        elif role:
            shape, shape_index = _find_placeholder(page, role)
            if shape is None:
                return self._tool_error("Placeholder '%s' not found." % role, available=_list_placeholders(page))
        else:
            return self._tool_error("Specify role or index.")

        if not hasattr(shape, "setString"):
            return self._tool_error("Shape does not support text.")

        shape.setString(text)
        return {"status": "ok", "text": text, "role": role, "index": shape_index}
