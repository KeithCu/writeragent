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

``shape_get_tree`` is the main-agent read of a page. Paper-form blanks
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
from typing import Any, Iterator

from plugin.doc.text_helpers import normalize_linebreaks
from plugin.draw.base import DRAW_PAGE_PROPERTY
from plugin.framework.errors import is_disposal, uno_read
from plugin.framework.tool import ToolBase, ToolContext

log = logging.getLogger(__name__)

# Placeholder-only text: underscores, leaders, box drawings, whitespace.
_NEAR_EMPTY_RE = re.compile(r"[\s._\-–—•·□■☐☑☒╳]*")

# Shapes that can hold paper-form text. Lines/connectors/graphics/tables/OLE
# are not fill targets even when getString exists.
_NON_FILLABLE_TYPE_MARKERS = ("LineShape", "ConnectorShape", "GroupShape", "GraphicObjectShape", "OLE2Shape", "TableShape", "PluginShape", "MediaShape", "ControlShape", "PageShape", "MeasureShape")

# 2 mm of import slop when deciding left/above alignment.
_LABEL_ALIGN_SLACK = 200


def short_shape_type(shape_type: str) -> str:
    return (shape_type or "").replace("com.sun.star.drawing.", "")


def is_near_empty_text(text: str | None) -> bool:
    """True when *text* is empty or only placeholder marks (____, …, boxes)."""
    return _NEAR_EMPTY_RE.fullmatch((text or "").strip()) is not None


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
    for attr, key in (("Text", "text"), ("State", "state"), ("StringItemList", "items"), ("SelectedItems", "selected")):
        if not hasattr(model, attr):
            continue
        try:
            raw = getattr(model, attr)
            info[key] = int(raw) if key == "state" else list(raw) if key in ("items", "selected") else raw
        except Exception as exc:
            if is_disposal(exc):
                raise
            log.debug("control snapshot %s read failed", attr, exc_info=True)
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
            if is_disposal(exc):
                raise
            log.debug("supportsService(%s) failed", service, exc_info=True)
    return "unknown"


def _safe_attr(obj: Any, name: str, default: str = "") -> str:
    try:
        val = getattr(obj, name, default)
        return default if val is None else str(val)
    except Exception as exc:
        # Closed documents raise DisposedException from shape attributes. Re-raise disposal
        # for DOCUMENT_DISPOSED; other failures keep the missing-page default.
        if is_disposal(exc):
            raise
        return default


def _shape_text(shape: Any) -> str:
    if not hasattr(shape, "getString"):
        return ""
    # getString on a closed document raises disposal. uno_read re-raises that and treats
    # a missing text property as empty string.
    return uno_read(lambda: shape.getString() or "", "")


def _shape_geometry(shape: Any) -> dict[str, int] | None:
    pos: Any = uno_read(lambda: shape.getPosition(), None)
    size: Any = uno_read(lambda: shape.getSize(), None)
    if pos is None or size is None:
        return None
    return {"x": int(pos.X), "y": int(pos.Y), "width": int(size.Width), "height": int(size.Height)}


def custom_shape_geometry(shape: Any) -> dict[str, Any]:
    """CustomShapeGeometry as ``{name: value}``; ``{}`` for a shape that has none."""
    props: Any = uno_read(lambda: shape.getPropertyValue("CustomShapeGeometry"), None)
    return {p.Name: p.Value for p in props or ()}


def shape_kind(shape: Any) -> str:
    """Human-readable shape kind.

    Writer's ``SwXFrame::getShapeType`` (``sw/source/core/unocore/unoframe.cxx``)
    reports every frame, image, and OLE object as ``"FrameShape"``, so frames
    and OLE shapes are distinguished by UNO service and CLSID.
    """
    type_name = uno_read(lambda: str(shape.getShapeType()).split(".")[-1], "Shape")
    if type_name == "CustomShape":
        preset = custom_shape_geometry(shape).get("Type")
        return "CustomShape (%s)" % preset if preset else type_name
    if type_name not in ("FrameShape", "OLE2Shape"):
        return type_name

    if type_name == "FrameShape":
        if uno_read(lambda: bool(shape.supportsService("com.sun.star.text.TextGraphicObject")), False):
            return "Image"
        if uno_read(lambda: bool(shape.supportsService("com.sun.star.text.TextFrame")), False):
            return "TextFrame"

    clsid = uno_read(lambda: str(shape.CLSID or "").lower(), "")
    if clsid:
        # lazy: calc.charts and writer.math pull in calc/writer modules that
        # import back into plugin.draw / plugin.doc; both are already loaded by
        # tool registration.
        from plugin.calc.charts import is_chart_clsid
        from plugin.writer.math.math_mml_convert import MATH_CLSID

        if is_chart_clsid(clsid):
            return "Chart"
        if clsid == MATH_CLSID.lower():
            return "Formula"

    return "EmbeddedObject" if type_name == "FrameShape" else type_name


def shape_summary_line(address: int | str, shape: Any) -> str:
    """One chat-context object line shared by Draw/Impress and Writer; unreadable fields are omitted.

    The address and Name are what ``shape_delete`` and ``chart_delete`` resolve;
    keep them in that form.
    """
    line = "- [%s] %s" % (address, shape_kind(shape))

    def read_name() -> str:
        raw = shape.Name
        return raw.strip() if isinstance(raw, str) else ""

    name = uno_read(read_name, "")
    if name:
        line += ' "%s"' % name
    geom = _shape_geometry(shape)
    if geom:
        line += ": pos(%d, %d) size(%dx%d)" % (geom["x"], geom["y"], geom["width"], geom["height"])
    get_string = getattr(shape, "getString", None)
    if callable(get_string):
        text = uno_read(lambda: normalize_linebreaks(str(get_string() or "")), "")
        if text:
            line += ' text: "%s"' % text[:200]
    return line


def _gap(label_g: dict[str, int], blank_g: dict[str, int], *, horizontal: bool) -> int | None:
    # Left: x/width, centers on y. Above: y/height, centers on x.
    pos, size, cross_pos, cross_size = ("x", "width", "y", "height") if horizontal else ("y", "height", "x", "width")
    label_c = label_g[cross_pos] + label_g[cross_size] / 2.0
    blank_c = blank_g[cross_pos] + blank_g[cross_size] / 2.0
    if label_g[pos] + label_g[size] <= blank_g[pos] + _LABEL_ALIGN_SLACK and abs(label_c - blank_c) <= max(label_g[cross_size], blank_g[cross_size], 1):
        return max(0, blank_g[pos] - (label_g[pos] + label_g[size]))
    return None


def nearest_label_hint(blank: dict[str, Any], candidates: list[dict[str, Any]]) -> str | None:
    """Return text of the nearest left-or-above labeled sibling, or None.

    *candidates* should already be filtered to nodes with non-empty text and geometry.
    Left is priority 0, above is priority 1 (left beats above even with a
    larger gap).
    """
    blank_g = blank.get("geometry")
    if not blank_g:
        return None
    best_text: str | None = None
    best_key: tuple[int, int] | None = None
    for cand in candidates:
        text = cand["text"]
        geom = cand["geometry"]
        left = _gap(geom, blank_g, horizontal=True)
        gap = left if left is not None else _gap(geom, blank_g, horizontal=False)
        if gap is None:
            continue
        key = (0 if left is not None else 1, gap)
        if best_key is None or key < best_key:
            best_key = key
            best_text = text
    return best_text


def attach_label_hints(nodes: list[dict[str, Any]]) -> None:
    """Set ``label_hint`` on fillable siblings. One level; ``build_shape_tree`` calls this per group."""
    labeled = [n for n in nodes if (n.get("text") or "").strip() and n.get("geometry") and not n.get("fillable")]
    for node in nodes:
        if node.get("fillable"):
            hint = nearest_label_hint(node, labeled)
            if hint:
                node["label_hint"] = hint


def shape_path(base: str | None, i: int) -> str:
    """"0" at page level, "2.0.1" under groups. The only path grammar."""
    return str(i) if base is None else f"{base}.{i}"


def iter_children(xshapes: Any) -> Iterator[tuple[int, Any]]:
    """Direct ``(slot, shape)`` pairs. ``slot`` is the XShapes index, not a compacted count.

    Non-disposal ``getCount`` ends the walk. Non-disposal ``getByIndex`` skips that slot.
    Only disposal escapes; a live-document RuntimeException ends the walk or skips the slot.
    """
    try:
        count = xshapes.getCount()
    except Exception as exc:
        if is_disposal(exc):
            raise
        return
    for i in range(count):
        try:
            shape = xshapes.getByIndex(i)
        except Exception as exc:
            if is_disposal(exc):
                raise
            continue
        yield i, shape


def is_group(shape: Any) -> bool:
    """True when *shape* is a GroupShape (disposal from getShapeType propagates)."""
    try:
        shape_type = shape.getShapeType()
    except Exception as exc:
        if is_disposal(exc):
            raise
        return False
    return "GroupShape" in str(shape_type)


def iter_shape_addresses(xshapes: Any, base: str | None = None) -> Iterator[tuple[str, Any, Any]]:
    """Preorder (address, shape, parent). parent is the XShapes to remove from (page or GroupShape).
    Recurse only when "GroupShape" is in the shape type. Disposal from getCount / getByIndex
    (iter_children) and getShapeType propagates as the original exception."""
    for i, shape in iter_children(xshapes):
        addr = shape_path(base, i)
        yield addr, shape, xshapes

        if is_group(shape):
            yield from iter_shape_addresses(shape, base=addr)


def page_shape_names(page: Any) -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    for _path, shape, _parent in iter_shape_addresses(page):
        name = shape.Name.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def ensure_shape_name(page: Any, shape: Any) -> str:
    """Return *shape*'s Name, assigning ``Shape<n>`` if it is empty.

    Writer's ``SwXDrawPage::add`` (``sw/source/core/unocore/unodraw.cxx:SwXDrawPage::add``)
    already assigns a unique Name, whereas Draw, Impress, and Calc leave Name
    empty and prompts tell the model to address shapes by Name.
    Uses one more than the highest existing ``Shape<n>`` number on the page so
    deleting a shape never hands its old name to a newly created shape.
    """
    current = str(shape.Name or "").strip()
    if current:
        return current
    highest = 0
    for name in page_shape_names(page):
        if name.startswith("Shape"):
            suffix = name[5:]
            if suffix.isascii() and suffix.isdigit():
                highest = max(highest, int(suffix))
    candidate = f"Shape{highest + 1}"
    shape.Name = candidate
    return candidate


def normalize_negative_span(x: int, y: int, width: int, height: int) -> tuple[int, int, int, int]:
    """Shift x/y by any negative width/height so position is top-left and size is non-negative."""
    if width < 0:
        x += width
        width = -width
    if height < 0:
        y += height
        height = -height
    return x, y, width, height


def find_shape_on_page(
    page: Any,
    *,
    index: int | str | None = None,
    name: str | None = None,
) -> tuple[int | str | None, Any | None, Any | None, str | None]:
    """(address, shape, parent, error).

    index: int page index, or dotted string "2.0.1" (also "2" for page level). No separate path_index parameter.
    address: int for a page-level shape, dotted str for a group child. Name lookup of a child returns that path, not None and not the group's page index.
    parent: the page or the GroupShape. None only on error.
    Duplicate names return an error listing all matching addresses.
    """
    if index is not None:
        if isinstance(index, bool) or not isinstance(index, (int, str)):
            return None, None, None, "Invalid shape index: %s" % index
        parts = str(index).split(".")
        if any(not p.isdigit() for p in parts):
            return None, None, None, "Invalid shape index: %s" % index
        seg_ints = [int(p) for p in parts]

        curr: Any = page
        parent: Any = None
        for d, seg_idx in enumerate(seg_ints):
            if d > 0 and not is_group(curr):
                return None, None, None, "Invalid shape index: %s" % index

            try:
                count = curr.getCount()
                if seg_idx >= count:
                    return None, None, None, "Invalid shape index: %s" % index
                child = curr.getByIndex(seg_idx)
            except Exception as exc:
                if is_disposal(exc):
                    raise
                return None, None, None, "Failed to find shape at index %s: %s" % (index, exc)

            parent = curr
            curr = child

        addr: int | str = seg_ints[0] if len(seg_ints) == 1 else ".".join(str(s) for s in seg_ints)
        return addr, curr, parent, None

    wanted = (name or "").strip()
    if not wanted:
        return None, None, None, "Pass name or index to address the shape."

    name_hits: list[tuple[int | str, Any, Any]] = []
    control_hits: list[tuple[int | str, Any, Any]] = []
    # A page-level hit keeps its int page index; a group child gets its dotted
    # path and the GroupShape as parent, so edit and delete reach the child.
    for path, shape, parent_obj in iter_shape_addresses(page):
        hit_addr: int | str = path if "." in path else int(path)
        if _safe_attr(shape, "Name") == wanted:
            name_hits.append((hit_addr, shape, parent_obj))
            continue
        shape_type = uno_read(lambda: shape.getShapeType(), "")
        if is_control_shape_type(shape_type):
            control = getattr(shape, "Control", None)
            if control is not None and _safe_attr(control, "Name") == wanted:
                control_hits.append((hit_addr, shape, parent_obj))

    hits = name_hits if name_hits else control_hits
    if len(hits) == 1:
        return hits[0][0], hits[0][1], hits[0][2], None
    if not hits:
        return None, None, None, "No shape named %r on this page." % wanted
    return None, None, None, "Several shapes named %r at %s; pass index instead." % (
        wanted,
        ", ".join(str(h[0]) for h in hits),
    )


def resolve_shape_ref(
    page: Any,
    ref: Any,
) -> tuple[int | str | None, Any | None, Any | None, str | None]:
    """Resolve *ref* as a page index, dotted index path, or shape Name.

    ``int`` (not ``bool``) and digit / dotted-digit strings use index lookup.
    Any other non-empty string uses name lookup. Empty, ``bool``, and other
    types return the same invalid-index string as ``find_shape_on_page``.
    """
    if isinstance(ref, bool):
        return None, None, None, "Invalid shape index: %s" % ref
    if isinstance(ref, int):
        return find_shape_on_page(page, index=ref)
    if isinstance(ref, str):
        stripped = ref.strip()
        if not stripped:
            return None, None, None, "Invalid shape index: %s" % ref
        parts = stripped.split(".")
        if all(part.isdigit() for part in parts):
            return find_shape_on_page(page, index=stripped)
        return find_shape_on_page(page, name=stripped)
    return None, None, None, "Invalid shape index: %s" % ref


def build_shape_tree(xshapes: Any, base_index: str | None = None) -> list[dict[str, Any]]:
    """Recursively build a semantic tree from an XShapes collection."""
    tree: list[dict[str, Any]] = []
    for i, shape in iter_children(xshapes):
        current_index = shape_path(base_index, i)

        shape_type = uno_read(lambda: shape.getShapeType(), "UnknownShape")

        node: dict[str, Any] = {"type": short_shape_type(shape_type)}

        if base_index is None:
            node["index"] = i
        else:
            node["path_index"] = current_index

        name = _safe_attr(shape, "Name")
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

        for attr, key in (("Description", "alt_description"), ("Title", "alt_title")):
            attr_val = _safe_attr(shape, attr)
            if attr_val:
                node[key] = attr_val

        geom = _shape_geometry(shape)
        if geom:
            node["geometry"] = geom

        if "ConnectorShape" in shape_type:
            for prop, key in (("StartShape", "connected_start"), ("EndShape", "connected_end")):
                linked: Any = uno_read(lambda: shape.getPropertyValue(prop), None)
                if linked:
                    node[key] = {"name": _safe_attr(linked, "Name"), "text": _shape_text(linked).strip()}

        style: dict[str, Any] = {}
        for prop in [
            "FillColor",
            "FillStyle",
            "LineColor",
            "LineStyle",
            "ZOrder",
            "RotateAngle",
            "LineWidth",
            "LineStartName",
            "LineEndName",
        ]:
            val: Any = uno_read(lambda: shape.getPropertyValue(prop), None)
            if val is not None:
                if prop in ("FillStyle", "LineStyle"):
                    style[prop] = val.value
                elif prop in ("FillColor", "LineColor") and isinstance(val, int) and val != -1:
                    style[prop] = f"#{val:06X}"
                elif prop in ("LineStartName", "LineEndName"):
                    if val:
                        style[prop] = val
                else:
                    style[prop] = val

        # Flag CustomShapes with no Path/ViewBox: they will not paint and must be
        # recreated because shape_upsert(action="edit") does not re-apply geometry.
        geom = custom_shape_geometry(shape)
        if geom:
            if "Type" in geom:
                node["custom_shape_type"] = geom["Type"]
            if not any(geom.get(k) not in (None, ()) for k in ("Path", "ViewBox")):
                node["custom_geometry_empty"] = True
        elif "CustomShape" in shape_type:
            node["custom_geometry_empty"] = True

        if style:
            node["style"] = style

        if "GroupShape" in shape_type:
            node["children"] = build_shape_tree(shape, current_index)

        tree.append(node)

    attach_label_hints(tree)
    return tree


class GetDrawTree(ToolBase):
    name: str | None = "shape_get_tree"
    description: str = (
        "Read the page as a shape tree so you can fill blanks and see widgets without a screenshot. "
        "Empty or near-empty text boxes are fill targets (fillable=true) with name, geometry, and a "
        "label_hint (nearest text to the left or above). ControlShapes include type, name, and current "
        "value/state. CustomShapes with custom_geometry_empty=true have no Path/ViewBox and will not "
        "paint (recreate them; edit does not re-apply geometry). Address shapes by name from this tree "
        "— draw-page index shifts when other shapes sit between fields."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": DRAW_PAGE_PROPERTY}, "required": []}
    uno_services: list[str] | None = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
    doc_types: list[str] | None = ["draw", "impress"]
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import DrawBridge

        page, actual_idx = DrawBridge(ctx.doc).resolve_page(kwargs.get("page"), active_page_index=ctx.active_page_index)

        return {"status": "ok", "page": actual_idx, "tree": build_shape_tree(page)}
