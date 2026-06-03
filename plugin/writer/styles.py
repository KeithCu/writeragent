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
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import uno
    from com.sun.star.beans import PropertyValue, NamedValue
else:
    try:
        import uno
        from com.sun.star.beans import PropertyValue, NamedValue
    except ImportError:
        # Mocks for testing outside LO
        class _UnoMock:
            @staticmethod
            def systemPathToFileUrl(path: str) -> str:
                return path
        uno = _UnoMock()

        class PropertyValue:
            def __init__(self, Name: Any = None, Value: Any = None, **kwargs: Any):
                self.Name = Name
                self.Value = Value

        class NamedValue:
            def __init__(self, Name: Any = None, Value: Any = None, **kwargs: Any):
                self.Name = Name
                self.Value = Value

from plugin.framework.tool import ToolBase as FrameworkToolBase
from .specialized_base import ToolWriterStyleBase
from .target_resolver import resolve_target_cursor


log = logging.getLogger("writeragent.writer")

_STYLE_FAMILIES = ["ParagraphStyles", "CharacterStyles"]

_CONDITIONAL_CONTEXTS = [
    "TableHeader", "Table", "Frame", "Section", "Footnote", "Endnote",
    "Header", "Footer", "OutlineLevel1", "OutlineLevel2", "OutlineLevel3",
    "OutlineLevel4", "OutlineLevel5", "OutlineLevel6", "OutlineLevel7",
    "OutlineLevel8", "OutlineLevel9", "OutlineLevel10",
    "NumberingLevel1", "NumberingLevel2", "NumberingLevel3", "NumberingLevel4",
    "NumberingLevel5", "NumberingLevel6", "NumberingLevel7", "NumberingLevel8",
    "NumberingLevel9", "NumberingLevel10"
]

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
    "CharHidden": {"type": "boolean", "description": "Whether text is hidden."},
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


class ListStyles(ToolWriterStyleBase):
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
                entry["parent_style"] = style.getParentStyle()
            except Exception:
                pass
            styles.append(entry)

        return {"status": "ok", "family": family, "styles": styles, "count": len(styles)}


class GetStyleInfo(ToolWriterStyleBase):
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
            if prop_name == "ParentStyle":
                try:
                    info["ParentStyle"] = style.getParentStyle()
                except Exception:
                    pass
            else:
                try:
                    info[prop_name] = style.getPropertyValue(prop_name)
                except Exception:
                    pass

        return {"status": "ok", **info}


def _capture_direct_char_overrides(doc, cursor):
    """Capture DIRECT character formatting in *cursor*'s range so it can survive a
    paragraph-style change.

    LibreOffice resets direct character formatting to the paragraph style's
    defaults when ParaStyleName is set.
    ``getPropertyState`` is unreliable at the text-portion level (it reports
    DEFAULT_VALUE even for hard-set values), so we detect overrides by VALUE:
    a Char* property whose value differs from the paragraph's *current* style
    default is a direct override worth preserving.

    KNOWN LIMITATION: because detection is value-vs-style-default (not a true
    "was set directly" check), a direct override whose value *equals* the current
    style's default is NOT captured. So applying a new style whose default differs
    can make that property visibly change (e.g. text directly set to normal weight,
    equal to Standard's default, becomes bold when Heading 1 is applied). This is a
    rare edge; the common case (override visibly differs from the style) is covered.

    Returns a list of ``(text_cursor, {prop_name: value})`` for
    :func:`_restore_char_overrides`.
    """
    overrides = []
    try:
        para_styles = doc.getStyleFamilies().getByName("ParagraphStyles")
    except Exception:
        para_styles = None
    try:
        READONLY = uno.getConstantByName("com.sun.star.beans.PropertyAttribute.READONLY")
    except Exception:
        READONLY = 0
    try:
        para_enum = cursor.createEnumeration()
    except Exception:
        return overrides
    # `is True` (not mere truthiness): real PyUNO returns a Python bool, so this
    # iterates correctly while exiting immediately for mocked / non-UNO objects
    # (whose hasMoreElements() returns a truthy stub) instead of looping forever.
    # The counters also cap pathological documents.
    _paras = 0
    while para_enum.hasMoreElements() is True and _paras < 200000:
        _paras += 1
        try:
            para = para_enum.nextElement()
        except Exception:
            break
        if not (hasattr(para, "supportsService") and para.supportsService("com.sun.star.text.Paragraph")):
            continue
        old_style = None
        if para_styles is not None:
            try:
                old_style = para_styles.getByName(para.getPropertyValue("ParaStyleName"))
            except Exception:
                old_style = None
        try:
            portion_enum = para.createEnumeration()
        except Exception:
            continue
        _portions = 0
        while portion_enum.hasMoreElements() is True and _portions < 50000:
            _portions += 1
            try:
                portion = portion_enum.nextElement()
                if portion.getPropertyValue("TextPortionType") != "Text":
                    continue
                portion_props = portion.getPropertySetInfo().getProperties()
            except Exception:
                continue
            props = {}
            for p in portion_props:
                name = p.Name
                if not name.startswith("Char"):
                    continue
                if READONLY and (p.Attributes & READONLY):
                    continue
                try:
                    val = portion.getPropertyValue(name)
                except Exception:
                    continue
                if old_style is not None:
                    try:
                        if val == old_style.getPropertyValue(name):
                            continue  # equals style default -> inherited, not a direct override
                    except Exception:
                        continue  # style lacks this property -> cannot classify, skip
                props[name] = val
            if props:
                try:
                    pc = portion.getText().createTextCursorByRange(portion.getStart())
                    pc.gotoRange(portion.getEnd(), True)
                except Exception:
                    continue
                overrides.append((pc, props))
    return overrides


def _restore_char_overrides(overrides):
    """Reapply captured direct character overrides (see :func:`_capture_direct_char_overrides`)."""
    for pc, props in overrides:
        for name, val in props.items():
            try:
                pc.setPropertyValue(name, val)
            except Exception:
                pass


def _expand_to_full_paragraphs(cursor):
    """Return a cursor spanning the FULL paragraph(s) that *cursor* touches.

    A paragraph style resets DIRECT character formatting across the FULL
    paragraph, but a ``target='search'`` cursor may cover only a sub-range of it
    (the matched substring). Capturing over the expanded span keeps direct
    formatting that lives OUTSIDE the match in the same paragraph. Returns None
    if the text does not support paragraph cursors (caller falls back to the
    original cursor).
    """
    try:
        text = cursor.getText()
        start = text.createTextCursorByRange(cursor.getStart())
        end = text.createTextCursorByRange(cursor.getEnd())
        # XParagraphCursor: snap to the enclosing paragraph boundaries.
        start.gotoStartOfParagraph(False)
        end.gotoEndOfParagraph(True)
        expanded = text.createTextCursorByRange(start.getStart())
        expanded.gotoRange(end.getEnd(), True)
        return expanded
    except Exception:
        return None


class ApplyStyle(FrameworkToolBase):
    """Apply a paragraph or character style."""

    name = "apply_style"
    intent = "edit"
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
            if family == "ParagraphStyles":
                # Applying a paragraph style wipes DIRECT character formatting; capture
                # it first and restore it after, so e.g. a hard-set color/bold survives.
                # The reset affects the WHOLE paragraph, so capture over the full
                # paragraph span (not just a sub-range search match) — otherwise direct
                # formatting outside the matched text would be lost.
                capture_cursor = _expand_to_full_paragraphs(cursor) or cursor
                overrides = _capture_direct_char_overrides(ctx.doc, capture_cursor)
                cursor.setPropertyValue(uno_prop, uno_value)
                _restore_char_overrides(overrides)
            else:
                cursor.setPropertyValue(uno_prop, uno_value)
        except Exception as e:
            return self._tool_error("Could not apply style: %s" % e)
        return {"status": "ok", "style_name": style_name, "family": family}


class UpdateStyle(ToolWriterStyleBase):
    """Update properties of an existing paragraph or character style."""

    name = "update_style"
    intent = "edit"
    description = (
        "Update the properties of an existing style. "
        "Provide 'family' (ParagraphStyles or CharacterStyles), 'style_name', and "
        "'property_updates': a dictionary of UNO property names to values "
        "(e.g. {'CharColor': '#FF0000', 'CharWeight': 150}). "
        "Colors can be provided as hex strings or integers. "
        "You can also update the 'parent_style' separately."
    )
    parameters = {
        "type": "object",
        "properties": {
            "style_name": {"type": "string", "description": "Name of the style to modify (e.g., 'Heading 1', 'Source Text')."},
            "family": {"type": "string", "enum": ["ParagraphStyles", "CharacterStyles"], "description": "Style family. Default: ParagraphStyles."},
            "parent_style": {"type": "string", "description": "Name of the style to inherit from."},
            "property_updates": {
                "type": "object",
                "description": "Dictionary of UNO property names to values (keys are listed in the schema).",
                "properties": _ALL_KNOWN_PROPERTIES,
            },
        },
        "required": ["style_name"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        style_name = kwargs.get("style_name", "").strip()
        if not style_name:
            return self._tool_error("style_name is required.")

        family = kwargs.get("family", "ParagraphStyles")
        parent_style = kwargs.get("parent_style")
        property_updates = kwargs.get("property_updates", {})

        doc = ctx.doc
        style_family = self.get_item(doc, "getStyleFamilies", family, missing_msg="Document does not support style families.", not_found_msg="Unknown style family: %s" % family)
        if isinstance(style_family, dict):
            return style_family

        if not style_family.hasByName(style_name):
            return self._tool_error("Style '%s' not found in %s." % (style_name, family))

        style = style_family.getByName(style_name)

        applied = {}
        failed = {}

        if parent_style is not None:
            try:
                style.setParentStyle(parent_style)
                applied["ParentStyle"] = parent_style
            except Exception as e:
                log.warning("Failed to set ParentStyle on %s: %s", style_name, e)
                failed["ParentStyle"] = str(e)

        if isinstance(property_updates, dict):
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
                result["message"] = "Failed to apply any updates."

        return result


class CreateStyle(ToolWriterStyleBase):
    """Create a new paragraph or character style."""

    name = "create_style"
    intent = "edit"
    description = (
        "Create a new paragraph or character style with optional inheritance "
        "and property settings. For paragraph styles, you can also define "
        "conditional rules mapping contexts (like Table or Header) to other styles."
    )
    parameters = {
        "type": "object",
        "properties": {
            "style_name": {"type": "string", "description": "Name of the new style."},
            "family": {"type": "string", "enum": ["ParagraphStyles", "CharacterStyles"], "description": "Style family. Default: ParagraphStyles."},
            "parent_style": {"type": "string", "description": "Name of the style to inherit from (e.g. 'Standard', 'Default Paragraph Style')."},
            "property_updates": {
                "type": "object",
                "description": "Initial properties to set on the style.",
                "properties": _ALL_KNOWN_PROPERTIES,
            },
            "conditional_rules": {
                "type": "array",
                "description": "Optional conditional rules (ParagraphStyles only).",
                "items": {
                    "type": "object",
                    "properties": {
                        "context": {"type": "string", "enum": _CONDITIONAL_CONTEXTS, "description": "Context where the rule applies."},
                        "target_style": {"type": "string", "description": "Name of the style to apply in this context."},
                    },
                    "required": ["context", "target_style"],
                },
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

        family = kwargs.get("family", "ParagraphStyles")
        parent_style = kwargs.get("parent_style")
        property_updates = kwargs.get("property_updates", {})
        conditional_rules = kwargs.get("conditional_rules")

        doc = ctx.doc
        style_families = doc.getStyleFamilies()
        if not style_families.hasByName(family):
            return self._tool_error("Document does not support style family: %s" % family)

        style_family = style_families.getByName(family)
        if style_family.hasByName(style_name):
            return self._tool_error("Style '%s' already exists in %s." % (style_name, family))

        try:
            # Service choice: ConditionalParagraphStyle vs ParagraphStyle vs CharacterStyle
            service = "com.sun.star.style.ParagraphStyle"
            if family == "ParagraphStyles" and conditional_rules:
                service = "com.sun.star.style.ConditionalParagraphStyle"
            elif family == "CharacterStyles":
                service = "com.sun.star.style.CharacterStyle"

            new_style = doc.createInstance(service)
            if not new_style:
                return self._tool_error("Failed to create style instance for %s" % service)

            # Set parent style
            actual_parent = parent_style
            if not actual_parent and service == "com.sun.star.style.ConditionalParagraphStyle":
                actual_parent = "Standard"

            if actual_parent:
                try:
                    new_style.setParentStyle(actual_parent)
                except Exception as e:
                    log.warning("Failed to set parent_style '%s' on new style: %s", actual_parent, e)

            # Apply properties
            if isinstance(property_updates, dict):
                for prop_name, prop_val in property_updates.items():
                    if prop_name in ("CharColor", "CharBackColor", "CharUnderlineColor"):
                        prop_val = _parse_color(prop_val)
                    try:
                        new_style.setPropertyValue(prop_name, prop_val)
                    except Exception as e:
                        log.warning("Failed to set property %s on new style: %s", prop_name, e)

            # Register style
            style_family.insertByName(style_name, new_style)

            # Apply conditional rules
            if family == "ParagraphStyles" and conditional_rules:
                conditions = []
                for rule in cast("list[dict[str, str]]", conditional_rules):
                    try:
                        nv = cast("Any", uno.createUnoStruct("com.sun.star.beans.NamedValue"))
                    except Exception:
                        nv = cast("Any", NamedValue())
                    nv.Name = rule["context"]
                    nv.Value = rule["target_style"]
                    conditions.append(nv)
                try:
                    new_style.setPropertyValue("ParaStyleConditions", tuple(conditions))
                except Exception as e:
                    log.warning("Failed to set ParaStyleConditions on new style: %s", e)

        except Exception as e:
            log.exception("Failed to create style '%s' in %s", style_name, family)
            msg = getattr(e, "Message", str(e))
            return self._tool_error("Failed to create style: %s" % msg)

        return {"status": "ok", "style_name": style_name, "family": family, "service": service}


class ImportStyles(ToolWriterStyleBase):
    """Import styles from an external document or template."""

    name = "import_styles"
    intent = "edit"
    description = (
        "Import styles from an external document or template (.odt, .ott). "
        "Specify which style types to load (paragraph, page, etc.) and "
        "whether to overwrite existing styles with the same name."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to the source document."},
            "overwrite": {"type": "boolean", "default": True, "description": "Overwrite existing styles with same name."},
            "load_paragraph_styles": {"type": "boolean", "default": True, "description": "Import paragraph and character styles."},
            "load_page_styles": {"type": "boolean", "default": False, "description": "Import page styles."},
            "load_frame_styles": {"type": "boolean", "default": False, "description": "Import frame styles."},
            "load_numbering_styles": {"type": "boolean", "default": False, "description": "Import numbering/list styles."},
        },
        "required": ["file_path"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        file_path = kwargs.get("file_path")
        if not file_path:
            return self._tool_error("file_path is required.")

        overwrite = kwargs.get("overwrite", True)
        load_text = kwargs.get("load_paragraph_styles", True)
        load_page = kwargs.get("load_page_styles", False)
        load_frame = kwargs.get("load_frame_styles", False)
        load_num = kwargs.get("load_numbering_styles", False)

        try:
            url = uno.systemPathToFileUrl(file_path)

            opts = (
                PropertyValue(Name="OverwriteStyles", Value=overwrite),
                PropertyValue(Name="LoadTextStyles", Value=load_text),
                PropertyValue(Name="LoadPageStyles", Value=load_page),
                PropertyValue(Name="LoadFrameStyles", Value=load_frame),
                PropertyValue(Name="LoadNumberingStyles", Value=load_num),
            )

            # The document object implements XStyleLoader
            ctx.doc.loadStylesFromURL(url, opts)

        except Exception as e:
            return self._tool_error("Failed to import styles from %s: %s" % (file_path, e))

        return {"status": "ok", "file_path": file_path, "overwrite": overwrite}
