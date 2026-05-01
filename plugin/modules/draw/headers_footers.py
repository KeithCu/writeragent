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
from typing import TYPE_CHECKING, Any, Dict

from plugin.framework.errors import WriterAgentException

if TYPE_CHECKING:
    from plugin.framework.tool_context import ToolContext

from plugin.modules.draw.base import ToolDrawHeaderFooterBase
from plugin.modules.draw.bridge import DrawBridge

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
    if page_index < 0 or page_index >= pages.getCount():
        raise WriterAgentException(
            "invalid_index",
            f"Slide index {page_index} is out of bounds. Must be between 0 and {pages.getCount() - 1}",
        )
    slide = pages.getByIndex(page_index)
    if is_master_page:
        try:
            master = slide.MasterPage
        except Exception as e:
            raise WriterAgentException(
                "no_master",
                f"Could not resolve master page for slide {page_index}: {e}",
            ) from e
        if master is None:
            raise WriterAgentException(
                "no_master",
                f"Slide {page_index} has no master page assigned.",
            )
        return master
    return slide


# Impress slide masters omit HeaderText/FooterText from XPropertySet (see sd unopage.cxx
# aMasterPagePropertyMap_Impl vs slide draw page map). Header/footer content lives on
# presentation placeholder shapes instead.
_SVC_HEADER = "com.sun.star.presentation.HeaderShape"
_SVC_FOOTER = "com.sun.star.presentation.FooterShape"
_SVC_DATETIME = "com.sun.star.presentation.DateTimeShape"
_SVC_SLIDENO = "com.sun.star.presentation.SlideNumberShape"


def _doc_is_presentation(doc: Any) -> bool:
    try:
        return bool(doc.supportsService("com.sun.star.presentation.PresentationDocument"))
    except Exception:
        return False


def _impress_master_hf_use_shapes(doc: Any, is_master_page: bool) -> bool:
    return _doc_is_presentation(doc) and is_master_page


def _iter_shapes_on_page(page: Any):
    try:
        n = int(page.getCount())
    except Exception:
        return
    for i in range(n):
        try:
            yield page.getByIndex(i)
        except Exception:
            continue


def _find_shape_on_page(page: Any, service_name: str) -> Any:
    for shape in _iter_shapes_on_page(page):
        try:
            if hasattr(shape, "supportsService") and shape.supportsService(service_name):
                return shape
        except Exception:
            continue
    base = service_name.rsplit(".", 1)[-1]
    for shape in _iter_shapes_on_page(page):
        try:
            if not hasattr(shape, "getSupportedServiceNames"):
                continue
            names = shape.getSupportedServiceNames()
            for name in names:
                nm = str(name)
                if nm == service_name or nm.endswith("." + base):
                    return shape
        except Exception:
            continue
    return None


def _shape_get_string(shape: Any) -> str:
    if shape is None:
        return ""
    try:
        if hasattr(shape, "getString"):
            return str(shape.getString() or "")
    except Exception:
        pass
    return ""


def _shape_set_string(shape: Any, text: str) -> bool:
    if shape is None:
        return False
    try:
        if hasattr(shape, "setString"):
            shape.setString(text)
            return True
    except Exception:
        log.debug("setString on header/footer shape failed", exc_info=True)
    return False


def _shape_get_visible(shape: Any) -> bool:
    if shape is None:
        return False
    try:
        if hasattr(shape, "getPropertyValue"):
            return bool(shape.getPropertyValue("Visible"))
        return bool(getattr(shape, "Visible", False))
    except Exception:
        return False


def _shape_set_visible(shape: Any, vis: bool) -> bool:
    if shape is None:
        return False
    try:
        if hasattr(shape, "setPropertyValue"):
            shape.setPropertyValue("Visible", vis)
            return True
    except Exception:
        log.debug("set Visible on header/footer shape failed", exc_info=True)
    return False


def _shape_get_datetime_fixed(shape: Any) -> bool:
    if shape is None:
        return False
    try:
        if hasattr(shape, "getPropertyValue"):
            return bool(shape.getPropertyValue("IsFixed"))
    except Exception:
        pass
    try:
        return bool(getattr(shape, "IsFixed", False))
    except Exception:
        return False


def _shape_set_datetime_fixed(shape: Any, fixed: bool) -> bool:
    if shape is None:
        return False
    try:
        if hasattr(shape, "setPropertyValue"):
            shape.setPropertyValue("IsFixed", fixed)
            return True
    except Exception:
        return False
    return False


def _read_impress_master_hf_shapes(page: Any, out: Dict[str, Any]) -> None:
    h = _find_shape_on_page(page, _SVC_HEADER)
    f = _find_shape_on_page(page, _SVC_FOOTER)
    d = _find_shape_on_page(page, _SVC_DATETIME)
    s = _find_shape_on_page(page, _SVC_SLIDENO)
    out["HeaderText"] = _shape_get_string(h)
    out["FooterText"] = _shape_get_string(f)
    out["DateTimeText"] = _shape_get_string(d)
    out["IsHeaderVisible"] = _shape_get_visible(h)
    out["IsFooterVisible"] = _shape_get_visible(f)
    out["IsDateTimeVisible"] = _shape_get_visible(d)
    out["IsPageNumberVisible"] = _shape_get_visible(s)
    out["IsDateTimeFixed"] = _shape_get_datetime_fixed(d)
    try:
        out["DateTimeFormat"] = page.getPropertyValue("DateTimeFormat")
    except Exception:
        out["DateTimeFormat"] = 0


def _write_impress_master_hf_shapes(page: Any, kwargs: Dict[str, Any]) -> int:
    f = _find_shape_on_page(page, _SVC_FOOTER)
    h = _find_shape_on_page(page, _SVC_HEADER)
    d = _find_shape_on_page(page, _SVC_DATETIME)
    s = _find_shape_on_page(page, _SVC_SLIDENO)

    if "footer_text" in kwargs and "is_footer_visible" not in kwargs:
        _shape_set_visible(f, True)
    if "header_text" in kwargs and "is_header_visible" not in kwargs:
        _shape_set_visible(h, True)

    updated = 0
    if "footer_text" in kwargs and _shape_set_string(f, str(kwargs["footer_text"])):
        updated += 1
    if "header_text" in kwargs and _shape_set_string(h, str(kwargs["header_text"])):
        updated += 1
    if "date_time_text" in kwargs and _shape_set_string(d, str(kwargs["date_time_text"])):
        updated += 1
    if "is_footer_visible" in kwargs and _shape_set_visible(f, _coerce_bool_arg(kwargs, "is_footer_visible", False)):
        updated += 1
    if "is_header_visible" in kwargs and _shape_set_visible(h, _coerce_bool_arg(kwargs, "is_header_visible", False)):
        updated += 1
    if "is_date_time_visible" in kwargs and _shape_set_visible(
        d, _coerce_bool_arg(kwargs, "is_date_time_visible", False)
    ):
        updated += 1
    if "is_page_number_visible" in kwargs and _shape_set_visible(
        s, _coerce_bool_arg(kwargs, "is_page_number_visible", False)
    ):
        updated += 1
    if "is_date_time_fixed" in kwargs and _shape_set_datetime_fixed(
        d, _coerce_bool_arg(kwargs, "is_date_time_fixed", False)
    ):
        updated += 1
    return updated


class GetHeadersFooters(ToolDrawHeaderFooterBase):
    """Tool for reading header and footer properties of a presentation slide or master page."""

    name = "get_headers_footers"
    description = (
        "Retrieves header, footer, date/time, and slide number configuration "
        "for a specific slide or master page in a presentation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": (
                    "0-based slide index. When is_master_page is false, reads that slide. "
                    "When true, reads the master page assigned to that slide (not the master list index)."
                ),
            },
            "is_master_page": {
                "type": "boolean",
                "description": "If True, read the master page linked to the slide at page_index. Defaults to False.",
            },
        },
        "required": ["page_index"],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> Dict[str, Any]:
        page_index = int(kwargs["page_index"])
        is_master_page = _coerce_bool_arg(kwargs, "is_master_page", False)

        page = _get_page(ctx, page_index, is_master_page)

        result: Dict[str, Any] = {
            "status": "ok",
            "page_index": page_index,
            "is_master_page": is_master_page,
            "properties": {}
        }

        props_to_fetch = [
            "HeaderText",
            "FooterText",
            "DateTimeText",
            "IsHeaderVisible",
            "IsFooterVisible",
            "IsPageNumberVisible",
            "IsDateTimeVisible",
            "IsDateTimeFixed",
            "DateTimeFormat",
        ]

        props = result["properties"]
        if _impress_master_hf_use_shapes(ctx.doc, is_master_page):
            _read_impress_master_hf_shapes(page, props)
        else:
            for prop_name in props_to_fetch:
                try:
                    props[prop_name] = page.getPropertyValue(prop_name)
                except Exception as e:
                    log.debug("Could not read property %s: %s", prop_name, e)

        return result


class SetHeadersFooters(ToolDrawHeaderFooterBase):
    """Tool for updating header and footer properties of a presentation slide or master page."""

    name = "set_headers_footers"
    description = (
        "Updates header, footer, date/time, and slide number configuration "
        "for a specific slide or master page in a presentation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": (
                    "0-based slide index. When is_master_page is false, updates that slide. "
                    "When true, updates the master page assigned to that slide."
                ),
            },
            "is_master_page": {
                "type": "boolean",
                "description": "If True, update the master page linked to the slide at page_index. Defaults to False.",
            },
            "header_text": {
                "type": "string",
                "description": "The text for the header.",
            },
            "footer_text": {
                "type": "string",
                "description": "The text for the footer.",
            },
            "date_time_text": {
                "type": "string",
                "description": "The fixed date/time text.",
            },
            "is_header_visible": {
                "type": "boolean",
                "description": "Whether the header is visible.",
            },
            "is_footer_visible": {
                "type": "boolean",
                "description": "Whether the footer is visible.",
            },
            "is_page_number_visible": {
                "type": "boolean",
                "description": "Whether the slide number is visible.",
            },
            "is_date_time_visible": {
                "type": "boolean",
                "description": "Whether the date/time is visible.",
            },
            "is_date_time_fixed": {
                "type": "boolean",
                "description": "If True, uses 'date_time_text'. If False, LibreOffice automatically updates it.",
            },
        },
        "required": ["page_index"],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> Dict[str, Any]:
        page_index = int(kwargs["page_index"])
        is_master_page = _coerce_bool_arg(kwargs, "is_master_page", False)

        page = _get_page(ctx, page_index, is_master_page)

        prop_map = {
            "header_text": "HeaderText",
            "footer_text": "FooterText",
            "date_time_text": "DateTimeText",
            "is_header_visible": "IsHeaderVisible",
            "is_footer_visible": "IsFooterVisible",
            "is_page_number_visible": "IsPageNumberVisible",
            "is_date_time_visible": "IsDateTimeVisible",
            "is_date_time_fixed": "IsDateTimeFixed",
        }

        if _impress_master_hf_use_shapes(ctx.doc, is_master_page):
            updated_count = _write_impress_master_hf_shapes(page, kwargs)
        else:
            # Draw / non-Impress masters: UNO exposes HeaderText/FooterText on the page.
            if is_master_page:
                if "footer_text" in kwargs and "is_footer_visible" not in kwargs:
                    try:
                        page.setPropertyValue("IsFooterVisible", True)
                    except Exception as e:
                        log.debug("Could not enable IsFooterVisible on master: %s", e)
                if "header_text" in kwargs and "is_header_visible" not in kwargs:
                    try:
                        page.setPropertyValue("IsHeaderVisible", True)
                    except Exception as e:
                        log.debug("Could not enable IsHeaderVisible on master: %s", e)

            updated_count = 0
            for kwarg_key, prop_name in prop_map.items():
                if kwarg_key in kwargs:
                    val = kwargs[kwarg_key]
                    try:
                        page.setPropertyValue(prop_name, val)
                        updated_count += 1
                    except Exception as e:
                        log.debug("Could not set property %s: %s", prop_name, e)

        return {
            "status": "ok",
            "updated_properties": updated_count,
            "message": f"Successfully updated {updated_count} header/footer properties on {'master page' if is_master_page else 'slide'} {page_index}."
        }
