# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""XHTML/FODT export and image stripping for Writer documents.

Public entries: ``document_to_content`` and ``xtext_to_content``.

CJK ruby: the XHTML filter concatenates ``text:ruby`` children and range copy
drops ``RubyText``. ``inject_ruby_into_html`` restores ``<ruby><rt>`` after
semantic post-process. Apply swaps ``<ruby>`` for placeholders then sets
``RubyText`` on the base run (``html_ruby.ruby_placeholders`` / ``html_import._place_ruby``).
"""

from __future__ import annotations

import logging
import re
import zlib
from html import escape as html_escape, unescape as html_unescape
from typing import Any, Callable, Iterator, NamedTuple

from plugin.doc.temp_files import with_temp_buffer
from plugin.doc.text_helpers import (
    DeletionState,
    TrackedDeletions,
    get_string_without_tracked_deletions,
    get_writer_selection_positions,
    iter_elements,
    iter_table_paragraphs,
    iter_xtext_paragraphs,
    starts_at_or_after,
    table_visible_text,
    visible_portions,
    visible_range_text,
    walk_cap_warning,
)
from plugin.framework.constants import HTML_WRITER_FILTER
from plugin.framework.errors import ToolExecutionError, best_effort, reraise_if_disposed, uno_read
from plugin.framework.scratch_writer import (
    close_scratch_document,
    load_scratch_document,
    new_blank_writer,
)
from plugin.framework.uno_context import create_property_value, get_desktop, supports_service
from plugin.writer.math.math_mml_export import (
    OBJECT_REPLACEMENT,
    inject_exported_math_tex,
    portion_math_embed,
    substitute_math_placeholders,
)
from . import html_ruby
from . import range_copy
from . import xhtml_style_postprocess as xhtml_post
from .image_placeholders import FRAME_HEAD

log = logging.getLogger("writeragent.writer")

XHTML_FILTER = "XHTML Writer File"
XHTML_EXTENSION = ".xhtml"
FLAT_ODF_FILTER = "OpenDocument Text Flat XML"
FODT_EXTENSION = ".fodt"

PAGE_NUMBER_NUMBERING_TYPE_NAMES: dict[int, str] = {
    0: "CHARS_UPPER_LETTER",
    1: "CHARS_LOWER_LETTER",
    2: "ROMAN_UPPER",
    3: "ROMAN_LOWER",
    4: "ARABIC",
    5: "NUMBER_NONE",
    6: "CHAR_SPECIAL",
    7: "PAGE_DESCRIPTOR",
    8: "BITMAP",
    9: "CHARS_UPPER_LETTER_N",
    10: "CHARS_LOWER_LETTER_N",
    11: "TRANSLITERATION",
    12: "NATIVE_NUMBERING",
    13: "FULLWIDTH_ARABIC",
    14: "CIRCLE_NUMBER",
    64: "ARABIC_ZERO",
}

PAGE_NUMBER_NUMBERING_TYPE_VALS: dict[str, int] = {
    name: val for val, name in PAGE_NUMBER_NUMBERING_TYPE_NAMES.items()
}


def strip_html_boilerplate(html_string: str) -> str:
    """Extract content between ``<body>`` tags if present."""
    if not html_string:
        return html_string
    match = re.search(r"<body[^>]*>(.*?)</body>", html_string, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return html_string


_DATA_URI_IMAGE_RE = re.compile(
    r"data:image/[^\"'\s);>]+;base64,[A-Za-z0-9+/=\s]+",
    re.IGNORECASE,
)
# The XHTML filter rewrites a fragment such as ``#2.3.4.Title.|outline`` into
# an HTML-safe id. The raw HyperLinkURL is put back on each exported anchor,
# in the order the source portions were copied.
# An <a> right before a picture's frame is the picture's own link only when that
# picture has a HyperLinkURL; otherwise it is a text link that starts with a picture.
_FRAME_IMG_AHEAD = (
    r'(?=[^>]*>(?:%s<(?:div|span)\b[^>]*\bid="(?P<picture>[^"]+)"[^>]*>'
    r"\s*(?:<p\b[^>]*>\s*)?<img\b)?)"
) % FRAME_HEAD
_ANCHOR_HREF_RE = re.compile(r'(<a\b[^>]*?\bhref=")([^"]*)(")' + _FRAME_IMG_AHEAD, re.IGNORECASE)


def strip_embedded_image_data(html: str) -> str:
    """Remove inline ``data:image`` base64 payloads from exported HTML; external URLs unchanged."""
    if not html:
        return html
    return _DATA_URI_IMAGE_RE.sub("", html)


# LibreOffice's XHTML export gives every heading an empty outline anchor, ``<a id="a__…">``,
# named after the heading's text. A picture anchored inside the heading puts its whole base64
# data into that name, so a heading holding one embedded screenshot gets a
# 40-65 KB id (the picture twice: once in the id and once in src), even with
# include_images=false. Nothing in WriterAgent, nor the agent, uses these ids.
# shorten_outline_anchor_ids gives a long id a short, stable name, in the id and in any
# ``href="#…"`` that points to it.
_MAX_OUTLINE_ANCHOR_ID = 200
_LONG_OUTLINE_ANCHOR_RE = re.compile(r'(\b(?:id|href)="#?)(a__[^"]{%d,})(")' % _MAX_OUTLINE_ANCHOR_ID)


def shorten_outline_anchor_ids(html: str) -> str:
    """Replace outline anchor ids longer than ``_MAX_OUTLINE_ANCHOR_ID`` with ``a__<crc32>``."""
    if not html or "a__" not in html:
        return html
    return _LONG_OUTLINE_ANCHOR_RE.sub(lambda m: "%sa__%08x%s" % (m.group(1), zlib.crc32(m.group(2).encode("utf-8")), m.group(3)), html)


def _apply_image_export_options(content: str, *, include_images: bool) -> str:
    content = shorten_outline_anchor_ids(content)
    return content if include_images else strip_embedded_image_data(content)


def _truncate_html(content: str, max_chars: int) -> str:
    """Cut *content* to *max_chars* without ending inside a tag or an entity.

    A plain slice ``content[:max_chars]`` can hand the model an unclosed
    ``<a href="…`` or a trailing ``&nb``. The marker stays so
    get_content can set ``truncated``.
    """
    window = content[:max_chars]
    last_lt = window.rfind("<")
    last_gt = window.rfind(">")
    if last_lt > last_gt:
        window = window[:last_lt]
    amp = window.rfind("&")
    if amp >= 0 and ";" not in window[amp:]:
        window = window[:amp]
    return window + "\n\n[... truncated ...]"


def _read_filter_text(doc: Any, filter_name: str, ext: str = ".html") -> str:
    with with_temp_buffer(ext=ext) as (path, file_url):
        props = (create_property_value("FilterName", filter_name),)
        doc.storeToURL(file_url, props)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()


def _export_starwriter_html(doc: Any) -> str:
    return strip_html_boilerplate(_read_filter_text(doc, HTML_WRITER_FILTER))


def _export_xhtml(doc: Any) -> str:
    """Export *doc* via the XHTML Writer File filter; return the raw XHTML string."""
    return _read_filter_text(doc, XHTML_FILTER, ext=XHTML_EXTENSION)


def _autostyle_maps(doc: Any, fodt: str | None = None) -> tuple[dict[str, str], dict[str, str], bool | None]:
    """Return ``(parents, overrides, has_ruby)`` from *doc*'s flat-ODF sidecar.

    Exports *doc* as flat ODF when *fodt* is None; otherwise parses *fodt*.
    ``parents`` (Pn -> base style name) lets the read path recover an autostyle paragraph's real
    style name when the XHTML CSS fingerprint matches nothing. ``overrides`` (Pn -> CSS text) is
    the paragraph's DIRECT formatting, which the flattened XHTML cannot distinguish from inherited
    values. ``has_ruby`` is True when the sidecar contains ``<text:ruby`` (ODF keeps ruby;
    the XHTML filter has no ``text:ruby`` rule and concatenates children). None means the
    export failed — the caller should walk portions rather than assume there is no ruby.

    Both come from the same export. Returns ``({}, {}, None)`` on any failure (the read still
    works, just without autostyle-name recovery and without the direct-formatting report).

    Both scopes report overrides: the range path copies the source paragraphs' direct formatting
    onto the temp document first (see paint_direct_formatting)."""
    def read_maps() -> tuple[dict[str, str], dict[str, str], bool | None]:
        sidecar = _read_filter_text(doc, FLAT_ODF_FILTER, ext=FODT_EXTENSION) if fodt is None else fodt
        parents, overrides = xhtml_post.parse_fodt_autostyles(sidecar)
        return (parents, overrides, "<text:ruby" in sidecar)

    failed: tuple[dict[str, str], dict[str, str], bool | None] = ({}, {}, None)
    return best_effort(read_maps, "_autostyle_maps: flat-ODF export failed", failed)


_COPY_PORTION_LIMIT = 50000


def _visible_portions(
    para: Any,
    limit: int = _COPY_PORTION_LIMIT,
    truncated_out: list[str] | None = None,
    state: DeletionState | None = None,
) -> Iterator[Any]:
    """Visible portions for offset paint. Same walk as the text helper.

    Aborts on portion enum / type failure so later runs are not painted at a
    drifted offset. ``get_string_without_tracked_deletions`` continues past a
    bad portion instead — that is the only intentional divergence.

    When the portion walk hits *limit*, log and append ``walk_cap_warning`` to
    *truncated_out* so truncated range reads are visible instead of omitting later
    Char* runs without a signal.
    """
    hit: list[int] = []
    yield from visible_portions(
        para, abort_on_portion_error=True, limit=limit, truncated_out=hit, state=state
    )
    if hit:
        msg = walk_cap_warning("text portions", hit[0], limit)
        log.warning("%s", msg)
        if truncated_out is not None:
            truncated_out.append(msg)


def _hyperlink_urls(portions: list[tuple[Any, str]]) -> list[str]:
    """One URL per hyperlink run of a paragraph, in order.

    The XHTML filter emits one ``<a>`` per run, not per text portion, and it
    rewrites the fragment. A bold split or a preserve-format replace leaves two
    portions with the same URL and one anchor; writing every portion would put
    the extra URL on the next anchor. A portion with no URL separates runs, so
    two copies of the same URL with plain text between them stay two anchors.
    A picture in the scratch copy ends the link run and the filter writes two
    anchors: an empty ``Frame`` portion resets *previous* when its URL differs
    from the surrounding link. The source document reports the text link's URL on
    the ``Frame``, whereas the scratch copy reports ``''``, and that difference
    tells one anchor from two.
    """
    urls = []
    previous = None
    for portion, chunk in portions:
        if not chunk:
            if uno_read(lambda: portion.getPropertyValue("TextPortionType"), "") == "Frame":
                url = uno_read(lambda: portion.getPropertyValue("HyperLinkURL"), "")
                if url != previous:
                    previous = None
            continue
        url = uno_read(lambda: portion.getPropertyValue("HyperLinkURL"), "")
        if not url:
            previous = None
            continue
        if url == previous:
            continue
        urls.append(url)
        previous = url
    return urls


def _picture_urls(doc: Any) -> dict[str, str]:
    """Map graphic object names to non-empty ``HyperLinkURL`` values for name-based lookup.

    ``create-href`` (``common/body.xsl``) builds ``#a_`` from all descendant text including
    base64 image data, so picture links cannot be paired by count.
    """
    graphics = doc.getGraphicObjects()
    urls: dict[str, str] = {}
    for name in graphics.getElementNames():
        url = graphics.getByName(name).getPropertyValue("HyperLinkURL")
        if url:
            urls[name] = url
    return urls


def _picture_url(match: re.Match[str], picture_urls: dict[str, str]) -> str | None:
    """Return the picture's own ``HyperLinkURL`` when *match* immediately precedes a linked picture wrapper."""
    name = match["picture"]
    return picture_urls.get(html_unescape(name)) if name else None


def _restore_anchor_hrefs(
    content: str,
    urls: list[str],
    anchor_re: re.Pattern[str],
    picture_urls: dict[str, str],
) -> str:
    """Write *urls* back onto exported anchors matching *anchor_re*. The filter does not keep ``|outline``."""
    if not content:
        return content
    remaining = iter(urls)

    def _replace(match: re.Match[str]) -> str:
        url = _picture_url(match, picture_urls) or next(remaining, None)
        if url is None:
            return match.group(0)
        return match.group(1) + html_escape(url, quote=True) + match.group(3)

    return anchor_re.sub(_replace, content)


# In LibreOffice's XSLT export (filter/source/xslt/odf2xhtml/export/common/body.xsl
# template create-href and common/table_of_content.xsl), an "|outline" link is written
# as "#a_" + the link's own visible text, so the heading target is lost. Footnote anchors
# (#ftn0) sit between them in the exported HTML, so only #a_ hrefs are paired with
# only outline URLs, and only when the counts agree. A tracked deletion that splits
# across a hyperlink leaves empty <a href="#a_"/> anchors in the split paragraphs;
# requiring at least one char after #a_ skips them so the count guard still matches.
_EXPORTED_OUTLINE_HREF_RE = re.compile(
    r'(<a\b[^>]*?\bhref=")(#a_[^"]+)(")' + _FRAME_IMG_AHEAD, re.IGNORECASE
)


def _is_outline_url(url: str) -> bool:
    return "|outline" in url or "%7Coutline" in url


def _put_outline_urls(content: str, urls: list[str], picture_urls: dict[str, str]) -> str:
    """Pure: write the outline URLs among *urls* onto exported #a_ anchors, in order; text unchanged on count mismatch."""
    outline_urls = [u for u in urls if _is_outline_url(u)]
    anchor_count = sum(
        1 for m in _EXPORTED_OUTLINE_HREF_RE.finditer(content) if _picture_url(m, picture_urls) is None
    )
    if anchor_count != len(outline_urls):
        log.debug(
            "_put_outline_urls: count mismatch (%d #a_ hrefs vs %d outline URLs); leaving text links unchanged",
            anchor_count,
            len(outline_urls),
        )
        outline_urls = []
    return _restore_anchor_hrefs(content, outline_urls, _EXPORTED_OUTLINE_HREF_RE, picture_urls)


def _body_hyperlink_urls(model: Any) -> list[str]:
    body = model.getText()
    urls: list[str] = []
    for para, state in iter_xtext_paragraphs(body, TrackedDeletions(model).state_at(body)):
        urls.extend(_hyperlink_urls(list(_visible_portions(para, state=state))))
    return urls


def _restore_outline_hrefs(model: Any, content: str) -> str:
    """Full-read counterpart of the range path's _restore_anchor_hrefs.

    Walks only body and table paragraphs; outline links in frames, footnotes, or
    headers make the counts differ, and the guard in ``_put_outline_urls`` then
    leaves text links unchanged. Note that ``iter_xtext_paragraphs`` walks tables
    via ``getCellNames()`` (``lcl_InspectLines`` in
    ``sw/source/core/unocore/unotbl.cxx:567``), which visits sub-cells of an
    old-style split cell before later columns in the same visual row, so
    outline links in such a table can be paired in ``getCellNames()`` order
    rather than export order. A walk failure returns *content* rather than
    raising so a cosmetic restore failure cannot push the whole read onto the
    StarWriter fallback (which would drop every style token).
    """
    if 'href="#a_' not in content:
        return content
    try:
        urls = _body_hyperlink_urls(model)
        picture_urls = _picture_urls(model)
    except Exception as exc:
        reraise_if_disposed(exc)
        log.debug("_restore_outline_hrefs: portion walk failed", exc_info=True)
        return content
    return _put_outline_urls(content, urls, picture_urls)


def _starts_before(text: Any, left: Any, right: Any) -> bool:
    """True when *left* is strictly before *right* (``compareRegionStarts`` == 1)."""
    return text.compareRegionStarts(left, right) == 1


def _ends_after(text: Any, left: Any, right: Any) -> bool:
    """True when *left* ends strictly after *right* (``compareRegionEnds`` == -1)."""
    return text.compareRegionEnds(left, right) == -1


def _element_overlaps(text: Any, element: Any, source: Any) -> bool:
    """True when *element* and *source* share a character.

    SwXText::ComparePositions (sw/source/core/unocore/unotext.cxx) throws only
    when a range is not in this text. The caller's window comes from that same
    text, so a failure means the read cannot place the paragraph, and the
    range read raises instead of guessing.
    """
    return (
        _starts_before(text, element.getStart(), source.getEnd())
        and _ends_after(text, element.getEnd(), source.getStart())
    )


class _TableStartRange(NamedTuple):
    """Range-like value for a selection that starts inside a body table and ends in the body."""

    text: Any
    anchor: Any
    end: Any

    def getText(self) -> Any:
        return self.text

    def getStart(self) -> Any:
        return self.anchor

    def getEnd(self) -> Any:
        return self.end


def _table_start_range(body: Any, start: Any, end: Any) -> _TableStartRange | None:
    """Resolve a table-start/body-end selection to the outer body table's anchor."""
    body.createTextCursorByRange(end)
    last_anchor: Any = None
    for el in iter_elements(body.createEnumeration()):
        if not supports_service(el, "com.sun.star.text.TextTable"):
            continue
        anchor = el.getAnchor()
        if not starts_at_or_after(body, start, anchor):
            break
        last_anchor = anchor
    return _TableStartRange(body, last_anchor, end) if last_anchor is not None else None


def _selection_cursor(model: Any, selection: Any | None = None) -> Any | None:
    """The current selection (or explicit *selection* range) as a cursor in its own text, or None.

    Selection scope builds a cursor in the selection's own XText instead of body
    character offsets, so index/TOC selections do not export a later body paragraph.

    A selection starting in a body table and ending in the body has two forms:
    Ctrl+A (``SwCursorShell::ExtendedSelectAll``, ``sw/source/core/crsr/crsrsh.cxx``)
    gives ``SwXCell`` whose ``createXTextCursorByRangeImpl`` (``unotbl.cxx``) returns
    None for an end outside the cell, and a programmatic ``SwXBodyText`` range whose
    ``start`` is inside a cell causes ``createTextCursorByRange`` to raise (interactive
    Shift+arrow cannot leave a table because ``SwCursor::IsSelOvr`` in
    ``sw/source/core/crsr/swcrsr.cxx`` restores the saved position). Both fall back
    to ``_table_start_range``.
    """
    found = get_writer_selection_positions(model, selection=selection)
    if not found:
        return None
    text, start, end = found

    def select() -> Any:
        if (
            uno_read(lambda: text.createTextCursorByRange(start), None) is None
            or uno_read(lambda: text.createTextCursorByRange(end), None) is None
        ):
            return _table_start_range(model.getText(), start, end)
        cursor = text.createTextCursorByRange(start)
        cursor.gotoRange(end, True)
        return cursor

    return best_effort(select, "_selection_cursor failed")


def _trim_to_source(
    text: Any, element: Any, para_text: str, source: Any, opened: bool
) -> tuple[int, int]:
    """Visible-text window of *element* that lies inside *source*."""
    # getStart/getEnd and region comparisons run on ranges from the same text;
    # any failure raises to the outer handler so the slice does not guess.
    start_inside = _starts_before(text, element.getStart(), source.getStart())
    end_inside = _ends_after(text, element.getEnd(), source.getEnd())
    if not start_inside and not end_inside:
        return 0, len(para_text)
    trim_start = 0
    trim_end = len(para_text)
    if start_inside:
        cur = text.createTextCursorByRange(element.getStart())
        cur.gotoRange(source.getStart(), True)
        trim_start = len(get_string_without_tracked_deletions(cur, DeletionState(opened)))
    if end_inside:
        cur = text.createTextCursorByRange(element.getStart())
        cur.gotoRange(source.getEnd(), True)
        trim_end = len(get_string_without_tracked_deletions(cur, DeletionState(opened)))
    return trim_start, trim_end


# (element, visible para text, tables just before it, opened inside a tracked
# deletion) -> (tables inside the window, (trim_start, trim_end) in visible
# coordinates (_visible_portions, tracked deletions removed) or None to skip
# the element). Tables are read whole.
_Window = Callable[[Any, str, "list[Any]", bool], "tuple[list[Any], tuple[int, int] | None]"]


def _selection_window(text: Any, source: Any) -> tuple[Callable[[Any], bool], _Window]:
    """Window filter and stop predicate for selection-range export."""
    def past_end(el: Any) -> bool:
        return starts_at_or_after(text, el.getStart(), source.getEnd())

    def window(
        el: Any, para_text: str, tables: list[Any], opened: bool
    ) -> tuple[list[Any], tuple[int, int] | None]:
        # A table has no getStart; its anchor is a point inside its first cell.
        # >= at start is needed because _TableStartRange.getStart() can be the table's own anchor.
        inside = [
            t for t in tables
            if _starts_before(text, t.getAnchor(), source.getEnd())
            and starts_at_or_after(text, t.getAnchor(), source.getStart())
        ]
        if past_end(el) or not _element_overlaps(text, el, source):
            return inside, None
        return inside, _trim_to_source(text, el, para_text, source, opened)

    return past_end, window


def _offset_window(start: int, end: int) -> tuple[Callable[[Any], bool], _Window]:
    """Window filter for character-offset slice export.

    Counts as ``text_helpers._body_blocks`` does: each element is its visible
    text plus one break. Tables are read whole when the range touches them.
    """
    pos = 0

    def window(
        el: Any, para_text: str, tables: list[Any], opened: bool
    ) -> tuple[list[Any], tuple[int, int] | None]:
        nonlocal pos
        inside = []
        for table in tables:
            table_start = pos
            pos += len(table_visible_text(table, opened)) + 1
            if table_start < end and pos > start:
                inside.append(table)
        para_start = pos
        para_end = para_start + len(para_text)
        pos = para_end + 1
        if para_end < start or (para_text and para_end == start) or para_start >= end:
            return inside, None
        return inside, (max(0, start - para_start), len(para_text) - max(0, para_end - end))

    return (lambda _el: False), window


def _copy_formula_runs(
    portions: list[tuple[Any, str]],
    trim_start: int,
    trim_end: int,
    temp_doc: Any,
    temp_text: Any,
    temp_cursor: Any,
    math_records: list[tuple[str, str, bool]],
) -> None:
    """Copy formula, graphic, and text runs into *temp_text*, recording placeholders."""
    # Standalone formula: paragraph has no other visible text.
    display_block = not any(
        chunk.strip() and chunk != OBJECT_REPLACEMENT and not portion_math_embed(p)
        for p, chunk in portions
    )
    cur_offset = 0
    for p, chunk in portions:
        run_len = len(chunk)
        run_start = cur_offset
        run_end = cur_offset + run_len
        cur_offset += run_len
        emb = portion_math_embed(p)
        if emb is not None:
            # A formula portion can be zero-length; keep it when it sits inside the window.
            if run_start < trim_start or run_start > trim_end:
                continue
            slice_text = range_copy.record_math_placeholder(math_records, emb, display_block=display_block)
        elif not chunk and uno_read(lambda: p.getPropertyValue("TextPortionType"), "") == "Frame":
            if trim_start <= run_start <= trim_end:
                range_copy.clone_graphics(p, temp_doc, temp_text, temp_cursor)
            continue
        else:
            if run_end <= trim_start or run_start >= trim_end:
                continue
            s_start = max(0, trim_start - run_start)
            s_end = run_len - max(0, run_end - trim_end)
            slice_text = chunk[s_start:s_end]
        if not slice_text:
            continue
        range_copy.copy_text_run(p, temp_text, temp_cursor, slice_text)


def _copy_window_to_scratch(
    model: Any,
    text: Any,
    temp_doc: Any,
    past_end: Callable[[Any], bool],
    window: _Window,
    walk_warnings: list[str] | None,
) -> tuple[list[Any], list[tuple[str, str, bool]]] | None:
    """Copy elements matching *window* into *temp_doc*.

    Returns (copied_ruby, math_records), or None when no element was copied.
    """
    temp_text = temp_doc.getText()
    temp_cursor = temp_text.createTextCursor()
    style_cache: dict[str, Any] = {}
    enum = text.createEnumeration()
    added_any = False
    copied_ruby = []
    math_records: list[tuple[str, str, bool]] = []

    deletions = TrackedDeletions(model)
    state = deletions.state_at(text)
    tables: list[Any] = []
    need_break = False
    for el in iter_elements(enum):
        if supports_service(el, "com.sun.star.text.TextTable"):
            tables.append(el)
            continue
        done = past_end(el)
        if done and not tables:
            break
        # Paragraphs always have ParaStyleName.
        style = el.getPropertyValue("ParaStyleName") or ""

        # Same _visible_portions walk as get_string_without_tracked_deletions so
        # paint offsets match the helper string. Still walk here (not just the
        # helper) because we need the portion objects to copy Char* properties.
        opened = state.in_delete
        portions = list(_visible_portions(el, truncated_out=walk_warnings, state=state))
        has_formula = any(portion_math_embed(p) is not None for p, _unused in portions)
        para_text = "".join(chunk for _unused, chunk in portions)
        struck = state.strikes_paragraph(el, opened)

        inside, trim = window(el, para_text, tables, opened)
        if deletions:
            # A table struck by a spanning deletion or by Delete Table is not live content.
            inside = [t for t in inside if not range_copy.table_is_struck(model, t, DeletionState(opened))]
        tables = []
        for table in inside:
            temp_cursor.gotoEnd(False)
            if need_break:
                range_copy.begin_next_paragraph(temp_text, temp_cursor)
            # Range reads with include_images=False emit placeholders named after the clone;
            # keeping the source name (when unused in temp_doc) prevents a later write-back
            # (image_placeholders) from restoring a different picture of that name.
            range_copy.copy_table(model, table, temp_doc, temp_text, temp_cursor, deletions, math_records, mark_struck=True)
            for para, table_state in iter_table_paragraphs(table):
                copied_ruby.extend(
                    html_ruby.ruby_spans_in_window(para, 0, len(para.getString()), table_state)
                )
            need_break = False
            added_any = True
        if done:
            break
        if trim is None:
            continue
        trim_start, trim_end = trim
        para_text = para_text[trim_start:trim_end]
        temp_cursor.gotoEnd(False)
        if need_break:
            range_copy.begin_next_paragraph(temp_text, temp_cursor)
        need_break = True
        applied = range_copy.apply_temp_para_style(temp_doc, temp_cursor, style, model)

        if not has_formula:
            temp_cursor.setString(para_text + xhtml_post.DELETED_MARK if struck else para_text)
            src_style = range_copy.source_style(model, style, style_cache) if applied else None
            range_copy.paint_direct_formatting(el, portions, temp_text, trim_start, trim_end, src_style)
            range_copy.clone_window_graphics(el, portions, temp_doc, temp_text, trim_start, trim_end, deletions)
        else:
            _copy_formula_runs(
                portions, trim_start, trim_end, temp_doc, temp_text, temp_cursor, math_records
            )
            if trim_start == 0:
                range_copy.clone_graphics(el, temp_doc, temp_text, temp_cursor, deletions)

        # RubyText lives on the empty Ruby start mark, not the Text run, so
        # _COPIED_CHAR_PROPERTIES cannot recreate ruby on the temp doc.
        # Collect source spans and rewrite the exported HTML after XHTML.
        copied_ruby.extend(
            html_ruby.ruby_spans_in_window(el, trim_start, trim_end, DeletionState(opened))
        )
        added_any = True

    return (copied_ruby, math_records) if added_any else None


def _export_semantic(doc: Any, *, read_ruby: bool, fodt: str | None = None) -> tuple[str, bool | None]:
    """Export *doc* as XHTML and convert to semantic HTML using flat-ODF autostyles.

    The sidecar is a second full export unless the caller already has *fodt*. It
    runs when there are autostyles to name, or when the caller needs the ruby
    flag (the full read skips a portion walk with it). has_ruby is None when the
    sidecar did not run, and finish() then walks portions itself. The function
    raises on export failure, and each caller owns its fallback.
    """
    xhtml = _export_xhtml(doc)
    if read_ruby or xhtml_post.xhtml_has_paragraph_autostyle(xhtml):
        parents, overrides, has_ruby = _autostyle_maps(doc, fodt)
    else:
        parents, overrides, has_ruby = {}, {}, None
    return xhtml_post.xhtml_to_semantic_html(xhtml, parents, overrides), has_ruby


def _range_to_content_via_temp_doc(
    model: Any,
    ctx: Any,
    start: int,
    end: int,
    max_chars: int | None,
    *,
    include_images: bool = False,
    walk_warnings: list[str] | None = None,
    source_range: Any = None,
) -> str:
    """Export a character range, or *source_range* itself, via a hidden temp document.

    *source_range* is the selection. It is copied from that cursor's text, not
    re-found by character offset.
    """
    temp_doc = None
    try:
        # Blank scratch doc: the factory URL honours the user's default template,
        # and this function reads the whole body back (see new_blank_writer).
        temp_doc = new_blank_writer(ctx)
        if not temp_doc:
            # Missing scratch Writer raises ToolExecutionError instead of returning
            # empty content with status ok.
            raise ToolExecutionError(
                "Could not read that part of the document (no scratch document)."
            )

        text = source_range.getText() if source_range is not None else model.getText()
        past_end, window = (
            _selection_window(text, source_range)
            if source_range is not None
            else _offset_window(start, end)
        )
        copied = _copy_window_to_scratch(model, text, temp_doc, past_end, window, walk_warnings)
        if copied is None:
            return ""
        copied_ruby, math_records = copied

        try:
            content, _unused_ruby = _export_semantic(temp_doc, read_ruby=False)
        except Exception as e:
            # A closed document must not start a second export.
            reraise_if_disposed(e)
            log.exception("_range_to_content_via_temp_doc (XHTML) failed; falling back to StarWriter")
            # Degraded path: strips the mark and leaves a blank paragraph rather than omitting it.
            content = _export_starwriter_html(temp_doc).replace(xhtml_post.DELETED_MARK, "")
        content = _restore_anchor_hrefs(
            content, _body_hyperlink_urls(temp_doc), _ANCHOR_HREF_RE, _picture_urls(temp_doc)
        )
        content = _apply_image_export_options(content, include_images=include_images)
        content = substitute_math_placeholders(content, math_records, ctx)
        content = html_ruby.inject_ruby_into_html(content, copied_ruby)
        if max_chars and len(content) > max_chars:
            content = _truncate_html(content, max_chars)
        return content
    except Exception as e:
        # Disposal is not "could not read that part". A ToolExecutionError
        # raised above (missing scratch doc) must not be wrapped again.
        reraise_if_disposed(e)
        if isinstance(e, ToolExecutionError):
            raise
        # Same rule as the full read: a failed read raises with its reason instead of returning ok with empty content.
        raise ToolExecutionError("Could not read that part of the document (%s)." % _reason(e)) from e
    finally:
        if temp_doc is not None:
            best_effort(lambda: close_scratch_document(temp_doc), "_range_to_content_via_temp_doc: close temp doc failed")
            range_copy.release_scratch_style_holds(temp_doc)


def _export_full(model: Any, ctx: Any) -> tuple[str, bool | None]:
    """Export the full document via XHTML, simplifying drawings and expanding deletions/hidden sections first.

    ``SwXMLExport::exportDoc`` hides deletions by joining paragraphs across a
    multi-paragraph deletion and ``odf2xhtml`` drops empty headings/blocks and
    ``text:section[@text:display='none']``, emits bare shape titles/text
    without wrappers, and leaks non-math OLE metadata. When tracked deletions
    cross a paragraph break or empty a paragraph, any section has ``IsVisible``
    false, or bare shapes/non-math OLE objects are present, rewrite the flat-ODF
    sidecar and export a hidden copy instead. Tables Accept All removes are left
    out of the copy.
    """
    deletions = bool(TrackedDeletions(model))
    struck: list[str] = []
    if deletions:
        # Judged on the model: the copy's struck cells hold marker text, so there they read as live.
        tables = model.getTextTables()
        struck = [
            name for name in tables.getElementNames()
            if range_copy.table_is_struck(model, tables.getByName(name), DeletionState())
        ]
    fodt = _read_filter_text(model, FLAT_ODF_FILTER, ext=FODT_EXTENSION)
    # simplify_drawings must run before expand_tracked_deletions: any draw: tag marks a
    # paragraph live, so a struck paragraph holding only an empty shape would lose its style token.
    readable = xhtml_post.show_hidden_sections(xhtml_post.simplify_drawings(fodt))
    if deletions:
        readable = xhtml_post.expand_tracked_deletions(readable) or readable
    if readable == fodt and not struck:
        return _export_semantic(model, read_ruby=True, fodt=fodt)
    with with_temp_buffer(readable, ext=FODT_EXTENSION) as (_path, url):
        copy = load_scratch_document(get_desktop(ctx), url)
        if copy is None:
            raise ToolExecutionError("Could not read the document (no scratch document).")
        try:
            copy_tables = copy.getTextTables()
            for name in struck:
                if copy_tables.hasByName(name):  # a nested one goes with its host
                    copy_tables.getByName(name).dispose()
            # The copy numbers its autostyles (P1...) again, so it needs its own sidecar.
            return _export_semantic(copy, read_ruby=True)
        finally:
            best_effort(lambda: close_scratch_document(copy), "_export_full: close copy failed")


def _starwriter_without_deletions(model: Any, ctx: Any) -> str:
    """Legacy StarWriter HTML of a hidden copy that has no pending deletions.

    The StarWriter HTML writer (sw/source/filter/html) has no redline handling, so
    exporting *model* itself prints struck text as live. The copy is loaded from flat
    ODF with deleted breaks split (expand_tracked_deletions) and the change list removed;
    a wholly struck paragraph keeps its place as an empty one.
    """
    fodt = _read_filter_text(model, FLAT_ODF_FILTER, ext=FODT_EXTENSION)
    readable = xhtml_post.drop_tracked_changes(xhtml_post.expand_tracked_deletions(fodt) or fodt)
    with with_temp_buffer(readable, ext=FODT_EXTENSION) as (_path, url):
        copy = load_scratch_document(get_desktop(ctx), url)
        if copy is None:
            raise ToolExecutionError("Could not read the document (no scratch document).")
        try:
            return _export_starwriter_html(copy).replace(xhtml_post.DELETED_MARK, "")
        finally:
            best_effort(lambda: close_scratch_document(copy), "_starwriter_without_deletions: close copy failed")


def document_to_content(
    model: Any,
    ctx: Any,
    max_chars: int | None = None,
    scope: str = "full",
    range_start: int | None = None,
    range_end: int | None = None,
    *,
    include_images: bool = False,
    walk_warnings: list[str] | None = None,
    selection: Any | None = None,
) -> str:
    """Export a Writer document (or part of it) as HTML.

    Args:
        model: UNO document model.
        ctx: UNO component context.
        max_chars: Approximate HTML character limit.
        scope: ``'full'``, ``'selection'``, or ``'range'``.
        range_start: Character offset start (for scope ``'range'``).
        range_end: Character offset end (for scope ``'range'``).
        include_images: When False (default), strip ``data:image`` base64 from export; external img URLs kept.
        walk_warnings: List that receives agent-facing notes about capped walks and the StarWriter fallback.
        selection: Explicit text range for scope ``'selection'``; None reads the live view.

    Returns:
        Content string.
    """
    def finish(content: str, ruby_flag: bool | None, why: str) -> str:
        content = _apply_image_export_options(content, include_images=include_images)
        content = inject_exported_math_tex(model, ctx, content)
        content = html_ruby.inject_ruby_from_model(model, content, ruby_flag)
        _raise_if_empty_export(model, content, why)
        if max_chars and len(content) > max_chars:
            content = _truncate_html(content, max_chars)
        return content

    if scope in ("selection", "range"):
        if scope == "selection":
            # Copy the selection's own paragraphs: offsets measured from the document start
            # would cost a walk per paragraph on a long file.
            source = _selection_cursor(model, selection=selection)
            if source is None:
                return ""
            start, end = 0, 0
        else:
            source = None
            start = max(0, int(range_start) if range_start is not None else 0)
            end = int(range_end) if range_end is not None else 0
            # No clamp to the document length: the walk below skips paragraphs past the end on its
            # own. CharacterCount is not the document_length space, so clamping to it would cut a
            # read at the end short, and measuring the real length here would walk the whole body
            # a second time per range read.
        return _range_to_content_via_temp_doc(
            model, ctx, start, end, max_chars,
            include_images=include_images, walk_warnings=walk_warnings,
            source_range=source,
        )

    # scope == "full" — preferred: XHTML (+ flat-ODF parent map) -> semantic data-lo-style.
    xhtml_error: Exception | None = None
    try:
        content, fodt_has_ruby = _export_full(model, ctx)
        content = _restore_outline_hrefs(model, content)
        return finish(content, fodt_has_ruby, "the XHTML export came back empty")
    except Exception as e:
        # A closed document must not start a second export.
        reraise_if_disposed(e)
        xhtml_error = e
        log.exception("document_to_content (full, XHTML) failed; falling back to StarWriter")

    # Fallback: legacy StarWriter export (so reads never hard-fail); a read with
    # pending deletions goes through a copy without them.
    try:
        deletions = bool(TrackedDeletions(model))
        content = _starwriter_without_deletions(model, ctx) if deletions else _export_starwriter_html(model)
        content = finish(content, None, "the HTML export came back empty")
        if walk_warnings is not None:
            warning = (
                "XHTML export failed, so this content came from the legacy HTML export and has no "
                "style tokens. LibreOffice's XHTML filter stops on long numbered lists that share one "
                "list style. For style tokens, read the document in parts with scope='range'."
            )
            if deletions:
                warning += " Pending tracked deletions are left out; a wholly deleted paragraph shows as an empty one."
            walk_warnings.append(warning)
        return content
    except Exception as e:
        # Disposal is not a double export failure.
        reraise_if_disposed(e)
        # If both exports fail, returning "" would make get_content
        # answer status ok with empty content, and neither the agent nor the
        # log reader could tell why. Raise a tool error that names both
        # reasons (a full /tmp, a filter error, ...).
        raise ToolExecutionError(
            "Could not read the document: the XHTML export failed (%s) and the HTML export "
            "failed too (%s)." % (_reason(xhtml_error), _reason(e))) from e


def _reason(error: Exception) -> str:
    """Short text for an export failure: the message, or the exception type when it has none
    (UNO exceptions often have an empty message)."""
    return str(error).strip() or type(error).__name__


def _raise_if_empty_export(model: Any, content: str, why: str) -> None:
    """Raise when an export produced nothing although the document has visible text."""
    if content and content.strip():
        return

    def has_text() -> bool:
        body = model.getText()
        # Portions cannot see content-control text, so the raw text decides when nothing is struck.
        text = visible_range_text(model, body) if TrackedDeletions(model) else body.getString()
        return bool(text.strip())

    if uno_read(has_text, True):
        raise ToolExecutionError(why)


_PAGE_NUMBER_SUBTYPE_NAMES: dict[int, str] = {
    0: "PREV",
    1: "CURRENT",
    2: "NEXT",
}

# Span titles whose fields carry a NumberingType, mapped to the field service.
PAGE_FIELD_SERVICES: dict[str, str] = {
    "page-number": "com.sun.star.text.textfield.PageNumber",
    "page-count": "com.sun.star.text.textfield.PageCount",
}

_PAGE_FIELD_SPAN_RE = re.compile(
    r'<span\b(?=[^>]*\btitle=["\'](%s)["\'])[^>]*>' % "|".join(PAGE_FIELD_SERVICES),
    re.IGNORECASE,
)


def _enum_token(value: Any, names_by_value: dict[int, str], valid_names: Any, default: str, label: str) -> str:
    if hasattr(value, "value"):
        val = value.value
        if isinstance(val, str) and val in valid_names:
            return val
    elif isinstance(value, int):
        if value in names_by_value:
            return names_by_value[value]
    elif isinstance(value, str):
        u = value.strip().upper()
        if u in valid_names:
            return u
        if u.isdigit() and int(u) in names_by_value:
            return names_by_value[int(u)]
    log.warning("Unknown %s: %r, defaulting to %s", label, value, default)
    return default


def _page_number_subtype_to_name(subtype: Any) -> str:
    """Return a readable SubType enum name ('PREV', 'CURRENT', 'NEXT')."""
    return _enum_token(subtype, _PAGE_NUMBER_SUBTYPE_NAMES, ("PREV", "CURRENT", "NEXT"), "CURRENT", "PageNumber SubType")


def _numbering_type_to_name(num_type: Any) -> str:
    """Return a readable NumberingType enum name (e.g. 'ROMAN_UPPER', 'ARABIC')."""
    return _enum_token(num_type, PAGE_NUMBER_NUMBERING_TYPE_NAMES, PAGE_NUMBER_NUMBERING_TYPE_VALS, "ARABIC", "field NumberingType")


def _page_field_props(field: Any) -> dict[str, Any] | None:
    """Return the span title and properties of a PageNumber or PageCount field, else None."""
    for title, service in PAGE_FIELD_SERVICES.items():
        if field.supportsService(service):
            names = ("SubType", "Offset", "NumberingType") if title == "page-number" else ("NumberingType",)
            return {"title": title, **{name: field.getPropertyValue(name) for name in names}}
    return None


def extract_page_fields(model: Any, text_obj: Any) -> list[dict[str, Any]]:
    """Walk *text_obj* in document order and collect PageNumber and PageCount field properties."""
    fields: list[dict[str, Any]] = []
    for para, state in iter_xtext_paragraphs(text_obj, TrackedDeletions(model).state_at(text_obj)):
        # Must skip the same fields as copy_xtext_into_doc, or the title check skips attribute injection.
        for portion, _unused in visible_portions(para, abort_on_portion_error=True, state=state):
            if uno_read(lambda: portion.getPropertyValue("TextPortionType"), "") == "TextField":
                try:
                    # A failed read skips the field instead of inventing CURRENT/0/ARABIC.
                    props = _page_field_props(portion.getPropertyValue("TextField"))
                except Exception as e:
                    log.debug("extract_page_fields TextField processing failed: %s", e)
                    continue
                if props is not None:
                    fields.append(props)
    return fields


def _inject_page_field_attributes(content: str, fields: list[dict[str, Any]]) -> str:
    """Add data-subtype/offset/numbering-type to page-number and page-count spans; defaults are omitted."""
    if not content or not fields:
        return content
    spans = list(_PAGE_FIELD_SPAN_RE.finditer(content))
    span_titles = [m.group(1).lower() for m in spans]
    field_titles = [f["title"] for f in fields]
    if span_titles != field_titles:
        log.warning(
            "xtext_to_content: page fields mismatch. Extracted %s, found spans %s. Skipping attribute injection.",
            field_titles, span_titles,
        )
        return content
    out: list[str] = []
    last_end = 0
    for match, props in zip(spans, fields):
        attrs = ""
        # Only inject attributes if different from defaults:
        # com.sun.star.text.PageNumberType.CURRENT = 1
        # Offset = 0
        # ARABIC = 4
        if props["title"] == "page-number":
            st_name = _page_number_subtype_to_name(props["SubType"])
            if st_name != "CURRENT":
                attrs += f' data-subtype="{st_name}"'
            if props["Offset"] != 0:
                attrs += f' data-offset="{props["Offset"]}"'
        nt_name = _numbering_type_to_name(props["NumberingType"])
        if nt_name != "ARABIC":
            attrs += f' data-numbering-type="{nt_name}"'
        out.append(content[last_end:match.start()])
        out.append(xhtml_post.inject_attr(match.group(0), attrs) if attrs else match.group(0))
        last_end = match.end()
    out.append(content[last_end:])
    return "".join(out)


def xtext_to_content(
    text_obj: Any,
    model: Any,
    ctx: Any,
    *,
    include_images: bool = True,
    max_chars: int | None = None,
) -> str:
    """Export an ``XText`` (header, footer, body, cell) via ``document_to_content``.

    Full-document XHTML omits page-style headers/footers, so the region is
    copied into a hidden Writer body's text and run through the same
    XHTML + postprocess stack as ``get_content``.
    """
    extracted_fields = extract_page_fields(model, text_obj)

    temp_doc = new_blank_writer(ctx)
    if not temp_doc:
        # Missing scratch Writer raises ToolExecutionError instead of reporting
        # ok with empty header/footer text.
        raise ToolExecutionError(
            "Could not read the document: no scratch document."
        )
    try:
        math_records: list[tuple[str, str, bool]] = []
        range_copy.copy_xtext_into_doc(model, text_obj, temp_doc, math_records=math_records)
        content = document_to_content(
            temp_doc,
            ctx,
            max_chars=max_chars,
            scope="full",
            include_images=include_images,
        )
        content = substitute_math_placeholders(content, math_records, ctx)

        content = _inject_page_field_attributes(content, extracted_fields)

        return content
    finally:
        best_effort(lambda: close_scratch_document(temp_doc), "xtext_to_content: close temp doc failed")
        range_copy.release_scratch_style_holds(temp_doc)
