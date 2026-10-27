# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
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
"""Tree (LO-DOM) tools for Draw/Impress documents.

``get_draw_tree`` is the main-agent read of a page. Paper-form blanks
(empty/near-empty text boxes) are marked ``fillable`` with an optional
``label_hint`` (nearest text to the left or above). ControlShapes are
surfaced with type/name/value/state so the main agent can see widgets
without a forms delegation.

Neighbor heuristic (documented for callers): among sibling nodes with
non-empty text, prefer any matching label strictly to the left (priority 0)
over a label strictly above (priority 1), sorting by distance within each
group. Left always beats above (not only on equal distance). Gap slack is
200 units (2 mm) so PDF→Draw imports that barely overlap still match.
This is a hint, not a reading-order parser.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from plugin.framework.errors import is_disposed_exception, is_real_disposal, uno_read
from plugin.framework.tool import ToolBase, ToolContext

log = logging.getLogger(__name__)

# Placeholder-only text: underscores, leaders, box drawings, whitespace.
_NEAR_EMPTY_RE = re.compile(r"^[\s._\-–—•·□■☐☑☒╳]+$")

# Shapes that can hold paper-form text. Lines/connectors/graphics/tables/OLE
# are not fill targets even when getString exists.
_NON_FILLABLE_TYPE_MARKERS = ("LineShape", "ConnectorShape", "GroupShape", "GraphicObjectShape", "OLE2Shape", "TableShape", "PluginShape", "MediaShape", "ControlShape", "PageShape", "MeasureShape")

# 2 mm of import slop when deciding left/above alignment.
_LABEL_ALIGN_SLACK = 200


def short_shape_type(shape_type: str) -> str:
    return (shape_type or "").replace("com.sun.star.drawing.", "")


def is_near_empty_text(text: str | None) -> bool:
    """True when *text* is empty or only placeholder marks (____, …, boxes)."""
    raw = "" if text is None else str(text)
    stripped = raw.strip()
    if not stripped:
        return True
    return bool(_NEAR_EMPTY_RE.fullmatch(stripped))


def is_control_shape_type(shape_type: str) -> bool:
    return "ControlShape" in short_shape_type(shape_type)


def is_text_capable_shape_type(shape_type: str) -> bool:
    short = short_shape_type(shape_type)
    if not short or short == "UnknownShape":
        return False
    return not any(marker in short for marker in _NON_FILLABLE_TYPE_MARKERS)


def coerce_control_state(value: Any) -> int | None:
    """Map a model/tool value to LibreOffice form State (0/1/2).

    Accepts bools, ints, and common strings (yes/checked → 1). Returns
    None when the value cannot be interpreted as a state.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on", "checked", "selected"):
        return 1
    if s in ("0", "false", "no", "off", "unchecked"):
        return 0
    if s in ("2", "unknown", "indeterminate"):
        return 2
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def control_snapshot(model: Any) -> dict[str, Any]:
    """Type, name, and current value/state from a form component model."""
    info: dict[str, Any] = {"type": readable_control_type(model), "name": _safe_attr(model, "Name")}
    label = _safe_attr(model, "Label")
    if label:
        info["label"] = label
    if hasattr(model, "Text"):
        try:
            info["text"] = model.Text
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            log.debug("control snapshot Text read failed", exc_info=True)
    if hasattr(model, "State"):
        try:
            info["state"] = int(model.State)
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            log.debug("control snapshot State read failed", exc_info=True)
    if hasattr(model, "StringItemList"):
        try:
            info["items"] = list(model.StringItemList)
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            log.debug("control snapshot StringItemList read failed", exc_info=True)
    if hasattr(model, "SelectedItems"):
        try:
            info["selected"] = list(model.SelectedItems)
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            log.debug("control snapshot SelectedItems read failed", exc_info=True)
    return info


# Shared form control mapping. Forms and the draw tree share this table; it
# lives here so the tree path does not import writer.forms (which imports UNO Point/Size).
CONTROL_TYPE_MAP: dict[str, str] = {
    "checkbox": "com.sun.star.form.component.CheckBox",
    "text": "com.sun.star.form.component.TextField",
    "radio": "com.sun.star.form.component.RadioButton",
    "date": "com.sun.star.form.component.DateField",
    "combobox": "com.sun.star.form.component.ComboBox",
    "button": "com.sun.star.form.component.CommandButton",
    "listbox": "com.sun.star.form.component.ListBox",
}


def readable_control_type(model: Any) -> str:
    for type_str, service in CONTROL_TYPE_MAP.items():
        try:
            if model is not None and model.supportsService(service):
                return type_str
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            log.debug("supportsService(%s) failed", service, exc_info=True)
            continue
    return "unknown"


def _safe_attr(obj: Any, name: str, default: str = "") -> str:
    try:
        val = getattr(obj, name, default)
        return default if val is None else str(val)
    except Exception as exc:
        # Bugfix: a closed document raises DisposedException from a shape attribute,
        # and this except turned that into a missing page / not-found result.
        # Re-raise real disposal so execute_safe reports DOCUMENT_DISPOSED.
        # Any other failure stays on the existing missing-page path.
        if is_real_disposal(exc):
            raise
        return default


def _shape_name(shape: Any) -> str:
    return _safe_attr(shape, "Name")


def _shape_text(shape: Any) -> str:
    if not hasattr(shape, "getString"):
        return ""
    # Bugfix: a closed document raises from getString; the old bare except
    # turned that into "" and the page looked empty. uno_read re-raises real
    # disposal (DOCUMENT_DISPOSED) and keeps a missing text property as "".
    return uno_read(lambda: shape.getString() or "", "")


def _shape_geometry(shape: Any) -> dict[str, int] | None:
    pos: Any = uno_read(lambda: shape.getPosition(), None)
    size: Any = uno_read(lambda: shape.getSize(), None)
    if pos is None or size is None:
        return None
    return {"x": int(pos.X), "y": int(pos.Y), "width": int(size.Width), "height": int(size.Height)}


def _cx(g: dict[str, int]) -> float:
    return g["x"] + g["width"] / 2.0


def _cy(g: dict[str, int]) -> float:
    return g["y"] + g["height"] / 2.0


def _gap_left(label_g: dict[str, int], blank_g: dict[str, int]) -> int | None:
    if label_g["x"] + label_g["width"] <= blank_g["x"] + _LABEL_ALIGN_SLACK and abs(_cy(label_g) - _cy(blank_g)) <= max(label_g["height"], blank_g["height"], 1):
        return max(0, blank_g["x"] - (label_g["x"] + label_g["width"]))
    return None


def _gap_above(label_g: dict[str, int], blank_g: dict[str, int]) -> int | None:
    if label_g["y"] + label_g["height"] <= blank_g["y"] + _LABEL_ALIGN_SLACK and abs(_cx(label_g) - _cx(blank_g)) <= max(label_g["width"], blank_g["width"], 1):
        return max(0, blank_g["y"] - (label_g["y"] + label_g["height"]))
    return None


def nearest_label_hint(blank: dict[str, Any], candidates: list[dict[str, Any]]) -> str | None:
    """Return text of the nearest left-or-above labeled sibling, or None.

    *candidates* should already be filtered to nodes with non-empty text.
    Left is priority 0, above is priority 1 (left beats above even with a
    larger gap).
    """
    blank_g = blank.get("geometry")
    if not blank_g:
        return None
    best_text: str | None = None
    best_key: tuple[int, int] | None = None
    for cand in candidates:
        if cand is blank:
            continue
        text = (cand.get("text") or "").strip()
        geom = cand.get("geometry")
        if not text or not geom:
            continue
        gap = _gap_left(geom, blank_g)
        if gap is not None:
            key = (0, gap)
        else:
            gap = _gap_above(geom, blank_g)
            if gap is not None:
                key = (1, gap)
            else:
                continue
        if best_key is None or key < best_key:
            best_key = key
            best_text = text
    return best_text


def attach_label_hints(nodes: list[dict[str, Any]]) -> None:
    """Set ``label_hint`` on fillable siblings; recurse into groups."""
    labeled = [n for n in nodes if (n.get("text") or "").strip() and not n.get("fillable")]
    for node in nodes:
        if node.get("fillable"):
            hint = nearest_label_hint(node, labeled)
            if hint:
                node["label_hint"] = hint
        children = node.get("children")
        if children:
            attach_label_hints(children)


def find_shape_on_page(page: Any, *, index: int | None = None, name: str | None = None) -> tuple[int | None, Any | None, str | None]:
    """Resolve a page shape by index or Name (shape.Name or Control.Name).

    Returns ``(index, shape, error)``. Name match is exact; several hits
    prefer a unique ``shape.Name`` match, else error so the caller can recover.
    ``index`` is the draw-page index from ``page.getByIndex``. A match inside
    a group has no draw-page index, so that index is None and ``shape`` is
    the child.
    """
    if index is not None:
        try:
            idx = int(index)
            if idx < 0 or idx >= page.getCount():
                return None, None, "Invalid shape index: %s" % idx
            return idx, page.getByIndex(idx), None
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            return None, None, "Failed to find shape at index %s: %s" % (index, exc)

    wanted = (name or "").strip()
    if not wanted:
        return None, None, "Pass name or index to address the shape."

    name_hits: list[tuple[int | None, Any]] = []
    control_hits: list[tuple[int | None, Any]] = []
    try:
        count = page.getCount()
    except Exception as exc:
        if is_disposed_exception(exc):
            raise
        return None, None, "Could not read page shapes: %s" % exc

    def _consider(shape: Any, top_index: int, *, at_page_level: bool) -> None:
        try:
            shape_type = shape.getShapeType()
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            shape_type = ""
        # Bugfix: name and control hits inside a GroupShape stored the group's
        # page index. shape_upsert echoes that index, and the next edit passes
        # it to page.getByIndex, which is the group. A descendant has no
        # draw-page index (the tree uses path_index), so only the top-level
        # shape gets one.
        hit_index = top_index if at_page_level else None
        if _shape_name(shape) == wanted:
            name_hits.append((hit_index, shape))
            return
        try:
            if is_control_shape_type(shape_type):
                control = getattr(shape, "Control", None)
                if control is not None and _safe_attr(control, "Name") == wanted:
                    control_hits.append((hit_index, shape))
                    return
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
        if "GroupShape" in str(shape_type):
            try:
                child_count = shape.getCount()
            except Exception as exc:
                if is_disposed_exception(exc):
                    raise
                return
            for j in range(child_count):
                try:
                    child = shape.getByIndex(j)
                except Exception as exc:
                    if is_disposed_exception(exc):
                        raise
                    continue
                _consider(child, top_index, at_page_level=False)

    for i in range(count):
        try:
            shape = page.getByIndex(i)
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            continue
        _consider(shape, i, at_page_level=True)

    hits = name_hits if name_hits else control_hits
    if len(hits) == 1:
        return hits[0][0], hits[0][1], None
    if not hits:
        return None, None, "No shape named %r on this page." % wanted
    return None, None, "Several shapes named %r; pass index as well." % wanted


def build_shape_tree(xshapes: Any, base_index: str | None = None) -> list[dict[str, Any]]:
    """Recursively build a semantic tree from an XShapes collection."""
    tree = _collect_shape_nodes(xshapes, base_index)
    attach_label_hints(tree)
    return tree


def _collect_shape_nodes(xshapes: Any, base_index: str | None = None) -> list[dict[str, Any]]:
    tree: list[dict[str, Any]] = []
    try:
        count = xshapes.getCount()
    except Exception as exc:
        if is_disposed_exception(exc):
            raise
        return tree

    for i in range(count):
        try:
            shape = xshapes.getByIndex(i)
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            continue

        current_index = str(i) if base_index is None else f"{base_index}.{i}"

        shape_type = uno_read(lambda: shape.getShapeType(), "UnknownShape")

        node: dict[str, Any] = {"type": short_shape_type(shape_type)}

        if base_index is None:
            node["index"] = i
        else:
            node["path_index"] = current_index

        name = _shape_name(shape)
        text = _shape_text(shape)
        text_capable = is_text_capable_shape_type(shape_type)
        control = is_control_shape_type(shape_type)

        if name or text_capable or control:
            node["name"] = name

        if text.strip():
            node["text"] = text.strip()

        if text_capable and is_near_empty_text(text):
            node["fillable"] = True

        if control:
            model: Any = uno_read(lambda: getattr(shape, "Control", None), None)
            unknown = {"type": "unknown", "name": name}
            # A non-disposal snapshot failure still degrades to "unknown" (as before); disposal leaves.
            node["control"] = uno_read(lambda: control_snapshot(model), unknown) if model is not None else unknown

        desc = _safe_attr(shape, "Description")
        if desc:
            node["alt_description"] = desc
        title = _safe_attr(shape, "Title")
        if title:
            node["alt_title"] = title

        geom = _shape_geometry(shape)
        if geom:
            node["geometry"] = geom

        if "ConnectorShape" in shape_type:
            start_shape: Any = uno_read(lambda: shape.getPropertyValue("StartShape"), None)
            if start_shape:
                node["connected_start"] = {"name": _shape_name(start_shape), "text": _shape_text(start_shape).strip()}
            end_shape: Any = uno_read(lambda: shape.getPropertyValue("EndShape"), None)
            if end_shape:
                node["connected_end"] = {"name": _shape_name(end_shape), "text": _shape_text(end_shape).strip()}

        style: dict[str, Any] = {}
        for prop in ["FillColor", "LineColor", "ZOrder", "RotateAngle", "LineWidth"]:
            val: Any = uno_read(lambda: shape.getPropertyValue(prop), None)
            if val is not None:
                if prop in ["FillColor", "LineColor"] and isinstance(val, int) and val != -1:
                    style[prop] = f"#{val:06X}"
                else:
                    style[prop] = val

        geom_props: Any = uno_read(lambda: shape.getPropertyValue("CustomShapeGeometry"), None)
        if geom_props:
            for p in geom_props:
                if p.Name == "Type":
                    node["custom_shape_type"] = p.Value

        if style:
            node["style"] = style

        if "GroupShape" in shape_type:
            node["children"] = _collect_shape_nodes(shape, current_index)

        tree.append(node)

    return tree


class GetDrawTree(ToolBase):
    name: str | None = "get_draw_tree"
    intent: str | None = "read"
    description: str = (
        "Read the page as a shape tree so you can fill blanks and see widgets without a screenshot. "
        "Empty or near-empty text boxes are fill targets (fillable=true) with name, geometry, and a "
        "label_hint (nearest text to the left or above). ControlShapes include type, name, and current "
        "value/state. Address shapes by name from this tree — draw-page index shifts when other shapes sit between fields."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based page index (active page if omitted)"}}, "required": []}
    uno_services: list[str] | None = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
    doc_types: list[str] | None = ["draw", "impress"]
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import resolve_shape_page

        page, actual_idx, err = resolve_shape_page(ctx, kwargs)
        if err or page is None:
            return self._tool_error(err or "No draw page available.")

        return {"status": "ok", "page": actual_idx, "tree": build_shape_tree(page)}
