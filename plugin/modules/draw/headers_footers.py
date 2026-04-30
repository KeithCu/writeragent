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

import logging
from typing import Any, Dict

from plugin.framework.errors import WriterAgentException
from plugin.framework.tool_context import ToolContext
from plugin.modules.draw.base import ToolDrawHeaderFooterBase
from plugin.modules.draw.bridge import DrawBridge

log = logging.getLogger("writeragent.draw.headers_footers")


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
        is_master_page = bool(kwargs.get("is_master_page", False))

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
        is_master_page = bool(kwargs.get("is_master_page", False))

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
