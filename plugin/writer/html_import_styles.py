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
from typing import Any, ClassVar, final

from plugin.framework.errors import reraise_if_disposed, uno_read

from . import xhtml_style_postprocess as xhtml_post

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
        prop, _ = part.split(":", 1)
        prop = prop.strip().lower()
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


def parse_data_lo_para(css: str) -> tuple[dict[str, tuple[str, Any]], list[str]]:
    """Parse a data-lo-para CSS fragment into UNO property mappings and unapplied names.

    Maps text-align, margin-left, margin-right, and background-color to their respective
    UNO property values. Returns a 2-tuple of (mapped_dict, dropped_css_names), where
    mapped_dict is {css_prop: (uno_prop_name, uno_value)}. Declarations that cannot be
    mapped, unrecognised property names, or mapped properties with invalid values are
    returned in dropped_css_names in the order they were declared.
    """
    try:
        from .styles import _para_adjust_to_uno, _style_color_to_uno
    except ImportError:
        _para_adjust_to_uno = None  # type: ignore[assignment]
        _style_color_to_uno = None  # type: ignore[assignment]

    seen: set[str] = set()
    mapped: dict[str, tuple[str, Any]] = {}
    dropped: list[str] = []

    for part in css.split(";"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        prop, val = part.split(":", 1)
        prop = prop.strip().lower()
        val = val.strip()
        if not prop or prop in seen:
            continue
        seen.add(prop)

        if prop == "text-align":
            mapped_val = xhtml_post._ODF_TEXT_ALIGN.get(val, val)
            if _para_adjust_to_uno is not None:
                try:
                    mapped[prop] = ("ParaAdjust", _para_adjust_to_uno(mapped_val))
                    continue
                except ValueError:
                    pass
            dropped.append(prop)
        elif prop == "margin-left":
            length = _parse_length_to_unit(val)
            if length is not None:
                mapped[prop] = ("ParaLeftMargin", length)
            else:
                dropped.append(prop)
        elif prop == "margin-right":
            length = _parse_length_to_unit(val)
            if length is not None:
                mapped[prop] = ("ParaRightMargin", length)
            else:
                dropped.append(prop)
        elif prop == "background-color":
            if _style_color_to_uno is not None:
                col = _style_color_to_uno(val)
                if col is not None:
                    mapped[prop] = ("ParaBackColor", col)
                    continue
            dropped.append(prop)
        else:
            dropped.append(prop)

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
    unapplied: list[str] = []
    for p in paras:
        if p:
            unapplied.extend(_declaration_names(p))
    for s in skipped:
        if s:
            unapplied.extend(_declaration_names(s))
    return unapplied


def _get_fill_style_enums() -> tuple[Any, Any]:
    """Return (SOLID, NONE) FillStyle enums for paragraph background fill."""
    import uno
    return (
        uno.Enum("com.sun.star.drawing.FillStyle", "SOLID"),
        uno.Enum("com.sun.star.drawing.FillStyle", "NONE"),
    )


def _apply_para_props(
    para_cur: Any,
    para_obj: Any,
    mapped: dict[str, tuple[str, Any]],
) -> list[str]:
    """Apply parsed UNO paragraph properties to *para_cur* and background fill to *para_obj*.

    Returns a list of CSS property names that failed to apply.
    """
    failed: list[str] = []
    for css_name, (uno_prop, val) in mapped.items():
        try:
            if css_name == "background-color":
                if para_obj is None:
                    failed.append(css_name)
                    continue
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
                para_cur.setPropertyValue(uno_prop, val)
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
    try:
        from .styles import apply_paragraph_style_preserving_direct_char
    except ImportError:
        failed_import: list[str] = []
        if paras:
            for p in paras:
                if p:
                    failed_import.extend(_declaration_names(p))
        return failed_import

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
        for p in para_list:
            if p:
                failed.extend(_declaration_names(p))
        return failed

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
                try:
                    para_cur = text_obj.createTextCursorByRange(walk_cur.getStart())
                    para_cur.gotoEndOfParagraph(True)
                    para_obj = None
                    try:
                        enum = para_cur.createEnumeration()
                        if enum is not None and getattr(enum, "hasMoreElements", lambda: False)():
                            el = enum.nextElement()
                            if getattr(el, "supportsService", lambda _: False)("com.sun.star.text.Paragraph") is True:
                                para_obj = el
                            elif el is not None:
                                para_obj = el
                    except Exception as exc:
                        reraise_if_disposed(exc)
                        log.debug("data-lo-para: could not enumerate paragraph object: %s", exc)

                    failed.extend(_apply_para_props(para_cur, para_obj, mapped))
                except Exception as exc:
                    reraise_if_disposed(exc)
                    log.debug("data-lo-para: cursor creation failed", exc_info=True)
                    failed.extend(mapped.keys())

        try:
            if not walk_cur.gotoNextParagraph(False):
                break
        except Exception as exc:
            reraise_if_disposed(exc)
            break

    for p in para_list[reached_count:]:
        if p:
            failed.extend(_declaration_names(p))

    return failed
