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
    bridge = DrawBridge(ctx.doc)
    if is_master_page:
        masters = ctx.doc.getMasterPages()
        if page_index < 0 or page_index >= masters.getCount():
            raise WriterAgentException(
                "invalid_index",
                f"Master page index {page_index} is out of bounds. Must be between 0 and {masters.getCount() - 1}",
            )
        return masters.getByIndex(page_index)
    else:
        pages = bridge.get_pages()
        if page_index < 0 or page_index >= pages.getCount():
            raise WriterAgentException(
                "invalid_index",
                f"Slide index {page_index} is out of bounds. Must be between 0 and {pages.getCount() - 1}",
            )
        return pages.getByIndex(page_index)


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
                "description": "Index of the slide or master page (0-based).",
            },
            "is_master_page": {
                "type": "boolean",
                "description": "If True, get the properties for the master page instead of a normal slide. Defaults to False.",
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
                "description": "Index of the slide or master page (0-based).",
            },
            "is_master_page": {
                "type": "boolean",
                "description": "If True, sets the properties for the master page instead of a normal slide. Defaults to False.",
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
