# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Extract and apply data-lo-style block styles for Writer HTML import.

Collects data-lo-style attributes per top-level block, strips them before StarWriter
import, and resolves/applies paragraph styles via UNO after import.
"""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, ClassVar, final

from plugin.framework.errors import reraise_if_disposed, uno_read

from . import xhtml_style_postprocess as xhtml_post

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

log = logging.getLogger("writeragent.writer")


def _strip_data_lo_attrs(start_tag: str) -> str:
    """Remove any data-lo-style or data-lo-para attribute from a single start-tag string."""
    return re.sub(r'\s+data-lo-(?:style|para)=(?:"[^"]*"|\'[^\']*\')', "", start_tag)


def _declaration_names(css: str) -> list[str]:
    """Collect CSS property names declared in a data-lo-para string in declaration order."""
    names: list[str] = []
    for part in css.split(";"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        prop = part.split(":", 1)[0].strip().lower()
        if prop:
            names.append(prop)
    return names


_LENGTH_RE = re.compile(r"^(-?(?:\d+(?:\.\d+)?|\.\d+))(cm|mm|in|pt|pc|px)$")


def _parse_length_to_unit(val: str) -> int | None:
    if val in ("0", "-0"):
        return 0
    m = _LENGTH_RE.match(val)
    if not m:
        return None
    num_str, unit = m.group(1), m.group(2)
    try:
        num = float(num_str)
    except ValueError:
        return None
    from plugin.doc.visual_helpers import UNITS_PER_INCH, mm_to_unit

    if unit == "cm":
        return mm_to_unit(num * 10)
    if unit == "mm":
        return mm_to_unit(num)
    if unit == "in":
        return int(round(num * UNITS_PER_INCH))
    if unit == "pt":
        return int(round(num * 2540 / 72))
    if unit == "pc":
        return int(round(num * 2540 / 6))
    if unit == "px":
        return int(round(num * 2540 / 96))
    return None


_PERCENT_RE = re.compile(r"^(\d+(?:\.\d+)?)%$")


def _length_writer(uno_prop: str) -> Callable[[str], tuple[str, Any] | None]:
    """Writer for a CSS length that maps onto one 1/100 mm paragraph property."""
    def write(val: str) -> tuple[str, Any] | None:
        length = _parse_length_to_unit(val)
        return None if length is None else (uno_prop, length)
    return write


def _line_height_fields(val: str) -> dict[str, Any] | None:
    """CSS line-height as ODF writes it → LineSpacing fields (Mode word, Height).

    ODF emits ``fo:line-height`` as a percentage (proportional spacing) or a length
    (fixed spacing). ``normal`` is single spacing, the same as 100%.
    """
    if val == "normal":
        return {"Mode": "prop", "Height": 100}
    m = _PERCENT_RE.match(val)
    if m:
        pct = int(round(float(m.group(1))))
        # Same rule as a zero/negative length: 0% would collapse the line.
        if pct <= 0:
            return None
        return {"Mode": "prop", "Height": pct}
    length = _parse_length_to_unit(val)
    if length is not None and length > 0:
        return {"Mode": "fix", "Height": length}
    return None


# CSS names that reads emit in data-lo-para (xhtml_post._FODT_OVERRIDE_ATTRS) but writes
# cannot apply, so they always come back in warnings. Paragraph-level character properties
# are set on the SwXParagraph, and Writer applies them over the whole paragraph text: a bold
# or coloured span inside the paragraph merges into one portion and loses its own value
# (checked against a live Writer with setPropertyValue("CharWeight") on a paragraph holding
# a bold run). An agent copying a paragraph back would silently flatten its spans.
# test_every_read_para_name_has_a_writer_or_is_listed_read_only keeps this set and the
# writers table covering every name the read side emits.
_READ_ONLY_PARA_NAMES = frozenset({"font-family", "font-size", "font-weight", "font-style", "color"})


def _para_writers() -> dict[str, Callable[[str], tuple[str, Any] | None]]:
    """CSS name → converter from the raw CSS value to ``(uno_prop, uno_value)``, or None.

    A converter returns None when the value cannot be understood; the name is then
    reported as not applied. Returns an empty table when styles.py is missing: LibrePy
    ships this module without it (scripts/librepy_bundle_paths.py), and
    test_librepy_shipped_function_level_imports_are_safe checks that the import stays
    guarded. The one caller, _apply_block_lo_styles, already returns before parsing in
    that build, so the empty table only keeps the import graph safe.
    """
    try:
        from .styles import _dict_to_uno_struct, _para_adjust_to_uno, _style_color_to_uno
    except ImportError:
        return {}

    def text_align(val: str) -> tuple[str, Any] | None:
        try:
            return ("ParaAdjust", _para_adjust_to_uno(xhtml_post._ODF_TEXT_ALIGN.get(val, val)))
        except ValueError:
            return None

    def line_height(val: str) -> tuple[str, Any] | None:
        fields = _line_height_fields(val)
        if fields is None:
            return None
        return ("ParaLineSpacing", _dict_to_uno_struct("ParaLineSpacing", "com.sun.star.style.LineSpacing", fields))

    def background(val: str) -> tuple[str, Any] | None:
        col = _style_color_to_uno(val)
        # _apply_para_props turns ParaBackColor into the FillColor/FillStyle pair.
        return None if col is None else ("ParaBackColor", col)

    return {
        "text-align": text_align,
        "margin-left": _length_writer("ParaLeftMargin"),
        "margin-right": _length_writer("ParaRightMargin"),
        "margin-top": _length_writer("ParaTopMargin"),
        "margin-bottom": _length_writer("ParaBottomMargin"),
        # Negative is valid here: it is a hanging first line.
        "text-indent": _length_writer("ParaFirstLineIndent"),
        "line-height": line_height,
        "background-color": background,
    }


def parse_data_lo_para(css: str) -> tuple[dict[str, tuple[str, Any]], list[str]]:
    """Parse a data-lo-para CSS fragment into UNO property mappings and unapplied names.

    Returns ``(mapped, dropped)``. ``mapped`` is {css_prop: (uno_prop_name, uno_value)} for
    every declaration _para_writers() can convert. ``dropped`` lists, in declaration order,
    the names with no writer or with a value the writer could not convert. When a name is
    declared twice, the first declaration wins.
    """
    writers = _para_writers()
    seen: set[str] = set()
    mapped: dict[str, tuple[str, Any]] = {}
    dropped: list[str] = []

    for part in css.split(";"):
        prop, sep, val = part.partition(":")
        prop = prop.strip().lower()
        if not sep or not prop or prop in seen:
            continue
        seen.add(prop)
        writer = writers.get(prop)
        try:
            result = writer(val.strip()) if writer is not None else None
        except ValueError:
            # _dict_to_uno_struct raises ValueError when a struct field rejects the value.
            result = None
        if result is None:
            dropped.append(prop)
        else:
            mapped[prop] = result

    return mapped, dropped


@final
class _BlockLoStyleExtractor(xhtml_post._HtmlPassthroughParser):
    """Collect data-lo-style and data-lo-para per TOP-LEVEL block (document order) and strip
    the attributes from the HTML, so the StarWriter import sees clean markup and we apply the
    formatting ourselves afterwards. Content inside <table> is left to the import (avoids order desync)."""

    # Paragraph-level blocks cannot contain blocks: a new one closes the open one (<p>a<p>b).
    _PARA_LEVEL: ClassVar[frozenset[str]] = frozenset({"p", "pre", "h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self) -> None:
        super().__init__()
        self.styles: list[str | None] = []
        self.paras: list[str | None] = []
        self.skipped_para: list[str] = []
        # Open block-level elements: [tag, first child block already shared this slot?], or
        # [tag, None] for a transparent <div> (no slot of its own).
        self._open: list[list[Any]] = []

    @staticmethod
    def _style_of(attrs: list[tuple[str, str | None]]) -> str | None:
        return dict(attrs).get("data-lo-style")

    @staticmethod
    def _para_of(attrs: list[tuple[str, str | None]]) -> str | None:
        return dict(attrs).get("data-lo-para")

    def _is_block(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        # A <div> is a transparent container -- except the one the read path gives a
        # data-lo-style or data-lo-para: LibreOffice exports a paragraph holding a frame
        # (an image) as <div class="paragraph-X">, and that div is one Writer paragraph on import too.
        return tag in xhtml_post.BLOCK_TAGS or (
            tag == "div" and (self._style_of(attrs) is not None or self._para_of(attrs) is not None)
        )

    def _slot(self, style: str | None, para: str | None) -> None:
        # <blockquote><p> and <li><p> take two slots but make ONE Writer
        # paragraph, so every later style lands one paragraph late ("2. DO
        # DIREITO" loses its heading, a list item becomes a quote). A
        # block's first child block is that same paragraph -- it shares
        # the slot, and its own style/para wins when it has one.
        parent = self._open[-1] if self._open else None
        if parent is not None and parent[1] is False:
            parent[1] = True
            if style is not None:
                self.styles[-1] = style
            if para is not None:
                self.paras[-1] = para
        else:
            self.styles.append(style)
            self.paras.append(para)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or ("<%s>" % tag)
        t = tag.lower()
        if t == "table":
            self._table_depth += 1
            para = self._para_of(attrs)
            if para is not None:
                self.skipped_para.append(para)
            self._out.append(_strip_data_lo_attrs(raw))
            return
        if self._table_depth or (t not in xhtml_post.BLOCK_TAGS and t != "div"):
            # Inline tags, and blocks inside a table: attributes are stripped (cell styles are out
            # of scope); data-lo-para is recorded as skipped.
            para = self._para_of(attrs)
            if para is not None:
                self.skipped_para.append(para)
            self._out.append(_strip_data_lo_attrs(raw))
            return
        if not self._is_block(t, attrs):
            self._open.append([t, None])  # transparent div
            self._out.append(_strip_data_lo_attrs(raw))
            return
        while self._open and self._open[-1][0] in self._PARA_LEVEL:
            self._open.pop()
        self._slot(self._style_of(attrs), self._para_of(attrs))
        self._open.append([t, False])
        self._out.append(_strip_data_lo_attrs(raw))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or ("<%s/>" % tag)
        t = tag.lower()
        if self._table_depth == 0 and self._is_block(t, attrs):
            self._slot(self._style_of(attrs), self._para_of(attrs))
            self._out.append(_strip_data_lo_attrs(raw))
        else:
            para = self._para_of(attrs)
            if para is not None:
                self.skipped_para.append(para)
            self._out.append(_strip_data_lo_attrs(raw))

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "table" and self._table_depth > 0:
            self._table_depth -= 1
        elif self._table_depth == 0 and (t in xhtml_post.BLOCK_TAGS or t == "div"):
            # Pop up to and including the matching open element (tolerates an omitted </p>).
            for idx in range(len(self._open) - 1, -1, -1):
                if self._open[idx][0] == t:
                    del self._open[idx:]
                    break
        self._out.append("</%s>" % tag)


def _extract_block_lo_styles(
    html: str,
) -> tuple[str, list[str | None], list[str | None], list[str]]:
    """Return ``(clean_html, styles, para_css, skipped_para)`` per top-level block.

    Short-circuits (returns the html unchanged, three empty lists) when neither data-lo-style
    nor data-lo-para is present in the markup."""
    if not html or ("data-lo-style" not in html and "data-lo-para" not in html):
        return html, [], [], []
    ex = _BlockLoStyleExtractor()
    ex.feed(html)
    ex.close()
    return "".join(ex._out), ex.styles, ex.paras, ex.skipped_para


def _resolve_paragraph_style_token(
    fam: Any,
    token: str,
    element_names: list[str] | tuple[str, ...],
) -> str:
    """Resolve an agent-facing compact ``data-lo-style`` token to a real UNO ParaStyleName.

    A candidate is any paragraph style whose name equals the token (covers space-free names
    and an agent that passed the exact spaced form) OR whose space-free form equals the token
    (``Heading1`` -> ``Heading 1``). Exactly one candidate -> use it. **More than one ->
    ambiguous** (e.g. a literal ``Heading1`` coexisting with built-in ``Heading 1``); fail safe
    to ``Standard`` instead of silently picking one. No candidate -> case-insensitive resolve,
    then ``Standard``. The ambiguity gate runs BEFORE any exact-name shortcut so a colliding
    token can never silently land on the wrong style (per docs/writer/html-style-model-plan.md)."""
    # dict.fromkeys keeps first-seen order and drops duplicates.
    candidates: list[str] = list(
        dict.fromkeys(
            n for n in element_names
            if n == token or xhtml_post.compact_lo_style_name(n) == token
        )
    )
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        log.debug("data-lo-style: token %r is ambiguous across %r; falling back to Standard",
                  token, candidates)
        return "Standard"
    lower = token.lower()
    resolved = token
    for name in element_names:
        if name.lower() == lower:
            resolved = name
            break
    if fam is None or uno_read(lambda: fam.hasByName(resolved), False):
        return resolved
    return "Standard"


def unapplied_para_names_for_content(content: str) -> list[str]:
    """Collect all data-lo-para property names declared in *content* (none applied)."""
    if "data-lo-para" not in content:
        return []
    _clean, _styles, paras, skipped = _extract_block_lo_styles(content)
    return declared_para_names(paras, skipped)


def declared_para_names(*css_lists: Iterable[str | None]) -> list[str]:
    """Return every property name declared across lists of data-lo-para strings, in order.

    Used whenever a whole set of declarations is reported as not applied: blocks the
    cursor walk never reached, targets that do not apply paragraph formatting, or a build
    without the styles module. ``None`` entries (blocks without the attribute) are skipped.
    """
    return [name for css_list in css_lists for css in css_list if css for name in _declaration_names(css)]


def _get_fill_style_enums() -> tuple[Any, Any]:
    """Return (SOLID, NONE) FillStyle enums for paragraph background fill."""
    import uno
    return (
        uno.Enum("com.sun.star.drawing.FillStyle", "SOLID"),
        uno.Enum("com.sun.star.drawing.FillStyle", "NONE"),
    )


def _apply_para_props(para_obj: Any, mapped: dict[str, tuple[str, Any]]) -> list[str]:
    """Apply parsed UNO paragraph properties to one Writer paragraph (``SwXParagraph``).

    Everything is set on the paragraph element, not on a text cursor: the paragraph is the
    only object that exposes the drawing-fill properties a background needs (see below), and
    it accepts ParaAdjust and the margins just as well. Returns the CSS property names that
    failed to apply.
    """
    failed: list[str] = []
    for css_name, (uno_prop, val) in mapped.items():
        try:
            if css_name == "background-color":
                # Setting ParaBackColor on a cursor only writes RES_BACKGROUND without
                # converting to fill attributes (only SwXStyle::SetPropertyValue in
                # sw/source/core/unocore/unostyle.cxx converts RES_BACKGROUND to drawing fill).
                # getPropertyValue("ParaBackColor") delegates via SwXParagraph to
                # getSvxBrushItemFromSourceSet (svx/source/unodraw/unobrushitemhelper.cxx),
                # which returns COL_AUTO (-1) when FillStyle is missing or NONE.
                # Direct paragraph backgrounds must set FillColor and FillStyle on the
                # SwXParagraph element itself (FILL_PROPERTIES_SW in
                # sw/source/core/unocore/unomap1.cxx GetParagraphPropertyMap; not exposed
                # on COMPLETE_TEXT_CURSOR_MAP).
                solid_style, none_style = _get_fill_style_enums()
                if val == -1:
                    para_obj.setPropertyValue("FillStyle", none_style)
                else:
                    para_obj.setPropertyValue("FillColor", val)
                    para_obj.setPropertyValue("FillStyle", solid_style)
            else:
                para_obj.setPropertyValue(uno_prop, val)
        except Exception as exc:
            reraise_if_disposed(exc)
            log.debug("data-lo-para: failed to set %s (%s)", uno_prop, css_name, exc_info=True)
            failed.append(css_name)
    return failed


def _apply_block_lo_styles(
    model: Any,
    text_obj: Any,
    start: Any,
    styles: list[str | None],
    end: Any = None,
    paras: list[str | None] | None = None,
) -> list[str]:
    """Apply each block's data-lo-style and data-lo-para to the inserted paragraphs.

    Forward cursor walk from the insertion point avoids scanning the whole document.
    Compact tokens (``Heading1``) are resolved to UNO names; unknown styles fall back to
    'Standard' (per the style-model plan). Para* properties are applied after the paragraph
    style so style application does not overwrite them. Returns CSS property names that
    could not be applied.
    """
    # LibrePy ships this module without styles.py (scripts/librepy_bundle_paths.py), so the
    # import fails there and no paragraph formatting is applied; every declaration is then
    # reported as not applied. test_librepy_shipped_function_level_imports_are_safe checks
    # that this import stays guarded.
    try:
        from .styles import apply_paragraph_style_preserving_direct_char
    except ImportError:
        return declared_para_names(paras or [])

    fam = uno_read(lambda: model.getStyleFamilies().getByName("ParagraphStyles"), None)
    element_names: list[str] = uno_read(lambda: list(fam.getElementNames()), []) if fam is not None else []
    para_list = paras if paras is not None else [None] * len(styles)
    failed: list[str] = []

    try:
        start_range = start.getStart() if hasattr(start, "getStart") else start
        walk_cur = text_obj.createTextCursorByRange(start_range)
        walk_cur.gotoStartOfParagraph(False)
    except Exception as exc:
        reraise_if_disposed(exc)
        return declared_para_names(para_list)

    style_cache: dict[Any, Any] = {}
    reached_count = 0

    for idx, (style, para_css) in enumerate(zip(styles, para_list)):
        # Stop at the first paragraph that does not start before the parked end cursor.
        try:
            if end is not None and text_obj.compareRegionStarts(walk_cur.getStart(), end) != 1:
                break
        except Exception as exc:
            reraise_if_disposed(exc)
            break

        reached_count = idx + 1

        if style:
            resolved = _resolve_paragraph_style_token(fam, style, element_names=element_names)
            # The import already set the style; re-applying only re-reads every portion.
            current = uno_read(lambda: walk_cur.getPropertyValue("ParaStyleName"), None)
            if current != resolved:
                try:
                    cur = text_obj.createTextCursorByRange(walk_cur.getStart())
                    cur.gotoEndOfParagraph(True)
                    apply_paragraph_style_preserving_direct_char(
                        model, cur, resolved, style_cache=style_cache,
                    )
                except Exception as exc:
                    reraise_if_disposed(exc)
                    log.debug("data-lo-style: failed to apply %r", style, exc_info=True)

        if para_css:
            mapped, dropped = parse_data_lo_para(para_css)
            failed.extend(dropped)
            if mapped:
                # A cursor spanning just this paragraph enumerates exactly one element: the
                # SwXParagraph at the walk position, never a paragraph elsewhere in the document.
                try:
                    para_cur = text_obj.createTextCursorByRange(walk_cur.getStart())
                    para_cur.gotoEndOfParagraph(True)
                    enum = para_cur.createEnumeration()
                    para_obj = enum.nextElement() if enum.hasMoreElements() else None
                except Exception as exc:
                    reraise_if_disposed(exc)
                    log.debug("data-lo-para: could not resolve the paragraph", exc_info=True)
                    para_obj = None
                if para_obj is None:
                    failed.extend(mapped.keys())
                else:
                    failed.extend(_apply_para_props(para_obj, mapped))

        try:
            if not walk_cur.gotoNextParagraph(False):
                break
        except Exception as exc:
            reraise_if_disposed(exc)
            break

    failed.extend(declared_para_names(para_list[reached_count:]))
    return failed
