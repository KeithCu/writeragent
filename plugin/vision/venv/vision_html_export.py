# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Export Docling / Paddle vision OCR results to HTML for LO import."""

from __future__ import annotations

import functools
import html as html_module
import importlib
import logging
import os
import re
import sys
from typing import Any

from plugin.vision.venv.vision_layout_html import _HEADING_TYPES
from plugin.vision.vision_common import DEFAULT_VISION_INSERT_MODE

log = logging.getLogger(__name__)

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

# Minimal stylesheet for vision-built fragments before css-inline hoists rules.
# Cell borders use HTML + inline CSS so StarWriter draws grids even when a
# <style> block is dropped. Header fill matches the post-inline th augment.
_HTML_FRAGMENT_STYLE = """<style>
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
_TABLE_BLOCK_RE = re.compile(r"(<table\b[^>]*>)(.*?)(</table>)", re.IGNORECASE | re.DOTALL)
_TR_BLOCK_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.IGNORECASE | re.DOTALL)
_MATH_BLOCK_RE = re.compile(r"<math\b[^>]*>.*?</math>", re.IGNORECASE | re.DOTALL)
_TABLE_PREFIX_RE = re.compile(
    r"^(?:\s*(?:<caption>.*?</caption>|<colgroup>.*?</colgroup>|<col\b[^>]*\/?>))+",
    re.IGNORECASE | re.DOTALL,
)


def _style_has_border_none(attrs: str) -> bool:
    """True for layout-only tables (two-column bbox HTML uses border:none)."""
    match = re.search(r"""(?i)\bstyle\s*=\s*(['"])(.*?)\1""", attrs)
    return bool(match and re.search(r"(?:^|;)\s*border\s*:\s*none\b", match.group(2), re.IGNORECASE))


def _merge_style_decls(tag: str, attrs: str, extra: str) -> str:
    """Merge extra CSS declarations into a tag's existing style attribute (or add one)."""
    if not extra:
        return f"<{tag}{attrs}>"
    match = re.search(r"""(?i)\bstyle\s*=\s*(['"])(.*?)\1""", attrs)
    existing_style = match.group(2) if match else ""
    existing_props = {
        part.split(":", 1)[0].strip().lower()
        for part in existing_style.split(";")
        if ":" in part
    }
    to_add = [
        part.strip()
        for part in extra.split(";")
        if ":" in part and part.split(":", 1)[0].strip().lower() not in existing_props
    ]
    if not to_add:
        return f"<{tag}{attrs}>"

    extra_str = "; ".join(to_add) + ";"
    if match:
        quote = match.group(1)
        merged = f"{existing_style.rstrip('; ')}; {extra_str}" if existing_style.strip() else extra_str
        start, end = match.span()
        new_attrs = f"{attrs[:start]}style={quote}{merged}{quote}{attrs[end:]}"
        return f"<{tag}{new_attrs}>"
    return f'<{tag} style="{extra_str}"{attrs}>'


def augment_lo_heading_styles(html: str) -> str:
    """Merge bold/font-size into heading tags — Docling CSS leaves h2 looking like body text."""

    def _repl(match: re.Match[str]) -> str:
        tag = match.group(1).lower()
        attrs = match.group(2) or ""
        extra = _LO_HEADING_INLINE.get(tag, "")
        if not extra:
            return match.group(0)
        return _merge_style_decls(tag, attrs, extra)

    return _HEADING_TAG_RE.sub(_repl, html)


def augment_lo_body_paragraph_styles(html: str) -> str:
    """Add Arial/line-height on bare <p> tags — Docling body lines have no inline styles."""

    def _repl(match: re.Match[str]) -> str:
        attrs = match.group(1) or ""
        return f'<p style="{_LO_BODY_PARAGRAPH_INLINE}"{attrs}>'

    return _PLAIN_P_TAG_RE.sub(_repl, html)


def _extension_root() -> str:
    """Return the extension root directory containing vendor/ and plugin/lib/."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


@functools.cache
def _import_latex2mathml_convert() -> Any | None:
    """latex2mathml is vendored; Docling also depends on it. Missing → leave TeX as-is."""
    try:
        from latex2mathml.converter import convert as latex_to_mathml

        return latex_to_mathml
    except ImportError:
        pass

    root = _extension_root()
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


def _convert_tex_in_text(text: str, convert_fn: Any) -> str:
    """Scan and convert TeX math within a single text run."""
    if not ("$" in text or r"\(" in text or r"\[" in text):
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if text.startswith("$$", i):
            close_at = text.find("$$", i + 2)
            if close_at != -1:
                inner = text[i + 2 : close_at]
                mathml = _latex_to_math_element(html_module.unescape(inner), display_block=True, convert_fn=convert_fn)
                if mathml is not None:
                    out.append(mathml)
                    i = close_at + 2
                    continue
            # A failed $$ convert emits and skips both dollar signs so the
            # second '$' cannot pair later as inline math.
            out.append("$$")
            i += 2
            continue
        elif text.startswith("\\[", i):
            close_at = text.find("\\]", i + 2)
            if close_at != -1:
                inner = text[i + 2 : close_at]
                mathml = _latex_to_math_element(html_module.unescape(inner), display_block=True, convert_fn=convert_fn)
                if mathml is not None:
                    out.append(mathml)
                    i = close_at + 2
                    continue
            out.append("\\[")
            i += 2
            continue
        elif text.startswith("\\(", i):
            close_at = text.find("\\)", i + 2)
            if close_at != -1:
                inner = text[i + 2 : close_at]
                mathml = _latex_to_math_element(html_module.unescape(inner), display_block=False, convert_fn=convert_fn)
                if mathml is not None:
                    out.append(mathml)
                    i = close_at + 2
                    continue
            out.append("\\(")
            i += 2
            continue
        elif ch == "$" and not _looks_like_currency_dollar(text, i):
            close_at = text.find("$", i + 1)
            if close_at != -1 and close_at > i + 1:
                inner = text[i + 1 : close_at]
                unescaped = html_module.unescape(inner)
                # Require a TeX cue so prose like "cost $foo later $bar" stays text.
                if "\\" in unescaped or "^" in unescaped or re.search(r"_[{\\A-Za-z]", unescaped):
                    mathml = _latex_to_math_element(unescaped, display_block=False, convert_fn=convert_fn)
                    if mathml is not None:
                        out.append(mathml)
                        i = close_at + 1
                        continue
        out.append(ch)
        i += 1
    return "".join(out)


def _replace_tex_outside_tags(s: str, convert_fn: Any) -> str:
    """Turn ``$$…$$`` / ``\\[…\\]`` / ``\\(…\\)`` (and conservative ``$…$``) into ``<math>``."""
    # Fast path: skip processing if no TeX delimiters are present.
    if not ("$" in s or r"\(" in s or r"\[" in s):
        return s

    # Match delimiters inside text runs only. Matching across tags swallows
    # markup, and TeX entities must be unescaped before MathML conversion.
    # Skip runs inside <code> and <pre>.
    tokens = re.split(r"(<[^>]*>)", s)
    out: list[str] = []
    in_code = 0
    for token in tokens:
        if not token:
            continue
        if token.startswith("<") and token.endswith(">"):
            out.append(token)
            tag_match = re.match(r"^<\s*(/)?\s*(code|pre)\b", token, re.IGNORECASE)
            if tag_match and not token.endswith("/>"):
                if tag_match.group(1):
                    in_code = max(0, in_code - 1)
                else:
                    in_code += 1
            continue

        if in_code > 0:
            out.append(token)
        else:
            out.append(_convert_tex_in_text(token, convert_fn))

    return "".join(out)


def convert_latex_delimiters_to_mathml(html: str) -> str:
    """Replace leftover Docling TeX delimiters with MathML for native LO Math objects.

    Docling's HTML serializer already sets ``formula_to_mathml=True``, but formula
    items that fail conversion (or TeX that landed in a ``<p>``) still use
    ``$$…$$``. Host ``insert_html_at_cursor`` maps ``<math display="block">`` to
    editable LibreOffice Math embeds. Leave original text if latex2mathml is
    missing or a fragment does not convert.
    """
    if not ("$" in html or r"\(" in html or r"\[" in html):
        return html
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
    """Move the first ``<th>`` row into ``<thead>`` so Writer can repeat header rows.

    Retains any <caption> and <colgroup> elements before <thead>.
    """
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

    # Keep <caption> and <colgroup> before <thead>. Putting them inside
    # <tbody>, or rewriting tables that have <tfoot> or an orphan <tbody>,
    # nests bodies. Rewrite only when the remaining prefix and suffix are
    # a single optional <tbody></tbody> pair.
    prefix_match = _TABLE_PREFIX_RE.match(before)
    if prefix_match:
        table_prefix = prefix_match.group(0)
        remaining_before = before[prefix_match.end() :].strip()
    else:
        table_prefix = ""
        remaining_before = before.strip()

    after_stripped = after.strip()

    # Reject if before has anything other than optional <tbody ...>
    tbody_open = bool(re.match(r"(?is)^<tbody\b[^>]*>$", remaining_before))
    if remaining_before and not tbody_open:
        return inner

    # Reject if after contains nested structural tags like <tfoot>, <thead>, or another <tbody>
    tbody_close = bool(re.search(r"(?is)</tbody\s*>$", after_stripped))
    if tbody_open != tbody_close:
        return inner

    if tbody_close:
        body_rows = re.sub(r"(?is)</tbody\s*>$", "", after_stripped).strip()
    else:
        body_rows = after_stripped

    # If body_rows contains <tfoot>, <thead>, or <tbody>, do not rewrite
    if re.search(r"(?is)<(?:tbody|tfoot|thead)\b", body_rows):
        return inner

    thead = f"<thead>{first}</thead>"
    if body_rows:
        return f"{table_prefix}{thead}<tbody>{body_rows}</tbody>"
    return f"{table_prefix}{thead}"


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
        return _merge_style_decls("table", attrs, _LO_TABLE_INLINE)

    def _cell_repl(match: re.Match[str]) -> str:
        tag = match.group(1).lower()
        attrs = match.group(2) or ""
        if _style_has_border_none(attrs):
            return match.group(0)
        extra = _LO_CELL_BORDER_INLINE
        if tag == "th":
            extra = f"{extra} {_LO_TH_INLINE}"
        return _merge_style_decls(tag, attrs, extra)

    with_tables = _TABLE_OPEN_RE.sub(_table_repl, html)
    return _TD_TH_OPEN_RE.sub(_cell_repl, with_tables)


def prepare_html_for_lo_import(html: str) -> str:
    """Inline CSS so LibreOffice HTML (StarWriter) import keeps typography."""
    try:
        import css_inline
    except ImportError as exc:
        raise ImportError(
            f"No module named 'css_inline' in {sys.executable}. "
            f"Install in your Settings → Python venv: {sys.executable} -m pip install css-inline"
        ) from exc

    stripped = (html or "").strip()
    if not stripped:
        return html or ""
    # css_inline.inline() loads remote stylesheets by default. Pass
    # CSSInliner(load_remote_stylesheets=False) so export does not fetch them.
    inliner = css_inline.CSSInliner(load_remote_stylesheets=False)
    inlined = inliner.inline(stripped)
    with_headings = augment_lo_heading_styles(inlined)
    with_body = augment_lo_body_paragraph_styles(with_headings)
    with_math = convert_latex_delimiters_to_mathml(with_body)
    with_thead = promote_table_header_rows(with_math)
    return augment_lo_table_styles(with_thead)


def _wrap_html_fragment(body: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset=\"UTF-8\">"
        f"{_HTML_FRAGMENT_STYLE}</head><body>{body}</body></html>"
    )


def export_docling_to_html(document: Any, params: dict[str, Any]) -> str:
    """Return rich HTML from a DoclingDocument (bold, tables, headings), css-inlined for LO."""
    del params  # reserved for future Docling HTML export options

    if hasattr(document, "export_to_html"):
        docling_doc_mod = importlib.import_module("docling_core.types.doc")
        image_ref_mode = docling_doc_mod.ImageRefMode
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
    log.warning("Docling document %r lacks export_to_html; returning empty HTML", type(document).__name__)
    return ""


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


def html_from_paddle_regions(regions: list[dict[str, Any]]) -> str:
    """Minimal HTML from Paddle OCR line regions (reading order), css-inlined for LO."""
    blocks = _blocks_from_vision_result({"regions": regions})
    parts = [f"<p>{html_module.escape(b['text'])}</p>" for b in blocks if b.get("text")]
    if not parts:
        return ""
    return prepare_html_for_lo_import(_wrap_html_fragment("\n".join(parts)))


def _is_table_markup(text: str) -> bool:
    """True when block text is a raw ``<table>`` fragment, not prose."""
    return "<table" in text.lower()


def _is_heading(block_type: str) -> bool:
    return block_type.strip().lower() in _HEADING_TYPES


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


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

    num_rows = len(grid)
    num_cols = max((len(r) for r in grid), default=0)
    if num_rows == 0 or num_cols == 0:
        return ""

    # Parse rowspan/colspan with a safe int and clamp to the grid. Raw values
    # raise TypeError/ValueError, and an unbounded span loops the grid.
    covered: set[tuple[int, int]] = set()
    span_at: dict[tuple[int, int], tuple[int, int]] = {}
    for span in spans or []:
        if not isinstance(span, dict):
            continue
        origin_row = _safe_int(span.get("row"), 0)
        origin_col = _safe_int(span.get("col"), 0)
        if origin_row < 0 or origin_row >= num_rows or origin_col < 0 or origin_col >= num_cols:
            continue
        raw_rowspan = _safe_int(span.get("rowspan"), 1)
        raw_colspan = _safe_int(span.get("colspan"), 1)
        rowspan = max(1, min(raw_rowspan, num_rows - origin_row))
        colspan = max(1, min(raw_colspan, num_cols - origin_col))
        if (origin_row, origin_col) in covered:
            continue
        if rowspan > 1 or colspan > 1:
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


def _tables_to_html(tables: list[dict[str, Any]] | None) -> list[str]:
    """Convert parsed tables into HTML table elements."""
    if not isinstance(tables, list):
        return []
    out: list[str] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        table_html = _html_table_from_columns_rows(
            list(table.get("columns") or []),
            [list(row) for row in (table.get("rows") or []) if isinstance(row, list)],
            list(table.get("spans") or []) if isinstance(table.get("spans"), list) else None,
        )
        if table_html:
            out.append(table_html)
    return out


def html_from_paddle_structure(
    blocks: list[dict[str, Any]],
    tables: list[dict[str, Any]],
) -> str:
    """Build HTML from Paddle PP-Structure blocks and parsed tables, css-inlined for LO."""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "text").strip().lower()
        text = str(block.get("text") or "").strip()
        # Tables are appended from ``tables`` below. The old guard only
        # skipped an empty ``text``, but PP-Structure stores the raw
        # ``<table>`` HTML on the block (``_text_from_structure_res`` read
        # the ``html`` key). That markup was escaped into a second ``<p>``
        # (``&lt;table&gt;``) beside the real table. Skip table markup even
        # when ``text`` is non-empty so insert_mode=html shows one table.
        if block_type == "table" and (not text or _is_table_markup(text)):
            continue
        if not text:
            continue
        if _is_heading(block_type):
            parts.append(f"<h2>{html_module.escape(text)}</h2>")
        else:
            parts.append(f"<p>{html_module.escape(text)}</p>")

    parts.extend(_tables_to_html(tables))

    if not parts:
        return ""
    return prepare_html_for_lo_import(_wrap_html_fragment("\n".join(parts)))


def structured_html_from_vision_result(result: dict[str, Any]) -> str:
    """Build bbox layout HTML from blocks/regions; must run in the user venv (needs css-inline)."""
    from plugin.vision.venv.vision_layout_html import html_from_layout_blocks

    body = html_from_layout_blocks(_blocks_from_vision_result(result))
    table_parts = _tables_to_html(result.get("tables"))
    if table_parts:
        tables_str = "\n".join(table_parts)
        body = f"{body}\n{tables_str}" if body else tables_str
    if not body.strip():
        return ""
    return prepare_html_for_lo_import(_wrap_html_fragment(body))


def apply_structured_insert_html(result: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """When insert_mode=structured, replace html in the worker before LO insert (css-inline lives in venv)."""
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
    # Calc structured insert falls back to HTML when the cell grid is empty.
    # Overwriting html used to make that fallback the bbox fragment, not Docling.
    if "html_docling" not in updated:
        updated["html_docling"] = result.get("html")
    updated["html"] = html
    return updated
