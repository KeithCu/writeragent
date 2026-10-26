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
from typing import Any, ClassVar

from plugin.framework.errors import reraise_if_disposed, uno_read

from . import xhtml_style_postprocess as xhtml_post

log = logging.getLogger("writeragent.writer")


def _strip_data_lo_style(start_tag: str) -> str:
    """Remove any data-lo-style attribute from a single start-tag string."""
    start_tag = re.sub(r'\s+data-lo-style="[^"]*"', "", start_tag)
    return re.sub(r"\s+data-lo-style='[^']*'", "", start_tag)



class _BlockLoStyleExtractor(xhtml_post._HtmlPassthroughParser):
    """Collect data-lo-style per TOP-LEVEL block (document order) and strip the attribute
    from the HTML, so the StarWriter import sees clean markup and we apply the named styles
    ourselves afterwards. Content inside <table> is left to the import (avoids order desync)."""

    _table_depth: int

    # Paragraph-level blocks cannot contain blocks: a new one closes the open one (<p>a<p>b).
    _PARA_LEVEL: ClassVar[frozenset[str]] = frozenset({"p", "pre", "h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self) -> None:
        super().__init__()
        self.styles: list[str | None] = []
        # Open block-level elements: [tag, first child block already shared this slot?], or
        # [tag, None] for a transparent <div> (no slot of its own).
        self._open: list[list[Any]] = []

    @staticmethod
    def _style_of(attrs: list[tuple[str, str | None]]) -> str | None:
        val = None
        for k, v in attrs:
            if k == "data-lo-style":
                val = v
        return val

    def _is_block(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        # A <div> is a transparent container -- except the one the read path gives a
        # data-lo-style: LibreOffice exports a paragraph holding a frame (an image) as
        # <div class="paragraph-X">, and that div is one Writer paragraph on import too.
        return tag in xhtml_post.BLOCK_TAGS or (tag == "div" and self._style_of(attrs) is not None)

    def _slot(self, val: str | None) -> None:
        # <blockquote><p> and <li><p> take two slots but make ONE Writer
        # paragraph, so every later style lands one paragraph late ("2. DO
        # DIREITO" loses its heading, a list item becomes a quote). A
        # block's first child block is that same paragraph -- it shares
        # the slot, and its own style wins when it has one.
        parent = self._open[-1] if self._open else None
        if parent is not None and parent[1] is False and self.styles:
            parent[1] = True
            if val is not None:
                self.styles[-1] = val
        else:
            self.styles.append(val)

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
            self._open.append([t, None])  # transparent div
            self._out.append(raw)
            return
        while self._open and self._open[-1][0] in self._PARA_LEVEL:
            self._open.pop()
        self._slot(self._style_of(attrs))
        self._open.append([t, False])
        self._out.append(_strip_data_lo_style(raw))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or ("<%s/>" % tag)
        t = tag.lower()
        if self._table_depth == 0 and self._is_block(t, attrs):
            self._slot(self._style_of(attrs))
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
                if self._open[idx][0] == t:
                    del self._open[idx:]
                    break
        self._out.append("</%s>" % tag)

    def result(self) -> tuple[str, list[str | None]]:
        return "".join(self._out), self.styles



def _extract_block_lo_styles(html: str) -> tuple[str, list[str | None]]:
    """Return ``(clean_html, [data_lo_style_or_None per top-level block])``.

    Short-circuits (returns the html unchanged, no styles) when there is no data-lo-style,
    so existing callers are completely unaffected."""
    if not html or "data-lo-style" not in html:
        return html, []
    ex = _BlockLoStyleExtractor()
    ex.feed(html)
    ex.close()
    return ex.result()



def _resolve_paragraph_style_token(
    model: Any,
    fam: Any,
    token: str,
    element_names: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Resolve an agent-facing compact ``data-lo-style`` token to a real UNO ParaStyleName.

    A candidate is any paragraph style whose name equals the token (covers space-free names
    and an agent that passed the exact spaced form) OR whose space-free form equals the token
    (``Heading1`` -> ``Heading 1``). Exactly one candidate -> use it. **More than one ->
    ambiguous** (e.g. a literal ``Heading1`` coexisting with built-in ``Heading 1``); fail safe
    to ``Standard`` instead of silently picking one. No candidate -> case-insensitive resolve,
    then ``Standard``. The ambiguity gate runs BEFORE any exact-name shortcut so a colliding
    token can never silently land on the wrong style (per docs/writer/html-style-model-plan.md)."""
    names = element_names
    if names is None and fam is not None:
        names = uno_read(lambda: list(fam.getElementNames()), [])
    if names:
        # dict.fromkeys keeps first-seen order and drops duplicates.
        candidates: list[str] = list(
            dict.fromkeys(
                n for n in names
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
    for name in names or ():
        if name.lower() == lower:
            resolved = name
            break
    if fam is None or uno_read(lambda: fam.hasByName(resolved), False):
        return resolved
    return "Standard"



def _apply_block_lo_styles(model: Any, text_obj: Any, start: Any, styles: list[str | None], end: Any = None) -> None:
    """Apply each block's data-lo-style to the inserted paragraphs (positionally).

    Forward cursor walk from the insertion point avoids scanning the whole document.
    Compact tokens (``Heading1``) are resolved to UNO names; unknown styles fall back to
    'Standard' (per the style-model plan)."""
    from . import format as format_mod

    fam = uno_read(lambda: model.getStyleFamilies().getByName("ParagraphStyles"), None)
    element_names: list[str] = uno_read(lambda: list(fam.getElementNames()), []) if fam is not None else []
    token_cache: dict[str, str] = {}

    try:
        start_range = start.getStart() if hasattr(start, "getStart") else start
        walk_cur = text_obj.createTextCursorByRange(start_range)
        walk_cur.gotoStartOfParagraph(False)
    except Exception as exc:
        reraise_if_disposed(exc)
        return

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
            resolved = token_cache.get(style)
            if resolved is None:
                resolved = _resolve_paragraph_style_token(model, fam, style, element_names=element_names)
                token_cache[style] = resolved
            # The import already set the style; re-applying only re-reads every portion.
            current = uno_read(lambda: walk_cur.getPropertyValue("ParaStyleName"), None)
            if current != resolved:
                try:
                    cur = text_obj.createTextCursorByRange(walk_cur.getStart())
                    cur.gotoEndOfParagraph(True)
                    format_mod.apply_paragraph_style_preserving_direct_char(
                        model, cur, resolved, style_cache=style_cache,
                    )
                except Exception as exc:
                    reraise_if_disposed(exc)
                    log.debug("data-lo-style: failed to apply %r", style, exc_info=True)

        try:
            if not walk_cur.gotoNextParagraph(False):
                break
        except Exception as exc:
            reraise_if_disposed(exc)
            break
