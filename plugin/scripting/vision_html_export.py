# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Export Docling / Paddle vision OCR results to HTML for LO import."""

from __future__ import annotations

import html as html_module
import importlib
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

CSS_INLINE_INSTALL_CMD = "pip install css-inline"

# Docling inlines h2-h6 with color/margins only; StarWriter needs explicit size/weight.
_LO_HEADING_INLINE: dict[str, str] = {
    "h1": "font-size: 18pt; font-weight: bold;",
    "h2": "font-size: 14pt; font-weight: bold;",
    "h3": "font-size: 12pt; font-weight: bold;",
    "h4": "font-size: 11pt; font-weight: bold;",
    "h5": "font-size: 10pt; font-weight: bold;",
    "h6": "font-size: 10pt; font-weight: bold; font-style: italic;",
}
_HEADING_TAG_RE = re.compile(r"<(h[1-6])(\s[^>]*)?>", re.IGNORECASE)
_PLAIN_P_TAG_RE = re.compile(r"<p(?![^>]*\bstyle\s*=)(\s[^>]*)?>", re.IGNORECASE)

_LO_BODY_PARAGRAPH_INLINE = "font-family: Arial, sans-serif; line-height: 1.6;"

# Minimal stylesheet for Paddle-built fragments before css-inline hoists rules.
_PADDLE_HTML_STYLE = """<style>
body { font-family: Arial, sans-serif; line-height: 1.6; }
h2 { font-size: 1.25em; font-weight: bold; margin: 0.75em 0 0.35em; }
p { margin: 0.35em 0; }
table { border-collapse: collapse; margin: 0.5em 0; width: 100%; }
th, td { border: 1px solid #ddd; padding: 6px 8px; text-align: left; }
th { background-color: #f2f2f2; font-weight: bold; }
</style>"""


def _merge_inline_style_on_open_tag(match: re.Match[str], extra: str) -> str:
    tag = match.group(1).lower()
    attrs = match.group(2) or ""
    if re.search(r'style\s*=\s*"', attrs, re.IGNORECASE):
        return re.sub(
            r'(style\s*=\s*")',
            lambda style_match: f"{style_match.group(1)}{extra}",
            f"<{tag}{attrs}>",
            count=1,
            flags=re.IGNORECASE,
        )
    return f'<{tag} style="{extra}"{attrs}>'


def augment_lo_heading_styles(html: str) -> str:
    """Merge bold/font-size into heading tags — Docling CSS leaves h2 looking like body text."""

    def _repl(match: re.Match[str]) -> str:
        tag = match.group(1).lower()
        extra = _LO_HEADING_INLINE.get(tag, "")
        if not extra:
            return match.group(0)
        return _merge_inline_style_on_open_tag(match, extra)

    return _HEADING_TAG_RE.sub(_repl, html)


def augment_lo_body_paragraph_styles(html: str) -> str:
    """Add Arial/line-height on bare <p> tags — Docling body lines have no inline styles."""

    def _repl(match: re.Match[str]) -> str:
        attrs = match.group(1) or ""
        return f'<p style="{_LO_BODY_PARAGRAPH_INLINE}"{attrs}>'

    return _PLAIN_P_TAG_RE.sub(_repl, html)


def prepare_html_for_lo_import(html: str) -> str:
    """Inline CSS so LibreOffice HTML (StarWriter) import keeps typography."""
    import css_inline

    stripped = (html or "").strip()
    if not stripped:
        return html or ""
    inlined = css_inline.inline(stripped)
    with_headings = augment_lo_heading_styles(inlined)
    return augment_lo_body_paragraph_styles(with_headings)


def _wrap_paddle_fragment(body: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset=\"UTF-8\">"
        f"{_PADDLE_HTML_STYLE}</head><body>{body}</body></html>"
    )


def export_docling_to_html(document: Any, params: dict[str, Any]) -> str:
    """Return rich HTML from a DoclingDocument (bold, tables, headings), css-inlined for LO."""
    del params  # reserved for future Docling HTML export options
    docling_doc_mod = importlib.import_module("docling_core.types.doc")
    image_ref_mode = docling_doc_mod.ImageRefMode

    if hasattr(document, "export_to_html"):
        raw = str(
            document.export_to_html(
                image_mode=image_ref_mode.EMBEDDED,
                split_page_view=False,
            )
            or ""
        )
        return prepare_html_for_lo_import(raw)
    return ""


def html_from_paddle_regions(regions: list[dict[str, Any]]) -> str:
    """Minimal HTML from Paddle OCR line regions (reading order), css-inlined for LO."""
    parts: list[str] = []
    for region in regions:
        if not isinstance(region, dict):
            continue
        text = str(region.get("text") or "").strip()
        if text:
            parts.append(f"<p>{html_module.escape(text)}</p>")
    if not parts:
        return ""
    return prepare_html_for_lo_import(_wrap_paddle_fragment("\n".join(parts)))


def _paddle_block_tag(block_type: str) -> str:
    label = block_type.strip().lower()
    if label in ("title", "section_header", "header", "heading"):
        return "h2"
    if label in ("caption", "footnote"):
        return "p"
    return "p"


def _html_table_from_columns_rows(columns: list[Any], rows: list[list[Any]]) -> str:
    if not columns and not rows:
        return ""
    lines = ["<table>"]
    if columns:
        lines.append("<thead><tr>")
        for col in columns:
            lines.append(f"<th>{html_module.escape(str(col))}</th>")
        lines.append("</tr></thead>")
    if rows:
        lines.append("<tbody>")
        for row in rows:
            if not isinstance(row, list):
                continue
            lines.append("<tr>")
            for cell in row:
                lines.append(f"<td>{html_module.escape(str(cell))}</td>")
            lines.append("</tr>")
        lines.append("</tbody>")
    lines.append("</table>")
    return "".join(lines)


def html_from_paddle_structure(
    blocks: list[dict[str, Any]],
    tables: list[dict[str, Any]],
) -> str:
    """Build HTML from Paddle PP-Structure blocks and parsed tables, css-inlined for LO."""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "text")
        text = str(block.get("text") or "").strip()
        if block_type == "table" and not text:
            continue
        tag = _paddle_block_tag(block_type)
        if not text:
            continue
        if tag == "h2":
            parts.append(f"<h2>{html_module.escape(text)}</h2>")
        else:
            parts.append(f"<p>{html_module.escape(text)}</p>")

    for table in tables:
        if not isinstance(table, dict):
            continue
        table_html = _html_table_from_columns_rows(
            list(table.get("columns") or []),
            [list(r) for r in (table.get("rows") or []) if isinstance(r, list)],
        )
        if table_html:
            parts.append(table_html)

    if not parts:
        return ""
    return prepare_html_for_lo_import(_wrap_paddle_fragment("\n".join(parts)))
