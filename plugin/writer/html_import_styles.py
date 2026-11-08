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

import html as html_mod
import logging
import re
from typing import Any, ClassVar, final

from plugin.framework.errors import reraise_if_disposed, uno_read

from . import xhtml_style_postprocess as xhtml_post

log = logging.getLogger("writeragent.writer")


def _strip_data_lo_style(start_tag: str) -> str:
    """Remove any data-lo-style attribute from a single start-tag string."""
    return re.sub(r'\s+data-lo-style=(?:"[^"]*"|\'[^\']*\')', "", start_tag)


@final
class _OpenBlock:
    """An open block element on the parser stack."""

    def __init__(self, tag: str, shared: bool | None, slot_index: int | None = None) -> None:
        self.tag: str = tag
        self.shared: bool | None = shared
        self.slot_index: int | None = slot_index


class _BlockLoStyleExtractor(xhtml_post._HtmlPassthroughParser):
    """Collect data-lo-style per TOP-LEVEL block (document order) and strip the attribute
    from the HTML, so the StarWriter import sees clean markup and we apply the named styles
    ourselves afterwards. Content inside <table> is left to the import (avoids order desync)."""

    # Paragraph-level blocks cannot contain blocks: a new one closes the open one (<p>a<p>b).
    _PARA_LEVEL: ClassVar[frozenset[str]] = frozenset({"p", "pre", "h1", "h2", "h3", "h4", "h5", "h6"})
    _table_depth: int

    def __init__(self) -> None:
        super().__init__()
        self.styles: list[str | None] = []
        # Open block-level elements: _OpenBlock(tag, shared?), or _OpenBlock(tag, None) for a transparent <div>.
        self._open: list[_OpenBlock] = []

    @staticmethod
    def _style_of(attrs: list[tuple[str, str | None]]) -> str | None:
        return dict(attrs).get("data-lo-style")

    def _is_block(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        # A <div> is a transparent container -- except the one the read path gives a
        # data-lo-style: LibreOffice exports a paragraph holding a frame (an image) as
        # <div class="paragraph-X">, and that div is one Writer paragraph on import too.
        return tag in xhtml_post.BLOCK_TAGS or (tag == "div" and self._style_of(attrs) is not None)

    def _on_slot_created(self, slot_idx: int, tag: str, val: str | None) -> None:
        pass

    def _on_slot_shared(self, slot_idx: int, tag: str, val: str | None) -> None:
        pass

    def _on_block_popped(self, item: _OpenBlock) -> None:
        pass

    def _slot(self, tag: str, val: str | None) -> int:
        # <blockquote><p> and <li><p> take two slots but make ONE Writer
        # paragraph, so every later style lands one paragraph late ("2. DO
        # DIREITO" loses its heading, a list item becomes a quote). A
        # block's first child block is that same paragraph -- it shares
        # the slot, and its own style wins when it has one.
        parent = self._open[-1] if self._open else None
        if parent is not None and parent.shared is False:
            parent.shared = True
            slot_idx = len(self.styles) - 1
            if val is not None:
                self.styles[-1] = val
            self._on_slot_shared(slot_idx, tag, val)
            return slot_idx
        slot_idx = len(self.styles)
        self.styles.append(val)
        self._on_slot_created(slot_idx, tag, val)
        return slot_idx

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or ("<%s>" % tag)
        t = tag.lower()
        if t == "table":
            self._table_depth += 1
            self._out.append(raw)
            return
        if self._table_depth or (t not in xhtml_post.BLOCK_TAGS and t != "div"):
            # Inline tags, and blocks inside a table (their data-lo-style is left for the import to
            # ignore -- v1 doesn't apply cell styles), stay verbatim.
            self._out.append(raw)
            return
        if not self._is_block(t, attrs):
            self._open.append(_OpenBlock(t, None))  # transparent div
            self._out.append(raw)
            return
        while self._open and self._open[-1].tag in self._PARA_LEVEL:
            popped = self._open.pop()
            self._on_block_popped(popped)
        slot_idx = self._slot(t, self._style_of(attrs))
        self._open.append(_OpenBlock(t, False, slot_idx))
        self._out.append(_strip_data_lo_style(raw))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or ("<%s/>" % tag)
        t = tag.lower()
        if self._table_depth == 0 and self._is_block(t, attrs):
            slot_idx = self._slot(t, self._style_of(attrs))
            self._on_block_popped(_OpenBlock(t, False, slot_idx))
            self._out.append(_strip_data_lo_style(raw))
        else:
            self._out.append(raw)

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
            if len(text) > 120:
                text = text[:120] + "…"
            self.headings.append({
                "level": slot.level,
                "text": text,
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


_BLOCK_TAGS = (
    "p|div|h[1-6]|ul|ol|li|dl|dt|dd|caption"
    "|table|thead|tbody|tfoot|tr|td|th"
    "|blockquote|pre|hr|section|article|aside|header|footer|nav|main"
    "|figure|figcaption|address|details|summary|form|fieldset|legend|hgroup"
)
_BLOCK_MARKUP_RE = re.compile(r"<(?:%s)\b" % _BLOCK_TAGS, re.IGNORECASE)
_LINEBREAK_MARKUP_RE = re.compile(r"</?(?:%s|br)\b" % _BLOCK_TAGS, re.IGNORECASE)
_COMPLETE_ENTITY_RE = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]{1,31});")


def _decode_complete_character_references(text: str) -> str:
    """Decode only HTML character references ending with a semicolon, ignoring incomplete references like '&sect 2o'."""
    if "&" not in text or not _COMPLETE_ENTITY_RE.search(text):
        return text
    return _COMPLETE_ENTITY_RE.sub(lambda m: html_mod.unescape(m.group(0)), text)


def _expand_escapes(text: str) -> str:
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

    content = _decode_complete_character_references(content)
    content = _expand_escapes(content)
    if not html_fragment_contains_mixed_math(content):
        content, _unused_ruby = html_ruby.extract_and_strip_ruby(content)
    content = escape_prose_angle_brackets(normalize(content))
    if re.search(r"<!DOCTYPE\s+html|<html[\s>]", content, re.IGNORECASE):
        content = html_export.strip_html_boilerplate(content)
    return content


def summarize_written_blocks(
    content: str, *, drop_heading_styles: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Summarize paragraph slot count and outline headings from written HTML/text content.

    Walks the blocks matching StarWriter paragraph slot allocation and extracts headings
    either from data-lo-style (levels 1-10) or h1-h6 tags without data-lo-style.
    """
    if not isinstance(content, str) or not content:
        return {"paragraph_count": 0, "headings": []}

    prepared = _prepare_import_html(content)
    if not _LINEBREAK_MARKUP_RE.search(prepared):
        paras = _plain_paragraphs(prepared)
        return {"paragraph_count": len(paras), "headings": []}

    parser = _BlockSummarizer()
    parser.feed(prepared)
    parser.close()

    headings = [
        {"level": h["level"], "text": h["text"]}
        for h in parser.headings
        if h["_slot"].style_token is None or h["_slot"].style_token not in drop_heading_styles
    ]
    return {"paragraph_count": len(parser.slots), "headings": headings}


def _extract_block_lo_styles(html: str) -> tuple[str, list[str | None]]:
    """Return ``(clean_html, [data_lo_style_or_None per top-level block])``.

    Short-circuits (returns the html unchanged, no styles) when there is no data-lo-style,
    so existing callers are completely unaffected."""
    if not html or "data-lo-style" not in html:
        return html, []
    ex = _BlockLoStyleExtractor()
    ex.feed(html)
    ex.close()
    return "".join(ex._out), ex.styles


def _resolve_paragraph_style_token(
    fam: Any,
    token: str,
    element_names: list[str] | tuple[str, ...],
) -> tuple[str, str | None]:
    """Resolve an agent-facing compact ``data-lo-style`` token to a real UNO ParaStyleName.

    Returns ``(resolved_style_name, reason)`` where reason is None on success, or
    ``'ambiguous'`` / ``'unknown'`` when falling back to 'Standard'.

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
        return candidates[0], None
    if len(candidates) > 1:
        log.debug("data-lo-style: token %r is ambiguous across %r; falling back to Standard",
                  token, candidates)
        return "Standard", "ambiguous"
    lower = token.lower()
    resolved = token
    for name in element_names:
        if name.lower() == lower:
            resolved = name
            break
    if fam is None:
        return resolved, None
    if uno_read(lambda: fam.hasByName(resolved), False):
        return resolved, None
    return "Standard", "unknown"


def _apply_block_lo_styles(
    model: Any, text_obj: Any, start: Any, styles: list[str | None], end: Any = None
) -> list[tuple[str, str]]:
    """Apply each block's data-lo-style to the inserted paragraphs (positionally).

    Forward cursor walk from the insertion point avoids scanning the whole document.
    Compact tokens (``Heading1``) are resolved to UNO names; ambiguous and unknown styles
    fall back to 'Standard' (per the style-model plan).

    Returns a list of ``(token, reason)`` pairs, unique in first-seen order, where reason
    is ``'ambiguous'``, ``'unknown'``, or ``'failed'``. If a token whose resolution fell back
    subsequently raises on application, ``'failed'`` replaces the earlier reason.
    """
    try:
        from .styles import apply_paragraph_style_preserving_direct_char
    except ImportError:
        return [(t, "failed") for t in dict.fromkeys(s for s in styles if s)]

    fam = uno_read(lambda: model.getStyleFamilies().getByName("ParagraphStyles"), None)
    element_names: list[str] = uno_read(lambda: list(fam.getElementNames()), []) if fam is not None else []

    try:
        start_range = start.getStart() if hasattr(start, "getStart") else start
        walk_cur = text_obj.createTextCursorByRange(start_range)
        walk_cur.gotoStartOfParagraph(False)
    except Exception as exc:
        reraise_if_disposed(exc)
        return [(t, "failed") for t in dict.fromkeys(s for s in styles if s)]

    reports: dict[str, str] = {}
    style_cache: dict[Any, Any] = {}
    for style in styles:
        # Stop at the first paragraph that does not start before the parked end cursor.
        try:
            if end is not None and text_obj.compareRegionStarts(walk_cur.getStart(), end) != 1:
                break
        except Exception as exc:
            reraise_if_disposed(exc)
            break

        if style:
            resolved, reason = _resolve_paragraph_style_token(fam, style, element_names=element_names)
            if reason is not None and style not in reports:
                reports[style] = reason
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
                    reports[style] = "failed"

        try:
            if not walk_cur.gotoNextParagraph(False):
                break
        except Exception as exc:
            reraise_if_disposed(exc)
            break

    return list(reports.items())
