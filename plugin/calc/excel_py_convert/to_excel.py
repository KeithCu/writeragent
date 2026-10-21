# SPDX-License-Identifier: GPL-3.0-or-later
"""DAG ``xl("%Pn%")`` / legacy ``data`` patterns → Excel package ``xl(%Pn%)``.

Forward import keeps runnable ``xl("%Pn%", …)`` call sites (sandbox binding-only
``xl``). Export:

* If code already has binding-style ``xl(`` / ``%P``, unquote tokens for the
  Microsoft package (``xl("%P2%")`` → ``xl(%P2%)``) and normalize headers spacing.
* Legacy DAG workbooks that still use ``data`` / ``data[i]`` / ``ranges[i]`` /
  ``.to_pandas()`` (and the older ``pd.DataFrame(data[1:], columns=data[0])``
  form) are reversed to ``xl(...)`` as before.

Native OOXML write banks restored scripts into ``pythonScripts.xml`` and cell
formulas as ``_xlfn._xlws.PY(...)`` (see ``xlws_py_formula`` /
``python_scripts_xml``). Ordering-only deps are ignored when reconstructing
``xl(%Pn%)``. Header mode and ``return_type`` are preserved when present on the
conversion report / cell metadata.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Sequence, cast

from plugin.calc.excel_py_convert.models import (
    BindingInfo,
    ConvertedCell,
    ConversionReport,
    HeaderMode,
    _as_header_mode,
)
from plugin.calc.excel_py_convert.to_dag import (
    _OBJECT_SUPPRESS,
    _normalize_excel_placeholders,
    analyze_xl_calls,
)
from plugin.calc.python.formula_edit import parse_python_formula
from plugin.framework.ast_stmt_edit import ast_source_offset, line_starts_exact

_PY_NS = "http://schemas.microsoft.com/office/spreadsheetml/2022/pythonscript"
# Calc ``Sheet.A1`` → Excel ``Sheet!A1`` for _xlws.PY deps (leave ranges without a sheet alone).
_CALC_SHEET_REF_RE = re.compile(r"^((?:'[^']*(?:''[^']*)*')|[^'!.]+)\.(\$?[A-Za-z]+\$?\d+(?::\$?[A-Za-z]+\$?\d+)?)$")

# (sheet, cell, formula) or (sheet, cell, formula, report-cell-meta)
DagFormulaItem = tuple[str, str, str] | tuple[str, str, str, dict[str, Any]]


_INVALID_XML_CHARS_RE = re.compile(r"[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]")


def _xml_text_escape(text: str) -> str:
    """Escape &, <, >, and CR for XML text nodes, rejecting characters outside XML 1.0."""
    raw = text or ""
    if _INVALID_XML_CHARS_RE.search(raw):
        raise ValueError(f"script contains invalid XML 1.0 character: {raw!r}")
    escaped = raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return escaped.replace("\r", "&#13;")


def _p_token(index: int) -> str:
    return f"%P{index + 2}%"


def _xl_expr(index: int, header_mode: HeaderMode) -> str:
    # Match Microsoft samples: no space after the comma in ``headers=…``.
    tok = _p_token(index)
    if header_mode == "true":
        return f"xl({tok},headers=True)"
    if header_mode == "false":
        return f"xl({tok},headers=False)"
    return f"xl({tok})"


def _data_header_modes(bindings: Sequence[BindingInfo], n: int) -> list[HeaderMode]:
    modes: list[HeaderMode] = []
    if bindings:
        for b in bindings:
            if b.role == "ordering":
                continue
            modes.append(_as_header_mode(b.header_mode))
    while len(modes) < n:
        modes.append("omit")
    return modes


def _omit_modes(n: int) -> list[HeaderMode]:
    return ["omit" for _unused in range(max(n, 0))]


def _node_span(node: ast.AST, src: str, line_starts: list[int]) -> tuple[int, int]:
    lineno = getattr(node, "lineno", None)
    col = getattr(node, "col_offset", None)
    end_lineno = getattr(node, "end_lineno", lineno)
    end_col = getattr(node, "end_col_offset", col)
    if lineno is None or col is None or end_lineno is None or end_col is None:
        return -1, -1
    start = ast_source_offset(src, lineno, col, line_starts=line_starts)
    end = ast_source_offset(src, end_lineno, end_col, line_starts=line_starts)
    return start, end


def _collect_legacy_spans(
    tree: ast.AST,
    src: str,
    line_starts: list[int],
    deps: list[str],
    modes: list[HeaderMode],
) -> tuple[list[tuple[int, int, str]], list[str], bool]:
    """Find legacy data / ranges / to_pandas / DataFrame AST nodes and their replacement spans."""
    issues: list[str] = []
    fatal = False
    spans: list[tuple[int, int, str]] = []
    handled: set[int] = set()

    for node in ast.walk(tree):
        # 1. pd.DataFrame(data[1:], columns=data[0]) or data[i] form
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "pd"
                and func.attr.lower() == "dataframe"
                and len(node.args) == 1
                and len(node.keywords) == 1
            ):
                arg0 = node.args[0]
                if (
                    isinstance(arg0, ast.Subscript)
                    and isinstance(arg0.slice, ast.Slice)
                    and isinstance(arg0.slice.lower, ast.Constant)
                    and arg0.slice.lower.value == 1
                    and arg0.slice.upper is None
                    and arg0.slice.step is None
                ):
                    idx: int | None = None
                    if isinstance(arg0.value, ast.Name) and arg0.value.id == "data":
                        idx = 0
                    elif (
                        isinstance(arg0.value, ast.Subscript)
                        and isinstance(arg0.value.value, ast.Name)
                        and arg0.value.value.id == "data"
                        and isinstance(arg0.value.slice, ast.Constant)
                        and isinstance(arg0.value.slice.value, int)
                    ):
                        idx = arg0.value.slice.value

                    kw = node.keywords[0]
                    if (
                        idx is not None
                        and kw.arg == "columns"
                        and isinstance(kw.value, ast.Subscript)
                        and isinstance(kw.value.slice, ast.Constant)
                        and kw.value.slice.value == 0
                    ):
                        cols_ok = False
                        if idx == 0 and isinstance(kw.value.value, ast.Name) and kw.value.value.id == "data":
                            cols_ok = True
                        elif (
                            idx != 0
                            and isinstance(kw.value.value, ast.Subscript)
                            and isinstance(kw.value.value.value, ast.Name)
                            and kw.value.value.value.id == "data"
                            and isinstance(kw.value.value.slice, ast.Constant)
                            and kw.value.value.slice.value == idx
                        ):
                            cols_ok = True

                        if cols_ok:
                            for descendant in ast.walk(node):
                                handled.add(id(descendant))
                            if idx >= len(deps):
                                issues.append(f"DataFrame pattern references data[{idx}] but only {len(deps)} deps")
                                fatal = True
                            else:
                                start, end = _node_span(node, src, line_starts)
                                if start >= 0 and end > start:
                                    spans.append((start, end, _xl_expr(idx, "true")))
                            continue

            # 2. (ranges|data)(?:[i])?.to_pandas(...)
            if isinstance(func, ast.Attribute) and func.attr.lower() == "to_pandas" and len(node.args) == 0:
                header_mode: HeaderMode | None = None
                if len(node.keywords) == 0:
                    header_mode = "true"
                elif len(node.keywords) == 1:
                    kw = node.keywords[0]
                    if kw.arg == "header_row" and isinstance(kw.value, ast.Constant) and kw.value.value is None:
                        header_mode = "false"

                if header_mode is not None:
                    target = func.value
                    tp_idx: int | None = None
                    if isinstance(target, ast.Name) and target.id in ("data", "ranges"):
                        tp_idx = 0
                    elif (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id in ("data", "ranges")
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, int)
                    ):
                        tp_idx = target.slice.value

                    if tp_idx is not None:
                        for descendant in ast.walk(node):
                            handled.add(id(descendant))
                        if tp_idx >= len(deps):
                            issues.append(f"to_pandas pattern references index {tp_idx} but only {len(deps)} deps")
                            fatal = True
                        else:
                            start, end = _node_span(node, src, line_starts)
                            if start >= 0 and end > start:
                                spans.append((start, end, _xl_expr(tp_idx, header_mode)))
                        continue

    # 3. Subscripts data[i] / ranges[i] and bare data
    for node in ast.walk(tree):
        if id(node) in handled:
            continue
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name) and node.value.id in ("data", "ranges"):
                if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int):
                    sub_idx = node.slice.value
                    handled.add(id(node))
                    handled.add(id(node.value))
                    if sub_idx >= len(deps):
                        issues.append(f"{node.value.id}[{sub_idx}] with only {len(deps)} deps")
                        fatal = True
                    else:
                        start, end = _node_span(node, src, line_starts)
                        if start >= 0 and end > start:
                            hm: HeaderMode = modes[sub_idx] if sub_idx < len(modes) else "omit"
                            spans.append((start, end, _xl_expr(sub_idx, hm)))
                    continue

        if isinstance(node, ast.Name) and node.id == "data" and isinstance(node.ctx, ast.Load):
            if id(node) in handled:
                continue
            # Tip: bare ``data`` is a CalcRange only for a single formula arg; with several args it is the ranges list.
            if len(deps) == 1:
                handled.add(id(node))
                start, end = _node_span(node, src, line_starts)
                if start >= 0 and end > start:
                    hm0: HeaderMode = modes[0] if modes else "omit"
                    spans.append((start, end, _xl_expr(0, hm0)))

    return spans, issues, fatal


def rewrite_dag_code_to_excel(
    code: str,
    data_args: list[str],
    *,
    header_modes: list[HeaderMode] | None = None,
    strip_object_suppress: bool = True,
) -> tuple[str, list[str], list[str], bool]:
    """Rewrite DAG code to Excel package ``xl(%Pn%)`` / ``xl(%Pn%, headers=…)``.

    Returns ``(excel_code, deps, issues, fatal)``. Only *data* args are returned as deps
    (ordering-only args must already be filtered by the caller).
    """
    issues: list[str] = []
    deps = list(data_args)
    modes: list[HeaderMode] = list(header_modes) if header_modes is not None else _omit_modes(len(deps))
    while len(modes) < len(deps):
        modes.append("omit")
    text = code or ""
    if strip_object_suppress and text.endswith(_OBJECT_SUPPRESS):
        text = text[:-len(_OBJECT_SUPPRESS)]

    if not text.strip():
        return text, deps, issues, False

    # Tip: the runnable DAG form quotes xl("%Pn%"); the Excel package uses bare %Pn%.
    normalized = _normalize_excel_placeholders(text)
    try:
        tree = ast.parse(normalized)
    except (SyntaxError, TypeError, MemoryError, RecursionError) as exc:
        msg = getattr(exc, "msg", str(exc))
        lineno = getattr(exc, "lineno", None)
        offset = getattr(exc, "offset", None)
        loc = f"line {lineno}" if lineno is not None else "unknown line"
        if offset is not None:
            loc = f"{loc}:{offset}"
        issues.append(f"Python syntax error at {loc}: {msg}")
        return code, deps, issues, True

    line_starts = line_starts_exact(text)
    all_spans: list[tuple[int, int, str]] = []

    analysis = analyze_xl_calls(text, num_deps=len(deps))
    issues.extend(analysis.issues)
    if analysis.fatal:
        return code, deps, issues, True

    for call in analysis.calls:
        if call.p_num is not None:
            idx = call.p_num - 2
            repl = _xl_expr(idx, _as_header_mode(call.header_mode))
            all_spans.append((call.start, call.end, repl))

    legacy_spans, legacy_issues, legacy_fatal = _collect_legacy_spans(tree, text, line_starts, deps, modes)
    issues.extend(legacy_issues)
    if legacy_fatal:
        return code, deps, issues, True

    all_spans.extend(legacy_spans)
    if not all_spans:
        return text, deps, issues, False

    # Apply spans from end of string to keep earlier offsets valid.
    all_spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    kept_spans: list[tuple[int, int, str]] = []
    last_end = -1
    for s in all_spans:
        if s[0] < last_end:
            continue
        kept_spans.append(s)
        last_end = s[1]

    out = text
    for start, end, repl in sorted(kept_spans, key=lambda s: s[0], reverse=True):
        out = out[:start] + repl + out[end:]

    return out, deps, issues, False


def excel_dep_ref(ref: str) -> str:
    """Normalize a Calc/Excel range token for ``_xlws.PY`` args (``Sheet.A1`` → ``Sheet!A1``)."""
    raw = (ref or "").strip()
    if not raw or "!" in raw:
        return raw
    m = _CALC_SHEET_REF_RE.match(raw)
    if not m:
        return raw
    return f"{m.group(1)}!{m.group(2)}"


def deps_for_xlws_export(cell: ConvertedCell) -> list[str]:
    """Deps to write on ``_xlws.PY``: prefer ``excel_deps`` tokens when aligned with ``data_args``.

    Policy: ``plugin.calc.excel_py_convert.models`` module doc / ``EXCEL_DEP_TOKEN_FIDELITY``.
    """
    data = [a for a in cell.data_args if a]
    excel = [a for a in cell.excel_deps if a]
    if excel and len(excel) == len(data):
        return excel
    return data


def xlws_py_formula(script_index: int, return_type: int, data_args: Sequence[str]) -> str:
    """Build ``_xlfn._xlws.PY(scriptIndex, returnType, deps…)`` (no leading ``=``)."""
    parts = [str(int(script_index)), str(int(return_type))]
    parts.extend(excel_dep_ref(a) for a in data_args if a)
    return "_xlfn._xlws.PY(" + ",".join(parts) + ")"


def python_scripts_xml(scripts: Sequence[str]) -> bytes:
    """Serialize ordered script bank to ``xl/pythonScripts.xml`` bytes (UTF-8)."""
    chunks = ['<?xml version="1.0" encoding="UTF-8"?>\n', f'<pythonScripts xmlns="{_PY_NS}">\n']
    for body in scripts:
        chunks.append(f"  <pythonScript><code>{_xml_text_escape(body or '')}</code></pythonScript>\n")
    chunks.append("</pythonScripts>\n")
    return "".join(chunks).encode("utf-8")


def assign_script_bank(cells: Sequence[ConvertedCell]) -> tuple[list[str], list[ConvertedCell]]:
    """Content-dedup ``converted_code`` → ``scripts`` and set ``script_index`` on each cell.

    Returns ``(scripts, cells)`` — *cells* are the same objects, mutated in place.
    """
    scripts: list[str] = []
    index_by_code: dict[str, int] = {}
    for cell in cells:
        if not cell.converted or not (cell.converted_code or "").strip():
            continue
        code = cell.converted_code
        idx = index_by_code.get(code)
        if idx is None:
            idx = len(scripts)
            scripts.append(code)
            index_by_code[code] = idx
        cell.script_index = idx
        cell.excel_formula = "=" + xlws_py_formula(idx, cell.return_type, deps_for_xlws_export(cell))
    return scripts, list(cells)


def expand_placeholders_to_literals(code: str, deps: list[str]) -> str:
    """Replace ``%Pk%`` with quoted A1 deps for FORMULATEXT-style output."""

    def repl(m: re.Match[str]) -> str:
        p_num = int(m.group(1))
        idx = p_num - 2
        if 0 <= idx < len(deps):
            return f'"{deps[idx]}"'
        return m.group(0)

    return re.sub(r"%P(\d+)%", repl, code)


def convert_dag_formula_to_excel(formula: str, *, sheet: str = "Sheet1", cell: str = "A1", return_type: int = 0, meta: dict[str, Any] | None = None) -> ConvertedCell:
    """Convert one DAG-style ``=PY("…"; ranges)`` formula string to Excel shape."""
    parts = parse_python_formula(formula)
    if parts is None:
        return ConvertedCell(sheet=sheet, cell=cell, direction="excel", original_code=formula, converted_code="", issues=["not a =PY/=PYTHON formula"], converted=False, return_type=return_type)

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

    bindings_raw = meta.get("bindings")
    bindings: list[BindingInfo] = []
    if isinstance(bindings_raw, list):
        for b in bindings_raw:
            if isinstance(b, dict):
                bindings.append(BindingInfo.from_dict(b))

    modes = _data_header_modes(bindings, len(data_args))

    excel_code, deps, issues, fatal = rewrite_dag_code_to_excel(parts.code, data_args, header_modes=modes)
    if ordering_args:
        issues.append("ignored ordering-only deps on reverse export")
    array_ref = str(meta.get("array_ref") or "")
    excel_deps_meta = [str(a) for a in (meta.get("excel_deps") or []) if a]
    # Keep export tokens aligned with rewritten data_args length.
    if excel_deps_meta and len(excel_deps_meta) == len(deps):
        excel_deps_out = excel_deps_meta
    elif excel_deps_meta and len(excel_deps_meta) == len(data_args) and deps == data_args:
        excel_deps_out = excel_deps_meta
    else:
        excel_deps_out = list(excel_deps_meta) if len(excel_deps_meta) == len(deps) else []
    out = ConvertedCell(
        sheet=sheet,
        cell=cell,
        direction="excel",
        original_code=parts.code,
        converted_code=excel_code,
        data_args=deps,
        excel_deps=excel_deps_out,
        ordering_args=ordering_args,
        bindings=bindings,
        excel_formula="",  # filled by assign_script_bank
        issues=issues,
        shared_kernel=not deps and "data" not in parts.code,
        return_type=rt,
        converted=not fatal,
        array_ref=array_ref,
    )
    assign_script_bank([out])
    return out


def convert_dag_cells_to_excel(formulas: Sequence[DagFormulaItem], *, return_type: int = 0, report_meta: dict[str, Any] | None = None) -> ConversionReport:
    """Convert DAG workbook formulas / report cells to Excel-shaped export."""
    report = ConversionReport(direction="excel")
    if report_meta and report_meta.get("source_path"):
        report.source_path = str(report_meta.get("source_path") or "")
    for item in formulas:
        sheet = item[0]
        cell = item[1]
        formula = item[2]
        meta: dict[str, Any] = {}
        if len(item) == 4:
            meta = cast("tuple[str, str, str, dict[str, Any]]", item)[3]
        report.cells.append(convert_dag_formula_to_excel(formula, sheet=sheet, cell=cell, return_type=return_type, meta=meta))
    assign_script_bank(report.cells)
    return report


def convert_dag_report_to_excel(dag_report: ConversionReport) -> ConversionReport:
    """Reverse a DAG ``ConversionReport`` to Excel shape (preserves return_type / excel_deps).

    ``_xlws.PY`` deps use ``deps_for_xlws_export`` (original Table/ANCHORARRAY tokens when
    present — see models module doc). Legacy ``ordering_args`` are not emitted.
    """
    out = ConversionReport(direction="excel", source_path=dag_report.source_path)
    for cell in dag_report.cells:
        if not cell.converted or not (cell.converted_code or "").strip():
            out.cells.append(
                ConvertedCell(sheet=cell.sheet, cell=cell.cell, direction="excel", original_code=cell.converted_code or cell.original_code, converted_code="", issues=list(cell.issues) + ["skipped: not converted on DAG pass"], converted=False, return_type=cell.return_type, array_ref=cell.array_ref)
            )
            continue
        modes = _data_header_modes(cell.bindings, len(cell.data_args))
        excel_code, deps, issues, fatal = rewrite_dag_code_to_excel(cell.converted_code, list(cell.data_args), header_modes=modes)
        if cell.ordering_args:
            issues.append("ignored ordering-only deps on reverse export")
        excel_deps = list(cell.excel_deps)
        if excel_deps and len(excel_deps) != len(deps):
            # data_args rewrite rarely changes length; if it does, fall back to A1.
            excel_deps = []
        out.cells.append(
            ConvertedCell(
                sheet=cell.sheet,
                cell=cell.cell,
                direction="excel",
                original_code=cell.converted_code,
                converted_code=excel_code,
                data_args=deps,
                excel_deps=excel_deps,
                ordering_args=list(cell.ordering_args),
                bindings=list(cell.bindings),
                issues=issues,
                shared_kernel=cell.shared_kernel,
                snapshot_deps=list(cell.snapshot_deps),
                return_type=int(cell.return_type or 0),
                converted=not fatal,
                array_ref=cell.array_ref,
            )
        )
    assign_script_bank(out.cells)
    return out
