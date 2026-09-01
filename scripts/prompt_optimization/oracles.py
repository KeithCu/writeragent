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
# 1.20 + 0.50 + 0.80 + 2.00 + 1.50 + 1.75
_TABLE_ENGINEERING_PRICE_TOTAL = 7.75

_TAX_BY_ITEM = {
    "Apple": (10.0, 0.8),
    "Banana": (5.0, 0.4),
    "Orange": (8.0, 0.64),
    "Pear": (12.5, 1.0),
}

_BULLET_ITEMS = (
    "First thing",
    "Second thing",
    "Third thing",
    "Fourth thing",
    "Fifth item",
    "Sixth with extra space",
    "Seventh (mixed)",
)

_HYPE = ("incredibly", "significant leap", "brand new")

_MONEY_RE = re.compile(
    r"\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+)"
)
_HN_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
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


def heading_texts(doc: str) -> list[tuple[int, str]]:
    """``(level, text)`` in document order from HTML and markdown headings."""
    found: list[tuple[int, int, str]] = []
    for m in _HN_RE.finditer(doc or ""):
        found.append((m.start(), int(m.group(1)), _inner_text(m.group(2))))
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


def oracle_table_engineering(doc: str) -> list[str]:
    fails: list[str] = []
    if not _TABLE_RE.search(doc or ""):
        fails.append("no HTML table")
    text = visible_text(doc)
    for token in ("Item", "Price", "Quantity", "Kiwi", "note"):
        if token not in text:
            fails.append(f"missing {token!r}")
    if not _has_total_label(doc, text):
        fails.append("no Total row")
    amounts = parse_money(text)
    if not any(_near(v, _TABLE_ENGINEERING_PRICE_TOTAL) for v in amounts):
        fails.append(f"price Total is not {_TABLE_ENGINEERING_PRICE_TOTAL}")
    return fails


def oracle_bulk_cleanup(doc: str) -> list[str]:
    fails: list[str] = []
    text = visible_text(doc)
    for token in (
        "This sentence has extra spaces",
        "https://example.com/test",
        "Quoted text",
    ):
        if token not in text:
            fails.append(f"missing {token!r}")
    # Score visible text only — raw LO XHTML indent is not a content error,
    # and a lone ASCII space is ordinary English, not a leftover artifact.
    # Check the tag-deleted string so inter-tag joins are not counted as "  ".
    if "  " in text:
        fails.append("visible double space")
    if " ." in text or " ," in text or ".." in text:
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
    for item in _BULLET_ITEMS:
        needle = f"- {item}."
        if needle not in text and needle not in (doc or ""):
            fails.append(f"missing hyphen+period bullet {needle!r}")
    return fails


def oracle_style_consistency(doc: str) -> list[str]:
    fails: list[str] = []
    text = visible_text(doc)
    if "Quotations" not in (doc or "") and "Quotations" not in text:
        fails.append("Default paragraphs were not mapped to Quotations")
    h1 = " | ".join(t.casefold() for t in h1_texts(doc))
    if "heading 2 text that should be upgraded" not in h1:
        fails.append("HEADING 2 line was not upgraded to Heading 1")
    if "heading 2 again" not in h1:
        fails.append("'Heading 2 again' was not upgraded to Heading 1")
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
    return fails


def oracle_comment_management(doc: str) -> list[str]:
    fails: list[str] = []
    text = visible_text(doc)
    blob = f"{doc or ''}\n{text}"
    if "uncertain" not in blob:
        fails.append("missing 'uncertain'")
    if "Review this before finalizing" not in blob:
        fails.append("missing review comment text")
    if "review requirement" not in blob.casefold():
        fails.append("missing review-requirement note")
    return fails


def oracle_flowchart_gen(doc: str) -> list[str]:
    fails: list[str] = []
    texts: list[str] = []
    data = parse_json_export(doc)
    if data:
        _collect_texts(data, texts)
    blob = " ".join(texts) if texts else visible_text(doc)
    for token in ("Start", "Process", "Decision", "End", "login", "credentials"):
        if token.casefold() not in blob.casefold():
            fails.append(f"flowchart missing {token!r}")
    # Labels alone are not a flowchart — require connector metadata.
    connected = 0
    if data:
        tree = data.get("tree")
        if isinstance(tree, list):
            for node in tree:
                if not isinstance(node, dict):
                    continue
                if node.get("connected_start") or node.get("connected_end"):
                    connected += 1
        edges = data.get("connections")
        if isinstance(edges, list) and edges:
            connected = max(connected, len(edges))
    if connected < 2:
        fails.append("flowchart missing connections")
    return fails


def oracle_data_sorting(doc: str) -> list[str]:
    grid = _calc_grid(doc)
    if not grid or len(grid) < 2:
        return ["no Calc grid/snapshot"]
    rows = grid[1:] if any(str(c).casefold() == "product" for c in grid[0]) else grid
    names = [str(r[0]) if r else "" for r in rows]
    try:
        tool_i = next(i for i, n in enumerate(names) if n == "Tool")
        widget_i = next(i for i, n in enumerate(names) if n == "Widget")
    except StopIteration:
        return ["sorted grid missing Tool or Widget"]
    if tool_i >= widget_i:
        return ["Revenue sort is not descending (Tool/2100 must precede Widget)"]
    revenues: list[float] = []
    for row in rows:
        if len(row) < 2:
            continue
        num = _as_float(row[1])
        if num is not None:
            revenues.append(num)
    if revenues != sorted(revenues, reverse=True):
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
    from eval_worlds import cell_in_a1_range

    found_outside = False
    found_py = False
    for addr, formula in formulas.items():
        text = str(formula)
        if text.lstrip().upper().startswith("=PY"):
            found_py = True
            if not cell_in_a1_range(str(addr), "A1:H500"):
                found_outside = True
    if not found_py:
        return ["no =PY formula recorded"]
    if not found_outside:
        return ["=PY dest is inside A1:H500"]
    return []


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
    for row in grid[1:]:
        if not row:
            continue
        name = str(row[item_idx])
        if name not in _TAX_BY_ITEM:
            continue
        price_expected, tax_expected = _TAX_BY_ITEM[name]
        price = _as_float(row[price_idx]) if len(row) > price_idx else None
        tax = _as_float(row[tax_idx]) if len(row) > tax_idx else None
        if price is None or not _near(price, price_expected, 0.05):
            return [f"{name} price is not {price_expected}"]
        if tax is None or not _near(tax, tax_expected, 0.02):
            return [f"{name} tax is not 8% ({tax_expected})"]
    return []


def oracle_reformat_resume(doc: str) -> list[str]:
    """Sanity for scripted/hard checks. Tone stays with the LLM judge."""
    fails: list[str] = []
    text = visible_text(doc)
    blob = f"{doc or ''}\n{text}"
    for token in ("John Doe", "WORK HISTORY", "EDUCATION", "SKILLS", "Acme Corp", "TechStart"):
        if token not in blob:
            fails.append(f"missing {token!r}")
    return fails


def oracle_logical_rewriting(doc: str) -> list[str]:
    fails: list[str] = []
    text = visible_text(doc)
    blob = f"{doc or ''}\n{text}"
    for token in ("WriterAgent", "2.0", "Dual-Mode", "G-Eval", "Prometheus"):
        if token not in blob:
            fails.append(f"missing {token!r}")
    if "LocalWriter" in blob:
        fails.append("rewrote WriterAgent as LocalWriter")
    lower = blob.casefold()
    for word in _HYPE:
        if word.casefold() in lower:
            fails.append(f"hype leftover {word!r}")
    return fails


def oracle_smart_summarization(doc: str) -> list[str]:
    fails: list[str] = []
    text = visible_text(doc)
    blob = f"{doc or ''}\n{text}"
    if "Executive Summary" not in blob:
        fails.append("Executive Summary heading missing")
    for token in ("99.9%", "45ms", "0.01%", "10k RPS", "40%"):
        if token not in blob:
            fails.append(f"summary missing {token!r}")
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
    "py_unique_beside": oracle_py_dest,
    "py_refuse_overlap": oracle_py_dest,
    "py_inplace_reframe": oracle_py_dest,
    "py_no_bulk_read": oracle_py_dest,
    "reformat_resume": oracle_reformat_resume,
    "logical_rewriting": oracle_logical_rewriting,
    "smart_summarization": oracle_smart_summarization,
}

CREATIVE_TASK_IDS = frozenset(
    {"reformat_resume", "logical_rewriting", "smart_summarization"}
)


def check_oracle(task_id: str, final_document: str) -> list[str]:
    """Return failure strings (empty means the exported doc passed)."""
    fn = ORACLES.get(task_id or "")
    if fn is None:
        return []
    return fn(final_document or "")


def uses_llm_judge(task_id: str, category: str = "") -> bool:
    """LLM-as-judge is for creative tasks only."""
    if task_id in CREATIVE_TASK_IDS:
        return True
    return (category or "") == "creative"
