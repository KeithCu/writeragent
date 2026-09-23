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

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar, cast

if TYPE_CHECKING:
    import uno
    from com.sun.star.beans import PropertyValue, NamedValue
    from plugin.framework.tool import ToolContext
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
            def __init__(self, Name: Any = None, Value: Any = None, **kwargs: Any) -> None:
                self.Name = Name
                self.Value = Value

        class NamedValue:
            def __init__(self, Name: Any = None, Value: Any = None, **kwargs: Any) -> None:
                self.Name = Name
                self.Value = Value

from plugin.doc.visual_helpers import parse_color_to_uno_int
from plugin.framework.tool import ToolBase as FrameworkToolBase
from .format import CLEARABLE_PARA_PROPERTIES, apply_paragraph_style_preserving_direct_char
from .page import get_page_style_properties
from .specialized_base import ToolWriterStyleBase
from .target_resolver import resolve_target_cursor


log = logging.getLogger("writeragent.writer")

# Families style_list will report and enumerate. PageStyles is listed because the page tools take
# a page-style NAME and a document converted from .docx rarely uses "Standard" — without it the
# only way to learn the real names was to guess one and read them off the error.
_STYLE_FAMILIES = ["ParagraphStyles", "CharacterStyles", "PageStyles"]

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

# Schema metadata only — names come from format.CLEARABLE_PARA_PROPERTIES so the
# inspect set and the clear_direct wipe cannot drift.
_PARA_PROPERTY_SCHEMA = {
    "ParaTopMargin": {"type": "integer", "description": "Top margin in 1/100th mm (1 inch = 2540)."},
    "ParaBottomMargin": {"type": "integer", "description": "Bottom margin in 1/100th mm."},
    "ParaLeftMargin": {"type": "integer", "description": "Left margin in 1/100th mm."},
    "ParaRightMargin": {"type": "integer", "description": "Right margin in 1/100th mm."},
    "ParaFirstLineIndent": {"type": "integer", "description": "First line indent in 1/100th mm."},
    "ParaAdjust": {
        "type": "string",
        "enum": ["left", "center", "right", "justify"],
        "description": "Paragraph alignment: left, center, right, or justify.",
    },
    "ParaBackColor": {"type": "string", "description": "Paragraph background color (hex string)."},
    "ParaKeepTogether": {"type": "boolean", "description": "Keep lines of the paragraph together."},
    "ParaSplit": {"type": "boolean", "description": "Whether the paragraph is allowed to split across pages."}
}
_KNOWN_PARAGRAPH_PROPERTIES = {
    name: _PARA_PROPERTY_SCHEMA[name] for name in CLEARABLE_PARA_PROPERTIES
}

# Combine properties for schema use
_ALL_KNOWN_PROPERTIES = {**_KNOWN_CHARACTER_PROPERTIES, **_KNOWN_PARAGRAPH_PROPERTIES}

# Properties to read per style family. Paragraph styles inherit character properties.
_FAMILY_PROPS = {
    "ParagraphStyles": ["ParentStyle", "FollowStyle"] + list(_KNOWN_PARAGRAPH_PROPERTIES.keys()) + list(_KNOWN_CHARACTER_PROPERTIES.keys()),
    "CharacterStyles": ["ParentStyle"] + list(_KNOWN_CHARACTER_PROPERTIES.keys()),
}


# ParaAdjust 0/1/2/3 is hostile (1 is right, 2 is justify, 3 is center). Schema
# is the words; these maps are the UNO integers (ParagraphAdjust).
_PARA_ADJUST_TO_UNO = {"left": 0, "right": 1, "justify": 2, "center": 3}
_PARA_ADJUST_FROM_INT = {0: "left", 1: "right", 2: "justify", 3: "center", 4: "justify"}
_PARA_ADJUST_FROM_ENUM = {
    "LEFT": "left", "RIGHT": "right", "BLOCK": "justify",
    "CENTER": "center", "STRETCH": "justify",
}


def _get_bool_prop(obj: Any, prop_name: str, default: bool = False) -> bool:
    """Safely get a boolean property from a UNO object."""
    try:
        return bool(obj.getPropertyValue(prop_name))
    except Exception:
        return default


def _close_style_names(wanted: str, names: list[str]) -> list[str]:
    """Best hint first: exact case-insensitive, then prefix, then substring.

    Shortest name wins on prefix/substring ('body text' → 'Body Text', not
    'Body Text Indent 2'). Same ranking as apply_style.
    """
    low = wanted.lower()
    exact = [n for n in names if n.lower() == low]
    prefix = sorted((n for n in names if n.lower().startswith(low)), key=len)
    contains = sorted((n for n in names if low in n.lower()), key=len)
    return exact or prefix or contains


def _missing_style_error(tool: Any, style_name: str, family: str, style_family: Any, *, label: str = "Style") -> dict[str, Any]:
    """Not-found error with a Did-you-mean hint and a short sample of names."""
    try:
        names = [str(n) for n in style_family.getElementNames()]
    except Exception:
        names = []
    close = _close_style_names(style_name, names) if names else []
    hint = (" Did you mean '%s'?" % close[0]) if close else ""
    sample = (" Available styles include: %s." % ", ".join(sorted(names)[:15])) if names else ""
    if label == "parent_style":
        return tool._tool_error(
            "parent_style '%s' is not a %s style.%s%s" % (style_name, family, hint, sample))
    return tool._tool_error(
        "Style '%s' not found in %s.%s%s" % (style_name, family, hint, sample))


def _para_adjust_to_uno(value: Any) -> tuple[Any | None, str | None]:
    """Schema word → UNO integer. Integers are refused (the old 0/1/2/3 trap)."""
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _PARA_ADJUST_TO_UNO:
            return _PARA_ADJUST_TO_UNO[key], None
    return None, (
        "ParaAdjust must be one of left, center, right, justify "
        "(not 0/1/2/3 — 1 is right, 2 is justify, 3 is center), got %r" % (value,)
    )


def _para_adjust_from_uno(raw: Any | None) -> str | None:
    """UNO ParaAdjust (int or enum) → schema word, or None if unknown."""
    if raw is None:
        return None
    name = getattr(raw, "value", None)
    if isinstance(name, str):
        return _PARA_ADJUST_FROM_ENUM.get(name)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return _PARA_ADJUST_FROM_INT.get(raw)
    return None


def _get_style_prop(style: Any, prop_name: str) -> Any:
    try:
        return style.getPropertyValue(prop_name)
    except Exception:
        return None


def _schema_prop_value(prop_name: str, raw: Any) -> Any:
    """Read-back form that matches the write schema (ParaAdjust as a word)."""
    if prop_name == "ParaAdjust":
        return _para_adjust_from_uno(raw)
    return raw


def _normalize_property_updates(property_updates: Any) -> tuple[dict[str, Any], str | None]:
    """Copy updates and translate ParaAdjust. Returns (dict, error_message)."""
    if not isinstance(property_updates, dict):
        return {}, None
    updates = dict(property_updates)
    if "ParaAdjust" not in updates:
        return updates, None
    uno_adj, err = _para_adjust_to_uno(updates["ParaAdjust"])
    if err:
        return {}, err
    updates["ParaAdjust"] = uno_adj
    return updates, None


def _installed_font_names(ctx: ToolContext) -> set[str] | None:
    """Casefolded names LibreOffice can use, or None if the list is unavailable."""
    try:
        from plugin.framework.uno_context import get_toolkit

        toolkit = get_toolkit(getattr(ctx, "ctx", None))
        if toolkit is None:
            return None
        device = toolkit.createScreenCompatibleDevice(1, 1)
        if device is None:
            return None
        return {str(f.Name).casefold() for f in device.getFontDescriptors() if getattr(f, "Name", None)}
    except Exception:
        log.debug("font list unavailable", exc_info=True)
        return None


def _font_not_installed_warning(ctx: ToolContext, font_name: Any) -> str | None:
    """Warn when CharFontName was stored but LO will substitute. None if unknown."""
    wanted = str(font_name or "").strip()
    if not wanted:
        return None
    installed = _installed_font_names(ctx)
    if installed is None or wanted.casefold() in installed:
        return None
    return (
        "Font '%s' is not installed here: LibreOffice keeps the "
        "name but displays a substitute." % wanted
    )


class StyleList(ToolWriterStyleBase):
    """List available styles in a given family."""

    name: str | None = "style_list"
    description: str = "List available styles in the document. Omit family to list all style family names; set family to list styles in that family."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"family": {"type": "string", "enum": _STYLE_FAMILIES, "description": ("Style family. Default: ParagraphStyles. Use PageStyles to find the page-style name the page tools need.")}}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
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


class StyleGetInfo(ToolWriterStyleBase):
    """Get detailed properties of a named style."""

    name: str | None = "style_get_info"
    description: str = (
        "Get detailed properties of a named style (font, size, paragraph margins). "
        "For family=PageStyles, return the same page-style margins/header/footer "
        "payload as page_get_style_properties so the caller does not need a second tool hop."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"style": {"type": "string", "description": "Name of the style to inspect."}, "family": {"type": "string", "description": "Style family. Default: ParagraphStyles. PageStyles returns the same page-style payload as page_get_style_properties."}}, "required": ["style"]}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        style_name = kwargs.get("style", "")
        family = kwargs.get("family", "ParagraphStyles")

        # In-process dispatch to the same reader page_get_style_properties uses — not an error
        # bounce and not a nested LLM call. Option B (deferred): hard-merge to a single public
        # API (winner TBD: general style_get_info vs the page toolkit).
        if family == "PageStyles":
            result = get_page_style_properties(ctx.doc, style_name or "Standard")
            if result.get("status") == "ok":
                result = {**result, "family": "PageStyles"}
            return result

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

        if "ParaAdjust" in info:
            word = _para_adjust_from_uno(info["ParaAdjust"])
            if word is not None:
                info["ParaAdjust"] = word

        return {"status": "ok", **info}


class ApplyStyle(FrameworkToolBase):
    """Apply a paragraph or character style."""

    name: str | None = "apply_style"
    intent: str | None = "edit"
    description: str = (
        "Apply a style to a target. Use family='ParagraphStyles' for paragraph "
        "styles (e.g. Heading 1) or family='CharacterStyles' for character "
        "styles (e.g. Source Text). Use 'No Character Style' "
        "with family='CharacterStyles' to remove a character style. "
        "Use target='selection' (default), 'beginning', 'end', 'full_document', "
        "or 'search' with old_content. "
        "Paragraph styles default to clear_direct='style_props': the style's font name and size "
        "show (house font wins) and paragraph indents/alignment follow the style; bold, italic "
        "and colour stay. Pass clear_direct='none' only to keep a hand-set font. "
        "Re-applying a style does not keep a quote indent — LibreOffice drops direct Para*."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "style": {"type": "string", "description": "Style name (e.g. Heading 1, Source Text)."},
            "family": {"type": "string", "enum": ["ParagraphStyles", "CharacterStyles"], "description": ("Style family. Default: ParagraphStyles.")},
            "target": {"type": "string", "enum": ["beginning", "end", "selection", "full_document", "search"], "description": "Where to apply the style."},
            "old_content": {"type": "string", "description": "Text to find and apply style to if target = 'search'."},
            "all_matches": {"type": "boolean", "description": "For target='search': apply to EVERY occurrence of old_content (default false = first only)."},
            "occurrence": {"type": "integer", "description": "For target='search': apply to this single 0-based occurrence instead of the first."},
            "clear_direct": {"type": "string", "enum": ["none", "style_props", "all"], "default": "style_props", "description": (
                "What to do with direct (hand-set) formatting on the target, ParagraphStyles only. "
                "'style_props' (default) drops the font name/size override and the paragraph "
                "indents/alignment so the style's own values show, keeping bold/italic/colour. "
                "The name means 'let the style's properties win' for the font/size/indent the "
                "style governs — not 'clear only what this style defines'. "
                "'none' keeps a hand-set font (historical; the style can look unchanged). "
                "'all' drops every direct override (Ctrl+M). "
                "'all' is not allowed with target='full_document' — that would flatten emphasis "
                "across the whole document; style each range instead.")},
        },
        "required": ["style"],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    is_mutation: bool | None = True

    # Maps family to the UNO property that holds the style name.
    _PROPERTY_MAP: ClassVar[dict[str, str]] = {"ParagraphStyles": "ParaStyleName", "CharacterStyles": "CharStyleName"}

    def _apply_one(self, ctx: ToolContext, family: str, uno_prop: str, uno_value: str, cursor: Any, clear_direct: str = "style_props") -> dict[str, Any] | None:
        """Apply the style to one cursor. Returns the direct-formatting report (or None)."""
        if family == "ParagraphStyles":
            return apply_paragraph_style_preserving_direct_char(ctx.doc, cursor, uno_value, clear_direct)
        cursor.setPropertyValue(uno_prop, uno_value)
        return None

    @staticmethod
    def _merge_reports(reports: list[Any]) -> dict[str, Any]:
        """Fold per-range reports into the fields echoed on the tool result."""
        out: dict[str, Any] = {}
        for rep in reports:
            if not rep:
                continue
            # One value per property across all ranges (last one wins). This is a signal that
            # direct formatting was in play, not a per-range ledger.
            for key in ("preserved_char_overrides", "removed_char_overrides"):
                if rep.get(key):
                    out.setdefault(key, {}).update(rep[key])
            for key in ("cleared_char_properties", "cleared_paragraph_properties"):
                if rep.get(key):
                    out[key] = rep[key]  # same list for every range; last one wins
            if rep.get("warning"):
                # First range's walk-cap note is enough — same cap applies to every range.
                out.setdefault("warning", rep["warning"])
        # No retry hint: default is already style_props (house font shows). preserved_char_overrides
        # is only set on the explicit none path, which the caller asked for.
        return out

    @staticmethod
    def _resolve_clear_direct(family: str, raw: Any) -> str:
        """Paragraph styles default to style_props so the house font shows without a retry.

        Character styles ignore the flag (it is not implemented for them); omitted there is none.
        """
        if raw is None or (isinstance(raw, str) and not str(raw).strip()):
            return "style_props" if family == "ParagraphStyles" else "none"
        return str(raw).strip()

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        style_name = str(kwargs.get("style") or "").strip()
        if not style_name:
            return self._tool_error("style is required.")

        family = kwargs.get("family", "ParagraphStyles")
        uno_prop = self._PROPERTY_MAP.get(family)
        if not uno_prop:
            return self._tool_error("Unknown family: %s. Use ParagraphStyles or CharacterStyles." % family)

        # UNO quirk: the default character style is applied by setting
        # CharStyleName to an empty string.
        uno_value = "" if (family == "CharacterStyles" and style_name == "No Character Style") else style_name

        # Validate the style EXISTS before touching the document: a near-miss name ('heading 1',
        # 'Body Text') used to dead-end in a raw UNO IllegalArgumentException with no hint. Offer
        # the case-insensitive match when there is one, plus a sample of valid names.
        if uno_value:
            try:
                fam = ctx.doc.getStyleFamilies().getByName(family)
                if not fam.hasByName(uno_value):
                    return _missing_style_error(self, style_name, family, fam)
            except Exception:
                pass  # can't enumerate styles (exotic doc) -> let the apply path report

        target = kwargs.get("target", "selection")
        old_content = kwargs.get("old_content")

        clear_direct = self._resolve_clear_direct(family, kwargs.get("clear_direct"))
        if clear_direct not in ("none", "style_props", "all"):
            return self._tool_error("clear_direct must be one of: none, style_props, all.")
        if clear_direct != "none":
            if family != "ParagraphStyles":
                return self._tool_error("clear_direct only applies to family='ParagraphStyles'.")
            if clear_direct == "all" and target == "full_document":
                # Ctrl+M on the whole document wipes emphasis as well as fonts — force the
                # per-range path. style_props (the default) is allowed: house font/size win
                # and bold/italic/colour stay.
                return self._tool_error(
                    "clear_direct='all' is not allowed with target='full_document': it would "
                    "flatten emphasis across the whole document. Style each range separately "
                    "(target='search') instead, or omit clear_direct to use style_props.")

        # Multi-occurrence styling (target='search' only). resolve_target_cursor styles only the
        # first match; all_matches / occurrence let a defined term or repeated quote lead be
        # styled everywhere. Each match gets a cursor in its OWN text object (cell/frame safe).
        all_matches = bool(kwargs.get("all_matches"))
        occ_raw = kwargs.get("occurrence")
        if target == "search" and (all_matches or occ_raw is not None):
            from plugin.writer.search import find_all_ranges, normalize_search_string_for_find
            from plugin.writer.format import content_has_markup, html_to_plain_text

            s = str(old_content).strip() if old_content is not None else ""
            if not s:
                return {"status": "error", "message": "target='search' requires old_content.", "applied": False, "matched": False}
            if content_has_markup(s):
                s = html_to_plain_text(s, ctx.ctx, ctx.services.get("config"))
            s = normalize_search_string_for_find(s)
            ranges = find_all_ranges(ctx.doc, s) if s else []
            if not ranges:
                return {"status": "error", "message": "old_content not found in document.", "target": "search", "applied": False, "matched": False}
            if occ_raw is not None:
                try:
                    occ = int(occ_raw)
                except (TypeError, ValueError):
                    return self._tool_error("occurrence must be an integer.")
                if occ < 0 or occ >= len(ranges):
                    return self._tool_error("occurrence %s out of range (found %d match(es), use 0..%d)." % (occ_raw, len(ranges), len(ranges) - 1))
                ranges = [ranges[occ]]
            applied = 0
            reports = []
            for found in ranges:
                try:
                    ftext = found.getText()
                    c = ftext.createTextCursorByRange(found.getStart())
                    c.gotoRange(found.getEnd(), True)
                    reports.append(self._apply_one(ctx, family, uno_prop, uno_value, c, clear_direct))
                    applied += 1
                except Exception as e:
                    return self._tool_error("Applied to %d of %d; failed on one match: %s" % (applied, len(ranges), e))
            result = {"status": "ok", "message": "Applied style '%s' (%s) to %d match(es)." % (style_name, family, applied),
                      "style_name": style_name, "family": family, "target": "search", "applied": True, "matched": True, "applied_count": applied}
            result.update(self._merge_reports(reports))
            from plugin.writer.edit_review import review_recording_enabled
            if review_recording_enabled(ctx.ctx):
                result["style_unreviewed"] = True
            return result

        try:
            cursor = resolve_target_cursor(ctx, target, old_content)
        except ValueError as ve:
            if target == "search":
                # Surface the search-miss as a structured (top-level) failure, like
                # apply_document_content, so a client can branch on matched/applied.
                # TODO(follow-up): consider _tool_error(..., details={...}) so this path also
                # carries a standard error code like other apply_style failures.
                return {"status": "error", "message": str(ve), "target": "search", "applied": False, "matched": False}
            return self._tool_error(str(ve))

        if not cursor:
            return self._tool_error("Failed to resolve target location.")

        try:
            report = self._apply_one(ctx, family, uno_prop, uno_value, cursor, clear_direct)
        except Exception as e:
            return self._tool_error("Could not apply style: %s" % e)
        result = {"status": "ok", "message": "Applied style '%s' (%s) to %s." % (style_name, family, target),
                  "style_name": style_name, "family": family, "target": target, "applied": True}
        result.update(self._merge_reports([report]))
        if target == "search":
            result["matched"] = True  # a search miss would have errored earlier in resolve_target_cursor
        # A style change is applied directly and does NOT become a tracked change (LibreOffice
        # records text/char-format edits as redlines, but not a style swap). When review mode is on
        # the agent expects its edits to be reviewable, so flag that this one was not -- the agent
        # should tell the user it changed a style they cannot accept/reject like a text edit.
        from plugin.writer.edit_review import review_recording_enabled

        if review_recording_enabled(ctx.ctx):
            result["style_unreviewed"] = True
        return result


class StyleUpdate(ToolWriterStyleBase):
    """Update properties of an existing paragraph or character style."""

    name: str | None = "style_update"
    intent: str | None = "edit"
    description: str = (
        "Update the properties of an existing style. "
        "Provide 'family' (ParagraphStyles or CharacterStyles), 'style_name', and "
        "'property_updates': a dictionary of UNO property names to values "
        "(e.g. {'CharColor': '#FF0000', 'CharWeight': 150, 'ParaAdjust': 'center'}). "
        "ParaAdjust is left/center/right/justify, not 0/1/2/3. "
        "Colors can be provided as hex strings or integers. "
        "You can also update the 'parent_style' separately."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "style": {"type": "string", "description": "Name of the style to modify (e.g., 'Heading 1', 'Source Text')."},
            "family": {"type": "string", "enum": ["ParagraphStyles", "CharacterStyles"], "description": "Style family. Default: ParagraphStyles."},
            "parent_style": {"type": "string", "description": "Name of the style to inherit from."},
            "property_updates": {
                "type": "object",
                "description": "Dictionary of UNO property names to values (keys are listed in the schema).",
                "properties": _ALL_KNOWN_PROPERTIES,
            },
        },
        "required": ["style"],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        style_name = str(kwargs.get("style") or "").strip()
        if not style_name:
            return self._tool_error("style is required.")

        family = kwargs.get("family", "ParagraphStyles")
        parent_style = kwargs.get("parent_style")
        property_updates = kwargs.get("property_updates", {})

        updates, adj_err = _normalize_property_updates(property_updates)
        if adj_err:
            return self._tool_error(adj_err)

        doc = ctx.doc
        style_family = self.get_item(doc, "getStyleFamilies", family, missing_msg="Document does not support style families.", not_found_msg="Unknown style family: %s" % family)
        if isinstance(style_family, dict):
            return style_family

        if not style_family.hasByName(style_name):
            return _missing_style_error(self, style_name, family, style_family)

        if parent_style is not None:
            parent_style = str(parent_style).strip()
            if not parent_style:
                return self._tool_error("parent_style is empty.")
            if not style_family.hasByName(parent_style):
                return _missing_style_error(
                    self, parent_style, family, style_family, label="parent_style")

        style = style_family.getByName(style_name)

        applied = {}
        failed = {}
        snapshot_keys = list(updates)
        if parent_style is not None:
            snapshot_keys = ["ParentStyle"] + snapshot_keys
        before = {}
        for key in snapshot_keys:
            if key == "ParentStyle":
                try:
                    before[key] = style.getParentStyle()
                except Exception:
                    before[key] = None
            else:
                before[key] = _schema_prop_value(key, _get_style_prop(style, key))

        if parent_style is not None:
            try:
                style.setParentStyle(parent_style)
                applied["ParentStyle"] = parent_style
            except Exception as e:
                log.warning("Failed to set ParentStyle on %s: %s", style_name, e, exc_info=True)
                failed["ParentStyle"] = str(e)

        for prop_name, prop_val in updates.items():
            # Handle color conversions (None = unparseable; do not pass raw strings to UNO).
            if prop_name in ("CharColor", "CharBackColor", "CharUnderlineColor"):
                parsed = parse_color_to_uno_int(prop_val)
                if parsed is None:
                    failed[prop_name] = "Invalid color: %r" % (prop_val,)
                    continue
                prop_val = parsed

            try:
                style.setPropertyValue(prop_name, prop_val)
                applied[prop_name] = (
                    _para_adjust_from_uno(prop_val) if prop_name == "ParaAdjust" else prop_val
                )
            except Exception as e:
                log.warning("Failed to set property %s on %s: %s", prop_name, style_name, e, exc_info=True)
                hint = ""
                close = _close_style_names(prop_name, list(_ALL_KNOWN_PROPERTIES))
                if close and close[0] != prop_name:
                    hint = " Did you mean '%s'?" % close[0]
                failed[prop_name] = "%s%s" % (e, hint)

        after = {}
        for key in snapshot_keys:
            if key == "ParentStyle":
                try:
                    after[key] = style.getParentStyle()
                except Exception:
                    after[key] = applied.get("ParentStyle")
            else:
                after[key] = _schema_prop_value(key, _get_style_prop(style, key))

        result = {"status": "ok", "style_name": style_name, "family": family}
        if snapshot_keys:
            result["before"] = before
            result["after"] = after
        if applied:
            result["updated_properties"] = applied
        if failed:
            result["failed_properties"] = failed
            if not applied:
                result["status"] = "error"
                result["message"] = "Failed to apply any updates."
        if "CharFontName" in applied:
            warning = _font_not_installed_warning(ctx, applied["CharFontName"])
            if warning:
                result["warning"] = warning

        # Style changes don't produce tracked changes, so the agent can't review them like a
        # text edit -- flag it (matches ApplyStyle) so the agent tells the user it changed a style.
        from plugin.writer.edit_review import review_recording_enabled

        if result.get("status") != "error" and review_recording_enabled(ctx.ctx):
            result["style_unreviewed"] = True
        return result


class StyleCreate(ToolWriterStyleBase):
    """Create a new paragraph or character style."""

    name: str | None = "style_create"
    intent: str | None = "edit"
    description: str = (
        "Create a new paragraph or character style with optional inheritance "
        "and property settings. For paragraph styles, you can also define "
        "conditional rules mapping contexts (like Table or Header) to other styles."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "style": {"type": "string", "description": "Name of the new style."},
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
        "required": ["style"],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        style_name = str(kwargs.get("style") or "").strip()
        if not style_name:
            return self._tool_error("style is required.")

        family = kwargs.get("family", "ParagraphStyles")
        parent_style = kwargs.get("parent_style")
        property_updates = kwargs.get("property_updates", {})
        conditional_rules = kwargs.get("conditional_rules")

        updates, adj_err = _normalize_property_updates(property_updates)
        if adj_err:
            return self._tool_error(adj_err)

        doc = ctx.doc
        style_families = doc.getStyleFamilies()
        if not style_families.hasByName(family):
            return self._tool_error("Document does not support style family: %s" % family)

        style_family = style_families.getByName(family)
        if style_family.hasByName(style_name):
            return self._tool_error("Style '%s' already exists in %s." % (style_name, family))

        # Service choice: ConditionalParagraphStyle vs ParagraphStyle vs CharacterStyle
        service = "com.sun.star.style.ParagraphStyle"
        if family == "ParagraphStyles" and conditional_rules:
            service = "com.sun.star.style.ConditionalParagraphStyle"
        elif family == "CharacterStyles":
            service = "com.sun.star.style.CharacterStyle"

        actual_parent = str(parent_style).strip() if parent_style is not None else ""
        if not actual_parent and service == "com.sun.star.style.ConditionalParagraphStyle":
            actual_parent = "Standard"
        if actual_parent and not style_family.hasByName(actual_parent):
            return _missing_style_error(
                self, actual_parent, family, style_family, label="parent_style")

        applied_font = None
        try:
            new_style = doc.createInstance(service)
            if not new_style:
                return self._tool_error("Failed to create style instance for %s" % service)

            if actual_parent:
                try:
                    new_style.setParentStyle(actual_parent)
                except Exception:
                    log.warning("Failed to set parent_style '%s' on new style", actual_parent, exc_info=True)

            # Apply properties
            applied_font = None
            for prop_name, prop_val in updates.items():
                if prop_name in ("CharColor", "CharBackColor", "CharUnderlineColor"):
                    parsed = parse_color_to_uno_int(prop_val)
                    if parsed is None:
                        log.warning("Skipping invalid color for %s on new style: %r", prop_name, prop_val)
                        continue
                    prop_val = parsed
                try:
                    new_style.setPropertyValue(prop_name, prop_val)
                    if prop_name == "CharFontName":
                        applied_font = prop_val
                except Exception:
                    log.warning("Failed to set property %s on new style", prop_name, exc_info=True)

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
                except Exception:
                    log.warning("Failed to set ParaStyleConditions on new style", exc_info=True)

        except Exception as e:
            log.exception("Failed to create style '%s' in %s", style_name, family)
            msg = getattr(e, "Message", str(e))
            return self._tool_error("Failed to create style: %s" % msg)

        result = {"status": "ok", "style_name": style_name, "family": family, "service": service}
        if applied_font is not None:
            warning = _font_not_installed_warning(ctx, applied_font)
            if warning:
                result["warning"] = warning
        # Style changes aren't reviewable as tracked changes -- flag it for the agent (matches ApplyStyle).
        from plugin.writer.edit_review import review_recording_enabled

        if review_recording_enabled(ctx.ctx):
            result["style_unreviewed"] = True
        return result


class StyleImport(ToolWriterStyleBase):
    """Import styles from an external document or template."""

    name: str | None = "style_import"
    intent: str | None = "edit"
    description: str = (
        "Import styles from an external document or template (.odt, .ott). "
        "Specify which style types to load (paragraph, page, etc.) and "
        "whether to overwrite existing styles with the same name."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the source document."},
            "overwrite": {"type": "boolean", "default": True, "description": "Overwrite existing styles with same name."},
            "load_paragraph_styles": {"type": "boolean", "default": True, "description": "Import paragraph and character styles."},
            "load_page_styles": {"type": "boolean", "default": False, "description": "Import page styles."},
            "load_frame_styles": {"type": "boolean", "default": False, "description": "Import frame styles."},
            "load_numbering_styles": {"type": "boolean", "default": False, "description": "Import numbering/list styles."},
        },
        "required": ["path"],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        file_path = kwargs.get("path")
        if not file_path:
            return self._tool_error("path is required.")

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

        result = {"status": "ok", "file_path": file_path, "overwrite": overwrite}
        # Imported styles aren't reviewable as tracked changes -- flag it for the agent (matches ApplyStyle).
        from plugin.writer.edit_review import review_recording_enabled

        if review_recording_enabled(ctx.ctx):
            result["style_unreviewed"] = True
        return result
