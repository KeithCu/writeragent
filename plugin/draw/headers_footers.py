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
"""Tools for manipulating headers, footers, page numbers, and dates in presentations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Iterator

from plugin.framework.errors import WriterAgentException, is_disposed_exception

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

from plugin.draw.base import ToolDrawHeaderFooterBase
from plugin.draw.bridge import DrawBridge

log = logging.getLogger("writeragent.draw.headers_footers")


def _coerce_bool_arg(kwargs: dict[str, Any], key: str, default: bool = False) -> bool:
    """Tool args may be real bools or JSON strings (e.g. ``\"true\"``); ``bool(\"false\")`` is wrong."""
    if key not in kwargs:
        return default
    v = kwargs[key]
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(v, (int, float)):
        return v != 0
    return default


def _get_page(ctx: ToolContext, page_index: int, is_master_page: bool) -> Any:
    """Resolve the draw page to read/write header-footer properties.

    When ``is_master_page`` is True, ``page_index`` selects a **slide**; the
    tool targets that slide's assigned ``MasterPage``. Using
    ``getMasterPages().getByIndex(n)`` is wrong here because master collection
    order does not match slide order in Impress.
    """
    bridge = DrawBridge(ctx.doc)
    pages = bridge.get_pages()
    # Pass code keyword-only; the second positional argument was stored as code, inverting the chat payload.
    if page_index < 0 or page_index >= pages.getCount():
        raise WriterAgentException(f"Slide index {page_index} is out of bounds. Must be between 0 and {pages.getCount() - 1}", code="INVALID_INDEX")
    slide = pages.getByIndex(page_index)
    if is_master_page:
        try:
            master = slide.MasterPage
        except Exception as e:
            if is_disposed_exception(e):
                raise
            raise WriterAgentException(f"Could not resolve master page for slide {page_index}: {e}", code="NO_MASTER") from e
        if master is None:
            raise WriterAgentException(f"Slide {page_index} has no master page assigned.", code="NO_MASTER")
        return master
    return slide


# Impress slide masters omit HeaderText/FooterText from XPropertySet (see sd unopage.cxx
# aMasterPagePropertyMap_Impl vs slide draw page map). Header/footer content lives on
# presentation placeholder shapes instead.
_SVC_HEADER = "com.sun.star.presentation.HeaderShape"
_SVC_FOOTER = "com.sun.star.presentation.FooterShape"
_SVC_DATETIME = "com.sun.star.presentation.DateTimeShape"
_SVC_SLIDENO = "com.sun.star.presentation.SlideNumberShape"

# Field table: schema key, UNO property, service, kind
_FIELDS = (
    ("header", "HeaderText", _SVC_HEADER, "text"),
    ("footer", "FooterText", _SVC_FOOTER, "text"),
    ("date_time", "DateTimeText", _SVC_DATETIME, "text"),
    ("header_visible", "IsHeaderVisible", _SVC_HEADER, "visible"),
    ("footer_visible", "IsFooterVisible", _SVC_FOOTER, "visible"),
    ("page_number_visible", "IsPageNumberVisible", _SVC_SLIDENO, "visible"),
    ("date_time_visible", "IsDateTimeVisible", _SVC_DATETIME, "visible"),
    ("date_time_fixed", "IsDateTimeFixed", None, "fixed"),
)


def _doc_is_presentation(doc: Any) -> bool:
    try:
        return bool(doc.supportsService("com.sun.star.presentation.PresentationDocument"))
    except Exception as e:
        if is_disposed_exception(e):
            raise
        return False


def _impress_master_hf_use_shapes(doc: Any, is_master_page: bool) -> bool:
    return _doc_is_presentation(doc) and is_master_page


def _iter_shapes_on_page(page: Any) -> Iterator[Any]:
    try:
        n = int(page.getCount())
    except Exception as e:
        if is_disposed_exception(e):
            raise
        return
    for i in range(n):
        try:
            yield page.getByIndex(i)
        except Exception as e:
            if is_disposed_exception(e):
                raise
            continue


def _collect_hf_shapes(page: Any) -> dict[str, Any]:
    services = [_SVC_HEADER, _SVC_FOOTER, _SVC_DATETIME, _SVC_SLIDENO]
    found: dict[str, Any] = {s: None for s in services}
    shapes = list(_iter_shapes_on_page(page))

    # Walk 1: strict match
    for shape in shapes:
        for svc in services:
            if found[svc] is not None:
                continue
            base = svc.rsplit(".", 1)[-1]
            try:
                st = getattr(shape, "ShapeType", None)
                if st is None and hasattr(shape, "getShapeType"):
                    st = shape.getShapeType()
                if st and (st == svc or str(st).endswith("." + base)):
                    found[svc] = shape
                    continue
                if hasattr(shape, "supportsService") and shape.supportsService(svc):
                    found[svc] = shape
            except Exception as e:
                if is_disposed_exception(e):
                    raise
                continue

    # Walk 2: loose match for services still empty
    for shape in shapes:
        for svc in services:
            if found[svc] is not None:
                continue
            base = svc.rsplit(".", 1)[-1]
            try:
                if not hasattr(shape, "getSupportedServiceNames"):
                    continue
                for name in shape.getSupportedServiceNames():
                    nm = str(name)
                    if nm == svc or nm.endswith("." + base):
                        found[svc] = shape
                        break
            except Exception as e:
                if is_disposed_exception(e):
                    raise
                continue

    return found


def _get_text(shape: Any) -> str:
    # What was wrong: hasattr(shape, "getString") outside try raised UnknownPropertyException on PyUNO objects.
    # How it happened: hasattr was lifted before the try block during cleanup.
    # Why this change: put hasattr inside try; re-raise disposal and return "" on other exceptions.
    if shape is None:
        return ""
    try:
        if not hasattr(shape, "getString"):
            return ""
        return str(shape.getString() or "")
    except Exception as e:
        if is_disposed_exception(e):
            raise
        return ""


def _write(fn: Any, what: str) -> bool:
    try:
        fn()
        return True
    except Exception as e:
        if is_disposed_exception(e):
            raise
        log.debug("%s failed", what, exc_info=True)
        return False


def _set_text(shape: Any, text: str) -> bool:
    # What was wrong: hasattr(shape, "setString") outside try raised UnknownPropertyException on PyUNO objects.
    # How it happened: hasattr was lifted before the try block during cleanup.
    # Why this change: put hasattr inside try; re-raise disposal and return False on other exceptions.
    if shape is None:
        return False
    try:
        if not hasattr(shape, "setString"):
            return False
        return _write(lambda: shape.setString(text), "setString on header/footer shape")
    except Exception as e:
        if is_disposed_exception(e):
            raise
        return False


def _set_visible(shape: Any, vis: bool) -> bool:
    # What was wrong: hasattr(shape, "setPropertyValue") outside try raised UnknownPropertyException on PyUNO objects.
    # How it happened: hasattr was lifted before the try block during cleanup.
    # Why this change: put hasattr inside try; re-raise disposal and return False on other exceptions.
    if shape is None:
        return False
    try:
        if not hasattr(shape, "setPropertyValue"):
            return False
        return _write(lambda: shape.setPropertyValue("Visible", vis), "set Visible on header/footer shape")
    except Exception as e:
        if is_disposed_exception(e):
            raise
        return False


def _get_bool(shape: Any, name: str, *, fallback_attr: bool = False) -> bool:
    # What was wrong: hasattr(shape, "getPropertyValue") outside try raised UnknownPropertyException on PyUNO objects.
    # How it happened: hasattr was lifted before the try block during cleanup.
    # Why this change: put hasattr inside try; re-raise disposal and fall back/return False on other exceptions.
    if shape is None:
        return False
    try:
        if hasattr(shape, "getPropertyValue"):
            return bool(shape.getPropertyValue(name))
    except Exception as e:
        if is_disposed_exception(e):
            raise
        if not fallback_attr:
            return False
    try:
        return bool(getattr(shape, name, False))
    except Exception as e:
        if is_disposed_exception(e):
            raise
        return False


def _read_impress_master_hf_shapes(page: Any, out: Dict[str, Any]) -> None:
    shapes = _collect_hf_shapes(page)
    for _schema_key, uno_prop, svc, kind in _FIELDS:
        if kind == "text" and svc:
            out[uno_prop] = _get_text(shapes.get(svc))
        elif kind == "visible" and svc:
            out[uno_prop] = _get_bool(shapes.get(svc), "Visible", fallback_attr=False)
        elif kind == "fixed":
            dt_shape = shapes.get(_SVC_DATETIME)
            out[uno_prop] = _get_bool(dt_shape, "IsFixed", fallback_attr=True)
    try:
        out["DateTimeFormat"] = page.getPropertyValue("DateTimeFormat")
    except Exception as e:
        if is_disposed_exception(e):
            raise
        out["DateTimeFormat"] = 0


def _write_impress_master_hf_shapes(page: Any, kwargs: Dict[str, Any]) -> int:
    shapes = _collect_hf_shapes(page)
    h = shapes.get(_SVC_HEADER)
    f = shapes.get(_SVC_FOOTER)

    if "footer" in kwargs and "footer_visible" not in kwargs:
        _set_visible(f, True)
    if "header" in kwargs and "header_visible" not in kwargs:
        _set_visible(h, True)

    updated = 0
    for schema_key, _uno_prop, svc, kind in _FIELDS:
        if schema_key not in kwargs or kind == "fixed" or not svc:
            continue
        shape = shapes.get(svc)
        if kind == "text":
            if _set_text(shape, str(kwargs[schema_key])):
                updated += 1
        elif kind == "visible":
            if _set_visible(shape, _coerce_bool_arg(kwargs, schema_key, False)):
                updated += 1
    return updated


class GetHeadersFooters(ToolDrawHeaderFooterBase):
    """Tool for reading header and footer properties of a presentation slide or master page."""

    name: str | None = "get_headers_footers"
    description: str = "Retrieves header, footer, date/time, and slide number configuration for a specific slide or master page in a presentation."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": ("0-based slide index. When is_master_page is false, reads that slide. When true, reads the master page assigned to that slide (not the master list index).")},
            "is_master_page": {"type": "boolean", "description": "If True, read the master page linked to the slide at page. Defaults to False."},
        },
        "required": ["page"],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> Dict[str, Any]:
        # type/required checks are done by the tool schema in ToolRegistry
        page_index = int(kwargs["page"])
        is_master_page = _coerce_bool_arg(kwargs, "is_master_page", False)

        page = _get_page(ctx, page_index, is_master_page)

        result: Dict[str, Any] = {"status": "ok", "page": page_index, "is_master_page": is_master_page, "properties": {}}

        props = result["properties"]
        if _impress_master_hf_use_shapes(ctx.doc, is_master_page):
            _read_impress_master_hf_shapes(page, props)
        else:
            props_to_fetch = [uno_prop for _, uno_prop, _, _ in _FIELDS] + ["DateTimeFormat"]
            for prop_name in props_to_fetch:
                try:
                    props[prop_name] = page.getPropertyValue(prop_name)
                except Exception as e:
                    if is_disposed_exception(e):
                        raise
                    log.debug("Could not read property %s: %s", prop_name, e)

        return result


class SetHeadersFooters(ToolDrawHeaderFooterBase):
    """Tool for updating header and footer properties of a presentation slide or master page."""

    name: str | None = "set_headers_footers"
    description: str = "Updates header, footer, date/time, and slide number configuration for a specific slide or master page in a presentation."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": ("0-based slide index. When is_master_page is false, updates that slide. When true, updates the master page assigned to that slide.")},
            "is_master_page": {"type": "boolean", "description": "If True, update the master page linked to the slide at page. Defaults to False."},
            "header": {"type": "string", "description": "Header text. Not available on a normal slide (notes page and handout master only). On an Impress master, written to the header shape."},
            "footer": {"type": "string", "description": "The text for the footer."},
            "date_time": {"type": "string", "description": "The fixed date/time text."},
            "header_visible": {"type": "boolean", "description": "Whether the header is visible."},
            "footer_visible": {"type": "boolean", "description": "Whether the footer is visible."},
            "page_number_visible": {"type": "boolean", "description": "Whether the slide number is visible."},
            "date_time_visible": {"type": "boolean", "description": "Whether the date/time is visible."},
            "date_time_fixed": {"type": "boolean", "description": "If True, uses 'date_time'. If False, LibreOffice automatically updates it. Slide property only; a master DateTimeShape has no fixed-date flag."},
        },
        "required": ["page"],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> Dict[str, Any]:
        # type/required checks are done by the tool schema in ToolRegistry
        page_index = int(kwargs["page"])
        is_master_page = _coerce_bool_arg(kwargs, "is_master_page", False)

        page = _get_page(ctx, page_index, is_master_page)

        if _impress_master_hf_use_shapes(ctx.doc, is_master_page):
            if "date_time_fixed" in kwargs:
                # DateTimeShape has no IsFixed. IsDateTimeFixed is a page
                # property, and a normal master does not publish it
                # (unoobj.cxx IMPRESS_MAP_ENTRIES). Writing it used to fail
                # and still return status ok.
                return self._tool_error("date_time_fixed is not available on a master page. Set it on the slide.")
            updated_count = _write_impress_master_hf_shapes(page, kwargs)
            requested = sum(1 for key, _, _, kind in _FIELDS if kind != "fixed" and key in kwargs)
            if requested and updated_count < requested:
                return self._tool_error("Could not update every requested master header/footer field.", updated_properties=updated_count)
        else:
            if not is_master_page and ("header" in kwargs or "header_visible" in kwargs):
                # A normal slide's property map has FooterText / DateTimeText
                # and no HeaderText (unopage.cxx aDrawPagePropertyMap_Impl).
                # HeaderText is on notes pages and the handout master.
                return self._tool_error("header is not a property of a normal slide. Notes pages and the handout master expose it.")
            # Draw / non-Impress masters: UNO exposes HeaderText/FooterText on the page.
            if is_master_page:
                if "footer" in kwargs and "footer_visible" not in kwargs:
                    try:
                        page.setPropertyValue("IsFooterVisible", True)
                    except Exception as e:
                        if is_disposed_exception(e):
                            raise
                        log.debug("Could not enable IsFooterVisible on master: %s", e)
                if "header" in kwargs and "header_visible" not in kwargs:
                    try:
                        page.setPropertyValue("IsHeaderVisible", True)
                    except Exception as e:
                        if is_disposed_exception(e):
                            raise
                        log.debug("Could not enable IsHeaderVisible on master: %s", e)

            updated_count = 0
            failed: list[str] = []
            for schema_key, prop_name, _, _ in _FIELDS:
                if schema_key in kwargs:
                    val = _coerce_bool_arg(kwargs, schema_key, False) if schema_key.endswith("_visible") or schema_key.endswith("_fixed") else kwargs[schema_key]
                    try:
                        page.setPropertyValue(prop_name, val)
                        updated_count += 1
                    except Exception as e:
                        if is_disposed_exception(e):
                            raise
                        log.debug("Could not set property %s: %s", prop_name, e)
                        failed.append(prop_name)
            if failed:
                return self._tool_error("Could not set %s." % ", ".join(failed), updated_properties=updated_count)

        return {"status": "ok", "updated_properties": updated_count, "message": f"Successfully updated {updated_count} header/footer properties on {'master page' if is_master_page else 'slide'} {page_index}."}
