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

from typing import TYPE_CHECKING, Any, ClassVar

from plugin.draw.base import ToolDrawShapeBase
from plugin.draw.tree import build_shape_tree, coerce_control_state, find_shape_on_page, is_control_shape_type
from plugin.framework.errors import is_real_disposal, uno_probe

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


def _pick_unique(hits: list[dict[str, Any]], missing: str, ambiguous: str) -> tuple[dict[str, Any] | None, str | None]:
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, missing
    return None, ambiguous


def _node_address(node: dict[str, Any]) -> str:
    if node.get("path_index") is not None:
        return str(node["path_index"])
    return str(node.get("index"))


def _locate_shape(page: Any, node: dict[str, Any]) -> tuple[Any | None, str | None]:
    """Return the page shape for a tree node; group children walk path_index."""
    path_index = node.get("path_index")
    if path_index is None:
        _idx, shape, err = find_shape_on_page(page, index=node.get("index"))
        return shape, err
    shape = page
    try:
        for part in str(path_index).split("."):
            shape = shape.getByIndex(int(part))
    except Exception as exc:
        if is_real_disposal(exc):
            raise
        return None, "Could not read shape at path_index %s: %s" % (path_index, exc)
    return shape, None


def resolve_field_node(
    tree: list[dict[str, Any]],
    *,
    name: str | None = None,
    index: int | None = None,
    path_index: str | None = None,
    label_hint: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Pick a tree node for a fill request.

    Precedence: name first (stable address; index/path_index only disambiguate
    duplicate names), then path_index, then index, then label_hint.
    """
    flat = _flatten_tree(tree)
    wanted_name = (name or "").strip()
    if wanted_name:
        name_hits = [n for n in flat if (n.get("name") or "") == wanted_name]
        if not name_hits:
            name_hits = [n for n in flat if isinstance(n.get("control"), dict) and (n["control"].get("name") or "") == wanted_name]
        if len(name_hits) > 1:
            narrowed: list[dict[str, Any]] = []
            if path_index is not None:
                p_wanted = str(path_index).strip()
                narrowed = [n for n in name_hits if str(n.get("path_index")) == p_wanted]
            if not narrowed and index is not None and not isinstance(index, bool):
                try:
                    idx = int(index)
                    narrowed = [n for n in name_hits if n.get("index") == idx]
                except (TypeError, ValueError):
                    pass
            if narrowed:
                name_hits = narrowed
        return _pick_unique(
            name_hits,
            "No shape named %r on this page." % wanted_name,
            "Several shapes named %r; pass path_index or index: %s."
            % (wanted_name, ", ".join(_node_address(n) for n in name_hits)),
        )

    if path_index is not None:
        wanted_path = str(path_index).strip()
        for node in flat:
            if str(node.get("path_index")) == wanted_path:
                return node, None
        return None, "No shape at path_index %s on this page." % path_index

    if index is not None:
        if isinstance(index, bool):
            return None, "Invalid shape index: %s" % index
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
        hint_hits = [n for n in flat if n.get("fillable") and (n.get("label_hint") or "").strip().lower() == hint]
        return _pick_unique(
            hint_hits,
            "No fillable shape with label_hint %r." % label_hint,
            "Several fillable shapes match label_hint %r; pass name, index, or path_index." % label_hint,
        )

    return None, "Each field needs name, index, path_index, or label_hint."


def _write(fn: Any, ok: str, fail: str) -> tuple[bool, str]:
    try:
        fn()
        return True, ok
    except Exception as exc:
        # is_disposed_exception matches bare RuntimeException on live documents,
        # which would abort remaining fields; only real disposal escapes.
        if is_real_disposal(exc):
            raise
        return False, fail % exc


def apply_fill_value(shape: Any, value: Any) -> tuple[bool, str]:
    """Write *value* onto a paper-form shape or ControlShape model."""
    shape_type = uno_probe(lambda: shape.getShapeType(), "")

    if is_control_shape_type(shape_type):
        return _apply_control_value(shape, value)

    if hasattr(shape, "setString"):
        val = "" if value is None else str(value)
        return _write(lambda: shape.setString(val), "set text", "setString failed: %s")
    return False, "Shape cannot hold text."


def _apply_control_value(shape: Any, value: Any) -> tuple[bool, str]:
    try:
        model = shape.Control
    except Exception as exc:
        if is_real_disposal(exc):
            raise
        return False, "ControlShape has no model: %s" % exc
    if model is None:
        return False, "ControlShape has no model."

    if hasattr(model, "State") and not hasattr(model, "Text"):
        state = coerce_control_state(value)
        if state is None:
            return False, "Could not interpret %r as a checkbox/radio State." % value
        def _set_state() -> None:
            model.State = state
        return _write(_set_state, "set state", "Setting State failed: %s")

    if hasattr(model, "Text"):
        val = "" if value is None else str(value)
        def _set_text() -> None:
            model.Text = val
        return _write(_set_text, "set text", "Setting Text failed: %s")

    if hasattr(shape, "setString"):
        val = "" if value is None else str(value)
        return _write(lambda: shape.setString(val), "set text", "setString failed: %s")
    return False, "Control cannot accept a text or State value."


class FillDrawFields(ToolDrawShapeBase):
    """Batch-write existing blanks / widgets. Does not spawn new controls."""

    name: str | None = "fill_draw_fields"
    description: str = (
        "Fill existing empty text boxes (paper-form fields) or ControlShape values on a Draw/Impress page "
        "so a GMP-style stand-in can be completed in one call. Empty boxes are fill targets — do not create "
        "new ControlShapes unless the user asked for live form widgets. Resolve each field by name (preferred), "
        "draw-page index, path_index for group children, or label_hint from get_draw_tree. Returns per-field "
        "ok/fail so you can recover. Not a PDF/AcroForm API."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based page index (active page if omitted)."},
            "fields": {
                "type": "array",
                "description": (
                    "Fields to write. Target precedence: name (preferred; disambiguated by path_index or index), "
                    "path_index, index, or label_hint. Each item needs a value."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Shape Name or Control.Name from get_draw_tree."},
                        "index": {
                            "type": "integer",
                            "description": "0-based draw-page shape index (fragile when non-fields sit between boxes; disambiguates duplicate names).",
                        },
                        "path_index": {
                            "type": "string",
                            "description": 'Dotted path from get_draw_tree for a shape inside a group (e.g. "2.0"); use when the group child has no name.',
                        },
                        "label_hint": {"type": "string", "description": "Neighbor label from get_draw_tree (left/above heuristic)."},
                        "value": {
                            "type": ["string", "number", "boolean", "null"],
                            "description": "Value to write (text, or checkbox/radio State 0/1/yes/no). Explicit null clears a text box; omitting 'value' reports an error.",
                        },
                    },
                    "required": ["value"],
                },
            },
        },
        "required": ["fields"],
    }
    doc_types: list[str] | None = ["draw", "impress"]
    is_mutation: bool | None = True
    required_core_tools: ClassVar[frozenset[str] | None] = frozenset(["get_draw_tree"])

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import resolve_shape_page

        fields = kwargs.get("fields")
        if not fields:
            return self._tool_error("fields must be a non-empty list of {name|index|label_hint, value}.")

        page, actual_idx, err = resolve_shape_page(ctx, kwargs)
        if err or page is None:
            return self._tool_error(err or "No draw page available.")

        # getByIndex raises on a bad index; it does not return None.
        tree = build_shape_tree(page)
        results: list[dict[str, Any]] = []
        ok_count = 0
        for i, raw in enumerate(fields):
            if not isinstance(raw, dict):
                results.append({"ok": False, "error": "Field %s is not an object." % i})
                continue
            if "value" not in raw:
                results.append({"ok": False, "error": "Field %s has no 'value' (use null to clear a text box)." % i})
                continue
            node, err = resolve_field_node(
                tree,
                name=raw.get("name"),
                index=raw.get("index"),
                path_index=raw.get("path_index"),
                label_hint=raw.get("label_hint"),
            )
            if err or node is None:
                results.append({
                    "ok": False,
                    "error": err or "Could not resolve field.",
                    "name": raw.get("name"),
                    "index": raw.get("index"),
                    "path_index": raw.get("path_index"),
                    "label_hint": raw.get("label_hint"),
                })
                continue
            shape, find_err = _locate_shape(page, node)
            if find_err or shape is None:
                results.append({
                    "ok": False,
                    "error": find_err or "Resolved tree node but the shape is gone.",
                    "name": node.get("name"),
                    "index": node.get("index"),
                })
                continue
            applied, detail = apply_fill_value(shape, raw["value"])
            entry: dict[str, Any] = {"ok": applied, "name": node.get("name") or "", "detail": detail}
            if node.get("index") is not None:
                entry["index"] = node["index"]
            if node.get("path_index") is not None:
                entry["path_index"] = node["path_index"]
            if node.get("label_hint"):
                entry["label_hint"] = node["label_hint"]
            if not applied:
                entry["error"] = detail
            else:
                ok_count += 1
            results.append(entry)

        status = "ok" if ok_count == len(fields) else ("partial" if ok_count else "error")
        return {"status": status, "page": actual_idx, "ok_count": ok_count, "fail_count": len(fields) - ok_count, "results": results}
