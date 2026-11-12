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

from dataclasses import dataclass, field
import html as html_mod
import logging
import re
from urllib.parse import unquote
from typing import TYPE_CHECKING, Any, ClassVar, Literal, final

from plugin.framework.errors import Diagnostic, reraise_if_disposed, uno_read
from plugin.framework.uno_context import enum_has_more

from . import xhtml_style_postprocess as xhtml_post

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from plugin.framework.errors import SuggestedCall

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
    guarded. The one caller, apply_block_lo_styles, already returns before parsing in
    that build, so the empty table only keeps the import graph safe.

    range_copy._COPIED_PARA_PROPERTIES must hold every UNO property a writer sets here
    (pinned by test_every_para_writer_property_is_copied_by_range_reads).
    """
    try:
        from .styles import _dict_to_uno_struct, _para_adjust_to_uno, _style_color_to_uno
    except ImportError:
        return {}

    def text_align(val: str) -> tuple[str, Any] | None:
        try:
            return ("ParaAdjust", _para_adjust_to_uno(xhtml_post.ODF_TEXT_ALIGN.get(val, val)))
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
class _OpenBlock:
    """An open block element on the parser stack."""

    def __init__(self, tag: str, shared: bool | None, slot_index: int | None = None) -> None:
        self.tag: str = tag
        self.shared: bool | None = shared
        self.slot_index: int | None = slot_index


class _BlockLoStyleExtractor(xhtml_post.HtmlPassthroughParser):
    """Collect data-lo-style and data-lo-para per TOP-LEVEL block (document order) and strip
    the attributes from the HTML, so the StarWriter import sees clean markup and we apply the
    formatting ourselves afterwards. Content inside <table> is left to the import (avoids order desync)."""

    # Paragraph-level blocks cannot contain blocks: a new one closes the open one (<p>a<p>b).
    _PARA_LEVEL: ClassVar[frozenset[str]] = frozenset({"p", "pre", "h1", "h2", "h3", "h4", "h5", "h6"})
    _table_depth: int

    def __init__(self) -> None:
        super().__init__()
        self.styles: list[str | None] = []
        self.paras: list[str | None] = []
        self.skipped_para: list[str] = []
        # Open block-level elements: _OpenBlock(tag, shared?), or _OpenBlock(tag, None) for a transparent <div>.
        self._open: list[_OpenBlock] = []

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

    def _on_slot_created(self, slot_idx: int, tag: str, val: str | None) -> None:
        pass

    def _on_slot_shared(self, slot_idx: int, tag: str, val: str | None) -> None:
        pass

    def _on_block_popped(self, item: _OpenBlock) -> None:
        pass

    def _slot(self, tag: str, style: str | None, para: str | None) -> int:
        # <blockquote><p> and <li><p> take two slots but make ONE Writer
        # paragraph, so every later style lands one paragraph late ("2. DO
        # DIREITO" loses its heading, a list item becomes a quote). A
        # block's first child block is that same paragraph -- it shares
        # the slot, and its own style/para wins when it has one.
        parent = self._open[-1] if self._open else None
        if parent is not None and parent.shared is False:
            parent.shared = True
            slot_idx = len(self.styles) - 1
            if style is not None:
                self.styles[-1] = style
            if para is not None:
                self.paras[-1] = para
            self._on_slot_shared(slot_idx, tag, style)
            return slot_idx
        slot_idx = len(self.styles)
        self.styles.append(style)
        self.paras.append(para)
        self._on_slot_created(slot_idx, tag, style)
        return slot_idx

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
            self._open.append(_OpenBlock(t, None))  # transparent div
            self._out.append(_strip_data_lo_attrs(raw))
            return
        while self._open and self._open[-1].tag in self._PARA_LEVEL:
            popped = self._open.pop()
            self._on_block_popped(popped)
        slot_idx = self._slot(t, self._style_of(attrs), self._para_of(attrs))
        self._open.append(_OpenBlock(t, False, slot_idx))
        self._out.append(_strip_data_lo_attrs(raw))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or ("<%s/>" % tag)
        t = tag.lower()
        if self._table_depth == 0 and self._is_block(t, attrs):
            slot_idx = self._slot(t, self._style_of(attrs), self._para_of(attrs))
            self._on_block_popped(_OpenBlock(t, False, slot_idx))
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
                if self._open[idx].tag == t:
                    for item in reversed(self._open[idx:]):
                        self._on_block_popped(item)
                    del self._open[idx:]
                    break
        self._out.append("</%s>" % tag)

    def close(self) -> None:
        super().close()
        for item in reversed(self._open):
            self._on_block_popped(item)
        self._open.clear()


_HEADING_STYLE_RE = re.compile(r"^heading\s*([1-9]|10)$", re.IGNORECASE)
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


@final
class _Slot:
    """A Writer paragraph slot, holding its resolved heading level and accumulated text."""

    def __init__(self, level: int | None, style_token: str | None) -> None:
        self.level: int | None = level
        self.style_token: str | None = style_token
        self.text_parts: list[str] = []
        self.flushed: bool = False


@final
class _BlockSummarizer(_BlockLoStyleExtractor):
    """Walk HTML blocks to count Writer paragraph slots and extract headings.

    Subclasses _BlockLoStyleExtractor so slot allocation and shared first-child
    rules are defined in exactly one place. Adds script/style skipping, character
    text accumulation per slot, and heading extraction.
    """

    def __init__(self) -> None:
        super().__init__()
        self.convert_charrefs = True
        self.slots: list[_Slot] = []
        self.headings: list[dict[str, Any]] = []
        self._ignored_depth: int = 0

    @staticmethod
    def _heading_info(tag: str, style_token: str | None) -> int | None:
        if style_token is not None:
            m = _HEADING_STYLE_RE.match(style_token.strip())
            return int(m.group(1)) if m else None
        if tag in _HEADING_TAGS:
            return int(tag[1])
        return None

    def _on_slot_created(self, slot_idx: int, tag: str, val: str | None) -> None:
        lvl = self._heading_info(tag, val)
        self.slots.append(_Slot(lvl, val))

    def _on_slot_shared(self, slot_idx: int, tag: str, val: str | None) -> None:
        slot = self.slots[slot_idx]
        if val is not None:
            # Child's own style token wins when present.
            slot.style_token = val
            slot.level = self._heading_info(tag, val)
        elif slot.style_token is None and slot.level is None and tag in _HEADING_TAGS:
            # When the shared slot has neither style token nor heading level, child tag applies.
            slot.level = self._heading_info(tag, None)

    def _on_block_popped(self, item: _OpenBlock) -> None:
        if item.slot_index is not None and item.slot_index < len(self.slots):
            self._flush_slot(self.slots[item.slot_index])

    def _flush_slot(self, slot: _Slot) -> None:
        if slot.flushed:
            return
        slot.flushed = True
        if slot.level is not None:
            raw = "".join(slot.text_parts)
            text = " ".join(raw.split())
            full_text = text
            if len(text) > 120:
                text = text[:120] + "…"
            self.headings.append({
                "level": slot.level,
                "text": text,
                "_full_text": full_text,
                "_slot": slot,
            })

    def _append_text(self, text: str) -> None:
        if self._table_depth > 0 or self._ignored_depth > 0:
            return
        for item in reversed(self._open):
            if item.slot_index is not None and item.slot_index < len(self.slots):
                self.slots[item.slot_index].text_parts.append(text)
                break

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in ("script", "style"):
            self._ignored_depth += 1
            return
        if self._ignored_depth > 0:
            return
        if t == "br":
            self._append_text(" ")
        super().handle_starttag(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if self._ignored_depth > 0 or t in ("script", "style"):
            return
        if t == "br":
            self._append_text(" ")
        super().handle_startendtag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style") and self._ignored_depth > 0:
            self._ignored_depth -= 1
            return
        if self._ignored_depth > 0:
            return
        super().handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        self._append_text(data)


# HTML block-level elements that define paragraphs. <br> is separate: it breaks a line but
# does not make the content block-level for routing.
_BLOCK_TAGS = (
    "p|div|h[1-6]|ul|ol|li|dl|dt|dd|caption"
    "|table|thead|tbody|tfoot|tr|td|th"
    "|blockquote|pre|hr|section|article|aside|header|footer|nav|main"
    "|figure|figcaption|address|details|summary|form|fieldset|legend|hgroup"
)
BLOCK_MARKUP_RE = re.compile(r"<(?:%s)\b" % _BLOCK_TAGS, re.IGNORECASE)
# Same tags plus closing forms and <br>: anything here already defines its own paragraphs.
LINEBREAK_MARKUP_RE = re.compile(r"</?(?:%s|br)\b" % _BLOCK_TAGS, re.IGNORECASE)
_COMPLETE_ENTITY_RE = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]{1,31});")


def decode_complete_character_references(text: str) -> str:
    """Decode only HTML character references ending with a semicolon, ignoring incomplete references like '&sect 2o'."""
    if "&" not in text or not _COMPLETE_ENTITY_RE.search(text):
        return text
    return _COMPLETE_ENTITY_RE.sub(lambda m: html_mod.unescape(m.group(0)), text)


def expand_escapes(text: str) -> str:
    """Turn the two-character sequences \\n and \\t that models often send literally into a real newline and tab.

    Every write entry point runs this once, before measuring ruby offsets or importing.
    """
    return text.replace("\\n", "\n").replace("\\t", "\t")


def _plain_paragraphs(content: str) -> list[str]:
    """Split plain-text content into paragraphs, collapsing 3+ newlines and dropping empty pieces."""
    collapsed = re.sub(r"\n{3,}", "\n\n", content)
    return [p for p in collapsed.split("\n\n") if p.strip()]


def _prepare_import_html(content: str) -> str:
    """Prepare HTML or plain-text content for import / summary walk.

    Applies complete-entity reference decoding, escape expansion, ruby stripping
    (when no math is present), line-break normalization, prose angle bracket escaping,
    and boilerplate stripping.
    """
    if not isinstance(content, str) or not content:
        return ""
    from plugin.doc.text_helpers import normalize_linebreaks as normalize
    from plugin.framework.html_stripper import escape_prose_angle_brackets
    from . import html_export, html_ruby
    from .math.html_math_segment import html_fragment_contains_mixed_math

    content = decode_complete_character_references(content)
    content = expand_escapes(content)
    if not html_fragment_contains_mixed_math(content):
        content, _unused_ruby = html_ruby.extract_and_strip_ruby(content)
    content = escape_prose_angle_brackets(normalize(content))
    if re.search(r"<!DOCTYPE\s+html|<html[\s>]", content, re.IGNORECASE):
        content = html_export.strip_html_boilerplate(content)
    return content


def _summarize_blocks(content: str) -> _BlockSummarizer | None:
    """Parse prepared HTML with _BlockSummarizer if markup with linebreaks is present."""
    if not isinstance(content, str) or not content:
        return None
    prepared = _prepare_import_html(content)
    if not LINEBREAK_MARKUP_RE.search(prepared):
        return None
    parser = _BlockSummarizer()
    parser.feed(prepared)
    parser.close()
    return parser


def written_heading_texts(content: str) -> list[str]:
    """Whitespace-collapsed, untruncated heading texts in written HTML (h1-h6 or data-lo-style HeadingN)."""
    parser = _summarize_blocks(content)
    if parser is None:
        return []
    return [str(h["_full_text"]) for h in parser.headings]


def summarize_written_blocks(
    content: str, *, drop_heading_styles: Iterable[str] = ()
) -> dict[str, Any]:
    """Summarize paragraph slot count and outline headings from written HTML/text content.

    Walks the blocks matching StarWriter paragraph slot allocation and extracts headings
    either from data-lo-style (levels 1-10) or h1-h6 tags without data-lo-style.
    """
    if not isinstance(content, str) or not content:
        return {"paragraph_count": 0, "headings": []}

    parser = _summarize_blocks(content)
    if parser is None:
        prepared = _prepare_import_html(content)
        paras = _plain_paragraphs(prepared)
        return {"paragraph_count": len(paras), "headings": []}

    drop_set = frozenset(drop_heading_styles)
    headings = [
        {"level": h["level"], "text": h["text"]}
        for h in parser.headings
        if h["_slot"].style_token is None or h["_slot"].style_token not in drop_set
    ]
    return {"paragraph_count": len(parser.slots), "headings": headings}


def extract_block_lo_styles(
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


def resolve_paragraph_style_token(
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
    """Collect all data-lo-para property names declared in *content* (none applied).

    Used for targets that never apply paragraph formatting (position before/after).
    """
    if "data-lo-para" not in content:
        return []
    _clean, _styles, paras, skipped = extract_block_lo_styles(content)
    return declared_para_names(paras, skipped)


MATH_IMPORT_FAILED = "MATH_IMPORT_FAILED"
LINK_TARGET_NOT_FOUND = "LINK_TARGET_NOT_FOUND"

StyleSkipCode = Literal["STYLE_IGNORED_PARTIAL_MATCH", "STYLE_IGNORED_RECORDING"]


@dataclass
class WriteReport:
    """What one HTML write could not apply, reported on the tool result as ``warnings``.

    Every write function in ``html_import`` returns one. ``unapplied_para`` holds the
    data-lo-para property names that were not applied. ``style_skips`` holds one code per
    write whose data-lo-style (and data-lo-para) was skipped because the replaced range was
    not whole paragraphs or tracked changes were recording. ``style_tokens`` is the distinct
    non-empty data-lo-style tokens in the content, in first-seen order; a skip with no token
    still explains the data-lo-para skip but names no style. ``math`` is one entry per
    formula conversion attempt. all_matches merges one report per match with ``extend``.
    """

    unapplied_para: list[str] = field(default_factory=list)
    style_skips: list[StyleSkipCode] = field(default_factory=list)
    style_tokens: list[str] = field(default_factory=list)
    math: list[dict[str, Any]] = field(default_factory=list)

    def extend(self, other: WriteReport) -> None:
        self.unapplied_para.extend(other.unapplied_para)
        self.style_skips.extend(other.style_skips)
        self.style_tokens = list(dict.fromkeys(self.style_tokens + other.style_tokens))
        self.math.extend(other.math)


def append_warnings(
    result: dict[str, Any], diags: list[Diagnostic]
) -> dict[str, Any]:
    """Append diagnostics to result["warnings"] on a successful result (creates the list)."""
    if not diags or result.get("status") != "ok":
        return result
    existing = result.get("warnings")
    if isinstance(existing, list):
        existing.extend(diags)
    else:
        result["warnings"] = list(diags)
    return result


def attach_write_report(
    result: dict[str, Any],
    report: WriteReport,
    *,
    total_matches: int = 1,
    suggest_style_fix: Callable[[str, int], SuggestedCall | None] | None = None,
) -> dict[str, Any]:
    """Turn one WriteReport into warnings (and math_objects) on a successful edit result.

    Every write tool that takes formatted HTML reports through here, so the model sees one
    message shape. Order: STYLE_IGNORED_* (also appended to message), PARA_* (deduped names,
    reason from the first style skip), then math_objects + MATH_IMPORT_FAILED.
    suggest_style_fix(token, total_matches) is called only for a partial-match skip with
    exactly one style token.

    math_objects is only published when the edit succeeded (``status == "ok"``) and attempted
    at least one formula conversion. Empty attempts or error results are returned unchanged
    because a failed or rolled-back edit left none of these objects in the document. ``ok``
    means a Math object was inserted into the document, not accepted in review; the review
    outcome is reported separately under ``review``. If any formula conversion failed, a
    ``MATH_IMPORT_FAILED`` diagnostic is added to ``warnings`` while preserving the overall
    ``ok`` status, so review workflows and surrounding edit metadata (counts, edited context)
    remain intact.
    """
    if result.get("status") != "ok":
        return result

    diags: list[Diagnostic] = []

    if report.style_skips and report.style_tokens:
        style_tokens = report.style_tokens
        token = style_tokens[0] if len(style_tokens) == 1 else None
        subject = f"data-lo-style='{token}'" if token else "data-lo-style"

        partial_count = report.style_skips.count("STYLE_IGNORED_PARTIAL_MATCH")
        recording_count = report.style_skips.count("STYLE_IGNORED_RECORDING")

        style_diags: list[Diagnostic] = []
        if partial_count > 0:
            scope_clause = (
                f" to {partial_count} of {total_matches} matches because they do not cover whole paragraphs"
                if total_matches > 1
                else " because the replaced range does not cover a whole paragraph"
            )
            msg = (
                f"{subject} was not applied{scope_clause}; "
                "applying it would restyle neighboring text in the host paragraph. "
                "apply_style restyles the whole paragraph containing the match. "
                "Restyle with apply_style(target='search', old_content=...)."
            )
            partial_diag: Diagnostic = {
                "code": "STYLE_IGNORED_PARTIAL_MATCH",
                "message": msg,
                "matches_skipped": partial_count,
                "matches_total": total_matches,
            }
            if token is not None and suggest_style_fix is not None:
                suggested = suggest_style_fix(token, total_matches)
                if suggested is not None:
                    partial_diag["suggested_call"] = suggested
            style_diags.append(partial_diag)

        if recording_count > 0:
            rec_scope = f" to {recording_count} of {total_matches} matches" if total_matches > 1 else ""
            rec_msg = (
                f"{subject} was not applied{rec_scope} while tracked changes are recording, "
                "because a paragraph style on this replace would turn the deletion into a format change. "
                "Restyle with apply_style(target='search', old_content=...) after the change is accepted."
            )
            rec_diag: Diagnostic = {
                "code": "STYLE_IGNORED_RECORDING",
                "message": rec_msg,
                "matches_skipped": recording_count,
                "matches_total": total_matches,
            }
            style_diags.append(rec_diag)

        if style_diags:
            diags.extend(style_diags)
            note_text = " ".join(d["message"] for d in style_diags)
            existing_message = result.get("message") or ""
            if existing_message:
                result["message"] = f"{existing_message} {note_text}"
            else:
                result["message"] = note_text

    if report.unapplied_para:
        deduped = list(dict.fromkeys(report.unapplied_para))
        skip_reason = report.style_skips[0] if report.style_skips else None
        if skip_reason == "STYLE_IGNORED_PARTIAL_MATCH":
            para_code = "PARA_IGNORED_PARTIAL_MATCH"
            para_suffix = " because the replaced range does not cover a whole paragraph"
        elif skip_reason == "STYLE_IGNORED_RECORDING":
            para_code = "PARA_IGNORED_RECORDING"
            para_suffix = " because tracked changes are recording"
        else:
            para_code = "PARA_PROPERTY_UNSUPPORTED"
            para_suffix = ""

        for name in deduped:
            diags.append({
                "code": para_code,
                "message": f"data-lo-para property '{name}' was not applied{para_suffix}",
            })

    if report.math:
        # Math objects are published only on successful edits (status == "ok"); a failed
        # or rolled-back edit left none of these objects in the document. MATH_IMPORT_FAILED
        # remains a warning so review workflows and surrounding edit metadata stay intact.
        result["math_objects"] = list(report.math)
        failed = [m for m in report.math if not m.get("ok")]
        if failed:
            listing = "; ".join(
                f"{m.get('snippet', '')}: {m.get('error') or 'conversion failed'}"
                for m in failed
            )
            diags.append({
                "code": MATH_IMPORT_FAILED,
                "message": (
                    f"The content was written, but {len(failed)} of {len(report.math)} "
                    f"formula(s) could not be converted to a Math object and stand in the "
                    f"document as literal '[Math import failed] <formula>' text: {listing}. "
                    f"Correct the formula and replace that literal text with target='search'."
                ),
            })

    return append_warnings(result, diags)

_HREF_FRAGMENT_RE = re.compile(r'(<a\b[^>]*?\bhref=)(["\'])#([^"\']*)\2', re.IGNORECASE)
_ID_NAME_RE = re.compile(r'(?<![-\w])(?:id|name)=(["\'])(.*?)\1', re.IGNORECASE)


def has_bare_fragment_links(content: str) -> bool:
    """Return True if content contains an anchor tag with a bare fragment href."""
    if not content or "#" not in content or "href" not in content.lower():
        return False
    for m in _HREF_FRAGMENT_RE.finditer(content):
        frag = m.group(3)
        if frag and "|" not in frag:
            return True
    return False


def resolve_heading_links(
    content: str,
    document_headings: Iterable[str],
    *,
    is_bookmark: Callable[[str], bool],
) -> tuple[str, list[Diagnostic]]:
    """Rewrite bare href="#Heading" anchors to href="#Heading|outline" when the fragment exactly
    matches a heading; return the rewritten HTML and one LINK_TARGET_NOT_FOUND warning per distinct
    fragment that matches no heading, no id/name in *content*, and no existing bookmark.
    """
    if not has_bare_fragment_links(content):
        return content, []

    # Targets matching an element id or anchor name within written HTML are preserved so
    # bookmark-based jumps succeed natively (sw/source/filter/html/htmlgrin.cxx:1370 InsertBookmark).
    id_names: set[str] = {
        " ".join(unquote(html_mod.unescape(m.group(2))).split())
        for m in _ID_NAME_RE.finditer(content)
    }

    # Collect normalized heading texts from written content and document headings.
    heading_set: set[str] = {
        " ".join(h.split())
        for h in written_heading_texts(content)
    }
    heading_set.update(
        " ".join(h.split())
        for h in document_headings
    )
    heading_set.discard("")

    diagnostics: list[Diagnostic] = []
    seen_missing: set[str] = set()

    # Writer resolves hyperlinks in SwView::JumpToSwMark (sw/source/uibase/uiview/view2.cxx).
    # Without a '|suffix', it only checks bookmarks and anchor names; bare '#Heading' links fail
    # to navigate. Adding '|outline' directs navigation to SwDoc::GotoOutline
    # (sw/source/core/doc/docnum.cxx), matching headings by outline text.
    def _replace_link(m: re.Match[str]) -> str:
        prefix = m.group(1)
        quote = m.group(2)
        frag = m.group(3)

        if not frag or "|" in frag:
            return m.group(0)

        name = " ".join(unquote(html_mod.unescape(frag)).split())
        if not name or name in id_names or is_bookmark(name):
            return m.group(0)

        if name in heading_set:
            escaped = html_mod.escape(name, quote=True)
            return f"{prefix}{quote}#{escaped}|outline{quote}"

        if name not in seen_missing:
            seen_missing.add(name)
            diagnostics.append({
                "code": LINK_TARGET_NOT_FOUND,
                "link_target": f"#{name}",
                "message": (
                    f'Link target "#{name}" matches no heading or bookmark and will not jump anywhere. '
                    'Use the exact heading text, href="#Heading text|outline", or give the target element '
                    'an id and link to that.'
                ),
            })
        return m.group(0)

    rewritten = _HREF_FRAGMENT_RE.sub(_replace_link, content)
    return rewritten, diagnostics


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


def apply_block_lo_styles(
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
            resolved = resolve_paragraph_style_token(fam, style, element_names=element_names)
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
                    para_obj = enum.nextElement() if enum_has_more(enum) else None
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
