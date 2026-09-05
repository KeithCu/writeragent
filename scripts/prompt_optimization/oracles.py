# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Result oracles for prompt_optimization eval.

Keith grades the **exported final document** (Writer HTML, Draw tree JSON,
Calc snapshot/grid), not which tools the student called. A wrong final doc
fails; a right one passes. Creative tasks still get light term checks here;
tone/quality stays with the LLM judge when one is configured.
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# 999 + 135.15 + 43 + 66 + 215.31
_TABLE_FROM_MESS_TOTAL = 1458.46
# Price×Qty with missing Qty = 0: 14.4+12+0+16+9+0
_TABLE_ENGINEERING_EXT_TOTAL = 51.40

_TAX_BY_ITEM = {
    "Apple": (10.0, 0.8),
    "Banana": (5.0, 0.4),
    "Orange": (8.0, 0.64),
    "Pear": (12.5, 1.0),
}

_BULLET_ITEMS = (
    "Pack the crate",
    "Ship to Oslo",
    "Call the depot",
    "Label the pallet",
    "Sweep the bay",
    "File the docket",
    "Seal the hatch",
)

_HYPE = ("incredibly", "significant leap", "brand new")

_MONEY_RE = re.compile(
    r"\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+)"
)
_HN_RE = re.compile(r"<h([1-6])\b([^>]*)>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
_P_BLOCK_RE = re.compile(r"<p\b([^>]*)>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_STYLE_ATTR_RE = re.compile(r'(?:data-lo-style|class)\s*=\s*["\']([^"\']+)["\']', re.I)
_MD_H_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_TABLE_RE = re.compile(r"<table\b", re.IGNORECASE)


def visible_text(doc: str) -> str:
    """Tag-stripped text. Used so LO XHTML indent is not scored as content."""
    return text_without_tags(doc)


def text_without_tags(doc: str) -> str:
    """Delete tags (no substitution) so ``</p><p>`` does not become a double space."""
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", "", doc or "")
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", "", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    return html.unescape(text).replace("\xa0", " ")


# NBSP / NNBSP / thin spaces. Models often write ``100 K`` / ``45 ms``.
_UNICODE_SPACE_RE = re.compile(r"[\xa0\u202f\u2007\u2008\u2009\u200a]")


def fold_eval_text(text: str) -> str:
    """Fold unicode spaces/hyphens so ``G‑Eval`` / ``100 K`` match ASCII needles."""
    s = _UNICODE_SPACE_RE.sub(" ", text or "")
    for dash in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2212"):
        s = s.replace(dash, "-")
    s = re.sub(r"(\d)\s+([KMkm])\b", r"\1\2", s)
    # No trailing \\b: tag-stripped HTML can glue ``45 ms`` to the next token.
    s = re.sub(r"(\d)\s+ms", r"\1ms", s, flags=re.I)
    return s


def haystack_has(doc: str, token: str) -> bool:
    """True if *token* is in *doc*, after unicode-space fold (case-insensitive)."""
    if not token:
        return True
    raw = doc or ""
    if token in raw:
        return True
    folded_tok = fold_eval_text(token)
    folded_doc = fold_eval_text(raw)
    if folded_tok in folded_doc:
        return True
    return folded_tok.casefold() in folded_doc.casefold()


def _norm_ws(text: str) -> str:
    return " ".join((text or "").split())


def _inner_text(fragment: str) -> str:
    return _norm_ws(visible_text(fragment))


def parse_json_export(doc: str) -> dict[str, Any] | None:
    raw = (doc or "").strip()
    if not raw.startswith("{"):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _level_from_style_attrs(attrs: str) -> int | None:
    from eval_worlds import heading_level_from_style

    for m in _STYLE_ATTR_RE.finditer(attrs or ""):
        for part in re.split(r"[\s,]+", m.group(1) or ""):
            level = heading_level_from_style(part.replace("Heading", "Heading "))
            if level is None:
                compact = re.sub(r"[\s_]+", "", part)
                hm = re.match(r"(?i)heading([1-6])$", compact)
                if hm:
                    level = int(hm.group(1))
            if level:
                return level
    return None


def heading_texts(doc: str) -> list[tuple[int, str]]:
    """``(level, text)`` from ``<hN>``, ``data-lo-style=HeadingN``, and markdown."""
    found: list[tuple[int, int, str]] = []
    for m in _HN_RE.finditer(doc or ""):
        found.append((m.start(), int(m.group(1)), _inner_text(m.group(3))))
    for m in _P_BLOCK_RE.finditer(doc or ""):
        level = _level_from_style_attrs(m.group(1) or "")
        if level:
            found.append((m.start(), level, _inner_text(m.group(2))))
    for m in _MD_H_RE.finditer(doc or ""):
        found.append((m.start(), len(m.group(1)), _norm_ws(m.group(2))))
    found.sort(key=lambda item: item[0])
    # LO compact export repeats the same headings in unwrap variants; keep first
    # occurrence of each (level, text) so order checks still see document order.
    out: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for _pos, level, text in found:
        key = (level, text.casefold())
        if not text or key in seen:
            continue
        seen.add(key)
        out.append((level, text))
    return out


def h1_texts(doc: str) -> list[str]:
    return [text for level, text in heading_texts(doc) if level == 1]


def parse_money(text: str) -> list[float]:
    values: list[float] = []
    for m in _MONEY_RE.finditer(text or ""):
        values.append(float(m.group(1).replace(",", "")))
    return values


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip().replace(",", "").replace("$", "")
        if not raw or raw in {"?", "—", "-"}:
            return None
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _collect_texts(node: Any, out: list[str]) -> None:
    if isinstance(node, dict):
        for key, val in node.items():
            if key == "text" and isinstance(val, str) and val.strip():
                out.append(val)
            else:
                _collect_texts(val, out)
    elif isinstance(node, list):
        for item in node:
            _collect_texts(item, out)


def _calc_grid(doc: str) -> list[list[Any]] | None:
    data = parse_json_export(doc)
    if not data:
        return None
    grid = data.get("grid") or data.get("rows")
    if isinstance(grid, list) and grid and isinstance(grid[0], list):
        return grid
    return None


def _near(actual: float, expected: float, tol: float = 0.02) -> bool:
    return abs(actual - expected) <= tol


def oracle_table_from_mess(doc: str) -> list[str]:
    fails: list[str] = []
    if not _TABLE_RE.search(doc or ""):
        fails.append("no HTML table")
    text = visible_text(doc)
    for token in ("Battle Born", "Victron", "SmartSolar", "NEMA 4"):
        if token not in text:
            fails.append(f"missing {token!r}")
    if not _has_total_label(doc, text):
        fails.append("no Total row")
    amounts = parse_money(text)
    if not any(_near(v, _TABLE_FROM_MESS_TOTAL) for v in amounts):
        fails.append(f"Total is not {_TABLE_FROM_MESS_TOTAL}")
    return fails


def _has_total_label(doc: str, text: str) -> bool:
    """True when a Total row exists. Tag-stripped ``1.75Total7.75`` has no ``\\b``."""
    if re.search(r"(?i)(?<![A-Za-z])Total(?![A-Za-z])", text or ""):
        return True
    return bool(re.search(r"(?i)>Total<", doc or ""))


def _html_table_data_rows(doc: str) -> list[list[str]]:
    """Visible text of ``<td>`` cells per row. Attributes on ``<td>`` are ignored."""
    rows: list[list[str]] = []
    for tr in re.finditer(r"(?is)<tr\b[^>]*>(.*?)</tr>", doc or ""):
        cells = [
            _inner_text(m.group(1))
            for m in re.finditer(r"(?is)<td\b[^>]*>(.*?)</td>", tr.group(1))
        ]
        if cells:
            rows.append(cells)
    return rows


def oracle_table_engineering(doc: str) -> list[str]:
    fails: list[str] = []
    if not _TABLE_RE.search(doc or ""):
        fails.append("no HTML table")
    text = visible_text(doc)
    for token in ("Item", "Price", "Quantity", "Kiwi"):
        if token not in text:
            fails.append(f"missing {token!r}")
    if not _has_total_label(doc, text):
        fails.append("no Total row")
    amounts = parse_money(text)
    if not any(_near(v, _TABLE_ENGINEERING_EXT_TOTAL) for v in amounts):
        fails.append(f"extended Total is not {_TABLE_ENGINEERING_EXT_TOTAL}")
    # Orange had no qty — inventing Banana's 24 is the easy cheat.
    if re.search(r"Orange[^<]{0,40}24", text) or re.search(
        r"Orange</td><td[^>]*>0\.80</td><td[^>]*>24", doc or "", re.I
    ):
        fails.append("Orange quantity was invented")
    vis = " ".join(text.split())
    # Bare ``<td>`` used to miss align="right" golds and fail honest students.
    qty_by_item = {
        (row[0] or "").casefold(): _as_float(row[2]) if len(row) >= 3 else None
        for row in _html_table_data_rows(doc)
    }
    orange_qty = qty_by_item.get("orange")
    kiwi_qty = qty_by_item.get("kiwi")
    if orange_qty is None or not _near(orange_qty, 0.0, 0.01):
        fails.append("Orange quantity is not 0")
    if kiwi_qty is None or not _near(kiwi_qty, 0.0, 0.01):
        fails.append("Kiwi quantity is not 0")
    if re.search(r"(?i)\[note\]", vis):
        fails.append("[note] was kept as a quantity")
    return fails


def oracle_bulk_cleanup(doc: str) -> list[str]:
    fails: list[str] = []
    text = visible_text(doc)
    cmd_re = re.compile(r"CMD:\s*git\s+log\s+--oneline")
    cmd_exact = "CMD: git  log  --oneline"
    if cmd_exact not in text:
        fails.append("CMD line not preserved exactly")
    body = cmd_re.sub(" ", text)
    for token in (
        "This sentence has extra spaces",
        "https://example.com/test",
        "Quoted text",
    ):
        if token not in body:
            fails.append(f"missing {token!r}")
    if "  " in body:
        fails.append("visible double space")
    if " ." in body or " ," in body or ".." in body:
        fails.append("punctuation artifact in visible text")
    return fails


def oracle_format_preservation(doc: str) -> list[str]:
    fails: list[str] = []
    text = visible_text(doc)
    if "Jane Smith - Project Lead" not in text:
        fails.append("title line is not Jane Smith")
    if "Contact person: John Doe (legacy ID JD-001)" not in text:
        fails.append("legal John Doe line was changed")
    if "John Doe - Project Lead" in text:
        fails.append("first-line John Doe was not replaced")
    if "Jane Smith (legacy ID JD-001)" in text:
        fails.append("legal line was rewritten to Jane Smith")
    return fails


def oracle_style_application(doc: str) -> list[str]:
    fails: list[str] = []
    h1 = h1_texts(doc)
    if not any(t == "Introduction" for t in h1):
        fails.append("Introduction is not Heading 1")
    if any(t == "Background" for t in h1):
        fails.append("Background was promoted to Heading 1")
    if any(t == "Summary" for t in h1):
        fails.append("Summary was promoted to Heading 1")
    text = visible_text(doc)
    if "Background" not in text or "Summary" not in text:
        fails.append("Background/Summary body text missing")
    return fails


def oracle_bullet_consistency(doc: str) -> list[str]:
    fails: list[str] = []
    text = visible_text(doc)
    blob = f"{doc or ''}\n{text}"

    def _has_item(item: str) -> bool:
        needle = f"- {item}."
        if needle in text or needle in (doc or ""):
            return True
        if re.search(
            rf"<li\b[^>]*>.*?{re.escape(item)}\.?",
            doc or "",
            re.I | re.DOTALL,
        ):
            return True
        return bool(
            re.search(
                rf"[•*]\s*{re.escape(item)}\.?",
                text,
            )
        )

    for item in _BULLET_ITEMS:
        if not _has_item(item):
            fails.append(f"missing bullet {item!r}")
    if ".." in text:
        fails.append("double period on a bullet")
    if "do not bullet this line" not in text.casefold():
        fails.append("Note paragraph missing")
    if re.search(r"[-•*]\s*Note:", blob) or re.search(
        r"<li\b[^>]*>\s*Note:", blob, re.I
    ):
        fails.append("Note paragraph was turned into a bullet")
    return fails


def oracle_style_consistency(doc: str) -> list[str]:
    fails: list[str] = []
    raw = doc or ""
    text = visible_text(raw)
    if not re.search(r'(?:class|data-lo-style)\s*=\s*["\'][^"\']*Quotations', raw, re.I):
        fails.append("Quotations is not a style/class on Default paragraphs")
    h1 = h1_texts(raw)
    h1_fold = " | ".join(t.casefold() for t in h1)
    if "heading 2 text that should be upgraded" not in h1_fold:
        fails.append("HEADING 2 line was not upgraded to Heading 1")
    if "heading 2 again" not in h1_fold:
        fails.append("'Heading 2 again' was not upgraded to Heading 1")
    if any("default style paragraph" in t.casefold() for t in h1):
        fails.append("Default paragraph was promoted to Heading 1")
    if "Default style paragraph one" not in text:
        fails.append("default paragraph content was lost")
    return fails


def oracle_section_refactor(doc: str) -> list[str]:
    fails: list[str] = []
    heads = [text for _level, text in heading_texts(doc)]
    folded = [h.casefold() for h in heads]
    if "conclusion" in folded:
        fails.append("Conclusion heading was not renamed")
    try:
        intro = folded.index("introduction")
        goal = folded.index("goal")
        body = folded.index("body")
    except ValueError:
        fails.append("headings must include Introduction, Goal, and Body")
        return fails
    if not (intro < goal < body):
        fails.append("expected heading order Introduction, Goal, Body")
    vis = visible_text(doc)
    if "See the Goal" not in vis and "see the Goal" not in vis:
        fails.append("cross-reference was not updated to Goal")
    return fails


def oracle_comment_management(doc: str) -> list[str]:
    fails: list[str] = []
    text = visible_text(doc)
    blob = f"{doc or ''}\n{text}"
    if "uncertain" not in blob:
        fails.append("missing 'uncertain'")
    if "Review this before finalizing" not in blob:
        fails.append("missing review comment text")
    anchored = bool(re.search(r"uncertain\s+\[Review this before finalizing\]", blob, re.I))
    # LO export uses an annotation span, not the string-harness ``[comment]``.
    lo_note = "annotation" in blob.casefold() and "Review this before finalizing" in blob
    if not anchored and not lo_note:
        fails.append("comment is not on 'uncertain'")
    return fails


def oracle_flowchart_gen(doc: str) -> list[str]:
    fails: list[str] = []
    texts: list[str] = []
    data = parse_json_export(doc)
    if data:
        _collect_texts(data, texts)
    blob = " ".join(texts) if texts else visible_text(doc)
    # Task asks for a Process *box for user login* and a Decision *'credentials valid?'*.
    # Do not require the jargon words Process/Decision in the shape text.
    for token in ("Start", "End", "login", "credentials"):
        if token.casefold() not in fold_eval_text(blob).casefold():
            fails.append(f"flowchart missing {token!r}")
    type_blob = json.dumps(data).casefold() if data else (doc or "").casefold()
    has_start_type = any(t in type_blob for t in ("ellipse", "oval", "terminator"))
    has_process_type = any(t in type_blob for t in ("process", "rectangle"))
    has_decision_type = any(t in type_blob for t in ("decision", "diamond"))
    if not has_start_type:
        fails.append("flowchart missing Start shape type")
    if not has_process_type:
        fails.append("flowchart missing Process shape type")
    if not has_decision_type:
        fails.append("flowchart missing Decision shape type")

    nodes: list[dict[str, Any]] = []
    if data:
        tree = data.get("tree")
        if isinstance(tree, list):
            nodes = [n for n in tree if isinstance(n, dict)]
    by_idx_text: dict[int, str] = {}
    for i, node in enumerate(nodes):
        by_idx_text[i] = str(node.get("text") or "").casefold()

    edges: list[tuple[str, str]] = []
    if data:
        conns = data.get("connections")
        if isinstance(conns, list):
            for conn in conns:
                if not isinstance(conn, dict):
                    continue
                try:
                    frm = int(conn.get("from_index"))
                    to = int(conn.get("to_index"))
                except (TypeError, ValueError):
                    continue
                edges.append((by_idx_text.get(frm, ""), by_idx_text.get(to, "")))
        if not edges:
            tree = data.get("tree")
            if isinstance(tree, list):
                for node in tree:
                    if not isinstance(node, dict):
                        continue
                    src = str(node.get("text") or "").casefold()
                    dest = node.get("connected_end")
                    if isinstance(dest, dict):
                        edges.append((src, str(dest.get("text") or "").casefold()))

    def _has_edge(src_key: str, dst_key: str) -> bool:
        for src, dst in edges:
            if src_key in src and dst_key in dst:
                return True
        return False

    if not _has_edge("start", "process") and not _has_edge("start", "login"):
        fails.append("missing Start→Process edge")
    if not _has_edge("process", "decision") and not _has_edge("login", "credential"):
        fails.append("missing Process→Decision edge")
    if not _has_edge("decision", "end") and not _has_edge("credential", "end"):
        fails.append("missing Yes Decision→End edge")
    if not _has_edge("decision", "process") and not _has_edge("credential", "login"):
        fails.append("missing No Decision→Process loop")
    return fails


def oracle_data_sorting(doc: str) -> list[str]:
    grid = _calc_grid(doc)
    if not grid or len(grid) < 2:
        return ["no Calc grid/snapshot"]
    rows = grid[1:] if any(str(c).casefold() == "product" for c in grid[0]) else grid
    header = [str(c) for c in grid[0]]
    if not any(h.casefold() == "product" for h in header) or not any(
        h.casefold() == "revenue" for h in header
    ):
        return ["header row is not first"]
    names = [str(r[0]) if r else "" for r in rows]
    want = ["Tool", "Device", "Widget", "Gadget", "Aardvark"]
    got = [n for n in names if n in want]
    if got != want:
        return [f"sort order is not {want} (got {got})"]
    numeric: list[float] = []
    junk_after_numeric = False
    for row in rows:
        if len(row) < 2:
            continue
        num = _as_float(row[1])
        if num is None:
            junk_after_numeric = True
        elif junk_after_numeric:
            return ["non-numeric Revenue row is not last"]
        else:
            numeric.append(num)
    if numeric != sorted(numeric, reverse=True):
        return ["Revenue column is not sorted descending"]
    return []


def oracle_py_dest(doc: str) -> list[str]:
    """Document gate for =PY dest: formula text at a cell outside A1:H500."""
    data = parse_json_export(doc)
    if not data:
        return ["no Calc snapshot"]
    formulas = data.get("formulas")
    if not isinstance(formulas, dict) or not formulas:
        return ["no =PY formula recorded"]
    from process_oracles import py_dest_conflicts_data, py_formula_refs_data

    found_outside = False
    found_py = False
    inside: list[str] = []
    missing_range = True
    for addr, formula in formulas.items():
        text = str(formula)
        if not text.lstrip().upper().startswith("=PY"):
            continue
        found_py = True
        if py_formula_refs_data(text):
            missing_range = False
        if py_dest_conflicts_data(str(addr), text):
            inside.append(str(addr))
        else:
            found_outside = True
    if not found_py:
        return ["no =PY formula recorded"]
    if inside:
        return [f"=PY dest overlaps the data range ({', '.join(inside)})"]
    if not found_outside:
        return ["=PY dest overlaps the data range"]
    if missing_range:
        return ["=PY formula does not reference the data range"]
    return []


# Ranking after PR 610 writes commutated / percent 8% forms (=0.08*B2,
# =B2*8%). A literal "=B{n}*0.08" match false-failed those.
_TAX_RATE = 0.08
_TAX_NUM_RE = re.compile(r"^(?P<body>\d*[.,]?\d+)(?P<pct>%)?$")
_TAX_FORMULA_RE = re.compile(r"=[^=]+")


def _normalize_tax_formula(text: str) -> str:
    """Strip $, spaces, and newlines; treat LO ';' as ',' (decimal/args)."""
    return (
        (text or "")
        .replace(" ", "")
        .replace("\t", "")
        .replace("$", "")
        .replace(";", ",")
        .replace("\n", "")
        .replace("\r", "")
        .upper()
    )


def _parse_tax_factor(token: str) -> float | None:
    """Parse 0.08, 0,08, 8%, or 8/100. None for cell refs or other junk."""
    if not token:
        return None
    if "/" in token:
        left, right = token.split("/", 1)
        if "/" in right:
            return None
        num = _parse_tax_factor(left)
        den = _parse_tax_factor(right)
        if num is None or den is None or den == 0:
            return None
        return num / den
    match = _TAX_NUM_RE.fullmatch(token)
    if match is None:
        return None
    body = match.group("body").replace(",", ".")
    if body in {".", ""}:
        return None
    try:
        value = float(body)
    except ValueError:
        return None
    if match.group("pct"):
        return value / 100.0
    return value


def _tax_formula_candidates(text: str) -> list[str]:
    """Split fill-down blobs so each '=…' formula is checked on its own."""
    chunks: list[str] = []
    for part in re.split(r"[\n\r]+", text or ""):
        part = part.strip()
        if not part:
            continue
        found = _TAX_FORMULA_RE.findall(part)
        chunks.extend(found if found else [part])
    return chunks


def _tax_formula_one(text: str, row_1based: int) -> bool:
    """True when text is a simple product of this row's B cell and 0.08."""
    compact = _normalize_tax_formula(text)
    if not compact.startswith("="):
        return False
    expr = compact[1:].strip(",")
    # Functions / quoted =PY / ranges are not a relative Price*8% write.
    if not expr or any(ch in expr for ch in "()[]{}\"'\\:"):
        return False
    parts = expr.split("*")
    if len(parts) != 2:
        return False
    left, right = parts
    price = f"B{row_1based}"
    if left == price:
        factor = _parse_tax_factor(right)
    elif right == price:
        factor = _parse_tax_factor(left)
    else:
        return False
    return factor is not None and abs(factor - _TAX_RATE) < 1e-12


def _tax_formula_ok(text: str, row_1based: int) -> bool:
    """Accept a relative 8% of this row's Price (B), including equivalent forms.

    Must reference B{row} and multiply by 0.08 (0.08, 8%, 8/100; either
    order; $, spaces, decimal comma). Wrong row, wrong factor, other
    columns, and non-relative junk still fail.
    """
    return any(_tax_formula_one(chunk, row_1based) for chunk in _tax_formula_candidates(text))


def oracle_tax_column(doc: str) -> list[str]:
    grid = _calc_grid(doc)
    if not grid:
        return ["no Calc grid/snapshot"]
    header = [str(c) for c in grid[0]]
    if not any(h.casefold() == "tax" for h in header):
        return ["no Tax column"]
    tax_idx = next(i for i, h in enumerate(header) if h.casefold() == "tax")
    item_idx = 0
    price_idx = 1 if len(header) > 1 else 1
    data = parse_json_export(doc) or {}
    formulas = data.get("formulas") if isinstance(data.get("formulas"), dict) else {}
    for row_i, row in enumerate(grid):
        if row_i == 0 or not row:
            continue
        name = str(row[item_idx])
        tax_cell = row[tax_idx] if len(row) > tax_idx else ""
        addr = f"C{row_i + 1}"
        formula = str(formulas.get(addr) or "")
        if name in _TAX_BY_ITEM:
            price_expected, tax_expected = _TAX_BY_ITEM[name]
            price = _as_float(row[price_idx]) if len(row) > price_idx else None
            if price is None or not _near(price, price_expected, 0.05):
                return [f"{name} price is not {price_expected}"]
            formula_ok = _tax_formula_ok(formula, row_i + 1) or _tax_formula_ok(
                str(tax_cell), row_i + 1
            )
            tax_num = _as_float(tax_cell)
            value_ok = tax_num is not None and _near(tax_num, tax_expected, 0.02)
            if "0.99" in str(tax_cell) and not formula_ok:
                return [f"{name} leftover wrong Tax value"]
            if not formula_ok and not value_ok:
                return [f"{name} Tax is not a relative 8% formula"]
        elif name.casefold() in {"note", "total"}:
            if _as_float(tax_cell) not in (None, 0.0):
                return [f"{name} row must not be taxed"]
            if str(formula).lstrip().startswith("="):
                return [f"{name} row must not be taxed"]
    return []


def oracle_reformat_resume(doc: str) -> list[str]:
    """Sanity for scripted/hard checks. Tone stays with the LLM judge."""
    fails: list[str] = []
    text = visible_text(doc)
    blob = f"{doc or ''}\n{text}"
    for token in ("John Doe", "WORK HISTORY", "EDUCATION", "SKILLS", "Acme Corp", "TechStart"):
        if not haystack_has(blob, token):
            fails.append(f"missing {token!r}")
    if not haystack_has(blob, "100K") and not haystack_has(blob, "100,000"):
        fails.append("missing 100K users achievement")
    if not haystack_has(blob, "100M") and not haystack_has(blob, "100,000,000"):
        fails.append("missing 100M requests achievement")
    return fails


def oracle_logical_rewriting(doc: str) -> list[str]:
    fails: list[str] = []
    text = visible_text(doc)
    blob = f"{doc or ''}\n{text}"
    for token in ("WriterAgent", "2.0", "Dual-Mode", "G-Eval", "Prometheus"):
        if not haystack_has(blob, token):
            fails.append(f"missing {token!r}")
    if "LocalWriter" in blob:
        fails.append("rewrote WriterAgent as LocalWriter")
    lower = blob.casefold()
    for word in _HYPE:
        if word.casefold() in lower:
            fails.append(f"hype leftover {word!r}")
    words = [w for w in text.split() if w]
    if len(words) > 70:
        fails.append(f"rewrite exceeds 70 words ({len(words)})")
    return fails


def oracle_smart_summarization(doc: str) -> list[str]:
    fails: list[str] = []
    text = visible_text(doc)
    blob = f"{doc or ''}\n{text}"
    if "Findings" not in blob and "Finding" not in blob:
        fails.append("Findings section missing")
    idx = text.casefold().find("executive summary")
    if idx < 0:
        fails.append("Executive Summary heading missing")
        return fails
    summary = text[idx:]
    # LO eval can echo the source Findings after the summary; stop at a later Findings.
    later = summary.casefold().find("findings", 1)
    if later > 20:
        summary = summary[:later]
    for token in ("99.9%", "45ms", "0.01%", "10k", "40%"):
        if not haystack_has(summary, token):
            fails.append(f"summary missing {token!r}")
    for junk in ("9001ms", "12%", "canary", "intern"):
        if junk.casefold() in summary.casefold():
            fails.append(f"distractor {junk!r} leaked into Executive Summary")
    raw = doc or ""
    raw_idx = raw.casefold().find("executive summary")
    raw_summary = raw[raw_idx:] if raw_idx >= 0 else raw
    n_li = len(re.findall(r"<li\b", raw_summary, re.I))
    n_md = len(re.findall(r"^[\-\*]\s+\S", summary, re.M))
    n_bullets = n_li or n_md
    if n_bullets != 5:
        fails.append(f"expected 5 summary bullets, got {n_bullets}")
    return fails


ORACLES: dict[str, Callable[[str], list[str]]] = {
    "table_from_mess": oracle_table_from_mess,
    "table_engineering": oracle_table_engineering,
    "bulk_cleanup": oracle_bulk_cleanup,
    "format_preservation": oracle_format_preservation,
    "style_application": oracle_style_application,
    "bullet_consistency": oracle_bullet_consistency,
    "style_consistency": oracle_style_consistency,
    "section_refactor": oracle_section_refactor,
    "comment_management": oracle_comment_management,
    "flowchart_gen": oracle_flowchart_gen,
    "data_sorting": oracle_data_sorting,
    "tax_column": oracle_tax_column,
    "py_refuse_overlap": oracle_py_dest,
    "py_no_bulk_read": oracle_py_dest,
    "reformat_resume": oracle_reformat_resume,
    "logical_rewriting": oracle_logical_rewriting,
    "smart_summarization": oracle_smart_summarization,
}

CREATIVE_TASK_IDS = frozenset(
    {"reformat_resume", "logical_rewriting", "smart_summarization"}
)
QUALITY_JUDGE_TASK_IDS = CREATIVE_TASK_IDS | frozenset(
    {"table_from_mess", "table_engineering"}
)


def check_oracle(task_id: str, final_document: str) -> list[str]:
    """Return failure strings (empty means the exported doc passed)."""
    fn = ORACLES.get(task_id or "")
    if fn is None:
        return []
    return fn(final_document or "")


def uses_llm_judge(task_id: str, category: str = "") -> bool:
    """LLM-as-judge is for quality ranking after the hard gate."""
    if task_id in QUALITY_JUDGE_TASK_IDS:
        return True
    return (category or "") == "creative"
