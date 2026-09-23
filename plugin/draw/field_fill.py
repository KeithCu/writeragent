# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Batch fill for Draw/Impress paper-form blanks and ControlShapes.

Empty text boxes imported from a PDF stand-in are fill targets. This
tool writes existing shapes; it does not create ControlShapes. Live
PDF/AcroForm widgets are out of scope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.draw.base import ToolDrawShapeBase
from plugin.draw.tree import (
    build_shape_tree,
    coerce_control_state,
    find_shape_on_page,
    is_control_shape_type,
)

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


def _flatten_tree(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in nodes:
        out.append(node)
        children = node.get("children")
        if children:
            out.extend(_flatten_tree(children))
    return out


def resolve_field_node(
    tree: list[dict[str, Any]],
    *,
    name: str | None = None,
    index: int | None = None,
    label_hint: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Pick a tree node for a fill request. Name, then index, then label_hint."""
    flat = _flatten_tree(tree)
    wanted_name = (name or "").strip()
    if wanted_name:
        name_hits = [n for n in flat if (n.get("name") or "") == wanted_name]
        if not name_hits:
            name_hits = [
                n
                for n in flat
                if isinstance(n.get("control"), dict) and (n["control"].get("name") or "") == wanted_name
            ]
        if len(name_hits) == 1:
            return name_hits[0], None
        if not name_hits:
            return None, "No shape named %r on this page." % wanted_name
        return None, "Several shapes named %r; pass index as well." % wanted_name

    if index is not None:
        try:
            idx = int(index)
        except (TypeError, ValueError):
            return None, "Invalid shape index: %s" % index
        for node in flat:
            if node.get("index") == idx:
                return node, None
        return None, "No shape at index %s on this page." % idx

    hint = (label_hint or "").strip().lower()
    if hint:
        hint_hits = [
            n
            for n in flat
            if n.get("fillable") and (n.get("label_hint") or "").strip().lower() == hint
        ]
        if len(hint_hits) == 1:
            return hint_hits[0], None
        if not hint_hits:
            return None, "No fillable shape with label_hint %r." % label_hint
        return None, "Several fillable shapes match label_hint %r; pass name or index." % label_hint

    return None, "Each field needs name, index, or label_hint."


def apply_fill_value(shape: Any, value: Any) -> tuple[bool, str]:
    """Write *value* onto a paper-form shape or ControlShape model."""
    try:
        shape_type = shape.getShapeType()
    except Exception:
        shape_type = ""

    if is_control_shape_type(shape_type):
        return _apply_control_value(shape, value)

    if hasattr(shape, "setString"):
        try:
            shape.setString("" if value is None else str(value))
            return True, "set text"
        except Exception as exc:
            return False, "setString failed: %s" % exc
    return False, "Shape cannot hold text."


def _apply_control_value(shape: Any, value: Any) -> tuple[bool, str]:
    try:
        model = shape.Control
    except Exception as exc:
        return False, "ControlShape has no model: %s" % exc
    if model is None:
        return False, "ControlShape has no model."

    if hasattr(model, "State") and not hasattr(model, "Text"):
        state = coerce_control_state(value)
        if state is None:
            return False, "Could not interpret %r as a checkbox/radio State." % value
        try:
            model.State = state
            return True, "set state"
        except Exception as exc:
            return False, "Setting State failed: %s" % exc

    if hasattr(model, "Text"):
        try:
            model.Text = "" if value is None else str(value)
            return True, "set text"
        except Exception as exc:
            return False, "Setting Text failed: %s" % exc

    if hasattr(model, "State"):
        state = coerce_control_state(value)
        if state is None:
            return False, "Could not interpret %r as a control State." % value
        try:
            model.State = state
            return True, "set state"
        except Exception as exc:
            return False, "Setting State failed: %s" % exc

    if hasattr(shape, "setString"):
        try:
            shape.setString("" if value is None else str(value))
            return True, "set text"
        except Exception as exc:
            return False, "setString failed: %s" % exc
    return False, "Control cannot accept a text or State value."


class FillDrawFields(ToolDrawShapeBase):
    """Batch-write existing blanks / widgets. Does not spawn new controls."""

    name = "fill_draw_fields"
    description = (
        "Fill existing empty text boxes (paper-form fields) or ControlShape values on a Draw/Impress page "
        "so a GMP-style stand-in can be completed in one call. Empty boxes are fill targets — do not create "
        "new ControlShapes unless the user asked for live form widgets. Resolve each field by name (preferred), "
        "draw-page index, or label_hint from get_draw_tree. Returns per-field ok/fail so you can recover. "
        "Not a PDF/AcroForm API."
    )
    parameters = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based page index (active page if omitted)."},
            "fields": {
                "type": "array",
                "description": "Fields to write. Each item needs a value plus name, index, or label_hint.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Shape Name or Control.Name from get_draw_tree."},
                        "index": {"type": "integer", "description": "0-based draw-page shape index (fragile when non-fields sit between boxes)."},
                        "label_hint": {"type": "string", "description": "Neighbor label from get_draw_tree (left/above heuristic)."},
                        "value": {"type": "string", "description": "Text to write, or checkbox/radio State (0/1, yes/no, checked)."},
                    },
                    "required": ["value"],
                },
            },
        },
        "required": ["fields"],
    }
    doc_types = ["draw", "impress"]
    is_mutation = True
    required_core_tools = frozenset(["get_draw_tree"])

    def execute(self, ctx: ToolContext, **kwargs: Any):
        from plugin.draw.bridge import DrawBridge

        fields = kwargs.get("fields") or []
        if not isinstance(fields, list) or not fields:
            return self._tool_error("fields must be a non-empty list of {name|index|label_hint, value}.")

        bridge = DrawBridge(ctx.doc)
        idx = kwargs.get("page")
        actual_idx = idx if idx is not None else ctx.active_page_index
        if actual_idx is None:
            actual_idx = bridge.get_active_page_index()

        try:
            page = bridge.get_pages().getByIndex(actual_idx)
        except Exception:
            return self._tool_error("Invalid page index: %s" % actual_idx)
        if page is None:
            return self._tool_error("No draw page available.")

        tree = build_shape_tree(page)
        results: list[dict[str, Any]] = []
        ok_count = 0
        for i, raw in enumerate(fields):
            if not isinstance(raw, dict):
                results.append({"ok": False, "error": "Field %s is not an object." % i})
                continue
            node, err = resolve_field_node(
                tree,
                name=raw.get("name"),
                index=raw.get("index"),
                label_hint=raw.get("label_hint"),
            )
            if err or node is None:
                results.append({"ok": False, "error": err or "Could not resolve field.", "name": raw.get("name"), "index": raw.get("index"), "label_hint": raw.get("label_hint")})
                continue
            shape_idx, shape, find_err = find_shape_on_page(page, index=node.get("index"), name=node.get("name") or None)
            if find_err or shape is None:
                # Group children expose path_index only; fall back to name.
                shape_idx, shape, find_err = find_shape_on_page(page, name=node.get("name") or None)
            if find_err or shape is None:
                results.append({"ok": False, "error": find_err or "Resolved tree node but the shape is gone.", "name": node.get("name"), "index": node.get("index")})
                continue
            applied, detail = apply_fill_value(shape, raw.get("value"))
            entry: dict[str, Any] = {
                "ok": applied,
                "index": shape_idx,
                "name": node.get("name") or "",
                "detail": detail,
            }
            if node.get("label_hint"):
                entry["label_hint"] = node["label_hint"]
            if not applied:
                entry["error"] = detail
            else:
                ok_count += 1
            results.append(entry)

        status = "ok" if ok_count == len(fields) else ("partial" if ok_count else "error")
        return {
            "status": status,
            "page": actual_idx,
            "ok_count": ok_count,
            "fail_count": len(fields) - ok_count,
            "results": results,
        }
