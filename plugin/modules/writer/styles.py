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

from plugin.framework.tool import ToolBase as FrameworkToolBase
from plugin.modules.writer.base import ToolWriterStyleBase as ToolBase
from plugin.modules.writer.target_resolver import resolve_target_cursor


log = logging.getLogger("writeragent.writer")

_STYLE_FAMILIES = ["ParagraphStyles", "CharacterStyles"]

_KNOWN_CHARACTER_PROPERTIES = {
    "CharColor": {"type": "string", "description": "Main text color (hex string like '#FF0000' or '#0055A4')."},
    "CharBackColor": {"type": "string", "description": "Background/highlight color (hex string)."},
    "CharUnderlineColor": {"type": "string", "description": "Underline color (hex string)."},
    "CharWeight": {"type": "number", "description": "Font weight (e.g., 100 for normal, 150 for bold)."},
    "CharHeight": {"type": "number", "description": "Font size in points."},
    "CharFontName": {"type": "string", "description": "Font family name (e.g., 'Arial')."},
    "CharStrikeout": {"type": "integer", "description": "Strikeout type (0=None, 1=Single, 2=Double)."},
    "CharCaseMap": {"type": "integer", "description": "Case mapping (0=None, 1=Uppercase, 2=Lowercase, 3=Title, 4=SmallCaps)."},
    "CharPosture": {"type": "integer", "description": "Italics/posture (0=None, 1=Italic, 2=Oblique)."},
    "CharShadowed": {"type": "boolean", "description": "Whether text is shadowed."},
    # "CharRelief": {"type": "integer", "description": "Relief style (0=None, 1=Embossed, 2=Engraved)."},
    # "CharHidden": {"type": "boolean", "description": "Whether text is hidden."},
    "CharWordMode": {"type": "boolean", "description": "Whether underline/strikeout applies only to words."}
}

_KNOWN_PARAGRAPH_PROPERTIES = {
    "ParaTopMargin": {"type": "integer", "description": "Top margin in 1/100th mm (1 inch = 2540)."},
    "ParaBottomMargin": {"type": "integer", "description": "Bottom margin in 1/100th mm."},
    "ParaLeftMargin": {"type": "integer", "description": "Left margin in 1/100th mm."},
    "ParaRightMargin": {"type": "integer", "description": "Right margin in 1/100th mm."},
    "ParaFirstLineIndent": {"type": "integer", "description": "First line indent in 1/100th mm."},
    "ParaAdjust": {"type": "integer", "description": "Paragraph alignment (0=Left, 1=Right, 2=Block, 3=Center)."},
    "ParaBackColor": {"type": "string", "description": "Paragraph background color (hex string)."},
    "ParaKeepTogether": {"type": "boolean", "description": "Keep lines of the paragraph together."},
    "ParaSplit": {"type": "boolean", "description": "Whether the paragraph is allowed to split across pages."}
}

# Combine properties for schema use
_ALL_KNOWN_PROPERTIES = {**_KNOWN_CHARACTER_PROPERTIES, **_KNOWN_PARAGRAPH_PROPERTIES}

# Properties to read per style family. Paragraph styles inherit character properties.
_FAMILY_PROPS = {
    "ParagraphStyles": ["ParentStyle", "FollowStyle"] + list(_KNOWN_PARAGRAPH_PROPERTIES.keys()) + list(_KNOWN_CHARACTER_PROPERTIES.keys()),
    "CharacterStyles": ["ParentStyle"] + list(_KNOWN_CHARACTER_PROPERTIES.keys()),
}


def _get_bool_prop(obj, prop_name, default=False):
    """Safely get a boolean property from a UNO object."""
    try:
        return bool(obj.getPropertyValue(prop_name))
    except Exception:
        return default


def _parse_color(val):
    """Parse a web color string (e.g., #FF0000, FF0000) or integer to a UNO color (integer)."""
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        val = val.strip()
        if val.startswith("#"):
            val = val[1:]
        try:
            return int(val, 16)
        except ValueError:
            pass
    return val


class ListStyles(ToolBase):
    """List available styles in a given family."""

    name = "list_styles"
    description = "List available styles in the document. Omit family to list all style family names; set family to list styles in that family."
    parameters = {"type": "object", "properties": {"family": {"type": "string", "enum": ["ParagraphStyles", "CharacterStyles"], "description": ("Style family (ParagraphStyles or CharacterStyles). Default: ParagraphStyles.")}}, "required": []}

    def execute(self, ctx, **kwargs):
        family = kwargs.get("family")
        doc = ctx.doc

        families = doc.getStyleFamilies()
        if not family or not str(family).strip():
            # Only return the families we officially support in this tool.
            available = [f for f in families.getElementNames() if f in _STYLE_FAMILIES]
            return {"status": "ok", "families": available, "count": len(available)}

        family = str(family or "ParagraphStyles").strip()
        style_family = self.get_item(doc, "getStyleFamilies", family, missing_msg="Document does not support style families.", not_found_msg="Unknown style family: %s" % family)
        if isinstance(style_family, dict):
            # To match old behavior returning available_families instead of available
            if "available" in style_family:
                style_family["available_families"] = style_family.pop("available")
            return style_family

        # Always use "auto" filter logic to show used, custom, and common built-in styles.
        styles = []
        element_names = style_family.getElementNames()

        for name in element_names:
            style = style_family.getByName(name)

            # Predicates for language-agnostic filtering
            in_use = style.isInUse()
            user_defined = style.isUserDefined()
            is_physical = _get_bool_prop(style, "IsPhysical", True)
            is_hidden = _get_bool_prop(style, "IsHidden", False)

            # Filter logic (auto)
            if is_hidden:
                continue

            # Core visibility logic:
            show = in_use or user_defined or is_physical

            # 1. Core structural fallback:
            if not show:
                if family == "ParagraphStyles":
                    # Always show Heading 1-5 (CHAPTER category).
                    try:
                        cat = style.getPropertyValue("Category")
                        if cat == 1:  # CHAPTER
                            show = True
                    except Exception:
                        pass
                elif family == "CharacterStyles":
                    # Always show core character styles.
                    core_char_styles = ("Default Style", "Source Text")
                    if name in core_char_styles:
                        show = True

            # 2. Strict "Essential" pruning for the 'auto' list:
            if show and family == "ParagraphStyles":
                try:
                    cat = style.getPropertyValue("Category")

                    # BLOCK List, Index, Extra, and HTML categories unless used/custom.
                    if cat in (2, 3, 4, 5) and not (in_use or user_defined):
                        show = False

                    # BLOCK abstract 'Heading' parent and the 'Standard' base style.
                    elif name in ("Heading", "Standard", "Default Paragraph Style"):
                        show = False

                    # BLOCK deep headings (> 5) unless used/custom.
                    elif cat == 1 and not (in_use or user_defined):
                        try:
                            level = int(name[len("Heading ") :])
                            if level > 5:
                                show = False
                        except (ValueError, TypeError):
                            pass

                    # For Category 0 (TEXT), only show "Core" styles if not used/custom.
                    # This prunes Salutation, Appendix, Marginalia, etc.
                    elif cat == 0 and not (in_use or user_defined):
                        core_text_styles = ("Text body", "Title", "Subtitle")
                        if name not in core_text_styles:
                            show = False
                except Exception:
                    pass

            if not show:
                continue

            entry = {"name": name, "is_user_defined": user_defined, "is_in_use": in_use}
            # Present the UNO "Default Style" as "No Character Style" — the
            # clearer name that matches what the LLM should pass to apply_style.
            if family == "CharacterStyles" and name == "Default Style":
                entry["name"] = "No Character Style"
            try:
                entry["parent_style"] = style.getPropertyValue("ParentStyle")
            except Exception:
                pass
            styles.append(entry)

        return {"status": "ok", "family": family, "styles": styles, "count": len(styles)}


class GetStyleInfo(ToolBase):
    """Get detailed properties of a named style."""

    name = "get_style_info"
    description = "Get detailed properties of a specific style (font, size, margins, etc.)."
    parameters = {"type": "object", "properties": {"style_name": {"type": "string", "description": "Name of the style to inspect."}, "family": {"type": "string", "description": "Style family. Default: ParagraphStyles."}}, "required": ["style_name"]}

    def execute(self, ctx, **kwargs):
        style_name = kwargs.get("style_name", "")
        family = kwargs.get("family", "ParagraphStyles")

        doc = ctx.doc
        style_family = self.get_item(doc, "getStyleFamilies", family, missing_msg="Document does not support style families.", not_found_msg="Unknown style family: %s" % family)
        if isinstance(style_family, dict):
            return style_family

        if not style_family.hasByName(style_name):
            return self._tool_error("Style '%s' not found in %s." % (style_name, family))

        style = style_family.getByName(style_name)
        info = {"name": style_name, "family": family, "is_user_defined": style.isUserDefined(), "is_in_use": style.isInUse()}

        for prop_name in _FAMILY_PROPS.get(family, []):
            try:
                info[prop_name] = style.getPropertyValue(prop_name)
            except Exception:
                pass

        return {"status": "ok", **info}


class ApplyStyle(FrameworkToolBase):
    """Apply a paragraph or character style."""

    name = "apply_style"
    intent = "edit"
    tier = "extended"
    description = (
        "Apply a style to a target. Use family='ParagraphStyles' for paragraph "
        "styles (e.g. Heading 1) or family='CharacterStyles' for character "
        "styles (e.g. Source Text). Use 'No Character Style' "
        "with family='CharacterStyles' to remove a character style. "
        "Use target='selection' (default), 'beginning', 'end', 'full_document', "
        "or 'search' with old_content."
    )
    parameters = {
        "type": "object",
        "properties": {
            "style_name": {"type": "string", "description": "Style name (e.g. Heading 1, Source Text)."},
            "family": {"type": "string", "enum": ["ParagraphStyles", "CharacterStyles"], "description": ("Style family. Default: ParagraphStyles.")},
            "target": {"type": "string", "enum": ["beginning", "end", "selection", "full_document", "search"], "description": "Where to apply the style."},
            "old_content": {"type": "string", "description": "Text to find and apply style to if target = 'search'."},
        },
        "required": ["style_name"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    # Maps family to the UNO property that holds the style name.
    _PROPERTY_MAP = {"ParagraphStyles": "ParaStyleName", "CharacterStyles": "CharStyleName"}

    def execute(self, ctx, **kwargs):
        style_name = (kwargs.get("style_name") or "").strip()
        if not style_name:
            return self._tool_error("style_name is required.")

        family = kwargs.get("family", "ParagraphStyles")
        uno_prop = self._PROPERTY_MAP.get(family)
        if not uno_prop:
            return self._tool_error("Unknown family: %s. Use ParagraphStyles or CharacterStyles." % family)

        # UNO quirk: the default character style is applied by setting
        # CharStyleName to an empty string.
        uno_value = "" if (family == "CharacterStyles" and style_name == "No Character Style") else style_name

        target = kwargs.get("target", "selection")
        old_content = kwargs.get("old_content")

        try:
            cursor = resolve_target_cursor(ctx, target, old_content)
        except ValueError as ve:
            return self._tool_error(str(ve))

        if not cursor:
            return self._tool_error("Failed to resolve target location.")

        try:
            cursor.setPropertyValue(uno_prop, uno_value)
        except Exception as e:
            return self._tool_error("Could not apply style: %s" % e)
        return {"status": "ok", "style_name": style_name, "family": family}


class UpdateStyle(ToolBase):
    """Update properties of an existing paragraph or character style."""

    name = "update_style"
    intent = "edit"
    tier = "extended"
    description = (
        "Update the properties of an existing style. "
        "Provide 'family' (ParagraphStyles or CharacterStyles), 'style_name', and "
        "'property_updates': a dictionary of UNO property names to values "
        "(e.g. {'CharColor': '#FF0000', 'CharWeight': 150}). "
        "Colors can be provided as hex strings or integers."
    )
    parameters = {
        "type": "object",
        "properties": {
            "style_name": {"type": "string", "description": "Name of the style to modify (e.g., 'Heading 1', 'Source Text')."},
            "family": {"type": "string", "enum": ["ParagraphStyles", "CharacterStyles"], "description": "Style family. Default: ParagraphStyles."},
            "property_updates": {
                "type": "object",
                "description": "Dictionary of UNO property names to values (keys are listed in the schema).",
                "properties": _ALL_KNOWN_PROPERTIES,
            },
        },
        "required": ["style_name", "property_updates"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        style_name = kwargs.get("style_name", "").strip()
        if not style_name:
            return self._tool_error("style_name is required.")

        family = kwargs.get("family", "ParagraphStyles")
        property_updates = kwargs.get("property_updates", {})
        if not isinstance(property_updates, dict) or not property_updates:
            return self._tool_error("property_updates must be a non-empty dictionary.")

        doc = ctx.doc
        style_family = self.get_item(doc, "getStyleFamilies", family, missing_msg="Document does not support style families.", not_found_msg="Unknown style family: %s" % family)
        if isinstance(style_family, dict):
            return style_family

        if not style_family.hasByName(style_name):
            return self._tool_error("Style '%s' not found in %s." % (style_name, family))

        style = style_family.getByName(style_name)

        applied = {}
        failed = {}

        for prop_name, prop_val in property_updates.items():
            # Handle color conversions
            if prop_name in ("CharColor", "CharBackColor", "CharUnderlineColor"):
                prop_val = _parse_color(prop_val)

            try:
                style.setPropertyValue(prop_name, prop_val)
                applied[prop_name] = prop_val
            except Exception as e:
                failed[prop_name] = str(e)

        result = {"status": "ok", "style_name": style_name, "family": family}
        if applied:
            result["updated_properties"] = applied
        if failed:
            result["failed_properties"] = failed
            if not applied:
                result["status"] = "error"
                result["message"] = "Failed to apply any properties."

        return result
