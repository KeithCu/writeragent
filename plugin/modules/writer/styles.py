"""Writer style inspection tools."""

import logging

from plugin.framework.tool_base import ToolBase

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
    intent = "edit"
    description = (
        "List available styles in the document. "
        "Call this before applying styles to discover exact style names."
    )
    parameters = {
        "type": "object",
        "properties": {
            "family": {
                "type": "string",
                "enum": _STYLE_FAMILIES,
                "description": "Style family to list. Default: ParagraphStyles.",
            },
        },
        "required": [],
    }
    doc_types = ["writer"]

    def execute(self, ctx, **kwargs):
        family = kwargs.get("family", "ParagraphStyles")
        doc = ctx.doc

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
    intent = "edit"
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
                "enum": _STYLE_FAMILIES,
                "description": "Style family. Default: ParagraphStyles.",
            },
        },
        "required": ["style_name"],
    }
    doc_types = ["writer"]

    def execute(self, ctx, **kwargs):
        style_name = kwargs.get("style_name", "")
        family = kwargs.get("family", "ParagraphStyles")

        if not style_name:
            return {"status": "error", "message": "style_name is required."}

        doc = ctx.doc
        style_family = self.get_item(
            doc, "getStyleFamilies", family,
            missing_msg="Document does not support style families.",
            not_found_msg="Unknown style family: %s" % family
        )
        if isinstance(style_family, dict):
            return style_family

        if not style_family.hasByName(style_name):
            return {
                "status": "error",
                "message": "Style '%s' not found in %s." % (style_name, family),
            }

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
