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
"""Writer style tools: list and inspect, create, update, clone, import styles, and format text directly.

Property updates use UNO property names; struct-valued properties take a dict of fields.
"""

from __future__ import annotations

from collections import deque
from contextlib import nullcontext
import copy
import logging
import os
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Literal, NamedTuple, TypedDict, cast

import uno
from com.sun.star.uno import RuntimeException

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

from plugin.doc.compound_undo import UndoUnavailable, WriterCompoundUndo, commit_or_rollback
from plugin.doc.text_helpers import (
    CLEARABLE_PARA_PROPERTIES,
    PROPERTY_READONLY,
    iter_elements_limited,
    record_walk_cap,
    reset_properties_to_default,
    set_paragraph_background,
    writable_char_names,
)
from plugin.doc.visual_helpers import color_param, parse_color_to_uno_int
from plugin.framework.errors import (
    ToolExecutionError,
    is_disposal,
    name_miss_details,
    reraise_if_disposed,
    similar_names,
    suggest_call,
    uno_read,
)
from plugin.framework.doc_type import CALC_DOCUMENT_SERVICE, TEXT_DOCUMENT_SERVICE
from plugin.framework.tool import ToolBase as FrameworkToolBase
from plugin.framework.uno_context import create_property_value, get_toolkit
from plugin.writer.edit_review import (
    agent_format_author,
    annotate_review_status,
    flag_style_unreviewed,
    next_agent_edit_undo_title,
    paragraph_redline_author,
    review_recording_enabled,
)
from plugin.writer.record_changes import is_recording_changes, record_changes
from . import search as search_mod
from .page import copy_page_style, get_page_style_properties
from plugin.writer.specialized_base import INSERT_TARGETS, ToolWriterStyleBase, enum_param
from .target_resolver import cursor_spanning_range, resolve_target_cursor


log = logging.getLogger("writeragent.writer")

FONT_NAME_PROPERTIES = ("CharFontName", "CharFontNameAsian", "CharFontNameComplex")

STYLE_GOVERNED_CHAR_PROPERTIES = FONT_NAME_PROPERTIES + (
    "CharHeight", "CharHeightAsian", "CharHeightComplex",
)

# Every name is one SwUnoCursorHelper::makeRedline accepts and lcl_RejectRedline restores.
# FillStyle/FillColor hold direct paragraph backgrounds.
# Names sharing one pool item must all be listed, because SfxItemPropertySet::setPropertyValue
# (itemprop.cxx) rebuilds the item from its pool default.
# A unit or complex-colour name must follow its plain name, because later values overwrite earlier ones.
# Never add FillBitmapMode: makeRedline crashes on its out-of-pool OWN_ATTR_FILLBMP_MODE id.
# Never add PageNumberOffset: capturing it alone drops PageDescName, which Reject otherwise keeps.
# FillBitmapSizeX/Y do not read back what was set (metric item conversion), so they are left out and reported.
_REVERT_PARA_PROPERTIES = CLEARABLE_PARA_PROPERTIES + (
    "ParaLeftMarginUnit", "ParaRightMarginUnit", "ParaFirstLineIndentUnit", "ParaIsAutoFirstLineIndent",
    "ParaContextMargin", "ParaLineSpacing", "ParaTabStops", "ParaVertAlignment", "ParaWidows", "ParaOrphans", "OutlineLevel",
    "ParaExpandSingleWord", "ParaLastLineAdjust", "ParaComposer",
    "ParaWordSpacing", "ParaWordSpacingMinimum", "ParaWordSpacingMaximum",
    "ParaLetterSpacingMinimum", "ParaLetterSpacingMaximum", "ParaScaleWidthMinimum", "ParaScaleWidthMaximum",
    "ParaIsHyphenation", "ParaHyphenationNoCaps", "ParaHyphenationNoLastWord",
    "ParaHyphenationMaxLeadingChars", "ParaHyphenationMaxTrailingChars",
    "ParaHyphenationCompoundMinLeadingChars", "ParaHyphenationCompoundMinTrailingChars",
    "ParaHyphenationMaxHyphens", "ParaHyphenationMinWordLength",
    "ParaHyphenationZone", "ParaHyphenationZoneAlways", "ParaHyphenationZoneColumn", "ParaHyphenationZonePage", "ParaHyphenationZoneSpread",
    "ParaHyphenationKeep", "ParaHyphenationKeepType", "ParaHyphenationKeepLine",
    "ParaRegisterModeActive", "ParaLineNumberCount", "ParaLineNumberStartValue",
    "ParaIsHangingPunctuation", "ParaIsCharacterDistance", "ParaIsForbiddenRules", "SnapToGrid", "ParaIsConnectBorder",
    "FillStyle", "FillColor", "FillComplexColor", "FillBackground", "FillTransparence", "FillTransparenceGradient",
    "FillGradient", "FillGradientStepCount", "FillHatch",
    "FillBitmap", "FillBitmapLogicalSize", "FillBitmapOffsetX", "FillBitmapOffsetY",
    "FillBitmapPositionOffsetX", "FillBitmapPositionOffsetY", "FillBitmapRectanglePoint", "FillBitmapStretch", "FillBitmapTile",
    "LeftBorder", "RightBorder", "TopBorder", "BottomBorder",
    "LeftBorderComplexColor", "RightBorderComplexColor", "TopBorderComplexColor", "BottomBorderComplexColor",
    "LeftBorderDistance", "RightBorderDistance", "TopBorderDistance", "BottomBorderDistance",
    "DropCapFormat", "DropCapWholeWord", "ParaShadowFormat", "BreakType", "WritingMode", "WritingModeAutomatic",
)

# Direct names that need no report when not captured.
_UNREPORTED_PARA_PROPERTIES = frozenset({
    # Restored through NumberingRules / ParaStyleName, or kept by Reject (lcl_RstAttr, docfmt.cxx).
    "ParaStyleName", "NumberingRules", "NumberingStyleName", "PageDescName", "PageNumberOffset",
    # Second names for captured items.
    "BorderDistance", "ParaBackTransparent", "ParaBackgroundComplexColor", "ParaBackGraphic",
    "ParaBackGraphicFilter", "ParaBackGraphicLocation", "ParaBackGraphicURL",
    "FillBitmapMode", "FillBitmapName", "FillBitmapURL", "FillGradientName", "FillHatchName",
    "FillTransparenceGradientName",
    # File metadata and legacy attributes without a visible effect.
    "FillColor2", "ParRsid", "ParaInteropGrabBag", "OutlineContentVisible",
    "ParaUserDefinedAttributes", "TextUserDefinedAttributes",
})
_MARGIN_UNIT_PROPERTIES = frozenset({"ParaLeftMarginUnit", "ParaRightMarginUnit", "ParaFirstLineIndentUnit"})

# Must cover everything style_props clears so the report echoes every dropped slot.
REPORTED_CHAR_PROPERTIES = STYLE_GOVERNED_CHAR_PROPERTIES + (
    "CharWeight", "CharPosture", "CharColor",
)

_CAPTURE_PARA_LIMIT = 200000
_CAPTURE_PORTION_LIMIT = 50000

_READ_FAILED = object()
_CHAR_PROPS_KEY = "__writable_char_props__"
FONT_SLANT_TO_INT: dict[str, int] = {
    "none": 0,
    "oblique": 1,
    "italic": 2,
}


def convert_direct_properties(updates: dict[str, Any]) -> dict[str, Any]:
    """Normalize ParaAdjust/structs and convert colors/CharPosture for direct formatting."""
    normalized = _normalize_property_updates(updates)
    return {name: _convert_value(name, val) for name, val in normalized.items()}


def set_cursor_properties(cursor: Any, updates: dict[str, Any]) -> None:
    """Apply already-converted UNO properties to *cursor* in a single setPropertyValues batch."""
    if not updates:
        return
    names = tuple(updates.keys())
    raw_values = tuple(updates.values())
    typed_values = tuple(_typed_property_value(name, val) for name, val in updates.items())
    use_invoke = any(typed is not raw for typed, raw in zip(typed_values, raw_values))
    try:
        if use_invoke:
            cast("Any", uno).invoke(cursor, "setPropertyValues", (names, typed_values))
        else:
            cursor.setPropertyValues(names, raw_values)
    except Exception as exc:
        reraise_if_disposed(exc)
        raise ValueError(
            "LibreOffice rejected the values for %s: %s" % (", ".join(names), exc)
        ) from exc


def _unsupported_property_error(
    tool: FrameworkToolBase,
    info: Any,
    names: Iterable[str],
) -> dict[str, Any] | None:
    """Validate property names against *info* (XPropertySetInfo) before mutating.

    SwUnoCursorHelper::SetPropertyValues queues unknown and read-only names and
    throws only after applying earlier valid properties, so validate up front.
    """
    unknown: list[str] = []
    readonly: list[str] = []
    for name in names:
        if not info.hasPropertyByName(name):
            unknown.append(name)
            continue
        prop = info.getPropertyByName(name)
        if prop.Attributes & PROPERTY_READONLY:
            readonly.append(name)
    if unknown:
        all_names = [str(p.Name) for p in info.getProperties()]
        hints = []
        for bad in unknown:
            close = _close_style_names(bad, all_names) if all_names else []
            if close and close[0] != bad:
                hints.append("'%s' -> '%s'?" % (bad, close[0]))
        hint_text = (" Did you mean: %s" % ", ".join(hints)) if hints else ""
        return tool._tool_error(
            "Unknown property: %s.%s" % (", ".join(unknown), hint_text),
            code="UNKNOWN_PROPERTY",
        )
    if readonly:
        return tool._tool_error(
            "Read-only property cannot be modified: %s." % ", ".join(readonly),
            code="READ_ONLY_PROPERTY",
        )
    return None


def _summarize_char_overrides(overrides: Any) -> dict[str, Any]:
    """Flat, JSON-safe {prop: value} of the captured direct Char* overrides (first portion wins)."""
    summary = {}
    for _pc, props in overrides:
        for name in REPORTED_CHAR_PROPERTIES:
            if name in props and name not in summary:
                val = _uno_scalar_to_json(props[name])
                if name == "CharPosture":
                    val = _font_slant_to_int(val)
                if isinstance(val, (str, int, float, bool)):
                    summary[name] = val
    return summary


class ParagraphStyleApplyReport(TypedDict, total=False):
    direct_formatting: str
    clear_direct: str
    preserved_char_overrides: dict[str, Any]
    removed_char_overrides: dict[str, Any]
    cleared_char_properties: list[str]
    cleared_paragraph_properties: list[str]
    warning: str
    capture_incomplete: bool
    tracked: bool
    style_unreviewed: bool


class _ParagraphRevert(NamedTuple):
    cursor: Any                  # spans the paragraph
    properties: tuple[Any, ...]  # PropertyValues for RedlineRevertProperties
    recorded: bool               # already has a ParagraphFormat redline
    numbered: bool
    unrestorable: tuple[str, ...]


class _DirectFormatReport(NamedTuple):
    """Per-cursor result of StyleFormatText: cleared names, whether any ParagraphFormat redline
    was recorded, whether every recordable touched paragraph was recorded (empty paragraphs
    do not clear ``para_complete`` while another paragraph was recorded), and any direct
    paragraph property names that Reject will drop.
    """

    cleared: list[str]
    para_recorded: bool
    para_complete: bool
    unrestorable: frozenset[str]


def _expand_to_full_paragraphs(cur: Any) -> Any:
    """Expand cursor *cur* to cover the full paragraphs it touches."""
    text = cur.getText()
    start = text.createTextCursorByRange(cur.getStart())
    end = text.createTextCursorByRange(cur.getEnd())
    start.gotoStartOfParagraph(False)
    end.gotoEndOfParagraph(True)
    expanded = text.createTextCursorByRange(start.getStart())
    expanded.gotoRange(end.getEnd(), True)
    return expanded


class _CountingReader:
    """UNO getter wrapper for the capture walk: a failed read returns _READ_FAILED and counts
    once; a disposed document re-raises as DocumentDisposedError (reraise_if_disposed)."""

    failures: int

    def __init__(self) -> None:
        self.failures = 0

    def __call__(self, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return fn(*args)
        except Exception as exc:
            reraise_if_disposed(exc)
            self.failures += 1
            return _READ_FAILED

    def fail(self) -> None:
        """Record one failure (e.g. for cached negative reads that count per portion)."""
        self.failures += 1


def _style_comparable_char_names(portion: Any, style: Any) -> list[str] | object:
    """``writable_char_names`` that paragraph *style* also has.

    CharStyleName and the other run-only names have no style value to compare with, and a
    paragraph style change leaves them alone. Returns ``_READ_FAILED`` when property info
    cannot be read.
    """
    names = writable_char_names(portion)
    if names is None:
        return _READ_FAILED
    info = style.getPropertySetInfo()
    return [name for name in names if info.hasPropertyByName(name)]


def _capture_paragraphs(read: _CountingReader, para_enum: Any, notes: list[str]) -> Iterator[Any]:
    """Yield the paragraphs of *para_enum*, descending into table cells (nested tables too)."""
    seen = 0
    for el in iter_elements_limited(para_enum, _CAPTURE_PARA_LIMIT):
        seen += 1
        if not hasattr(el, "supportsService"):
            continue
        if el.supportsService("com.sun.star.text.Paragraph"):
            yield el
        elif el.supportsService("com.sun.star.text.TextTable"):
            names = read(el.getCellNames)
            for name in () if names is _READ_FAILED else names:
                cell = read(el.getCellByName, name)
                cell_enum = _READ_FAILED if cell is _READ_FAILED else read(cell.createEnumeration)
                if cell_enum is not _READ_FAILED:
                    yield from _capture_paragraphs(read, cell_enum, notes)
    record_walk_cap(para_enum, seen, _CAPTURE_PARA_LIMIT, "paragraphs", notes)


def _touched_paragraphs(cursor: Any) -> Iterator[Any]:
    """Yield every paragraph a paragraph-level write on *cursor* changes, tables included."""
    read = _CountingReader()
    notes: list[str] = []
    span = _expand_to_full_paragraphs(cursor)
    yield from _capture_paragraphs(read, span.createEnumeration(), notes)
    # A range enumeration omits an empty last paragraph after a table
    # (SwXParagraphEnumerationImpl::IgnoreLastElement, sw/source/core/unocore/unoobj2.cxx);
    # otherwise this repeats the last paragraph.
    end = span.getText().createTextCursorByRange(span.getEnd())
    yield from _capture_paragraphs(read, end.createEnumeration(), notes)
    if read.failures or notes:
        raise ToolExecutionError("Not every paragraph in the range could be read.")


def _capture_direct_char_overrides(
    doc: Any,
    cursor: Any,
    style_cache: dict[Any, Any],
) -> tuple[list[tuple[Any, dict[str, Any]]], int, list[str]]:
    """Capture direct character formatting overrides within *cursor*.

    The walk follows one rule: read it; on failure count one and skip; disposal re-raises.

    Cached across calls via *style_cache*, which holds three key shapes:
      - style name -> style UNO object or _READ_FAILED
      - (style name, prop name) -> default style value or _READ_FAILED
      - _CHAR_PROPS_KEY -> list of writable Char* property names or _READ_FAILED
    """
    read = _CountingReader()
    notes: list[str] = []
    overrides: list[tuple[Any, dict[str, Any]]] = []

    para_styles = read(lambda: doc.getStyleFamilies().getByName("ParagraphStyles"))
    para_enum = read(cursor.createEnumeration)
    if para_styles is _READ_FAILED or para_enum is _READ_FAILED:
        return [], read.failures, notes

    for para in _capture_paragraphs(read, para_enum, notes):
        name = read(para.getPropertyValue, "ParaStyleName")
        if name is _READ_FAILED:
            continue

        if name not in style_cache:
            style_cache[name] = read(para_styles.getByName, name)
        old_style = style_cache[name]
        if old_style is _READ_FAILED:
            continue

        portion_enum = read(para.createEnumeration)
        if portion_enum is _READ_FAILED:
            continue

        portions = 0
        for portion in iter_elements_limited(portion_enum, _CAPTURE_PORTION_LIMIT):
            portions += 1
            if read(portion.getPropertyValue, "TextPortionType") != "Text":
                continue

            if _CHAR_PROPS_KEY not in style_cache:
                style_cache[_CHAR_PROPS_KEY] = uno_read(lambda: _style_comparable_char_names(portion, old_style), _READ_FAILED)

            char_names = style_cache[_CHAR_PROPS_KEY]
            if char_names is _READ_FAILED:
                read.fail()
                continue
            if not char_names:
                continue

            props: dict[str, Any] = {}
            for prop_name in char_names:
                val = read(portion.getPropertyValue, prop_name)
                if val is _READ_FAILED:
                    continue

                cache_key = (name, prop_name)
                if cache_key in style_cache:
                    style_val = style_cache[cache_key]
                else:
                    style_val = read(old_style.getPropertyValue, prop_name)
                    style_cache[cache_key] = style_val

                if style_val is _READ_FAILED:
                    continue
                if val == style_val:
                    continue
                props[prop_name] = val

            if props:
                pc = read(cursor_spanning_range, portion)
                if pc is not _READ_FAILED:
                    overrides.append((pc, props))

        record_walk_cap(portion_enum, portions, _CAPTURE_PORTION_LIMIT, "text portions", notes)
    return overrides, read.failures, notes


def _has_paragraph_format_redline(para: Any) -> bool:
    portion_enum = para.createEnumeration()
    for portion in iter_elements_limited(portion_enum, _CAPTURE_PORTION_LIMIT):
        if (
            portion.getPropertyValue("TextPortionType") == "Redline"
            and portion.getPropertyValue("RedlineType") == "ParagraphFormat"
        ):
            return True
    return False


def _format_reject_drops_paragraph_restore(para: Any) -> bool:
    """Return True if a whole-paragraph Format redline sits before ParagraphFormat in the table.

    Reject All works from the end of the redline table; a ``Format`` redline over the whole
    paragraph that sits before the ``ParagraphFormat`` one is rejected last, and its
    ``ResetAttrs`` drops the direct formatting just restored (``lcl_RejectRedline``,
    ``DocumentRedlineManager.cxx``; ``SwDoc::ResetAttrs``, ``docfmt.cxx``). Redline portions
    at one position come in table order (``lcl_FillRedlineArray``, ``unoportenum.cxx``).
    ``lcl_SetRedlines`` (``DocumentContentOperationsManager.cxx``) makes one Format redline
    per run and paragraph, so a partial one is harmless.
    """
    leading: set[str] = set()  # Format redlines from the paragraph start, listed before any ParagraphFormat
    closing: set[str] = set()  # redlines that end after the last text
    at_start = True
    has_paragraph_format = False
    for portion in iter_elements_limited(para.createEnumeration(), _CAPTURE_PORTION_LIMIT):
        if portion.getPropertyValue("TextPortionType") != "Redline":
            if portion.getString():
                at_start = False
                closing.clear()
            continue
        identifier = portion.getPropertyValue("RedlineIdentifier")
        kind = portion.getPropertyValue("RedlineType")
        if not portion.getPropertyValue("IsStart"):
            closing.add(identifier)
        elif kind == "ParagraphFormat":
            has_paragraph_format = True
        elif kind == "Format" and at_start and not has_paragraph_format:
            leading.add(identifier)
    return has_paragraph_format and not leading.isdisjoint(closing)


def _flag_stacked_format_redlines(res: dict[str, Any], doc: Any, cursors: list[Any]) -> None:
    """Flag when a whole-paragraph Format redline will be rejected after ParagraphFormat.

    Revert data is not readable through UNO, so a paragraph that had no direct formatting
    is flagged too. Nothing is walked without a ``ParagraphFormat`` redline in the document.
    """
    try:
        redlines = doc.getRedlines()
        # The loop variable must keep each redline alive: dropping it before the next read crashes headless soffice.
        if not any(
            rl.getPropertyValue("RedlineType") == "ParagraphFormat"
            for rl in iter_elements_limited(redlines.createEnumeration(), int(redlines.getCount()))
        ):
            return
        for cursor in cursors:
            for para in _capture_paragraphs(_CountingReader(), _expand_to_full_paragraphs(cursor).createEnumeration(), []):
                if _format_reject_drops_paragraph_restore(para):
                    res["style_unreviewed"] = True
                    msg = (
                        "Rejecting all changes may drop direct paragraph formatting (indents, alignment) "
                        "from before the tracked changes, because a tracked character change covers a whole "
                        "paragraph that also has a tracked paragraph change."
                    )
                    existing = res.get("warning")
                    res["warning"] = f"{existing} {msg}" if existing else msg
                    return
    except Exception as exc:
        reraise_if_disposed(exc)
        # Do not fail the tool: the style is already applied.
        log.exception("Failed to inspect redlines for stacked Format warning")


def _paragraph_property_names(para: Any) -> tuple[str, ...]:
    """Writable paragraph-level property names (all paragraphs share one property map)."""
    return tuple(
        p.Name for p in para.getPropertySetInfo().getProperties()
        if not p.Attributes & PROPERTY_READONLY and not _is_text_property(p.Name)
    )


def _revert_value(name: str, value: Any) -> Any:
    if name in _MARGIN_UNIT_PROPERTIES:
        # SvxTextLeftMarginItem::PutValue (frmitems.cxx) accepts only the exact Pair type.
        return cast("Any", uno).Any("com.sun.star.beans.Pair<double,short>", value)
    return _typed_property_value(name, value)


def _unrestorable_note(names: Iterable[str]) -> str:
    return "Rejecting the paragraph change will not restore these hand-set properties: %s." % ", ".join(sorted(names))


def _paragraph_revert_states(capture_cursor: Any, style_name: str | None = None) -> tuple[list[_ParagraphRevert], bool]:
    """Capture each touched paragraph's pre-change formatting for a ParagraphFormat redline.

    Pass *style_name* to skip paragraphs already in that style without direct properties,
    or ``None`` to capture every touched paragraph.
    """
    read = _CountingReader()
    notes: list[str] = []
    para_enum = capture_cursor.createEnumeration()
    states: list[_ParagraphRevert] = []
    names: tuple[str, ...] = ()
    for para in _capture_paragraphs(read, para_enum, notes):
        if not names:
            names = _paragraph_property_names(para)
        old_style = para.getPropertyValue("ParaStyleName")
        direct = {n for n, s in zip(names, para.getPropertyStates(names)) if s.value == "DIRECT_VALUE"}
        # Void: e.g. a margin set in character units has a value only under its *Unit name.
        captured = [(n, v) for n in _REVERT_PARA_PROPERTIES if n in direct and (v := para.getPropertyValue(n)) is not None]
        unrestorable = tuple(
            n for n in sorted(direct.difference(_REVERT_PARA_PROPERTIES, _UNREPORTED_PARA_PROPERTIES))
            if para.getPropertyValue(n) not in (None, "")
        )
        if style_name is not None and old_style == style_name and not captured and not unrestorable:
            continue
        # ParaStyleName must stay first (unknown names stop makeRedline's loop; empty falls back to default).
        props: list[Any] = [create_property_value("ParaStyleName", old_style)]
        props.extend(create_property_value(n, _revert_value(n, v)) for n, v in captured)
        if para.getPropertyValue("NumberingStyleName"):
            # Reject runs DelNumRules (DocumentRedlineManager.cxx lcl_RejectRedline); without
            # NumberingRules, hand-numbered paragraphs lose their number.
            props.append(create_property_value("NumberingRules", para.getPropertyValue("NumberingRules")))
        # NumberingRules reads as direct for every paragraph in a list, headings included
        # (the Outline list). Reject runs DelNumRules, which drops level, restart and the
        # numbered flag.
        numbered = bool(para.getPropertyValue("ListLabelString")) or (
            "NumberingRules" in direct
        )
        states.append(_ParagraphRevert(
            cursor=cursor_spanning_range(para),
            properties=tuple(props),
            recorded=_has_paragraph_format_redline(para),
            numbered=numbered,
            unrestorable=unrestorable,
        ))
    return states, bool(read.failures or notes)


def _record_paragraph_formats(states: list[_ParagraphRevert], author: str) -> tuple[int, int, set[str]]:
    """Record one ParagraphFormat redline per changed paragraph, returning ``(recorded, failed, unrestorable)``.

    An existing ``ParagraphFormat`` redline is kept as the only one because it holds the state
    before both changes, and Reject All would end on the later redline's state.
    """
    recorded = 0
    failed = 0
    unrestorable: set[str] = set()
    for state in states:
        if state.recorded:
            recorded += 1
            continue
        if state.cursor.isCollapsed():  # AppendRedline refuses empty ranges.
            continue
        try:
            state.cursor.makeRedline(
                "ParagraphFormat",
                (
                    # Explicit RedlineAuthor: makeRedline otherwise uses nAuthor=0
                    # (SwUnoCursorHelper::makeRedline, unocrsrhelper.cxx:1261).
                    create_property_value("RedlineAuthor", author),
                    create_property_value(
                        "RedlineRevertProperties",
                        # Inner sequence must be typed as []PropertyValue.
                        cast("Any", uno).Any("[]com.sun.star.beans.PropertyValue", state.properties),
                    ),
                ),
            )
            recorded += 1
            unrestorable.update(state.unrestorable)
        except Exception as exc:
            reraise_if_disposed(exc)
            # Do not fail the tool: the style is already applied.
            log.exception("Failed to record ParagraphFormat redline")
            failed += 1
    return recorded, failed, unrestorable


def apply_paragraph_style_preserving_direct_char(
    doc: Any,
    cursor: Any,
    style_name: str,
    clear_direct: Literal["none", "style_props", "all"] = "none",
    style_cache: dict[Any, Any] | None = None,
    *,
    author: str | None = None,
) -> ParagraphStyleApplyReport:
    """Set *cursor*'s ParaStyleName to *style_name*, deciding what happens to direct formatting.

    Setting ParaStyleName to a DIFFERENT style resets hand-set Char* properties to that style's
    defaults — across the WHOLE paragraph, even for a sub-range cursor — which is what the capture
    below exists to undo. Re-applying the style a paragraph already has does not reset Char*
    (LibreOffice 26.2). It *does* drop the paragraph's direct Para* (margins, alignment, first-line
    indent) — so re-applying Standard does not keep a hand-set quote indent. Clearing a Char*
    override is never a side effect to rely on; see reset_properties_to_default.
    ``getPropertyState`` is unreliable at the text-portion level (except for hint-level
    overrides on hyperlinks, see ``read_link_look`` in ``hyperlink_fixup``), so overrides
    are detected by VALUE (Char* differs from the paragraph's current style default).

    With *author* set, recording is off and each changed paragraph gets a
    ``ParagraphFormat`` redline via ``SwUnoCursorHelper::makeRedline`` whose Reject restores
    its style, direct paragraph formatting and numbering style.

    *clear_direct* decides what happens to those captured overrides. The helper default is
    ``"none"`` so HTML import / style-restore callers keep inline fonts; ``apply_style``
    defaults to ``"style_props"`` so a house-font update is visible without a retry:
      ``"style_props"`` restores everything except STYLE_GOVERNED_CHAR_PROPERTIES and clears
      CLEARABLE_PARA_PROPERTIES, so font/size/indent obey the style and bold/italic/colour survive;
      ``"none"`` restores all captured Char* — keeps a hand-set font (the style can look like a
      no-op). Does not restore Para*; LibreOffice already dropped those when ParaStyleName was set;
      ``"all"`` restores nothing and clears the paragraph properties too (Ctrl+M semantics).

    A result with a ``warning`` and ``capture_incomplete=True`` means later runs were not read
    back, so the override dict may be short. A warning without ``capture_incomplete`` means
    some captured overrides could not be re-applied; the style still applied.

    Returns a report dict so callers can tell the agent what actually happened.

    KNOWN LIMITATION: a direct override whose value equals the old style default is not
    captured; applying a style with a different default can change that property visibly.
    """
    restore_failures = 0
    if style_cache is None:
        style_cache = {}

    capture_cursor = _expand_to_full_paragraphs(cursor)
    overrides, capture_failures, walk_notes = _capture_direct_char_overrides(doc, capture_cursor, style_cache)
    found = _summarize_char_overrides(overrides)
    states, revert_incomplete = _paragraph_revert_states(capture_cursor, style_name) if author is not None else ([], False)
    try:
        cursor.setPropertyValue("ParaStyleName", style_name)
    except Exception as exc:
        reraise_if_disposed(exc)
        raise

    skip = STYLE_GOVERNED_CHAR_PROPERTIES if clear_direct == "style_props" else ()
    if clear_direct != "all":
        for pc, props in overrides:
            for name, val in props.items():
                if name in skip:
                    continue
                try:
                    pc.setPropertyValue(name, val)
                except Exception as exc:
                    reraise_if_disposed(exc)
                    restore_failures += 1

    report: ParagraphStyleApplyReport = {
        "direct_formatting": "preserved" if clear_direct == "none" else "cleared",
        "clear_direct": clear_direct,
    }
    if clear_direct == "none":
        if found:
            report["preserved_char_overrides"] = found
    else:
        removed = {k: v for k, v in found.items() if clear_direct == "all" or k in skip}
        if removed:
            report["removed_char_overrides"] = removed
        char_targets = (
            STYLE_GOVERNED_CHAR_PROPERTIES if clear_direct == "style_props"
            else sorted({n for _pc, props in overrides for n in props})
        )
        report["cleared_char_properties"] = reset_properties_to_default(capture_cursor, char_targets)
        report["cleared_paragraph_properties"] = reset_properties_to_default(capture_cursor, CLEARABLE_PARA_PROPERTIES)

    if author is not None:
        recorded, failed, unrestorable = _record_paragraph_formats(states, author)
        report["tracked"] = recorded > 0
        unreviewed_notes: list[str] = []
        if report.get("removed_char_overrides") or (clear_direct == "all" and bool(overrides)):
            unreviewed_notes.append(
                "The cleared direct font or character formatting is not a tracked change and cannot be rejected."
            )
        if any(state.numbered for state in states):
            unreviewed_notes.append("Rejecting the style change may not restore list levels or numbers.")
        if unrestorable:
            unreviewed_notes.append(_unrestorable_note(unrestorable))
        # Empty paragraphs are not flagged when at least one paragraph was recorded,
        # because LibreOffice records nothing for them either.
        if failed or revert_incomplete or (states and recorded == 0):
            unreviewed_notes.append(
                "Some paragraphs (for example empty ones) were restyled without a tracked change."
            )
        if unreviewed_notes:
            report["style_unreviewed"] = True
            walk_notes.extend(unreviewed_notes)

    if capture_failures > 0:
        walk_notes.append("Some direct formatting overrides could not be read; the capture may be incomplete.")
    if restore_failures > 0:
        walk_notes.append("Some direct formatting overrides could not be re-applied.")
    if walk_notes:
        report["warning"] = " ".join(walk_notes)
    if capture_failures > 0:
        report["capture_incomplete"] = True
    return report

_STYLE_KEEPALIVE: deque[Any] = deque(maxlen=256)


def keep_style_alive(style: Any) -> None:
    """Hold a reference to *style* so SwXStyle destructor does not race SwXStyleFamily FindStyle.

    The last Python ref sends a oneway URP release. The destructor blocks on
    the SolarMutex at refcount 0. A concurrent or subsequent getByName that
    holds the mutex queryInterfaces that dying listener, and soffice aborts.
    Keeping proxies alive in a bounded deque ensures destruction never overlaps lookup.
    """
    if style is not None:
        _STYLE_KEEPALIVE.append(style)

_CLONE_TEXT_SERVICES = {
    "ParagraphStyles": "com.sun.star.style.ParagraphStyle",
    "CharacterStyles": "com.sun.star.style.CharacterStyle",
}
_TEXT_STYLE_FAMILIES = list(_CLONE_TEXT_SERVICES)

# Families style_list will report and enumerate. PageStyles is listed because the page tools take
# a page-style NAME and a document converted from .docx rarely uses "Standard" — without it the
# only way to learn the real names was to guess one and read them off the error.
_STYLE_FAMILIES = [*_TEXT_STYLE_FAMILIES, "PageStyles", "CellStyles"]
_WRITER_STYLE_LIST_FAMILIES = [*_TEXT_STYLE_FAMILIES, "PageStyles"]


def _style_list_families_for_doc_type(doc_type: str | None) -> list[str]:
    if doc_type == "calc":
        return ["CellStyles"]
    return list(_WRITER_STYLE_LIST_FAMILIES)

_CONDITIONAL_CONTEXTS = [
    "TableHeader", "Table", "Frame", "Section", "Footnote", "Endnote",
    "Header", "Footer",
    *[f"OutlineLevel{i}" for i in range(1, 11)],
    *[f"NumberingLevel{i}" for i in range(1, 11)],
]

_KNOWN_CHARACTER_PROPERTIES = {
    "CharColor": color_param("Main text color (hex string like '#FF0000' or '#0055A4'). Hex, integer, or -1 / automatic / transparent for automatic or no fill."),
    "CharBackColor": color_param("Background/highlight color (hex string). Hex, integer, or -1 / automatic / transparent for automatic or no fill."),
    "CharUnderlineColor": color_param("Underline color (hex string). Hex, integer, or -1 / automatic / transparent for automatic or no fill."),
    "CharWeight": {"type": "number", "description": "Font weight (e.g., 100 for normal, 150 for bold)."},
    "CharHeight": {"type": "number", "description": "Font size in points."},
    "CharFontName": {"type": "string", "description": "Font family name (e.g., 'Arial')."},
    "CharStrikeout": {"type": "integer", "description": "Strikeout type (0=None, 1=Single, 2=Double)."},
    "CharCaseMap": {"type": "integer", "description": "Case mapping (0=None, 1=Uppercase, 2=Lowercase, 3=Title, 4=SmallCaps)."},
    # CharPosture follows com.sun.star.awt.FontSlant (0=None, 1=Oblique, 2=Italic). The schema must match that mapping or italic/oblique requests set the wrong UNO constant.
    "CharPosture": {"type": "integer", "description": "Font slant (com.sun.star.awt.FontSlant: 0=None, 1=Oblique, 2=Italic)."},
    "CharShadowed": {"type": "boolean", "description": "Whether text is shadowed."},
    # "CharRelief": {"type": "integer", "description": "Relief style (0=None, 1=Embossed, 2=Engraved)."},
    "CharHidden": {"type": "boolean", "description": "Whether text is hidden."},
    "CharWordMode": {"type": "boolean", "description": "Whether underline/strikeout applies only to words."}
}

# Schema metadata only — names come from CLEARABLE_PARA_PROPERTIES (in plugin.doc.text_helpers) so the
# inspect set and the clear_direct wipe cannot drift.
_PARA_PROPERTY_SCHEMA = {
    "ParaTopMargin": {"type": "integer", "description": "Top margin, 1/100 mm (1 inch = 2540)."},
    "ParaBottomMargin": {"type": "integer", "description": "Bottom margin, 1/100 mm."},
    "ParaLeftMargin": {"type": "integer", "description": "Left margin, 1/100 mm."},
    "ParaRightMargin": {"type": "integer", "description": "Right margin, 1/100 mm."},
    "ParaFirstLineIndent": {"type": "integer", "description": "First line indent, 1/100 mm."},
    "ParaAdjust": enum_param(["left", "center", "right", "justify"], "Paragraph alignment: left, center, right, or justify."),
    "ParaBackColor": color_param("Paragraph background color (hex string). Hex, integer, or -1 / automatic / transparent for automatic or no fill."),
    "ParaKeepTogether": {"type": "boolean", "description": "Keep lines of the paragraph together."},
    "ParaSplit": {"type": "boolean", "description": "Whether the paragraph is allowed to split across pages."}
}
_KNOWN_PARAGRAPH_PROPERTIES = {
    name: _PARA_PROPERTY_SCHEMA[name] for name in CLEARABLE_PARA_PROPERTIES
}

_BORDER_PROPERTIES = (
    "LeftBorder",
    "RightBorder",
    "TopBorder",
    "BottomBorder",
    "CharLeftBorder",
    "CharRightBorder",
    "CharTopBorder",
    "CharBottomBorder",
)
_BORDER_SCHEMA = {"type": "object", "description": "BorderLine2. Color is a hex string or -1. Line widths are 1/100 mm."}
_STRUCT_SCHEMA = {
    "ParaLineSpacing": {"type": "object", "description": "Mode is prop, minimum, leading, or fix. Height is a percent for prop (150 is 1.5 lines) and 1/100 mm otherwise."},
    "DropCapFormat": {"type": "object", "description": "Fields Lines, Count, Distance."},
    **{b: _BORDER_SCHEMA for b in _BORDER_PROPERTIES},
    "ParaTabStops": {"type": "array", "description": "List of tab stops. Position is 1/100 mm. Alignment is LEFT, CENTER, RIGHT, or DECIMAL."},
}
_UPDATE_SCHEMA_PROPERTIES = {**_KNOWN_CHARACTER_PROPERTIES, **_KNOWN_PARAGRAPH_PROPERTIES, **_STRUCT_SCHEMA}


# ParaAdjust 0/1/2/3 is hostile (1 is right, 2 is justify, 3 is center), so the
# schema uses the words and these maps hold the UNO integers (ParagraphAdjust).
# START and END exist since LO 26.2; older SvxAdjustItem::PutValue
# (editeng/source/items/paraitem.cxx) silently ignores 5 and 6, so the schema
# does not offer them and para_adjust_to_uno falls back to left/right there.
_PARA_ADJUST_TO_UNO = {"left": 0, "right": 1, "justify": 2, "center": 3, "start": 5, "end": 6}
_PARA_ADJUST_FROM_INT = {
    0: "left", 1: "right", 2: "justify", 3: "center", 4: "justify", 5: "start", 6: "end",
}
_PARA_ADJUST_FROM_ENUM = {
    "LEFT": "left", "RIGHT": "right", "BLOCK": "justify",
    "CENTER": "center", "STRETCH": "justify", "START": "start", "END": "end",
}


def _close_style_names(wanted: str, names: list[str]) -> list[str]:
    """Best hint first: exact case-insensitive, then prefix, then substring.

    Shortest name wins on prefix/substring ('body text' → 'Body Text', not
    'Body Text Indent 2'). Same ranking as apply_style.
    """
    low = wanted.lower()
    exact = [n for n in names if n.lower() == low]
    if exact:
        return exact
    # Empty list is falsy, so `exact or prefix` is not list[str] to ty
    # (truthy arm is list[str] & ~AlwaysFalsy). sorted(generator, key=len)
    # also widens to list[Sized] because len's parameter is Sized.
    prefix = [n for n in names if n.lower().startswith(low)]
    if prefix:
        prefix.sort(key=len)
        return prefix
    contains = [n for n in names if low in n.lower()]
    contains.sort(key=len)
    if contains:
        return contains
    return similar_names(wanted, names)


def _missing_style_error(
    tool: Any,
    style_name: str,
    family: str,
    style_family: Any,
    *,
    label: str = "Style",
    replay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Not-found error with a Did-you-mean hint and a short sample of names.

    *replay* is the caller's own arguments, or None when an old_content search needle
    would be truncated (over 100 characters).
    """
    try:
        names = [str(n) for n in style_family.getElementNames()]
    except Exception as exc:
        reraise_if_disposed(exc)
        names = []
    close = _close_style_names(style_name, names) if names else []
    hint = (" Did you mean '%s'?" % close[0]) if close else ""
    sample = (" Available styles include: %s." % ", ".join(sorted(names)[:15])) if names else ""
    miss_details: dict[str, Any] = {"hint": "Call style_list with family=%s." % family}
    if close:
        miss_details["similar"] = close[:5]
    # Only an unambiguous single match is safe to replay (docs/writer/structured-edit-diagnostics-plan.md §3.4).
    suggested = suggest_call(str(tool.name), **{**replay, "style": close[0]}) if replay is not None and len(close) == 1 else None

    if label == "parent_style":
        return tool._tool_error(
            "parent_style '%s' is not a %s style.%s%s" % (style_name, family, hint, sample),
            code="STYLE_NOT_FOUND",
            suggested_call=suggested,
            **miss_details,
        )
    return tool._tool_error(
        "Style '%s' not found in %s.%s%s" % (style_name, family, hint, sample),
        code="STYLE_NOT_FOUND",
        suggested_call=suggested,
        **miss_details,
    )


def _has_paragraph_adjust(name: str) -> bool:
    """Whether this LibreOffice's ParagraphAdjust enum has *name* (START and END exist since 26.2).

    Asks the type system rather than the version number because Collabora Office
    numbers its releases differently. An unknown member makes pyuno's checkEnum
    (pyuno/source/module/pyuno_module.cxx) raise RuntimeException.
    """
    try:
        uno.Enum("com.sun.star.style.ParagraphAdjust", name)
    except RuntimeException:
        return False
    return True


def para_adjust_to_uno(value: Any) -> int:
    """Schema word → UNO integer. Integers are refused: UNO's order (1 right, 2 justify, 3 center) is easy to get wrong."""
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _PARA_ADJUST_TO_UNO:
            if key in ("start", "end") and not _has_paragraph_adjust(key.upper()):
                # Matches pre-26.2 ODF import (xmloff adjushdl.cxx); RTL paragraphs still get left, as old LO did.
                return _PARA_ADJUST_TO_UNO["left" if key == "start" else "right"]
            return _PARA_ADJUST_TO_UNO[key]
    raise ValueError(
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


_AUTO_COLOR_WORDS = frozenset({"automatic", "auto", "transparent", "none"})


def style_color_to_uno(val: Any) -> int | None:
    # Style properties allow -1 and auto words for automatic or no fill;
    # parse_color_to_uno_int refuses them, so handle them here first.
    if isinstance(val, bool):
        return None
    if val == -1:
        return -1
    if isinstance(val, str):
        stripped = val.strip().lower()
        if stripped in _AUTO_COLOR_WORDS:
            return -1
    return parse_color_to_uno_int(val)


def _font_slant_to_int(raw: Any) -> Any:
    # com.sun.star.awt.FontSlant: NONE=0, OBLIQUE=1, ITALIC=2.
    # Return an integer so style_get_info round-trips cleanly through json.dumps.
    if isinstance(raw, int):
        return raw
    val = getattr(raw, "value", raw)
    if isinstance(val, str):
        low = val.strip().lower()
        if low in FONT_SLANT_TO_INT:
            return FONT_SLANT_TO_INT[low]
    return raw


def _get_style_prop(style: Any, prop_name: str, default: Any = None) -> Any:
    try:
        return style.getPropertyValue(prop_name)
    except Exception as exc:
        # A property this style does not have is None. A closed document is not.
        reraise_if_disposed(exc)
        return default


def _schema_prop_value(prop_name: str, raw: Any) -> Any:
    """Read-back form that matches the write schema (ParaAdjust as a word, structs as objects)."""
    if prop_name == "ParaAdjust":
        return _para_adjust_from_uno(raw)
    if prop_name == "CharPosture":
        return _font_slant_to_int(raw)
    if prop_name == "ParaTabStops" and isinstance(raw, (list, tuple)):
        return [_struct_to_json(_STRUCT_PROPERTIES[prop_name], item) for item in raw]
    if prop_name in _STRUCT_PROPERTIES:
        return _struct_to_json(_STRUCT_PROPERTIES[prop_name], raw)
    return _uno_scalar_to_json(raw)


# Properties whose value is a UNO struct, not a simple type. PyUNO cannot take a
# plain dict for these: setPropertyValue calls getTypes() on whatever it is given,
# so a dict would raise "AttributeError: 'dict' object has no attribute 'getTypes'" and
# fail the whole update. Line spacing is the one users hit — it is the only way
# to set 1.5 lines or an exact leading on a style.
_STRUCT_PROPERTIES = {
    "ParaLineSpacing": "com.sun.star.style.LineSpacing",
    "DropCapFormat": "com.sun.star.style.DropCapFormat",
    **{b: "com.sun.star.table.BorderLine2" for b in _BORDER_PROPERTIES},
    "ParaTabStops": "com.sun.star.style.TabStop",
}

# Properties to read per style family. Paragraph styles inherit character properties.
_FAMILY_PROPS = {
    "ParagraphStyles": (
        ["FollowStyle"]
        + list(_KNOWN_PARAGRAPH_PROPERTIES.keys())
        + list(_KNOWN_CHARACTER_PROPERTIES.keys())
        + list(_STRUCT_PROPERTIES.keys())
    ),
    "CharacterStyles": (
        list(_KNOWN_CHARACTER_PROPERTIES.keys())
        + [k for k in _STRUCT_PROPERTIES if k.startswith("Char")]
    ),
}

# com.sun.star.style.LineSpacingMode. Models write the word far more often than
# the number, and the number alone is unreadable in a tool call.
_LINE_SPACING_MODES = {
    "prop": 0, "proportional": 0, "percent": 0,
    "minimum": 1, "min": 1,
    "leading": 2,
    "fix": 3, "exact": 3,
}


# Field lists for reading a struct back as JSON. A UNO struct is not JSON-serializable:
# returning one would make update_style apply the change and then fail to report it.
_STRUCT_FIELDS = {
    "com.sun.star.style.LineSpacing": ("Mode", "Height"),
    "com.sun.star.style.DropCapFormat": ("Lines", "Count", "Distance"),
    "com.sun.star.table.BorderLine2": (
        "Color", "InnerLineWidth", "OuterLineWidth", "LineDistance", "LineStyle", "LineWidth"),
    "com.sun.star.style.TabStop": ("Position", "Alignment", "DecimalChar", "FillChar"),
}
_LINE_SPACING_MODE_WORDS = {0: "prop", 1: "minimum", 2: "leading", 3: "fix"}


def _uno_scalar_to_json(value: Any) -> Any:
    """uno.Enum / uno.Char -> their plain value; everything else unchanged."""
    inner = getattr(value, "value", None)
    if inner is not None and type(value).__module__ in ("uno", "pyuno"):
        return inner
    return value


def _struct_to_json(struct_name: str, value: Any) -> Any:
    fields = _STRUCT_FIELDS.get(struct_name)
    if value is None or not fields:
        return value
    out = {}
    for field in fields:
        try:
            out[field] = _uno_scalar_to_json(getattr(value, field))
        except Exception as exc:
            reraise_if_disposed(exc)
            log.debug("style struct field %s unreadable", field, exc_info=True)
            continue
    if struct_name == "com.sun.star.style.LineSpacing" and isinstance(out.get("Mode"), int):
        out["Mode"] = _LINE_SPACING_MODE_WORDS.get(out["Mode"], out["Mode"])
    return out


def _coerce_uno_field(struct: Any, field: str, value: Any) -> Any:
    """Strings into UNO enum / char fields, detected from the field's own default.

    TabStop.Alignment is a com.sun.star.style.TabAlign enum and FillChar a UNO char:
    setattr with a plain "LEFT" or "." is refused, which would fail the whole update.
    """
    if not isinstance(value, str):
        return value
    current: Any = getattr(struct, field)
    try:
        if isinstance(current, uno.Enum):
            return uno.Enum(cast("Any", current).typeName, value.strip().upper())
        if isinstance(current, uno.Char):
            return uno.Char(value[:1] or " ")
    except Exception:
        # uno.Enum rejects an unknown member; the caller's setattr then reports the field and value.
        return value
    return value


def _struct_field_value(struct_name: str, field: str, value: Any) -> Any:
    """Translate the handful of fields that read better as words."""
    if field == "Color":
        # BorderLine2.Color accepts hex, integer, or -1 / automatic / transparent.
        col = style_color_to_uno(value)
        if col is None:
            raise ValueError("%s.Color is not a color: %r" % (struct_name, value))
        return col
    if struct_name == "com.sun.star.style.LineSpacing" and field == "Mode" and isinstance(value, str):
        mode = _LINE_SPACING_MODES.get(value.strip().lower())
        if mode is None:
            raise ValueError(
                "ParaLineSpacing Mode must be one of prop, minimum, leading, fix "
                "(or the UNO integer), got %r" % (value,)
            )
        return mode
    return value


def dict_to_uno_struct(prop_name: str, struct_name: str, value: Any) -> Any:
    """Build a UNO struct from a plain dict."""
    if not isinstance(value, dict):
        raise ValueError(
            "%s takes an object of fields, e.g. {\"Mode\": \"prop\", \"Height\": 150}, got %r"
            % (prop_name, value)
        )
    try:
        struct = cast("Any", uno.createUnoStruct(struct_name))
    except Exception as e:
        raise ValueError("Could not build %s for %s: %s" % (struct_name, prop_name, e))
    # A TabStop's FillChar starts as NUL; Writer's own tab stops use a space.
    fields = {"FillChar": " "} if struct_name == "com.sun.star.style.TabStop" else {}
    fields.update(value)
    for field, raw in fields.items():
        if not hasattr(struct, str(field)):
            raise ValueError(
                "%s has no field %r. Fields for %s: %s"
                % (prop_name, field, struct_name,
                   ", ".join(sorted(n for n in dir(struct) if not n.startswith("_"))))
            )
        converted = _struct_field_value(struct_name, str(field), raw)
        converted = _coerce_uno_field(struct, str(field), converted)
        try:
            setattr(struct, str(field), converted)
        except Exception as e:
            raise ValueError("%s.%s rejected %r: %s" % (prop_name, field, raw, e))
    return struct


_COLOR_PROPERTIES = ("CharColor", "CharBackColor", "CharUnderlineColor", "ParaBackColor")
# Properties of a text run; every other property of a text range belongs to its paragraph.
_TEXT_PROPERTY_PREFIXES = ("Char", "HyperLink", "Ruby", "Visited", "Unvisited")
# Paragraph properties whose change Reject undoes. Reject keeps a direct page break, page
# style, or writing direction (lcl_RstAttr in docfmt.cxx), so those changes stay untracked.
_RECORDABLE_PARA_PROPERTIES = frozenset({*_REVERT_PARA_PROPERTIES, "NumberingStyleName"} - {"BreakType", "WritingMode"})


def _is_text_property(name: str) -> bool:
    return name.startswith(_TEXT_PROPERTY_PREFIXES)


# Writer rejects setting these to their own default, so a reset of them cannot be recorded.
_UNRECORDABLE_CHAR_RESETS = frozenset({"CharAutoStyleName", "CharInteropGrabBag"})


def _recordable_reset_values(cursor: Any, names: Iterable[str]) -> dict[str, Any]:
    """Values to set before a reset so Track Changes records it.

    SwDoc::ResetAttrs (docfmt.cxx) records nothing. lcl_SetRedline
    (DocumentContentOperationsManager.cxx) stores each portion's old character
    attributes when a value is set. The value itself is irrelevant because the
    reset drops it.
    """
    values: dict[str, Any] = {}
    for name in names:
        if name == "CharStyleName":
            # CharStyleName's property default is "", which SwUnoCursorHelper::SetCharStyle rejects.
            values[name] = _ROOT_CHAR_STYLE
        elif name.startswith("Char") and name not in _UNRECORDABLE_CHAR_RESETS:
            default = cursor.getPropertyDefault(name)
            if default is not None:  # void for property-map entries without a pool item
                values[name] = default
    return values


def _convert_value(prop_name: str, value: Any) -> Any:
    """Convert color strings and validate CharPosture before passing to UNO."""
    if prop_name in _COLOR_PROPERTIES:
        parsed = style_color_to_uno(value)
        if parsed is None:
            raise ValueError("Invalid color: %r" % (value,))
        return parsed
    if prop_name == "CharPosture":
        # com.sun.star.awt.FontSlant: NONE=0, OBLIQUE=1, ITALIC=2. Must be int in {0, 1, 2} and not bool.
        if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1, 2):
            raise ValueError("CharPosture is com.sun.star.awt.FontSlant: 0=None, 1=Oblique, 2=Italic.")
    return value


def _typed_property_value(prop_name: str, value: Any) -> Any:
    """Wrap sequence-of-struct values in uno.Any so PyUNO passes a typed sequence."""
    if prop_name == "ParaTabStops" and isinstance(value, tuple):
        # cast: the type stubs do not declare uno.Any or uno.invoke as callable this way (same for the other casts of uno here).
        return cast("Any", uno).Any("[]com.sun.star.style.TabStop", value)
    return value


def _set_style_property(style: Any, prop_name: str, value: Any) -> None:
    """setPropertyValue that also handles sequence-of-struct properties.

    PyUNO passes a Python tuple of structs as a generic sequence, and Writer refuses it
    for ParaTabStops with a bare IllegalArgumentException. The property needs the
    typed sequence ``[]com.sun.star.style.TabStop``, which only uno.invoke with a
    uno.Any can express (a plain uno.Any argument is rejected outside uno.invoke).
    """
    typed = _typed_property_value(prop_name, value)
    if typed is not value:
        cast("Any", uno).invoke(style, "setPropertyValue", (prop_name, typed))
        return
    style.setPropertyValue(prop_name, value)


def _apply_style_updates(style: Any, updates: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Apply style updates with color conversion and font slant checking. Returns (applied, failed)."""
    applied: dict[str, Any] = {}
    failed: dict[str, str] = {}
    for prop_name, prop_val in updates.items():
        try:
            prop_val = _convert_value(prop_name, prop_val)
        except ValueError as exc:
            failed[prop_name] = str(exc)
            continue

        try:
            _set_style_property(style, prop_name, prop_val)
            applied[prop_name] = _schema_prop_value(prop_name, prop_val)
        except Exception as e:
            if is_disposal(e):
                raise
            log.warning("Failed to set property %s: %s", prop_name, e, exc_info=True)
            hint = ""
            close = _close_style_names(prop_name, list(_UPDATE_SCHEMA_PROPERTIES))
            if close and close[0] != prop_name:
                hint = " Did you mean '%s'?" % close[0]
            failed[prop_name] = "%s%s" % (e, hint)

    return applied, failed


def _validate_conditional_rules(rules: Any, style_family: Any) -> str | None:
    """Validate conditional rules for ParagraphStyles before creating the style."""
    if not rules:
        return None
    for idx, rule in enumerate(rules):
        if not isinstance(rule, dict):
            return f"conditional_rule {idx} must be an object."
        context = rule.get("context")
        target_style = rule.get("target_style")
        if not isinstance(context, str) or not context.strip():
            return f"conditional_rule {idx} missing context."
        if not isinstance(target_style, str) or not target_style.strip():
            return f"conditional_rule {idx} missing target_style."
        if context not in _CONDITIONAL_CONTEXTS:
            return f"conditional_rule {idx}: unknown context '{context}'."
        if not style_family.hasByName(target_style):
            return f"conditional_rule {idx}: target_style '{target_style}' does not exist in ParagraphStyles."
    return None


# Programmatic name of the root character style; its UI name is localized
# (SwStyleNameMapper::FillUIName). Writing it to CharStyleName clears the character style
# and is recorded; an empty string makes SwUnoCursorHelper::SetCharStyle throw.
_ROOT_CHAR_STYLE = "Standard"


def _no_character_style_token_error(tool: Any, family: str, style_name: str) -> dict[str, Any] | None:
    # 'No Character Style' is the apply_style token that clears the character style by writing
    # the root style ('Standard'). Resolving or updating it as a style object would modify that root style.
    if family == "CharacterStyles" and style_name.strip() == "No Character Style":
        return tool._tool_error(
            "No Character Style is the apply_style token that clears the character style. "
            "It is not a style you can inspect, update, or clone. "
            "The character style object is named 'Standard'.",
            code="INVALID_PARAM",
        )
    return None


# Metadata, plus read-only diffs that fail or change value when copied.
# FollowStyle is not in the direct-value walk: paragraph clones set it explicitly
# (even when inherited), and a new page style's FollowStyle must stay itself
# (copying the source's would chain the clone into the source's next style).
_CLONE_SKIP_PROPS = frozenset({
    "Name", "DisplayName", "IsPhysical", "IsUserDefined", "Category",
    "ParentStyle", "FollowStyle", "IsInUse", "UserDefinedAttributes",
    "CharDiffHeight", "CharDiffHeightAsian", "CharDiffHeightComplex",
    # Header/footer XText is read-only (PropertyAttribute.READONLY, attrs 17)
    # and must not be assigned onto the clone. Listing the names here keeps
    # that true if a future LibreOffice drops READONLY. Flow is copied by
    # page._copy_page_regions, not by setPropertyValue. No TypeClass.INTERFACE
    # filter: this walk also clones paragraph and character styles.
    "HeaderText", "HeaderTextLeft", "HeaderTextRight", "HeaderTextFirst",
    "FooterText", "FooterTextLeft", "FooterTextRight", "FooterTextFirst",
})


def copy_direct_style_properties(source: Any, dest: Any) -> tuple[list[str], list[str]]:
    """Copy DIRECT_VALUE properties. Inherited defaults stay inherited.

    Returns (copied names, names that were direct but could not be set).
    page.copy_page_style imports this at call time: this module already imports page.
    """
    try:
        props = source.getPropertySetInfo().getProperties()
    except Exception as e:
        reraise_if_disposed(e)
        raise RuntimeError("Could not read style properties: %s" % e) from e
    copied: list[str] = []
    skipped: list[str] = []
    for prop in props:
        name = getattr(prop, "Name", None)
        if not isinstance(name, str) or name in _CLONE_SKIP_PROPS:
            continue
        try:
            attrs = int(getattr(prop, "Attributes", 0) or 0)
        except (TypeError, ValueError):
            attrs = 0
        if attrs & PROPERTY_READONLY:
            continue
        try:
            state = source.getPropertyState(name)
        except Exception as exc:
            reraise_if_disposed(exc)
            skipped.append(name)
            continue
        # PropertyState.DIRECT_VALUE enum's integer is 0 (first enumerator).
        # Comparing against 1 would copy inherited defaults and skip real overrides.
        if getattr(state, "value", None) != "DIRECT_VALUE":
            continue
        try:
            dest.setPropertyValue(name, source.getPropertyValue(name))
        except Exception as exc:
            reraise_if_disposed(exc)
            log.warning("style clone: could not copy property %s", name, exc_info=True)
            skipped.append(name)
            continue
        copied.append(name)
    return copied, skipped


def _clone_text_style(
    doc: Any,
    family: str,
    source: Any,
    new_name: str,
    style_family: Any,
) -> dict[str, Any]:
    """Paragraph or character sibling: same parent, direct overrides, paragraph FollowStyle."""
    service = _CLONE_TEXT_SERVICES[family]
    clone = doc.createInstance(service)
    if not clone:
        raise RuntimeError("Failed to create style instance for %s" % service)
    try:
        parent = str(source.getParentStyle() or "")
    except Exception as exc:
        reraise_if_disposed(exc)
        parent = ""
    if parent:
        clone.setParentStyle(parent)
    if family == "ParagraphStyles":
        # Set even when FollowStyle is only inherited, so the sibling continues
        # into the same next style instead of the new style's own default.
        follow = source.getPropertyValue("FollowStyle")
        if follow:
            clone.setPropertyValue("FollowStyle", follow)
    copied, skipped = copy_direct_style_properties(source, clone)
    style_family.insertByName(new_name, clone)
    keep_style_alive(clone)
    return {
        "properties_copied": len(copied),
        "skipped_properties": skipped,
        "shapes_copied": 0,
        "skipped_shapes": [],
    }


def _normalize_property_updates(property_updates: Any) -> dict[str, Any]:
    """Copy updates and translate ParaAdjust and UNO structs."""
    updates = dict(property_updates)
    if "ParaAdjust" in updates:
        updates["ParaAdjust"] = para_adjust_to_uno(updates["ParaAdjust"])
    for name, struct_name in _STRUCT_PROPERTIES.items():
        if name in updates:
            val = updates[name]
            if name == "ParaTabStops":
                if isinstance(val, list):
                    updates[name] = tuple(dict_to_uno_struct(name, struct_name, item) for item in val)
            elif isinstance(val, dict):
                updates[name] = dict_to_uno_struct(name, struct_name, val)
    return updates


def _validate_style_updates(
    tool: Any, kwargs: dict[str, Any]
) -> tuple[str, Any, Any, dict[str, Any]] | dict[str, Any]:
    style_name = kwargs["style"].strip()
    if not style_name:
        return tool._tool_error("style is required.", code="INVALID_PARAM")

    family = kwargs["family"]
    parent_style = kwargs.get("parent_style")

    token_err = _no_character_style_token_error(tool, family, style_name)
    if token_err:
        return token_err

    property_updates = kwargs.get("property_updates", {})
    if not isinstance(property_updates, dict):
        return tool._tool_error("property_updates must be an object.", code="INVALID_PARAM")

    try:
        updates = _normalize_property_updates(property_updates)
    except ValueError as exc:
        return tool._tool_error(str(exc), code="INVALID_PARAM")

    return style_name, family, parent_style, updates


_installed_font_names_cache: set[str] | None = None


def _installed_font_names(ctx: ToolContext) -> set[str] | None:
    """Casefolded names LibreOffice can use, or None if the list is unavailable.

    A successful font-name list is cached and reused so CharFontName updates do not
    rebuild a VCL device font walk every time. None is never cached so a headless
    toolkit that appears later can still populate installed fonts.
    """
    global _installed_font_names_cache
    if _installed_font_names_cache is not None:
        return _installed_font_names_cache
    try:
        toolkit = get_toolkit(getattr(ctx, "ctx", None))
        if toolkit is None:
            return None
        device = toolkit.createScreenCompatibleDevice(1, 1)
        if device is None:
            return None
        names = {str(f.Name).casefold() for f in device.getFontDescriptors() if getattr(f, "Name", None)}
        _installed_font_names_cache = names
        return names
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


def _style_family(tool: Any, doc: Any, family: str) -> Any:
    return tool.get_item(
        doc,
        "getStyleFamilies",
        family,
        missing_msg="Document does not support style families.",
        not_found_msg="Unknown style family: %s" % family,
        list_tool="style_list",
    )


class StyleList(ToolWriterStyleBase):
    """List available styles in a given family."""

    name: str | None = "style_list"
    uno_services: list[str] | None = [TEXT_DOCUMENT_SERVICE, CALC_DOCUMENT_SERVICE]
    description: str = (
        "List available styles in the document. Omit family to list style family names; "
        "set family to list styles in that family (Writer: paragraph, character, and page styles; "
        "Calc: cell styles only)."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "family": enum_param(
                _STYLE_FAMILIES,
                "Omit to list family names. On Writer: ParagraphStyles, CharacterStyles, or PageStyles. On Calc: CellStyles.",
            )
        },
        "required": [],
    }

    def get_parameters(self, doc_type: str | None = None) -> dict[str, Any] | None:
        params = copy.deepcopy(cast("dict[str, Any]", self.parameters))
        family_prop = cast("dict[str, Any]", params["properties"]["family"])
        family_prop["enum"] = _style_list_families_for_doc_type(doc_type)
        return params

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        family = kwargs.get("family")
        doc = ctx.doc
        families = doc.getStyleFamilies()
        list_families = _style_list_families_for_doc_type(ctx.doc_type)

        if not family or not str(family).strip():
            if ctx.doc_type == "calc":
                available = ["CellStyles"] if families.hasByName("CellStyles") else []
            else:
                available = [f for f in families.getElementNames() if f in list_families]
            return {"status": "ok", "families": available, "count": len(available)}

        family = str(family).strip()
        if ctx.doc_type == "calc" and family != "CellStyles":
            available = ["CellStyles"] if families.hasByName("CellStyles") else []
            return self._tool_error(
                "Unknown style family: %s" % family,
                code="UNO_OBJECT_ERROR",
                **name_miss_details(family, available, "style_list"),
            )

        style_family = _style_family(self, doc, family)
        if isinstance(style_family, dict):
            return style_family

        # Always use "auto" filter logic to show used, custom, and common built-in styles.
        styles = []
        element_names = style_family.getElementNames()

        for name in element_names:
            style = style_family.getByName(name)

            # Predicates for language-agnostic filtering
            in_use = style.isInUse()
            user_defined = style.isUserDefined()
            custom = in_use or user_defined
            is_physical = bool(_get_style_prop(style, "IsPhysical", True))
            is_hidden = bool(_get_style_prop(style, "Hidden", False))

            # Filter logic (auto)
            if is_hidden:
                continue

            # Core visibility logic:
            show = custom or is_physical

            if family == "ParagraphStyles":
                try:
                    cat = style.getPropertyValue("Category")
                except Exception as exc:
                    reraise_if_disposed(exc)
                    log.debug("style list: Category unreadable for %r", name, exc_info=True)
                else:
                    if not show and cat == 1:
                        show = True
                    if show:
                        if cat in (2, 3, 4, 5) and not custom:
                            show = False
                        elif name in ("Heading", "Standard", "Default Paragraph Style"):
                            show = False
                        elif cat == 1 and not custom:
                            try:
                                level = int(name[len("Heading ") :])
                                if level > 5:
                                    show = False
                            except (ValueError, TypeError):
                                pass
                        elif cat == 0 and not custom:
                            core_text_styles = ("Text body", "Title", "Subtitle")
                            if name not in core_text_styles:
                                show = False
            elif family == "CharacterStyles" and not show:
                core_char_styles = (_ROOT_CHAR_STYLE, "Source Text")
                if name in core_char_styles:
                    show = True

            if not show:
                continue

            entry = {"name": name, "is_user_defined": user_defined, "is_in_use": in_use}
            # Present the root character style as "No Character Style" — the
            # clearer name that matches what the LLM should pass to apply_style.
            if family == "CharacterStyles" and name == _ROOT_CHAR_STYLE:
                entry["name"] = "No Character Style"
            try:
                entry["parent_style"] = style.getParentStyle()
            except Exception as exc:
                reraise_if_disposed(exc)
                log.debug("style list: getParentStyle failed for %r", name, exc_info=True)
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
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"style": {"type": "string", "description": "Name of the style to inspect."}, "family": {"type": "string", "default": "ParagraphStyles", "description": "Style family. PageStyles returns the same page-style payload as page_get_style_properties."}}, "required": ["style"]}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        style_name = kwargs["style"]
        family = kwargs["family"]

        # In-process dispatch to the same reader page_get_style_properties uses — not an error
        # bounce and not a nested LLM call.
        if family == "PageStyles":
            result = get_page_style_properties(ctx.doc, style_name or "Standard")
            if result.get("status") == "ok":
                result = {**result, "family": "PageStyles"}
            return result

        token_err = _no_character_style_token_error(self, family, style_name)
        if token_err:
            return token_err

        doc = ctx.doc
        style_family = _style_family(self, doc, family)
        if isinstance(style_family, dict):
            return style_family

        if not style_family.hasByName(style_name):
            return _missing_style_error(self, style_name, family, style_family)

        style = style_family.getByName(style_name)
        info = {"name": style_name, "family": family, "is_user_defined": style.isUserDefined(), "is_in_use": style.isInUse()}

        if family in _FAMILY_PROPS:
            # getParentStyle does not throw on a live style.
            info["ParentStyle"] = style.getParentStyle()
            for prop_name in _FAMILY_PROPS[family]:
                info[prop_name] = _schema_prop_value(prop_name, _get_style_prop(style, prop_name))

        return {"status": "ok", **info}


def target_cursors(
    tool: FrameworkToolBase,
    ctx: ToolContext,
    kwargs: dict[str, Any],
) -> tuple[list[Any], dict[str, Any] | None, search_mod.MatchMode | None]:
    """Resolve target kwargs into (cursors, error_payload, search_mode)."""
    target = kwargs.get("target", "selection")
    if target != "search":
        try:
            cursor = resolve_target_cursor(
                ctx,
                target,
                kwargs.get("old_content"),
                case_sensitive=kwargs.get("case_sensitive"),
            )
            return [cursor], None, None
        except ValueError as ve:
            return [], tool._tool_error(str(ve), code="INVALID_PARAM"), None

    # target='search' resolves its own ranges here so a miss returns the structured
    # build_search_not_found_response payload; resolve_target_cursor serves other targets
    # and raises ValueError. Each match gets a cursor in its own text object (cell/frame safe).
    s = search_mod.prepare_old_content(kwargs.get("old_content"))
    if not s:
        return [], tool._tool_error(
            "target='search' requires old_content.",
            code="INVALID_PARAM",
            top_level={"applied": False, "matched": False},
        ), None
    cs = search_mod.explicit_case_sensitive(kwargs.get("case_sensitive"))
    occ = kwargs.get("occurrence")
    many = bool(kwargs.get("all_matches")) or occ is not None
    ranges, mode = search_mod.find_literal_ranges(
        ctx.doc,
        s,
        case_sensitive=cs,
        limit=search_mod.MAX_SEARCH_REPLACEMENTS if many else 1,
    )
    if not ranges:
        res = search_mod.build_search_not_found_response(
            ctx.doc, s, case_sensitive=mode.case_sensitive, all_matches=many, refuse_oversized=True
        )
        res.update({"target": "search", "applied": False, "matched": False})  # build_search_not_found_response takes no top_level kwarg; the shared builder's payload is extended here
        res.update(mode.result_fields())
        return [], res, mode
    if occ is not None:
        # Schema type integer (bool fails). validate has no min/max.
        if occ < 0 or occ >= len(ranges):
            err = tool._tool_error(
                "occurrence %s out of range (found %d match(es), use 0..%d)."
                % (occ, len(ranges), len(ranges) - 1),
                code="OCCURRENCE_OUT_OF_RANGE",
            )
            err.update(mode.result_fields())
            return [], err, mode
        ranges = [ranges[occ]]
    return [cursor_spanning_range(r) for r in ranges], None, mode


def apply_to_cursors(
    tool: FrameworkToolBase,
    ctx: ToolContext,
    cursors: list[Any],
    apply_one: Callable[[Any], Any],
    label: str,
) -> tuple[list[Any], dict[str, Any] | None]:
    # One compound undo step per tool call: one range can take many UNO calls.
    with agent_format_author(ctx.doc, ctx.ctx, cursors):
        title = next_agent_edit_undo_title()
        reports: list[Any] = []

        def mutate() -> None:
            for c in cursors:
                reports.append(apply_one(c))

        try:
            outcome = commit_or_rollback(ctx.doc, title, mutate)
        # Writing here anyway would add no undo entry, so Ctrl+Z would revert the user's
        # previous edit and leave the style in place
        # (SfxUndoManager::ImplAddUndoAction_NoNotify, svl/source/undo/undo.cxx).
        except UndoUnavailable:
            return [], tool._tool_error(
                (
                    "Cannot apply %s: LibreOffice is not recording undo for this document, "
                    "so the change could not be undone. Nothing was changed. Do not repeat the call. "
                    "Tell the user to save, close and reopen the document; if it still fails, "
                    "Undo/Steps in LibreOffice's Expert Configuration must be at least 1."
                )
                % label,
                code="UNDO_UNAVAILABLE",
            )
        if outcome.mutate_error is not None:
            error = outcome.mutate_error
            if not outcome.group_closed:
                return [], tool._tool_error(
                    "Applied to %d of %d; failed on one match: %s. The undo step could not be closed, so part of the change may be in the document."
                    % (len(reports), len(cursors), error),
                    code="TOOL_EXECUTION_ERROR",
                )
            # Not rolled back with no finished range: the first write was refused before it
            # changed anything, so the undo group is empty.
            if outcome.rolled_back or not reports:
                return [], tool._tool_error(
                    "Nothing was changed: %s" % (error,),
                    # set_cursor_properties raises ValueError for a value Writer rejects.
                    code="INVALID_PARAM" if isinstance(error, ValueError) else "TOOL_EXECUTION_ERROR",
                )
            return [], tool._tool_error(
                "Applied to %d of %d; failed on one match: %s. Earlier matches stay applied."
                % (len(reports), len(cursors), error),
                code="TOOL_EXECUTION_ERROR",
            )

        err = outcome.error
        if err is not None:
            if err.get("code") == "UNDO_CONTEXT_LEAK":
                return [], tool._tool_error(
                    "Applied %s to %d match(es), but the undo step could not be closed (%s). "
                    "The changes are in the document and the undo context is still open."
                    % (label, len(reports), err.get("message")),
                    code="UNDO_CONTEXT_LEAK",
                )
            if err.get("code") == "ROLLBACK_FAILED":
                return [], tool._tool_error(
                    "Applied to %d of %d; failed on one match: %s. Earlier matches stay applied."
                    % (len(reports), len(cursors), err.get("message")),
                    code="ROLLBACK_FAILED",
                )
        return reports, None


def _with_normalized_old_content(kwargs: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    if kwargs.get("target") == "search":
        result.setdefault("normalized_old_content", search_mod.prepare_old_content(kwargs.get("old_content")))
    return result


_SEARCH_MATCH_PROPERTIES: dict[str, Any] = {
    "case_sensitive": {"type": "boolean", "description": f"For target='search': {search_mod.LADDER_CASE_SENSITIVE_DESCRIPTION}"},
    "all_matches": {"type": "boolean", "default": False, "description": "For target='search': apply to EVERY occurrence of old_content (false = first only)."},
    "occurrence": {"type": "integer", "description": "For target='search': apply to this single 0-based occurrence instead of the first."},
}


class ApplyStyle(FrameworkToolBase):
    """Apply a paragraph or character style."""

    name: str | None = "apply_style"
    description: str = (
        "Apply a style to a target. Use family='ParagraphStyles' for paragraph "
        "styles (e.g. Heading 1) or family='CharacterStyles' for character "
        "styles (e.g. Source Text). Use 'No Character Style' "
        "with family='CharacterStyles' to remove a character style (hand-set formatting stays). "
        "Use target='selection' (default), 'beginning', 'end', 'full_document', "
        "or 'search' with old_content. "
        "Re-applying a style does not keep a quote indent — LibreOffice drops direct Para*."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "style": {"type": "string", "description": "Style name (e.g. Heading 1, Source Text)."},
            "family": enum_param(_TEXT_STYLE_FAMILIES, ("Style family."), default="ParagraphStyles"),
            "target": enum_param(INSERT_TARGETS, "Where to apply the style."),
            "old_content": {"type": "string", "description": "Text to find and apply style to if target = 'search'."},
            **_SEARCH_MATCH_PROPERTIES,
            "clear_direct": {"type": "string", "enum": ["none", "style_props", "all"], "default": "style_props", "description": (
                "Direct (hand-set) formatting on the target, ParagraphStyles only. "
                "'none' keeps hand-set character formatting (font, size, emphasis); a style change still resets direct paragraph indents and alignment. "
                "'style_props' drops direct font, size, paragraph indents/spacing and alignment so the style shows; bold, italic and colour stay. "
                "'all' drops every direct override, like Ctrl+M; refused with target='full_document'. "
                "To drop hand-set formatting without changing the style, use style_format_text reset.")},
        },
        "required": ["style"],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    is_mutation: bool | None = True

    def _apply_one(
        self,
        ctx: ToolContext,
        family: str,
        uno_value: str,
        cursor: Any,
        clear_direct: Literal["none", "style_props", "all"] = "style_props",
        *,
        author: str | None = None,
    ) -> ParagraphStyleApplyReport | None:
        """Apply the style to one cursor. Returns the direct-formatting report (or None)."""
        if family == "ParagraphStyles":
            return apply_paragraph_style_preserving_direct_char(
                ctx.doc, cursor, uno_value, clear_direct, author=author
            )
        cursor.setPropertyValue("CharStyleName", uno_value)
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
                # Keep distinct warnings across ranges without repeating identical ones.
                existing = out.get("warning")
                if not existing:
                    out["warning"] = rep["warning"]
                elif rep["warning"] not in existing:
                    out["warning"] = f"{existing} {rep['warning']}"
            if rep.get("capture_incomplete"):
                out["capture_incomplete"] = True
            if "tracked" in rep:
                out["tracked"] = out.get("tracked", False) or rep["tracked"]
            if rep.get("style_unreviewed"):
                out["style_unreviewed"] = True
        # No retry hint: default is already style_props (house font shows). preserved_char_overrides
        # is only set on the explicit none path, which the caller asked for.
        return out

    @staticmethod
    def _resolve_clear_direct(family: str, raw: Any) -> str:
        """Paragraph styles default to style_props so the house font shows without a retry.

        Character styles ignore direct-clear flags; omitted or schema-default 'style_props'
        treats as 'none'. Explicit 'all' is preserved so execute can reject it.
        """
        val = "" if raw is None else str(raw).strip()
        if family == "CharacterStyles":
            if val in ("", "none", "style_props"):
                return "none"
            return val
        if not val:
            return "style_props"
        return val

    def _style_not_found(
        self,
        ctx: ToolContext,
        style_name: str,
        family: str,
        uno_value: str,
        kwargs: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Resolve the style name before any document write so near-miss names return a guided
        tool error with case-insensitive suggestions instead of an opaque UNO
        IllegalArgumentException.
        """
        fam = uno_read(lambda: ctx.doc.getStyleFamilies().getByName(family), None)
        if fam is None or uno_read(lambda: fam.hasByName(uno_value), True):
            return None  # can't enumerate styles (exotic doc): let the apply path report
        props = (self.parameters or {}).get("properties", {})
        args = {k: v for k, v in kwargs.items() if k in props}
        old_c = args.get("old_content")
        replay = None if isinstance(old_c, str) and len(old_c) > 100 else args  # no truncated needle
        return _missing_style_error(self, style_name, family, fam, replay=replay)

    def _success(
        self,
        style_name: str,
        family: str,
        target: str,
        reports: list[ParagraphStyleApplyReport | None],
        *,
        char_tracked: bool,
    ) -> dict[str, Any]:
        res: dict[str, Any] = {
            "status": "ok",
            "style_name": style_name,
            "family": family,
            "target": target,
            "applied": True,
        }
        if target == "search":
            res.update({"matched": True, "applied_count": len(reports)})
            res["message"] = "Applied style '%s' (%s) to %d match(es)." % (style_name, family, len(reports))
        else:
            res["message"] = "Applied style '%s' (%s) to %s." % (style_name, family, target)
        res.update(self._merge_reports(reports))
        if char_tracked:
            res["tracked"] = True
        if "tracked" not in res:
            flag_style_unreviewed(res)
        if res.get("tracked"):
            return annotate_review_status(res)
        return res

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        return _with_normalized_old_content(kwargs, self._execute(ctx, **kwargs))

    def _execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        style_name = kwargs["style"].strip()
        if not style_name:
            return self._tool_error("style is required.", code="INVALID_PARAM")

        family = kwargs.get("family", "ParagraphStyles")
        target = kwargs.get("target", "selection")

        # The clear token writes the root style; see _ROOT_CHAR_STYLE.
        uno_value = _ROOT_CHAR_STYLE if (family == "CharacterStyles" and style_name == "No Character Style") else style_name

        miss = self._style_not_found(ctx, style_name, family, uno_value, kwargs)
        if miss is not None:
            return miss

        clear_direct_raw = self._resolve_clear_direct(family, kwargs.get("clear_direct"))
        if clear_direct_raw not in ("none", "style_props", "all"):
            return self._tool_error("clear_direct must be one of: none, style_props, all.", code="INVALID_PARAM")
        clear_direct = cast("Literal['none', 'style_props', 'all']", clear_direct_raw)
        if clear_direct != "none":
            if family != "ParagraphStyles":
                return self._tool_error(
                    "clear_direct only applies to family='ParagraphStyles'. "
                    "Use style_format_text reset to drop hand-set character formatting.",
                    code="INVALID_PARAM",
                )
            if clear_direct == "all" and target == "full_document":
                # Ctrl+M on the whole document wipes emphasis as well as fonts — force the
                # per-range path. style_props (the default) is allowed: house font/size win
                # and bold/italic/colour stay.
                return self._tool_error(
                    "clear_direct='all' is not allowed with target='full_document': it would "
                    "flatten emphasis across the whole document. Style each range separately "
                    "(target='search') instead, or omit clear_direct to use style_props.",
                    code="INVALID_PARAM")

        cursors, err, search_mode = target_cursors(self, ctx, kwargs)
        if err is not None:
            return err

        author = None
        recording_cm: Any = nullcontext()
        if family == "CharacterStyles":
            if review_recording_enabled():
                recording_cm = record_changes(ctx.doc, True)
        elif review_recording_enabled() or is_recording_changes(ctx.doc):
            author = paragraph_redline_author(ctx.ctx)
            # Recording is off during the write because LibreOffice would record the Char* restore
            # as Format redlines whose Reject keeps the new style; makeRedline records the paragraph
            # change itself, whether or not the document was recording.
            recording_cm = record_changes(ctx.doc, False)
        with recording_cm:
            # A caret has no text to record a Format redline on.
            char_tracked = (
                family == "CharacterStyles"
                and is_recording_changes(ctx.doc)
                and not all(c.isCollapsed() for c in cursors)
            )
            reports, err = apply_to_cursors(
                self,
                ctx,
                cursors,
                lambda c: self._apply_one(ctx, family, uno_value, c, clear_direct, author=author),
                "style '%s'" % style_name,
            )
        if err is not None:
            return err

        res = self._success(style_name, family, target, reports, char_tracked=char_tracked)
        if res.get("tracked"):
            _flag_stacked_format_redlines(res, ctx.doc, cursors)
        if search_mode is not None:
            res.update(search_mode.result_fields())
        return res


class StyleFormatText(ToolWriterStyleBase):
    """Set or reset direct formatting properties on text without touching other attributes."""

    name: str | None = "style_format_text"
    description: str = (
        "Set or reset direct formatting on text, keeping everything else. "
        "properties uses UNO names and the same value forms as style_update: "
        "CharWeight (150 bold, 100 normal), CharHeight (points), CharPosture (2 italic), "
        "CharFontName/CharFontNameAsian/CharFontNameComplex, CharColor/CharBackColor (hex), "
        "ParaAdjust (left/center/right/justify), ParaLineSpacing object, Para*Margin (1/100 mm); "
        "any property of a Writer text range is accepted. "
        "reset lists names whose hand-set value is dropped so the style shows. "
        "Paragraph-level properties (Para*, NumberingStyleName, PageDescName, BreakType) change every paragraph the target touches. "
        "target='full_document' covers body text and tables (not headers, footers, frames or footnotes)."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "properties": {
                "type": "object",
                "description": "UNO property name to value to set as direct formatting.",
            },
            "reset": {
                "type": "array",
                "items": {"type": "string"},
                "description": "UNO property names whose direct formatting is removed so the style shows.",
            },
            "target": enum_param(["selection", "full_document", "search"], "Text to change.", default="selection"),
            "old_content": {"type": "string", "description": "Text to find and change if target = 'search'."},
            **_SEARCH_MATCH_PROPERTIES,
        },
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        return _with_normalized_old_content(kwargs, self._execute(ctx, **kwargs))

    def _execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        raw_props = kwargs.get("properties")
        if raw_props is None:
            raw_props = {}
        if not isinstance(raw_props, dict):
            return self._tool_error("properties must be an object.", code="INVALID_PARAM")

        raw_reset = kwargs.get("reset")
        if raw_reset is None:
            raw_reset = []
        if not isinstance(raw_reset, list):
            return self._tool_error("reset must be an array of property names.", code="INVALID_PARAM")

        reset_names: list[str] = []
        seen_reset: set[str] = set()
        for item in raw_reset:
            if not isinstance(item, str) or not item.strip():
                return self._tool_error("reset entries must be non-empty property names.", code="INVALID_PARAM")
            name = item.strip()
            if name not in seen_reset:
                seen_reset.add(name)
                reset_names.append(name)

        if not raw_props and not reset_names:
            return self._tool_error("Specify at least one property in properties or reset.", code="INVALID_PARAM")

        overlap = [name for name in raw_props if name in seen_reset]
        if overlap:
            return self._tool_error(
                "Cannot both set and reset the same property: %s." % ", ".join(overlap),
                code="INVALID_PARAM",
            )

        try:
            updates = convert_direct_properties(raw_props)
        except ValueError as exc:
            return self._tool_error(str(exc), code="INVALID_PARAM")

        target = kwargs.get("target", "selection")
        cursors, err, search_mode = target_cursors(self, ctx, kwargs)
        if err is not None:
            return err
        if target == "selection" and cursors[0].isCollapsed():
            return self._tool_error(
                "Selection is empty (caret only): select text first, or use target='search'/set_selection.",
                code="INVALID_PARAM",
            )

        all_names = list(updates.keys()) + reset_names
        prop_err = _unsupported_property_error(self, cursors[0].getPropertySetInfo(), all_names)
        if prop_err is not None:
            return prop_err

        reset_values = _recordable_reset_values(cursors[0], reset_names)
        char_tracked = False
        para_author: str | None = None

        # One body-spanning cursor covers tables (including nested tables) because
        # lcl_InsAttr and ResetAttrs walk every node in the range.
        def apply_one(cursor: Any) -> _DirectFormatReport:
            states, incomplete = (
                _paragraph_revert_states(_expand_to_full_paragraphs(cursor))
                if para_author is not None
                else ([], False)
            )
            pre_updates = {
                k: v
                for k, v in ({**reset_values, **updates} if char_tracked else updates).items()
                if k != "ParaBackColor"
            }
            set_cursor_properties(cursor, pre_updates)
            if "ParaBackColor" in updates:
                for para in _touched_paragraphs(cursor):
                    set_paragraph_background(para, updates["ParaBackColor"])
            cleared = reset_properties_to_default(cursor, reset_names)
            # SwXTextCursor::setPropertiesToDefault (sw/source/core/unocore/unoobj.cxx:2593-2607)
            # silently skips properties outside RES_FRMATR_END (except NumberingStartValue),
            # such as ParaStyleName or ListId; those remain DIRECT_VALUE on the cursor.
            actually_cleared: list[str] = []
            for name in cleared:
                try:
                    state = cursor.getPropertyState(name)
                    if getattr(state, "value", None) == "DIRECT_VALUE":
                        continue
                except Exception as exc:
                    reraise_if_disposed(exc)
                actually_cleared.append(name)
            if para_author is None:
                return _DirectFormatReport(actually_cleared, False, False, frozenset())
            # After the character write: Reject All works backwards, and rejecting a Format redline
            # over a whole paragraph also drops the paragraph's direct formatting (SwDoc::ResetAttrs),
            # so the ParagraphFormat redline must be rejected last. Rejecting it on a list item or a
            # heading drops the list level or numbering (DelNumRules in lcl_RejectRedline), so those
            # get none.
            recordable = [s for s in states if s.recorded or not s.numbered]
            recorded, failed, unrestorable = _record_paragraph_formats(recordable, para_author)
            complete = recorded > 0 and not failed and not incomplete and len(recordable) == len(states)
            return _DirectFormatReport(actually_cleared, recorded > 0, complete, frozenset(unrestorable))

        with (record_changes(ctx.doc, True) if review_recording_enabled() else nullcontext()):
            recording = is_recording_changes(ctx.doc)
            char_tracked = recording and bool(reset_values or any(_is_text_property(n) for n in updates))
            if recording and any(n in _RECORDABLE_PARA_PROPERTIES for n in all_names):
                para_author = paragraph_redline_author(ctx.ctx)
            reports, err = apply_to_cursors(self, ctx, cursors, apply_one, "direct formatting")
        if err is not None:
            return err

        tracked = char_tracked or any(r.para_recorded for r in reports)
        para_complete = all(r.para_complete for r in reports)
        unrestorable = frozenset().union(*(r.unrestorable for r in reports))
        cleared_set = {name for rep in reports for name in rep.cleared}
        reset_done = [name for name in reset_names if name in cleared_set]
        not_reset = [name for name in reset_names if name not in cleared_set]

        applied_props = {name: _schema_prop_value(name, val) for name, val in updates.items()}
        after: dict[str, Any] = {}
        for name in all_names:
            try:
                if name == "ParaBackColor":
                    # The cursor reads its own RES_BACKGROUND, not the paragraph fill.
                    raw_after = next(_touched_paragraphs(cursors[0])).getPropertyValue(name)
                else:
                    raw_after = cursors[0].getPropertyValue(name)
                after[name] = _schema_prop_value(name, raw_after) if raw_after is not None else None
            except Exception as exc:
                reraise_if_disposed(exc)
                after[name] = None

        res: dict[str, Any] = {
            "status": "ok",
            "target": target,
            "applied": bool(applied_props or reset_done),
            "properties": applied_props,
            "reset": reset_done,
            "after": after,
            "tracked": tracked,
        }
        if not_reset:
            res["not_reset"] = not_reset
        if target == "search":
            res.update({"matched": True, "applied_count": len(reports)})
            scope_desc = "%d match(es)" % len(reports)
        else:
            scope_desc = target

        parts = []
        if applied_props:
            parts.append("set %s" % ", ".join(applied_props.keys()))
        if reset_done:
            parts.append("reset %s" % ", ".join(reset_done))
        if parts:
            res["message"] = "Applied direct formatting (%s) on %s." % ("; ".join(parts), scope_desc)
        else:
            res["message"] = "No direct formatting was changed on %s (could not reset: %s)." % (
                scope_desc, ", ".join(not_reset)
            )

        warnings: list[str] = []
        for font_prop in FONT_NAME_PROPERTIES:
            if font_prop in updates:
                w = _font_not_installed_warning(ctx, updates[font_prop])
                if w:
                    warnings.append(w)

        def is_untracked(name: str) -> bool:
            if name in _RECORDABLE_PARA_PROPERTIES:
                return not para_complete
            if name in reset_names:
                return name not in reset_values
            return not _is_text_property(name)

        untracked = [n for n in (*updates, *reset_done) if is_untracked(n)]
        if recording and untracked:
            warnings.append(
                "Not fully recorded as a tracked change (headings, list items and empty paragraphs "
                "are not recorded, nor are page breaks, page styles, writing direction and list levels): %s."
                % ", ".join(untracked)
            )
        if unrestorable:
            warnings.append(_unrestorable_note(unrestorable))
        if not_reset:
            warnings.append(
                "Could not reset as direct formatting: %s." % ", ".join(not_reset)
            )
        if warnings:
            res["warning"] = " ".join(warnings)

        if tracked:
            res = annotate_review_status(res)
            _flag_stacked_format_redlines(res, ctx.doc, cursors)
        if untracked or not tracked or unrestorable:
            flag_style_unreviewed(res, ctx.doc)
        if search_mode is not None:
            res.update(search_mode.result_fields())
        return res


class StyleUpdate(ToolWriterStyleBase):
    """Update properties of an existing paragraph or character style."""

    name: str | None = "style_update"
    description: str = (
        "Update the properties of an existing style. "
        "Provide 'family' (ParagraphStyles or CharacterStyles), 'style_name', and "
        "'property_updates': a dictionary of UNO property names to values "
        "(e.g. {'CharColor': '#FF0000', 'CharWeight': 150, 'ParaAdjust': 'center'}). "
        "ParaAdjust is left/center/right/justify, not 0/1/2/3. "
        "Colors can be provided as hex strings or integers. "
        "Struct properties take an object of fields: ParaLineSpacing "
        "{'Mode': 'prop'|'minimum'|'leading'|'fix', 'Height': N} (Height is a percent "
        "for prop -- 150 is 1.5 lines -- and 1/100 mm for the others); the border "
        "properties (Left/Right/Top/BottomBorder, and the Char* ones) take a "
        "BorderLine2 object; ParaTabStops takes a list of TabStop objects. "
        "You can also update the 'parent_style' separately."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "style": {"type": "string", "description": "Name of the style to modify (e.g., 'Heading 1', 'Source Text')."},
            "family": enum_param(_TEXT_STYLE_FAMILIES, "Style family.", default="ParagraphStyles"),
            "parent_style": {"type": "string", "description": "Name of the style to inherit from."},
            "property_updates": {
                "type": "object",
                "description": "Dictionary of UNO property names to values (keys are listed in the schema).",
                "properties": _UPDATE_SCHEMA_PROPERTIES,
            },
        },
        "required": ["style"],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        validated = _validate_style_updates(self, kwargs)
        if isinstance(validated, dict):
            return validated
        style_name, family, parent_style, updates = validated

        if not updates and parent_style is None:
            return self._tool_error("Nothing to update. Pass property_updates and/or parent_style.", code="INVALID_PARAM")

        doc = ctx.doc
        style_family = _style_family(self, doc, family)
        if isinstance(style_family, dict):
            return style_family

        if not style_family.hasByName(style_name):
            return _missing_style_error(self, style_name, family, style_family)

        if parent_style is not None:
            parent_style = str(parent_style).strip()
            if not parent_style:
                return self._tool_error("parent_style is empty.", code="INVALID_PARAM")
            if not style_family.hasByName(parent_style):
                return _missing_style_error(
                    self, parent_style, family, style_family, label="parent_style")

        style = style_family.getByName(style_name)

        applied: dict[str, Any] = {}
        failed: dict[str, str] = {}
        snapshot_keys = list(updates)
        if parent_style is not None:
            snapshot_keys = ["ParentStyle"] + snapshot_keys

        def snap(parent_fallback: Any) -> dict[str, Any]:
            res: dict[str, Any] = {}
            for key in snapshot_keys:
                if key == "ParentStyle":
                    try:
                        res[key] = style.getParentStyle()
                    except Exception as exc:
                        reraise_if_disposed(exc)
                        res[key] = parent_fallback
                else:
                    res[key] = _schema_prop_value(key, _get_style_prop(style, key))
            return res

        before = snap(None)

        if parent_style is not None:
            try:
                style.setParentStyle(parent_style)
                applied["ParentStyle"] = parent_style
            except Exception as e:
                if is_disposal(e):
                    raise
                log.warning("Failed to set ParentStyle on %s: %s", style_name, e, exc_info=True)
                failed["ParentStyle"] = str(e)

        applied_props, failed_props = _apply_style_updates(style, updates)
        applied.update(applied_props)
        failed.update(failed_props)

        after = snap(applied.get("ParentStyle"))

        fields: dict[str, Any] = {"style_name": style_name, "family": family}
        if snapshot_keys:
            fields["before"] = before
            fields["after"] = after
        if applied:
            fields["updated_properties"] = applied
        if failed:
            fields["failed_properties"] = failed

        if failed and not applied:
            result = self._tool_error("Failed to apply any updates.", top_level=fields)
        else:
            result = {"status": "ok", **fields}

        if "CharFontName" in applied:
            warning = _font_not_installed_warning(ctx, applied["CharFontName"])
            if warning:
                result["warning"] = warning

        flag_style_unreviewed(result, ctx.doc)
        return result


class StyleCreate(ToolWriterStyleBase):
    """Create a new paragraph or character style."""

    name: str | None = "style_create"
    description: str = (
        "Create a new paragraph or character style with optional inheritance "
        "and property settings. For paragraph styles, you can also define "
        "conditional rules mapping contexts (like Table or Header) to other styles."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "style": {"type": "string", "description": "Name of the new style."},
            "family": enum_param(_TEXT_STYLE_FAMILIES, "Style family.", default="ParagraphStyles"),
            "parent_style": {"type": "string", "description": "Name of the style to inherit from (e.g. 'Standard', 'Default Paragraph Style')."},
            "property_updates": {
                "type": "object",
                "description": "Initial properties to set on the style.",
                "properties": _UPDATE_SCHEMA_PROPERTIES,
            },
            "conditional_rules": {
                "type": "array",
                "description": "Optional conditional rules (ParagraphStyles only).",
                "items": {
                    "type": "object",
                    "properties": {
                        "context": enum_param(_CONDITIONAL_CONTEXTS, "Context where the rule applies."),
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
        validated = _validate_style_updates(self, kwargs)
        if isinstance(validated, dict):
            return validated
        style_name, family, parent_style, updates = validated
        conditional_rules = kwargs.get("conditional_rules")

        doc = ctx.doc
        style_family = _style_family(self, doc, family)
        if isinstance(style_family, dict):
            return style_family
        if style_family.hasByName(style_name):
            return self._tool_error("Style '%s' already exists in %s." % (style_name, family), code="INVALID_PARAM")

        if conditional_rules:
            if family == "CharacterStyles":
                return self._tool_error("conditional_rules are only supported for ParagraphStyles.", code="INVALID_PARAM")
            cond_err = _validate_conditional_rules(conditional_rules, style_family)
            if cond_err:
                return self._tool_error(cond_err, code="INVALID_PARAM")

        service = "com.sun.star.style.ConditionalParagraphStyle" if conditional_rules else _CLONE_TEXT_SERVICES.get(family, "com.sun.star.style.ParagraphStyle")

        actual_parent = str(parent_style).strip() if parent_style is not None else ""
        if not actual_parent and conditional_rules:
            actual_parent = "Standard"
        if actual_parent and not style_family.hasByName(actual_parent):
            return _missing_style_error(
                self, actual_parent, family, style_family, label="parent_style")

        new_style = doc.createInstance(service)
        if not new_style:
            return self._tool_error("Failed to create style instance for %s" % service, code="TOOL_EXECUTION_ERROR")

        if actual_parent:
            try:
                new_style.setParentStyle(actual_parent)
            except Exception as e:
                if is_disposal(e):
                    raise
                log.warning("Failed to set parent_style '%s' on new style", actual_parent, exc_info=True)
                return self._tool_error(f"Failed to set parent_style '{actual_parent}': {e}", code="TOOL_EXECUTION_ERROR")

        applied, failed = _apply_style_updates(new_style, updates)
        if failed:
            return self._tool_error(
                "Style was not created. Failed properties: %s" % (failed,),
                top_level={"failed_properties": failed},
            )

        # Only insert if all requested properties were applied without failure.
        style_family.insertByName(style_name, new_style)

        # Apply conditional rules
        if conditional_rules:
            try:
                conditions = []
                for rule in cast("list[dict[str, str]]", conditional_rules):
                    # createUnoStruct("com.sun.star.beans.NamedValue") is the UNO struct.
                    nv = cast("Any", uno.createUnoStruct("com.sun.star.beans.NamedValue"))
                    nv.Name = rule["context"]
                    nv.Value = rule["target_style"]
                    conditions.append(nv)
                new_style.setPropertyValue("ParaStyleConditions", tuple(conditions))
            except Exception as cond_err:
                if is_disposal(cond_err):
                    raise
                log.warning("Failed to set ParaStyleConditions on new style", exc_info=True)
                # ParaStyleConditions is read-only at the UNO level; fail loudly and rollback the created style.
                try:
                    if style_family.hasByName(style_name):
                        style_family.removeByName(style_name)
                except Exception as rb_err:
                    log.warning("Failed to rollback created style '%s' after condition failure: %s", style_name, rb_err)
                return self._tool_error(f"Failed to apply conditional rules to style: {cond_err}", code="TOOL_EXECUTION_ERROR")

        keep_style_alive(new_style)

        result = {"status": "ok", "style_name": style_name, "family": family, "service": service}
        if "CharFontName" in applied:
            warning = _font_not_installed_warning(ctx, applied["CharFontName"])
            if warning:
                result["warning"] = warning
        flag_style_unreviewed(result, ctx.doc)
        return result


class StyleClone(ToolWriterStyleBase):
    """Clone a paragraph, character, or page style under a new name."""

    name: str | None = "style_clone"
    description: str = (
        "Clone a paragraph, character, or page style into a new name. "
        "Paragraph and character clones share the source parent and copy direct "
        "property overrides; paragraph clones also copy FollowStyle. "
        "family=PageStyles copies direct page properties, in-flow header/footer "
        "content (including fields), and floating graphics anchored in those "
        "regions. The new style is not assigned to any page."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "style": {"type": "string", "description": "Name of the style to copy."},
            "new_style": {"type": "string", "description": "Name of the new style. Must not already exist."},
            "family": enum_param(["ParagraphStyles", "PageStyles", "CharacterStyles"], "Style family.", default="ParagraphStyles"),
        },
        "required": ["style", "new_style"],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        style_name = kwargs["style"].strip()
        new_name = kwargs["new_style"].strip()
        if not style_name:
            return self._tool_error("style is required.", code="INVALID_PARAM")
        if not new_name:
            return self._tool_error("new_style is required.", code="INVALID_PARAM")
        if style_name == new_name:
            return self._tool_error("new_style must be a different name from style.", code="INVALID_PARAM")

        family = kwargs["family"]
        # Schema enum: ParagraphStyles, PageStyles, CharacterStyles.

        token_err = _no_character_style_token_error(self, family, style_name)
        if token_err:
            return token_err

        doc = ctx.doc
        # getStyleFamilies does not fail on a live document. execute_safe
        # reports a closed document.
        style_family = _style_family(self, doc, family)
        if isinstance(style_family, dict):
            return style_family
        if not style_family.hasByName(style_name):
            return _missing_style_error(self, style_name, family, style_family)
        if style_family.hasByName(new_name):
            return self._tool_error("Style '%s' already exists in %s." % (new_name, family), code="INVALID_PARAM")

        with WriterCompoundUndo(doc, "WriterAgent: Clone style"):
            if family == "PageStyles":
                stats = copy_page_style(doc, style_name, new_name)
            else:
                stats = _clone_text_style(
                    doc, family, style_family.getByName(style_name), new_name, style_family,
                )

        result = {
            "status": "ok",
            "family": family,
            "source_style": style_name,
            "new_style": new_name,
            **stats,
        }
        flag_style_unreviewed(result, doc)
        return result


class StyleImport(ToolWriterStyleBase):
    """Import styles from an external document or template."""

    name: str | None = "style_import"
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
        file_path: str = kwargs["path"]
        if not file_path.strip():
            return self._tool_error("path is required.", code="INVALID_PARAM")
        file_path = file_path.strip()
        if not os.path.isabs(file_path):
            return self._tool_error("path must be an absolute filesystem path.", code="INVALID_PARAM")
        if not os.path.isfile(file_path):
            return self._tool_error("path does not exist: %s" % file_path, code="INVALID_PARAM")
        # SwDocShell::LoadStylesFromFile (sw/source/uibase/app/docsh2.cxx:1632-1693) silently skips
        # non-Writer ODF storage packages and returns ERRCODE_NONE without throwing.
        if os.path.splitext(file_path)[1].lower() not in (".odt", ".ott"):
            return self._tool_error("style_import only supports .odt and .ott files.", code="INVALID_PARAM")

        overwrite = kwargs.get("overwrite", True)
        load_text = kwargs.get("load_paragraph_styles", True)
        load_page = kwargs.get("load_page_styles", False)
        load_frame = kwargs.get("load_frame_styles", False)
        load_num = kwargs.get("load_numbering_styles", False)

        # Without any Load* flag and OverwriteStyles=True, SwgReaderOption::IsFormatsOnly
        # (sw/inc/shellio.hxx:114) is false so LO imports content.xml into the document;
        # with OverwriteStyles=False the call is a silent no-op.
        if not any((load_text, load_page, load_frame, load_num)):
            return self._tool_error("Choose at least one style type to load.", code="INVALID_PARAM")

        url = uno.systemPathToFileUrl(file_path)

        opts = (
            create_property_value("OverwriteStyles", overwrite),
            create_property_value("LoadTextStyles", load_text),
            create_property_value("LoadPageStyles", load_page),
            create_property_value("LoadFrameStyles", load_frame),
            create_property_value("LoadNumberingStyles", load_num),
        )

        # XStyleLoader is implemented by the style families, not the document
        # (sw/source/core/unocore/unostyle.cxx:SwXStyleFamilies::loadStylesFromURL).
        ctx.doc.getStyleFamilies().loadStylesFromURL(url, opts)

        result = {"status": "ok", "file_path": file_path, "overwrite": overwrite}
        flag_style_unreviewed(result, ctx.doc)
        return result
