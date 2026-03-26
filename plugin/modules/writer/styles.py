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
"""Writer style inspection tools."""

import logging

from plugin.framework.tool_base import ToolBase as FrameworkToolBase
from plugin.modules.writer.base import ToolWriterStyleBase as ToolBase

log = logging.getLogger("writeragent.writer")

_STYLE_FAMILIES = [
    "ParagraphStyles",
    "CharacterStyles",
    "PageStyles",
    "FrameStyles",
    "NumberingStyles",
]

# Properties to read per style family.
_FAMILY_PROPS = {
    "ParagraphStyles": [
        "ParentStyle", "FollowStyle",
        "CharFontName", "CharHeight", "CharWeight",
        "ParaAdjust", "ParaTopMargin", "ParaBottomMargin",
    ],
    "CharacterStyles": [
        "ParentStyle", "CharFontName", "CharHeight",
        "CharWeight", "CharPosture", "CharColor",
    ],
}


class ListStyles(ToolBase):
    """List available styles in a given family."""

    name = "list_styles"
    description = (
        "List available styles in the document. "
        "Omit family to list all style family names; set family to list styles in that family."
    )
    parameters = {
        "type": "object",
        "properties": {
            "family": {
                "type": "string",
                "description": (
                    "Style family (ParagraphStyles, CharacterStyles, PageStyles, "
                    "FrameStyles, NumberingStyles). Omit to list family names only."
                ),
            },
        },
        "required": [],
    }

    def execute(self, ctx, **kwargs):
        family = kwargs.get("family")
        doc = ctx.doc

        families = doc.getStyleFamilies()
        if not family or not str(family).strip():
            available = list(families.getElementNames())
            return {
                "status": "ok",
                "families": available,
                "count": len(available),
            }

        family = str(family).strip()
        style_family = self.get_item(
            doc, "getStyleFamilies", family,
            missing_msg="Document does not support style families.",
            not_found_msg="Unknown style family: %s" % family
        )
        if isinstance(style_family, dict):
            # To match old behavior returning available_families instead of available
            if "available" in style_family:
                style_family["available_families"] = style_family.pop("available")
            return style_family

        styles = []
        for name in style_family.getElementNames():
            style = style_family.getByName(name)
            entry = {
                "name": name,
                "is_user_defined": style.isUserDefined(),
                "is_in_use": style.isInUse(),
            }
            try:
                entry["parent_style"] = style.getPropertyValue("ParentStyle")
            except Exception:
                pass
            styles.append(entry)

        return {
            "status": "ok",
            "family": family,
            "styles": styles,
            "count": len(styles),
        }


class GetStyleInfo(ToolBase):
    """Get detailed properties of a named style."""

    name = "get_style_info"
    description = (
        "Get detailed properties of a specific style "
        "(font, size, margins, etc.)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "style_name": {
                "type": "string",
                "description": "Name of the style to inspect.",
            },
            "family": {
                "type": "string",
                "description": "Style family. Default: ParagraphStyles.",
            },
        },
        "required": ["style_name"],
    }

    def execute(self, ctx, **kwargs):
        style_name = kwargs.get("style_name", "")
        family = kwargs.get("family", "ParagraphStyles")

        doc = ctx.doc
        style_family = self.get_item(
            doc, "getStyleFamilies", family,
            missing_msg="Document does not support style families.",
            not_found_msg="Unknown style family: %s" % family
        )
        if isinstance(style_family, dict):
            return style_family

        if not style_family.hasByName(style_name):
            return self._tool_error("Style '%s' not found in %s." % (style_name, family))

        style = style_family.getByName(style_name)
        info = {
            "name": style_name,
            "family": family,
            "is_user_defined": style.isUserDefined(),
            "is_in_use": style.isInUse(),
        }
        for prop_name in _FAMILY_PROPS.get(family, []):
            try:
                info[prop_name] = style.getPropertyValue(prop_name)
            except Exception:
                pass

        return {"status": "ok", **info}


class StylesApply(FrameworkToolBase):
    """Apply a paragraph style."""

    name = "styles_apply"
    intent = "edit"
    tier = "extended"
    description = (
        "Apply a paragraph style name to a specific target. "
        "Use target='beginning', 'end', or 'selection' to apply to those positions. "
        "Use target='search' with old_content to apply to the found text. "
        "For a full style list, use delegate_to_specialized_writer_toolset(domain=styles) "
        "or discover names from the document / Styles sidebar."
    )
    parameters = {
        "type": "object",
        "properties": {
            "style_name": {
                "type": "string",
                "description": "Paragraph style name (e.g. Heading 1).",
            },
            "target": {
                "type": "string",
                "enum": ["beginning", "end", "selection", "full_document", "search"],
                "description": "Where to apply the style.",
            },
            "old_content": {
                "type": "string",
                "description": "Text to find and apply style to if target = 'search'.",
            },
        },
        "required": ["style_name"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        style_name = (kwargs.get("style_name") or "").strip()
        if not style_name:
            return self._tool_error("style_name is required.")

        target = kwargs.get("target", "selection")
        old_content = kwargs.get("old_content")

        from plugin.modules.writer.target_resolver import resolve_target_cursor
        try:
            cursor = resolve_target_cursor(ctx, target, old_content)
        except ValueError as ve:
            return self._tool_error(str(ve))

        if not cursor:
            return self._tool_error("Failed to resolve target location.")

        try:
            cursor.setPropertyValue("ParaStyleName", style_name)
        except Exception as e:
            return self._tool_error(
                "Could not apply style (select text or a paragraph): %s" % e
            )
        return {"status": "ok", "style_name": style_name}
