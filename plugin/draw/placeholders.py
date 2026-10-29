# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Impress/Draw slide placeholder tools.

Presentation placeholders (title, subtitle, body, etc.) are shapes on the
slide with specific presentation types. These tools provide direct access
to placeholders by role rather than shape index.
"""

from __future__ import annotations

from typing import Any

from plugin.draw.bridge import DrawBridge
from plugin.framework.errors import uno_probe
from plugin.framework.tool import ToolBase, ToolContext

# Specific tokens first so matching is not shape-order / substring-any.
# Name tags, not class names. "title" is also a substring of "subtitle" — subtitle first.
_ROLE_PRIORITY = (
    ("titletext", "title"),
    ("subtitle", "subtitle"),
    ("outliner", "body"),
    ("outline", "body"),
    ("title", "title"),
    ("body", "body"),
    ("notes", "notes"),
)

# (index, shape, short service type, role from type, Name, has getString)
_ScanRow = tuple[int, Any, str, str, str, bool]


def _role_from_label(label: Any) -> str | None:
    """Map a shape Name tag to a role. First matching token wins."""
    if not label:
        return None
    lower = str(label).lower()
    for token, role in _ROLE_PRIORITY:
        if token in lower:
            return role
    return None


def _shape_type(shape: Any) -> str:
    """Short UNO service name, e.g. ``TitleTextShape``."""
    raw = uno_probe(lambda: shape.getShapeType(), "") or uno_probe(lambda: getattr(shape, "ClassName", ""), "")
    return str(raw).rsplit(".", 1)[-1] if raw else ""


def _scan_shapes(page: Any) -> list[_ScanRow]:
    """Read the page once. A broken shape is skipped; disposal still raises."""
    out = []
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        name = uno_probe(lambda: str(shape.Name), "") or ""
        shape_type = _shape_type(shape)
        role = _role_from_label(shape_type) or ""
        has_text = uno_probe(lambda: hasattr(shape, "getString"), False)
        out.append((i, shape, shape_type, role, name, has_text))
    return out


def _find_placeholder(page: Any, role: str, scan: list[_ScanRow] | None = None) -> tuple[Any | None, int | None]:
    """Find a placeholder shape by role name.

    Tries multiple identification strategies:
    1. shape-type role from _role_from_label
    2. shape Name through the same table. Never by position.
    """
    role_lower = role.lower()
    if scan is None:
        scan = _scan_shapes(page)

    # Strategy 1: role taken from the shape type
    for i, shape, _shape_type, type_role, _name, _has_text in scan:
        if type_role == role_lower:
            return shape, i

    # Strategy 2: shape Name via _role_from_label
    for i, shape, _shape_type, _type_role, name, _has_text in scan:
        if name and _role_from_label(name) == role_lower:
            return shape, i

    return None, None


def _list_placeholders(page: Any, scan: list[_ScanRow] | None = None) -> list[dict[str, Any]]:
    """List all text-capable shapes on a page with role detection."""
    if scan is None:
        scan = _scan_shapes(page)
    result = []
    for i, shape, class_name, type_role, name, has_text in scan:
        if not has_text:
            continue
        text = uno_probe(lambda: shape.getString(), None)
        if text is None:
            continue
        entry: dict[str, Any] = {"index": i, "text": text}
        if name:
            entry["name"] = name
        if class_name:
            entry["class"] = class_name
        role = type_role or _role_from_label(name)
        if role:
            entry["role"] = role
        result.append(entry)
    return result


# C1: available=[] is truthful (no presentation placeholders yet) but mercury
# retried the same role call. Hint + suggest_layout points at delegate
# slide_layouts — set_slide_layout is specialized and not on the main tool list.
_EMPTY_PLACEHOLDER_HINT = "Slide may lack a text layout. Delegate domain=slide_layouts with layout='text', then list_placeholders."


def _role_miss_error_kwargs(page: Any, scan: list[_ScanRow] | None = None) -> dict[str, Any]:
    """Details for set_placeholder_text when role lookup fails."""
    if scan is None:
        scan = _scan_shapes(page)
    available = _list_placeholders(page, scan)
    extra: dict[str, Any] = {"available": available}
    if available:
        return extra
    extra["hint"] = _EMPTY_PLACEHOLDER_HINT
    extra["suggest_layout"] = "text"
    extra["shape_text_count"] = sum(1 for _, _, _, _, _, has_text in scan if has_text)
    fallback: list[dict[str, Any]] = []
    for i, _shape, class_name, _type_role, name, has_text in scan:
        if not has_text and (class_name or name):
            item: dict[str, Any] = {"index": i}
            if class_name:
                item["class"] = class_name
            if name:
                item["name"] = name
            fallback.append(item)
    if fallback:
        extra["fallback_indices"] = fallback
    return extra


def _locate(page: Any, role: str | None, index: int | None, scan: list[_ScanRow] | None = None) -> tuple[Any | None, int | None, str | None]:
    if index is not None:
        if index < 0 or index >= page.getCount():
            return None, None, "Shape index out of range."
        return page.getByIndex(index), index, None
    if role:
        shape, shape_index = _find_placeholder(page, role, scan)
        if shape is None:
            return None, None, "not_found"
        return shape, shape_index, None
    return None, None, "Specify role or index."


class ListPlaceholders(ToolBase):
    """List all text placeholders on a slide."""

    name: str | None = "list_placeholders"
    intent: str | None = "navigate"
    description: str = "List all text placeholders on a slide with their role (title, subtitle, body), text content, and index. Call this before set_placeholder_text. If count=0, delegate domain=slide_layouts with layout='text', then retry."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}}, "required": []}
    uno_services: list[str] | None = ["com.sun.star.presentation.PresentationDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page_idx = kwargs.get("page")
        page = DrawBridge.get_slide_for_tool(ctx.doc, page_idx)
        scan = _scan_shapes(page)
        placeholders = _list_placeholders(page, scan)
        return {"status": "ok", "page": page_idx, "placeholders": placeholders, "count": len(placeholders)}


class GetPlaceholderText(ToolBase):
    """Get text from a slide placeholder by role or shape index."""

    name: str | None = "get_placeholder_text"
    intent: str | None = "navigate"
    description: str = "Get text from a slide placeholder. Specify role ('title', 'subtitle', 'body') or index. Prefer list_placeholders first; use index when roles are missing."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"role": {"type": "string", "description": "Placeholder role: 'title', 'subtitle', or 'body'."}, "index": {"type": "integer", "description": "Shape index (from list_placeholders)."}, "page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}},
        "required": [],
    }
    uno_services: list[str] | None = ["com.sun.star.presentation.PresentationDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page_idx = kwargs.get("page")
        page = DrawBridge.get_slide_for_tool(ctx.doc, page_idx)
        role = kwargs.get("role")
        shape_index = kwargs.get("index")
        scan = None if shape_index is not None else _scan_shapes(page)

        shape, shape_index, err = _locate(page, role, shape_index, scan)
        if err == "not_found":
            payload = self._tool_error("Placeholder '%s' not found on this slide." % role)
            payload["details"] = _role_miss_error_kwargs(page, scan)
            return payload
        if err or shape is None:
            return self._tool_error(err or "Shape not found.")

        if not hasattr(shape, "getString"):
            return self._tool_error("Shape has no text.")

        return {"status": "ok", "text": shape.getString(), "role": role, "index": shape_index}


class SetPlaceholderText(ToolBase):
    """Set text on a slide placeholder by role or shape index."""

    name: str | None = "set_placeholder_text"
    intent: str | None = "edit"
    description: str = "Set text on a slide placeholder. Specify role ('title', 'subtitle', 'body') or index. Prefer list_placeholders first; prefer index when role lookup fails. Empty available means the slide lacks a text layout — set layout 'text' then retry, not a missing argument."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to set on the placeholder."},
            "role": {"type": "string", "description": "Placeholder role: 'title', 'subtitle', or 'body'."},
            "index": {"type": "integer", "description": "Shape index (from list_placeholders)."},
            "page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."},
        },
        "required": ["text"],
    }
    uno_services: list[str] | None = ["com.sun.star.presentation.PresentationDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page_idx = kwargs.get("page")
        page = DrawBridge.get_slide_for_tool(ctx.doc, page_idx)
        text = kwargs["text"]
        role = kwargs.get("role")
        shape_index = kwargs.get("index")
        scan = None if shape_index is not None else _scan_shapes(page)

        shape, shape_index, err = _locate(page, role, shape_index, scan)
        if err == "not_found":
            # Named details dict (not **unpack) so mixed list/str values
            # do not collide with _tool_error's code= parameter.
            payload = self._tool_error("Placeholder '%s' not found on this slide." % role)
            payload["details"] = _role_miss_error_kwargs(page, scan)
            return payload
        if err or shape is None:
            return self._tool_error(err or "Shape not found.")

        if not hasattr(shape, "setString"):
            return self._tool_error("Shape does not support text.")

        shape.setString(text)
        return {"status": "ok", "text": text, "role": role, "index": shape_index}
