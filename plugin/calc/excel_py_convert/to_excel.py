# SPDX-License-Identifier: GPL-3.0-or-later
"""DAG-style ``data`` / DataFrame patterns → Excel ``xl(%Pn%)`` (inverse of ``to_dag``).

Only reverses the *data bridge* we introduced: ``data`` / ``data[i]`` /
``pd.DataFrame(data[1:], columns=data[0])`` become ``xl(...)`` again. Other Python
is unchanged.

This is a **script/dependency export**, not a writer of native
``pythonScripts.xml`` / ``_xlws.PY``. Ordering-only deps are ignored when
reconstructing ``xl(%Pn%)``. Header mode and ``return_type`` are preserved when
present on the conversion report / cell metadata.
"""

from __future__ import annotations

import ast
import re
from typing import Any

from plugin.calc.excel_py_convert.models import BindingInfo, ConvertedCell, ConversionReport, HeaderMode
from plugin.calc.python.formula_edit import escape_code_for_excel_formula, parse_python_formula

_DF_DATA_RE = re.compile(
    r"pd\.DataFrame\(\s*(data(?:\[\s*(\d+)\s*\])?)\[1:\]\s*,\s*columns\s*=\s*(data(?:\[\s*(\d+)\s*\])?)\[0\]\s*\)",
    re.IGNORECASE,
)
_OBJECT_SUPPRESS_RE = re.compile(
    r"\n?# excel_py: returnType=1 \(Object\).*?\nresult = None\s*$",
    re.DOTALL,
)


def _p_token(index: int) -> str:
    return f"%P{index + 2}%"


def _xl_expr(index: int, header_mode: HeaderMode) -> str:
    tok = _p_token(index)
    if header_mode == "true":
        return f"xl({tok}, headers=True)"
    if header_mode == "false":
        return f"xl({tok}, headers=False)"
    return f"xl({tok})"


def _header_modes_from_bindings(bindings: list[BindingInfo] | list[dict[str, Any]] | None, n: int) -> list[HeaderMode]:
    modes: list[HeaderMode] = ["omit"] * max(n, 0)
    if not bindings:
        return modes
    for b in bindings:
        if isinstance(b, dict):
            a1 = str(b.get("a1") or "")
            hm = str(b.get("header_mode") or "omit")
            role = str(b.get("role") or "data")
            idxs = list(b.get("original_indices") or [])
        else:
            a1 = b.a1
            hm = b.header_mode
            role = b.role
            idxs = list(b.original_indices)
        if role == "ordering" or not a1:
            continue
        mode: HeaderMode = "true" if hm == "true" else "false" if hm == "false" else "omit"
        # Bindings are stored in normalized data-arg order.
        # Prefer mapping via position in bindings list when original_indices empty.
        if not idxs:
            continue
        for oi in idxs:
            if 0 <= oi < len(modes):
                modes[oi] = mode
    return modes


def rewrite_dag_code_to_excel(
    code: str,
    data_args: list[str],
    *,
    header_modes: list[HeaderMode] | None = None,
    strip_object_suppress: bool = True,
) -> tuple[str, list[str], list[str]]:
    """Rewrite DAG ``data`` usage to ``xl(%Pn%)`` / ``xl(%Pn%, headers=…)``.

    Returns ``(excel_code, deps, issues)``. Only *data* args are returned as deps
    (ordering-only args must already be filtered by the caller).
    """
    issues: list[str] = []
    deps = list(data_args)
    modes = list(header_modes) if header_modes is not None else ["omit"] * len(deps)
    while len(modes) < len(deps):
        modes.append("omit")
    multi = len(deps) > 1
    text = code or ""
    if strip_object_suppress:
        text = _OBJECT_SUPPRESS_RE.sub("", text)

    def df_repl(m: re.Match[str]) -> str:
        left_idx = m.group(2)
        if left_idx is not None:
            idx = int(left_idx)
        else:
            idx = 0
        if idx >= len(deps):
            issues.append(f"DataFrame pattern references data[{idx}] but only {len(deps)} deps")
            idx = 0
        return _xl_expr(idx, "true")

    text = _DF_DATA_RE.sub(df_repl, text)

    # Token-position rewrite for data / data[i] via AST when possible.
    rewritten, ast_issues = _rewrite_data_names_ast(text, deps, modes, multi)
    issues.extend(ast_issues)
    if rewritten is not None:
        return rewritten, deps, issues

    # Regex fallback
    def data_index_repl(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        if idx >= len(deps):
            issues.append(f"data[{idx}] with only {len(deps)} deps")
            return m.group(0)
        return _xl_expr(idx, modes[idx] if idx < len(modes) else "omit")

    text = re.sub(r"\bdata\[\s*(\d+)\s*\]", data_index_repl, text)

    if not multi and deps:

        def bare_repl(_m: re.Match[str]) -> str:
            return _xl_expr(0, modes[0] if modes else "omit")

        text = re.sub(r"(?<![\w.])data(?!\s*\[)", bare_repl, text)
    elif re.search(r"(?<![\w.])data(?!\s*\[)", text) and multi:
        issues.append("bare `data` with multiple deps; ambiguous")

    return text, deps, issues


def _rewrite_data_names_ast(
    code: str,
    deps: list[str],
    modes: list[HeaderMode],
    multi: bool,
) -> tuple[str | None, list[str]]:
    """Rewrite ``data`` / ``data[i]`` Name/Subscript nodes; preserve strings/comments."""
    issues: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None, issues

    class _Hit:
        __slots__ = ("start", "end", "repl")

        def __init__(self, start: int, end: int, repl: str) -> None:
            self.start = start
            self.end = end
            self.repl = repl

    hits: list[_Hit] = []

    def offset(lineno: int, col: int) -> int:
        lines = code.splitlines(keepends=True)
        return sum(len(lines[i]) for i in range(lineno - 1)) + col

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "data":
            sl = node.slice
            idx = None
            if isinstance(sl, ast.Constant) and isinstance(sl.value, int):
                idx = sl.value
            if idx is None:
                continue
            if idx >= len(deps):
                issues.append(f"data[{idx}] with only {len(deps)} deps")
                continue
            # Skip if this subscript is inside a DataFrame pattern already handled — still OK to rewrite.
            start = offset(node.lineno, node.col_offset)
            end = offset(node.end_lineno or node.lineno, node.end_col_offset or node.col_offset)
            hits.append(_Hit(start, end, _xl_expr(idx, modes[idx] if idx < len(modes) else "omit")))
        elif isinstance(node, ast.Name) and node.id == "data" and isinstance(node.ctx, ast.Load):
            if multi:
                issues.append("bare `data` with multiple deps; ambiguous")
                continue
            if not deps:
                continue
            start = offset(node.lineno, node.col_offset)
            end = offset(node.end_lineno or node.lineno, node.end_col_offset or node.col_offset)
            hits.append(_Hit(start, end, _xl_expr(0, modes[0] if modes else "omit")))

    if not hits:
        # Still may have had DataFrame regex applied by caller — return None to use regex path? 
        # If AST parsed but found no data names, return code unchanged.
        return code, issues

    # Drop overlapping hits (prefer longer / outer) — simple: sort by start, skip overlaps
    hits.sort(key=lambda h: (h.start, -(h.end - h.start)))
    kept: list[_Hit] = []
    last_end = -1
    for h in hits:
        if h.start < last_end:
            continue
        kept.append(h)
        last_end = h.end

    out = code
    for h in sorted(kept, key=lambda x: x.start, reverse=True):
        out = out[: h.start] + h.repl + out[h.end :]
    return out, issues


def excel_formulatext(code: str, return_type: int = 0) -> str:
    """Build Excel-style ``=PY("…", return_type)`` display string (literals, not %Pn%)."""
    escaped = escape_code_for_excel_formula(code)
    return f'=PY("{escaped}",{int(return_type)})'


def expand_placeholders_to_literals(code: str, deps: list[str]) -> str:
    """Replace ``%Pk%`` with quoted A1 deps for FORMULATEXT-style output."""

    def repl(m: re.Match[str]) -> str:
        p_num = int(m.group(1))
        idx = p_num - 2
        if 0 <= idx < len(deps):
            return f'"{deps[idx]}"'
        return m.group(0)

    return re.sub(r"%P(\d+)%", repl, code)


def convert_dag_formula_to_excel(
    formula: str,
    *,
    sheet: str = "Sheet1",
    cell: str = "A1",
    return_type: int = 0,
    meta: dict[str, Any] | None = None,
) -> ConvertedCell:
    """Convert one DAG-style ``=PY("…"; ranges)`` formula string to Excel shape."""
    parts = parse_python_formula(formula)
    if parts is None:
        return ConvertedCell(
            sheet=sheet,
            cell=cell,
            direction="excel",
            original_code=formula,
            converted_code="",
            issues=["not a =PY/=PYTHON formula"],
            converted=False,
            return_type=return_type,
        )

    from plugin.calc.python.formula_edit import format_data_binding_display, parse_data_binding_text

    meta = meta or {}
    rt = int(meta.get("return_type", return_type) or 0)
    data_text = format_data_binding_display(parts.data_suffix)
    all_args = parse_data_binding_text(data_text)

    # Prefer explicit data_args / ordering_args from report metadata.
    if "data_args" in meta or "ordering_args" in meta:
        data_args = [str(a) for a in (meta.get("data_args") or [])]
        ordering_args = [str(a) for a in (meta.get("ordering_args") or [])]
    else:
        # Without metadata, treat trailing args as data (cannot know ordering-only).
        data_args = list(all_args)
        ordering_args = []
        # Heuristic: if report omitted meta, keep all — document limitation.
        if ordering_args:
            pass

    bindings_raw = meta.get("bindings")
    bindings: list[BindingInfo] = []
    if isinstance(bindings_raw, list):
        for b in bindings_raw:
            if isinstance(b, dict):
                bindings.append(
                    BindingInfo(
                        a1=str(b.get("a1") or ""),
                        header_mode=b.get("header_mode") or "omit",  # type: ignore[arg-type]
                        role=b.get("role") or "data",  # type: ignore[arg-type]
                        original_indices=list(b.get("original_indices") or []),
                    )
                )

    # Header modes aligned to normalized data_args order
    modes: list[HeaderMode] = []
    if bindings:
        for b in bindings:
            if b.role == "ordering":
                continue
            modes.append(b.header_mode if b.header_mode in ("true", "false", "omit") else "omit")
    while len(modes) < len(data_args):
        modes.append("omit")

    excel_code, deps, issues = rewrite_dag_code_to_excel(parts.code, data_args, header_modes=modes)
    if ordering_args:
        issues.append("ignored ordering-only deps on reverse export")
    issues.append("script/dependency export only — does not write native pythonScripts.xml / _xlws.PY")
    display_code = expand_placeholders_to_literals(excel_code, deps)
    excel_formula = excel_formulatext(display_code, return_type=rt)

    return ConvertedCell(
        sheet=sheet,
        cell=cell,
        direction="excel",
        original_code=parts.code,
        converted_code=excel_code,
        data_args=deps,
        ordering_args=ordering_args,
        bindings=bindings,
        excel_formula=excel_formula,
        issues=issues,
        shared_kernel=not deps and "data" not in parts.code,
        return_type=rt,
        converted=True,
    )


def convert_dag_cells_to_excel(
    formulas: list[tuple[str, str, str]] | list[tuple[str, str, str, dict[str, Any]]],
    *,
    return_type: int = 0,
    report_meta: dict[str, Any] | None = None,
) -> ConversionReport:
    """Convert DAG workbook formulas / report cells to Excel-shaped export."""
    report = ConversionReport(direction="excel")
    if report_meta and report_meta.get("source_path"):
        report.source_path = str(report_meta.get("source_path") or "")
    report.issues.append(
        "to-excel is a script/dependency export until native OOXML pythonScripts/_xlws.PY writing ships"
    )
    for item in formulas:
        meta: dict[str, Any] = {}
        if len(item) == 4:
            sheet, cell, formula, meta = item  # type: ignore[misc]
        else:
            sheet, cell, formula = item  # type: ignore[misc]
        report.cells.append(
            convert_dag_formula_to_excel(formula, sheet=sheet, cell=cell, return_type=return_type, meta=meta)
        )
    return report
