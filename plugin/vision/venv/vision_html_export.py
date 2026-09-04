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
# Cell borders use HTML + inline CSS so StarWriter draws grids even when a
# <style> block is dropped. Header fill matches the post-inline th augment.
_PADDLE_HTML_STYLE = """<style>
body { font-family: Arial, sans-serif; line-height: 1.6; }
h2 { font-size: 1.25em; font-weight: bold; margin: 0.75em 0 0.35em; }
p { margin: 0.35em 0; }
table { border-collapse: collapse; margin: 0.5em 0; width: 100%; border: 1px solid #ccc; }
th, td { border: 1px solid #ccc; padding: 6px 8px; text-align: left; }
th { background-color: #f0f0f0; font-weight: bold; }
</style>"""

# StarWriter often ignores stylesheet table rules; HTML border="1" plus these
# inline decls make gridlines and header chrome survive css-inline + import.
_LO_TABLE_INLINE = "border-collapse: collapse; border: 1px solid #ccc;"
_LO_CELL_BORDER_INLINE = "border: 1px solid #ccc;"
_LO_TH_INLINE = "background-color: #f0f0f0; font-weight: bold;"

_TABLE_OPEN_RE = re.compile(r"<table(\s[^>]*)?>", re.IGNORECASE)
_TD_TH_OPEN_RE = re.compile(r"<(td|th)(\s[^>]*)?>", re.IGNORECASE)
_TH_OPEN_RE = re.compile(r"<th(\s[^>]*)?>", re.IGNORECASE)
_TABLE_BLOCK_RE = re.compile(r"(<table\b[^>]*>)(.*?)(</table>)", re.IGNORECASE | re.DOTALL)
_TR_BLOCK_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.IGNORECASE | re.DOTALL)
_MATH_BLOCK_RE = re.compile(r"<math\b[^>]*>.*?</math>", re.IGNORECASE | re.DOTALL)


def _open_tag_with_style(tag: str, attrs: str, extra: str) -> str:
    if not extra:
        return f"<{tag}{attrs}>"
    if re.search(r'style\s*=\s*"', attrs, re.IGNORECASE):
        return re.sub(
            r'(style\s*=\s*")',
            lambda style_match: f"{style_match.group(1)}{extra}",
            f"<{tag}{attrs}>",
            count=1,
            flags=re.IGNORECASE,
        )
    return f'<{tag} style="{extra}"{attrs}>'


def _merge_inline_style_on_open_tag(match: re.Match[str], extra: str) -> str:
    tag = match.group(1).lower()
    attrs = match.group(2) or ""
    return _open_tag_with_style(tag, attrs, extra)


def _style_attr(attrs: str) -> str:
    match = re.search(r'style\s*=\s*"([^"]*)"', attrs, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def _style_has_property(style: str, prop: str) -> bool:
    return re.search(rf"(?:^|;)\s*{re.escape(prop)}\s*:", style, flags=re.IGNORECASE) is not None


def _style_has_border_none(attrs: str) -> bool:
    """True for layout-only tables (two-column bbox HTML uses border:none)."""
    return bool(re.search(r"(?:^|;)\s*border\s*:\s*none\b", _style_attr(attrs), flags=re.IGNORECASE))


def _new_style_decls(attrs: str, extra: str) -> str:
    """Return extra ``prop: val;`` pieces whose properties are not already in style=."""
    existing = _style_attr(attrs)
    kept: list[str] = []
    for part in extra.split(";"):
        piece = part.strip()
        if not piece or ":" not in piece:
            continue
        prop = piece.split(":", 1)[0].strip()
        if not prop or _style_has_property(existing, prop):
            continue
        kept.append(f"{piece};")
    return " ".join(kept)


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


def _import_latex2mathml_convert() -> Any | None:
    """latex2mathml is vendored; Docling also depends on it. Missing → leave TeX as-is."""
    try:
        from latex2mathml.converter import convert as latex_to_mathml

        return latex_to_mathml
    except ImportError:
        pass
    import os
    import sys

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
    for extra in (os.path.join(root, "vendor"), os.path.join(root, "plugin", "lib")):
        if os.path.isdir(os.path.join(extra, "latex2mathml")) and extra not in sys.path:
            sys.path.insert(0, extra)
    try:
        from latex2mathml.converter import convert as latex_to_mathml

        return latex_to_mathml
    except ImportError:
        return None


def _latex_to_math_element(inner: str, *, display_block: bool, convert_fn: Any) -> str | None:
    trimmed = (inner or "").strip()
    if not trimmed:
        return None
    display_mode = "block" if display_block else "inline"
    try:
        mathml = convert_fn(trimmed, display=display_mode)
    except Exception as exc:
        log.debug("latex2mathml convert failed: %s", exc, exc_info=True)
        return None
    if not isinstance(mathml, str) or not mathml.strip():
        return None
    text = mathml.strip()
    if not text.lower().startswith("<math"):
        return None
    return text


def _looks_like_currency_dollar(s: str, idx: int) -> bool:
    """Skip ``$ 35,934`` / ``$9.00`` so financial tables are not treated as TeX."""
    j = idx + 1
    n = len(s)
    while j < n and s[j] in " \t":
        j += 1
    return j < n and s[j].isdigit()


def _replace_tex_outside_tags(s: str, convert_fn: Any) -> str:
    """Turn ``$$…$$`` / ``\\[…\\]`` / ``\\(…\\)`` (and conservative ``$…$``) into ``<math>``."""
    out: list[str] = []
    i = 0
    n = len(s)
    in_tag = False
    while i < n:
        ch = s[i]
        if not in_tag and ch == "<":
            in_tag = True
            out.append(ch)
            i += 1
            continue
        if in_tag:
            out.append(ch)
            if ch == ">":
                in_tag = False
            i += 1
            continue
        if s.startswith("$$", i):
            close_at = s.find("$$", i + 2)
            if close_at != -1:
                mathml = _latex_to_math_element(s[i + 2 : close_at], display_block=True, convert_fn=convert_fn)
                if mathml is not None:
                    out.append(mathml)
                    i = close_at + 2
                    continue
        elif s.startswith("\\[", i):
            close_at = s.find("\\]", i + 2)
            if close_at != -1:
                mathml = _latex_to_math_element(s[i + 2 : close_at], display_block=True, convert_fn=convert_fn)
                if mathml is not None:
                    out.append(mathml)
                    i = close_at + 2
                    continue
        elif s.startswith("\\(", i):
            close_at = s.find("\\)", i + 2)
            if close_at != -1:
                mathml = _latex_to_math_element(s[i + 2 : close_at], display_block=False, convert_fn=convert_fn)
                if mathml is not None:
                    out.append(mathml)
                    i = close_at + 2
                    continue
        elif ch == "$" and not _looks_like_currency_dollar(s, i):
            close_at = s.find("$", i + 1)
            if close_at != -1 and close_at > i + 1:
                inner = s[i + 1 : close_at]
                # Require a TeX cue so prose like "cost $foo later $bar" stays text.
                if "\\" in inner or "^" in inner or re.search(r"_[{\\A-Za-z]", inner):
                    mathml = _latex_to_math_element(inner, display_block=False, convert_fn=convert_fn)
                    if mathml is not None:
                        out.append(mathml)
                        i = close_at + 1
                        continue
        out.append(ch)
        i += 1
    return "".join(out)


def convert_latex_delimiters_to_mathml(html: str) -> str:
    """Replace leftover Docling TeX delimiters with MathML for native LO Math objects.

    Docling's HTML serializer already sets ``formula_to_mathml=True``, but formula
    items that fail conversion (or TeX that landed in a ``<p>``) still use
    ``$$…$$``. Host ``insert_html_at_cursor`` maps ``<math display="block">`` to
    editable LibreOffice Math embeds. Leave original text if latex2mathml is
    missing or a fragment does not convert.
    """
    convert_fn = _import_latex2mathml_convert()
    if convert_fn is None:
        return html
    parts: list[str] = []
    pos = 0
    for math_match in _MATH_BLOCK_RE.finditer(html):
        parts.append(_replace_tex_outside_tags(html[pos : math_match.start()], convert_fn))
        parts.append(math_match.group(0))
        pos = math_match.end()
    parts.append(_replace_tex_outside_tags(html[pos:], convert_fn))
    return "".join(parts)


def _header_row_to_th(row_html: str) -> str:
    row_html = re.sub(r"<td\b", "<th", row_html, flags=re.IGNORECASE)
    return re.sub(r"</td\b", "</th", row_html, flags=re.IGNORECASE)


def _promote_first_header_row(inner: str) -> str:
    """Move the first ``<th>`` row into ``<thead>`` so Writer can repeat header rows."""
    if re.search(r"<thead\b", inner, flags=re.IGNORECASE):
        return inner
    tr_match = _TR_BLOCK_RE.search(inner)
    if tr_match is None:
        return inner
    first = tr_match.group(0)
    if not re.search(r"<th\b", first, flags=re.IGNORECASE):
        return inner
    first = _header_row_to_th(first)
    before = inner[: tr_match.start()]
    after = inner[tr_match.end() :]
    before_stripped = before.strip()
    after_stripped = after.strip()
    thead = f"<thead>{first}</thead>"
    tbody_open = re.match(r"(?is)^<tbody\b[^>]*>$", before_stripped)
    tbody_close = re.search(r"(?is)</tbody\s*>$", after_stripped)
    if tbody_open and tbody_close:
        body_rows = after_stripped[: tbody_close.start()].strip()
        if body_rows:
            return f"{thead}<tbody>{body_rows}</tbody>"
        return thead
    if re.search(r"(?is)<tbody\b[^>]*>\s*$", before) and re.search(r"(?is)</tbody\s*>", after):
        after_no_close = re.sub(r"(?is)</tbody\s*>\s*$", "", after_stripped).strip()
        before_no_open = re.sub(r"(?is)^\s*<tbody\b[^>]*>", "", before_stripped).strip()
        body = f"{before_no_open}{after_no_close}".strip()
        if body:
            return f"{thead}<tbody>{body}</tbody>"
        return thead
    rest = f"{before}{after}".strip()
    if rest:
        return f"{thead}<tbody>{rest}</tbody>"
    return thead


def promote_table_header_rows(html: str) -> str:
    """Wrap the first header row of each data table in ``<thead>`` (skip layout tables)."""

    def _repl(match: re.Match[str]) -> str:
        open_tag, inner, close_tag = match.group(1), match.group(2), match.group(3)
        attrs = open_tag[6:-1] if len(open_tag) >= 7 else ""
        if _style_has_border_none(attrs):
            return match.group(0)
        return f"{open_tag}{_promote_first_header_row(inner)}{close_tag}"

    return _TABLE_BLOCK_RE.sub(_repl, html)


def augment_lo_table_styles(html: str) -> str:
    """Force visible gridlines and distinct ``<th>`` chrome for StarWriter import."""

    def _table_repl(match: re.Match[str]) -> str:
        attrs = match.group(1) or ""
        if _style_has_border_none(attrs):
            return match.group(0)
        if not re.search(r"\bborder\s*=", attrs, flags=re.IGNORECASE):
            attrs = f' border="1"{attrs}'
        elif re.search(r"""\bborder\s*=\s*(['"]?)0\1""", attrs, flags=re.IGNORECASE):
            attrs = re.sub(
                r"""\bborder\s*=\s*(['"]?)0\1""",
                'border="1"',
                attrs,
                count=1,
                flags=re.IGNORECASE,
            )
        extra = _new_style_decls(attrs, _LO_TABLE_INLINE)
        return _open_tag_with_style("table", attrs, extra)

    def _cell_repl(match: re.Match[str]) -> str:
        tag = match.group(1).lower()
        attrs = match.group(2) or ""
        if _style_has_border_none(attrs):
            return match.group(0)
        extras = [_new_style_decls(attrs, _LO_CELL_BORDER_INLINE)]
        if tag == "th":
            extras.append(_new_style_decls(attrs, _LO_TH_INLINE))
        extra = " ".join(piece for piece in extras if piece)
        return _open_tag_with_style(tag, attrs, extra)

    with_tables = _TABLE_OPEN_RE.sub(_table_repl, html)
    return _TD_TH_OPEN_RE.sub(_cell_repl, with_tables)


def prepare_html_for_lo_import(html: str) -> str:
    """Inline CSS so LibreOffice HTML (StarWriter) import keeps typography."""
    try:
        import css_inline
    except ImportError as exc:
        import sys

        raise ImportError(
            f"No module named 'css_inline' in {sys.executable}. "
            f"Install in your Settings → Python venv: {sys.executable} -m pip install css-inline"
        ) from exc

    stripped = (html or "").strip()
    if not stripped:
        return html or ""
    inlined = css_inline.inline(stripped)
    with_headings = augment_lo_heading_styles(inlined)
    with_body = augment_lo_body_paragraph_styles(with_headings)
    with_math = convert_latex_delimiters_to_mathml(with_body)
    with_thead = promote_table_header_rows(with_math)
    return augment_lo_table_styles(with_thead)


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
        # PLACEHOLDER: do not embed source/figure PNGs back into Writer (EMBEDDED
        # dumps data:image payloads that StarWriter inserts as extra graphics).
        # formula_to_mathml=True (Docling default): FormulaItem LaTeX → <math>
        # so Writer can create native Math objects. Leftover $$…$$ is converted
        # later in prepare_html_for_lo_import.
        raw = str(
            document.export_to_html(
                image_mode=image_ref_mode.PLACEHOLDER,
                formula_to_mathml=True,
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


def _html_table_from_columns_rows(
    columns: list[Any],
    rows: list[list[Any]],
    spans: list[dict[str, Any]] | None = None,
) -> str:
    if not columns and not rows:
        return ""
    grid = [list(columns)] if columns else []
    for row in rows:
        if isinstance(row, list):
            grid.append(list(row))
    if not grid:
        return ""
    covered: set[tuple[int, int]] = set()
    span_at: dict[tuple[int, int], tuple[int, int]] = {}
    for span in spans or []:
        if not isinstance(span, dict):
            continue
        origin_row = int(span.get("row") or 0)
        origin_col = int(span.get("col") or 0)
        rowspan = max(int(span.get("rowspan") or 1), 1)
        colspan = max(int(span.get("colspan") or 1), 1)
        span_at[(origin_row, origin_col)] = (rowspan, colspan)
        for rr in range(origin_row, origin_row + rowspan):
            for cc in range(origin_col, origin_col + colspan):
                if (rr, cc) != (origin_row, origin_col):
                    covered.add((rr, cc))

    lines = ['<table border="1">']
    header_is_th = bool(columns)
    body_opened = False
    for r_idx, row in enumerate(grid):
        tag = "th" if r_idx == 0 and header_is_th else "td"
        if r_idx == 0 and header_is_th:
            lines.append("<thead>")
        elif not body_opened:
            lines.append("<tbody>")
            body_opened = True
        lines.append("<tr>")
        for c_idx, cell in enumerate(row):
            if (r_idx, c_idx) in covered:
                continue
            rowspan, colspan = span_at.get((r_idx, c_idx), (1, 1))
            attrs = ""
            if rowspan > 1:
                attrs += f' rowspan="{rowspan}"'
            if colspan > 1:
                attrs += f' colspan="{colspan}"'
            lines.append(f"<{tag}{attrs}>{html_module.escape(str(cell))}</{tag}>")
        lines.append("</tr>")
        if r_idx == 0 and header_is_th:
            lines.append("</thead>")
    if body_opened:
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
            list(table.get("spans") or []) if isinstance(table.get("spans"), list) else None,
        )
        if table_html:
            parts.append(table_html)

    if not parts:
        return ""
    return prepare_html_for_lo_import(_wrap_paddle_fragment("\n".join(parts)))


def _blocks_from_vision_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = result.get("blocks")
    if isinstance(blocks, list) and blocks:
        return [block for block in blocks if isinstance(block, dict)]
    regions = result.get("regions")
    if not isinstance(regions, list):
        return []
    out: list[dict[str, Any]] = []
    for region in regions:
        if not isinstance(region, dict):
            continue
        text = str(region.get("text") or "").strip()
        if not text:
            continue
        out.append({"type": "text", "text": text, "box": region.get("box") or [0, 0, 0, 0]})
    return out


def structured_html_from_vision_result(result: dict[str, Any]) -> str:
    """Build bbox layout HTML from blocks/regions; must run in the user venv (needs css-inline)."""
    from plugin.vision.venv.vision_layout_html import html_from_layout_blocks

    body = html_from_layout_blocks(_blocks_from_vision_result(result), {})
    tables = result.get("tables")
    if isinstance(tables, list):
        for table in tables:
            if not isinstance(table, dict):
                continue
            table_html = _html_table_from_columns_rows(
                list(table.get("columns") or []),
                [list(row) for row in (table.get("rows") or []) if isinstance(row, list)],
                list(table.get("spans") or []) if isinstance(table.get("spans"), list) else None,
            )
            if table_html:
                body = f"{body}\n{table_html}" if body else table_html
    if not body.strip():
        return ""
    return prepare_html_for_lo_import(_wrap_paddle_fragment(body))


def apply_structured_insert_html(result: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """When insert_mode=structured, replace html in the worker before LO insert (css-inline lives in venv)."""
    from plugin.vision.vision_common import DEFAULT_VISION_INSERT_MODE

    if result.get("status") != "ok":
        return result
    helper = str(result.get("helper") or "")
    if helper not in ("extract_text", "extract_structure"):
        return result
    mode = str(params.get("insert_mode") or DEFAULT_VISION_INSERT_MODE).strip().lower()
    if mode != "structured":
        return result
    html = structured_html_from_vision_result(result)
    if not html.strip():
        return result
    updated = dict(result)
    updated["html"] = html
    return updated
