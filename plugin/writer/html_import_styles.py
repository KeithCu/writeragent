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

import html.parser
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
class _BlockLoStyleExtractor(xhtml_post._HtmlPassthroughParser):
    """Collect data-lo-style per TOP-LEVEL block (document order) and strip the attribute
    from the HTML, so the StarWriter import sees clean markup and we apply the named styles
    ourselves afterwards. Content inside <table> is left to the import (avoids order desync)."""

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
        return dict(attrs).get("data-lo-style")

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
        if parent is not None and parent[1] is False:
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


def _apply_block_lo_styles(model: Any, text_obj: Any, start: Any, styles: list[str | None], end: Any = None) -> None:
    """Apply each block's data-lo-style to the inserted paragraphs (positionally).

    Forward cursor walk from the insertion point avoids scanning the whole document.
    Compact tokens (``Heading1``) are resolved to UNO names; unknown styles fall back to
    'Standard' (per the style-model plan)."""
    try:
        from .styles import apply_paragraph_style_preserving_direct_char
    except ImportError:
        return

    fam = uno_read(lambda: model.getStyleFamilies().getByName("ParagraphStyles"), None)
    element_names: list[str] = uno_read(lambda: list(fam.getElementNames()), []) if fam is not None else []

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

        try:
            if not walk_cur.gotoNextParagraph(False):
                break
        except Exception as exc:
            reraise_if_disposed(exc)
            break


# ------------------------------------------------------------------
# Write warning collection and result attachment
# ------------------------------------------------------------------

# Known property categories parsed out of data-lo-para, in stable emission order.
# Each entry pairs a warning code with the specific CSS property names belonging to it.
_KNOWN_PROPERTY_SPECS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("paragraph_margin_ignored", ("margin", "margin-left", "margin-right", "margin-top", "margin-bottom")),
    ("paragraph_indent_ignored", ("text-indent",)),
    ("paragraph_alignment_ignored", ("text-align",)),
    ("paragraph_line_height_ignored", ("line-height",)),
    ("paragraph_font_ignored", ("font", "font-family", "font-size", "font-weight", "font-style")),
    ("paragraph_color_ignored", ("color", "background", "background-color")),
)

_KNOWN_PROPERTY_MAP: dict[str, str] = {
    prop: code for code, props in _KNOWN_PROPERTY_SPECS for prop in props
}

# The read path reports hand-set paragraph formatting as data-lo-para. When an agent passes
# it back to a write tool, the write filter drops it. We append this note to the success message
# whenever data-lo-para was detected, pointing the caller to apply_style.
_DATA_LO_PARA_NOTE = (
    " Note: data-lo-para in the content was ignored — it is a read-only report of a paragraph's hand-set"
    " formatting. To change an indent or a font, apply a named style (apply_style;"
    " default clear_direct lets the style's font and size show)."
)


@final
class _WriteWarningCollector(html.parser.HTMLParser):
    """Scan HTML fragments for attributes and styles dropped by the import filter.

    Tracks table nesting depth so data-lo-style tags inside table cells are flagged
    separately from body blocks. Tags with data-lo-para are parsed into property declarations,
    retaining the first seen value for each unique property name.
    """

    def __init__(self, styles_applied: bool) -> None:
        super().__init__()
        self.styles_applied = styles_applied
        self._table_depth = 0
        self.has_data_lo_para = False
        self.para_properties: dict[str, str] = {}
        self.has_data_lo_style_ignored = False
        self.has_table_cell_style_ignored = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t == "table":
            self._table_depth += 1
        attr_dict = {k.lower(): v for k, v in attrs if v is not None}
        if "data-lo-para" in attr_dict:
            self.has_data_lo_para = True
            raw_para = attr_dict["data-lo-para"]
            for piece in raw_para.split(";"):
                piece = piece.strip()
                if not piece or ":" not in piece:
                    continue
                name, val = piece.split(":", 1)
                name = name.strip().lower()
                val = val.strip()
                if name and name not in self.para_properties:
                    self.para_properties[name] = val

        if "data-lo-style" in attr_dict:
            raw_style = attr_dict["data-lo-style"]
            if raw_style is not None and raw_style.strip():
                if self._table_depth > 0:
                    self.has_table_cell_style_ignored = True
                else:
                    is_block = t in xhtml_post.BLOCK_TAGS or t == "div"
                    if not (self.styles_applied and is_block):
                        self.has_data_lo_style_ignored = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "table" and self._table_depth > 0:
            self._table_depth -= 1


def _build_property_warning(
    code: str,
    prop: str,
    val: str,
    suggest: str,
    suggest_arguments: dict[str, Any] | None,
) -> dict[str, Any]:
    """Construct a warning dictionary for a single dropped paragraph property declaration."""
    if code == "paragraph_margin_ignored":
        message = f"Paragraph margin {prop}={val} was not applied."
    elif code == "paragraph_indent_ignored":
        message = f"Paragraph indent {prop}={val} was not applied. A named style has to carry the indent."
    elif code == "paragraph_alignment_ignored":
        message = f"Paragraph alignment {prop}={val} was not applied."
    elif code == "paragraph_line_height_ignored":
        message = f"Paragraph line-height {prop}={val} was not applied."
    elif code == "paragraph_font_ignored":
        message = f"Paragraph font {prop}={val} was not applied."
    elif code == "paragraph_color_ignored":
        message = f"Paragraph colour {prop}={val} was not applied."
    else:
        message = f"Paragraph property {prop}={val} was not applied."

    suggested_call: dict[str, Any] | None = None
    if suggest == "apply_style":
        suggested_call = {
            "tool": "apply_style",
            "arguments": {"family": "ParagraphStyles"},
        }
    elif suggest == "set_style":
        args: dict[str, Any] = {}
        if suggest_arguments:
            args.update(suggest_arguments)
        if prop == "text-align" and val.lower() in ("left", "center", "right", "justify"):
            args["h_align"] = val.lower()
            suggested_call = {"tool": "set_style", "arguments": args}
        elif prop in ("color", "background-color", "background"):
            clean_val = val.strip()
            is_hex = bool(re.match(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$", clean_val))
            is_word = bool(re.match(r"^[a-zA-Z]+$", clean_val))
            if is_hex or is_word:
                color_key = "bg_color" if "background" in prop else "font_color"
                args[color_key] = clean_val
                suggested_call = {"tool": "set_style", "arguments": args}

    w: dict[str, Any] = {
        "code": code,
        "attribute": prop,
        "value": val,
        "message": message,
    }
    if suggested_call is not None:
        w["suggested_call"] = suggested_call
    return w


def collect_write_warnings(
    content: Any,
    *,
    styles_applied: bool,
    suggest: str = "apply_style",
    suggest_arguments: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Scan write content and return structured warning dictionaries for dropped attributes.

    Emits the umbrella data_lo_para_ignored warning first, followed by known paragraph
    properties in stable category order, unknown properties in first-seen order,
    data_lo_style_ignored, and table_cell_style_ignored.
    """
    if isinstance(content, (list, tuple)):
        content = "\n".join(x for x in content if isinstance(x, str))
    if not isinstance(content, str) or "data-lo-" not in content.lower():
        return []

    collector = _WriteWarningCollector(styles_applied=styles_applied)
    collector.feed(content)
    collector.close()

    warnings: list[dict[str, Any]] = []

    if collector.has_data_lo_para:
        w_umbrella: dict[str, Any] = {
            "code": "data_lo_para_ignored",
            "attribute": "data-lo-para",
            "message": "data-lo-para was ignored. It is a read-only report of hand-set paragraph formatting, not a write instruction.",
        }
        if suggest == "apply_style":
            w_umbrella["suggested_call"] = {
                "tool": "apply_style",
                "arguments": {"family": "ParagraphStyles"},
            }
        warnings.append(w_umbrella)

    for _code, prop_names in _KNOWN_PROPERTY_SPECS:
        for prop in prop_names:
            if prop in collector.para_properties:
                val = collector.para_properties[prop]
                warnings.append(
                    _build_property_warning(
                        _code,
                        prop,
                        val,
                        suggest,
                        suggest_arguments,
                    )
                )

    for prop, val in collector.para_properties.items():
        if prop not in _KNOWN_PROPERTY_MAP:
            warnings.append(
                _build_property_warning(
                    "data_lo_para_property_ignored",
                    prop,
                    val,
                    suggest,
                    suggest_arguments,
                )
            )

    if collector.has_data_lo_style_ignored:
        msg_style = "named styles are applied only on full_document, beginning, and end, and only on paragraph blocks outside tables."
        w_style: dict[str, Any] = {
            "code": "data_lo_style_ignored",
            "attribute": "data-lo-style",
        }
        if suggest == "apply_style":
            w_style["message"] = f"{msg_style} Pass old_content and the real style name to apply_style."
            w_style["suggested_call"] = {
                "tool": "apply_style",
                "arguments": {
                    "family": "ParagraphStyles",
                    "target": "search",
                },
            }
        else:
            w_style["message"] = msg_style
        warnings.append(w_style)

    if collector.has_table_cell_style_ignored:
        msg_table = "data-lo-style inside a table is not applied."
        w_table: dict[str, Any] = {
            "code": "table_cell_style_ignored",
            "attribute": "data-lo-style",
        }
        if suggest == "apply_style":
            w_table["message"] = f"{msg_table} Pass old_content and the real style name to apply_style."
            w_table["suggested_call"] = {
                "tool": "apply_style",
                "arguments": {
                    "family": "ParagraphStyles",
                    "target": "search",
                },
            }
        else:
            w_table["message"] = msg_table
        warnings.append(w_table)

    return warnings


def attach_write_warnings(
    result: Any,
    content: Any,
    *,
    styles_applied: bool,
    suggest: str = "apply_style",
    suggest_arguments: dict[str, Any] | None = None,
) -> Any:
    """Attach write warnings to successful document write results.

    Non-dict returns and error responses pass through untouched without a warnings key.
    Successful results always receive a warnings list (empty when nothing was dropped),
    and have the explanatory data-lo-para note appended to message when applicable.
    """
    if not isinstance(result, dict) or result.get("status") != "ok":
        return result

    copied = dict(result)
    warnings = collect_write_warnings(
        content,
        styles_applied=styles_applied,
        suggest=suggest,
        suggest_arguments=suggest_arguments,
    )
    copied["warnings"] = warnings
    if any(w.get("code") == "data_lo_para_ignored" for w in warnings):
        copied["message"] = (copied.get("message") or "") + _DATA_LO_PARA_NOTE
    return copied

