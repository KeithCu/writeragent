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
from typing import Any, ClassVar, Sequence, cast

from plugin.calc.excel_py_convert.models import BindingInfo, ConvertedCell, ConversionReport, DepRole, HeaderMode
from plugin.calc.excel_py_convert.to_dag import ast_source_offset
from plugin.calc.python.formula_edit import escape_code_for_excel_formula, parse_python_formula

_PY_NS = "http://schemas.microsoft.com/office/spreadsheetml/2022/pythonscript"
# Calc ``Sheet.A1`` → Excel ``Sheet!A1`` for _xlws.PY deps (leave ranges without a sheet alone).
_CALC_SHEET_REF_RE = re.compile(r"^((?:'[^']*(?:''[^']*)*')|[^'!.]+)\.(\$?[A-Za-z]+\$?\d+(?::\$?[A-Za-z]+\$?\d+)?)$")
_DF_DATA_RE = re.compile(
    r"pd\.DataFrame\(\s*(data(?:\[\s*(\d+)\s*\])?)\[1:\]\s*,\s*columns\s*=\s*(data(?:\[\s*(\d+)\s*\])?)\[0\]\s*\)",
    re.IGNORECASE,
)
_TO_PANDAS_TRUE_RE = re.compile(
    r"(ranges|data)(?:\[\s*(\d+)\s*\])?\.to_pandas\(\s*\)",
    re.IGNORECASE,
)
_TO_PANDAS_FALSE_RE = re.compile(
    r"(ranges|data)(?:\[\s*(\d+)\s*\])?\.to_pandas\(\s*header_row\s*=\s*None\s*\)",
    re.IGNORECASE,
)
_OBJECT_SUPPRESS_RE = re.compile(
    r"\n?# excel_py: returnType=1 \(Object\).*?\nresult = None\s*$",
    re.DOTALL,
)
# Runnable DAG form uses quoted tokens; Excel package uses bare %Pn%.
_QUOTED_P_TOKEN_RE = re.compile(
    r"""xl\(\s*(['"])%P(\d+)%\1\s*(,\s*headers\s*=\s*(True|False))?\s*\)""",
    re.IGNORECASE,
)
_HAS_XL_BINDING_RE = re.compile(r"""xl\s*\(\s*['"]?%P\d+%""", re.IGNORECASE)

# (sheet, cell, formula) or (sheet, cell, formula, report-cell-meta)
DagFormulaItem = tuple[str, str, str] | tuple[str, str, str, dict[str, Any]]


def _xml_text_escape(text: str) -> str:
    """Escape &, <, > for XML text nodes (not an XML parser — no XXE surface)."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

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


def _as_header_mode(value: object) -> HeaderMode:
    if value == "true":
        return "true"
    if value == "false":
        return "false"
    return "omit"


def _as_dep_role(value: object) -> DepRole:
    if value == "ordering":
        return "ordering"
    return "data"


def _omit_modes(n: int) -> list[HeaderMode]:
    return ["omit" for _unused in range(max(n, 0))]


def _dag_xl_already_bound(code: str) -> bool:
    """True when *code* already uses binding-style ``xl("%Pn%")`` / ``xl(%Pn%)``."""
    return bool(_HAS_XL_BINDING_RE.search(code or ""))


def _unquote_xl_binding_tokens(code: str) -> str:
    """``xl("%P2%",headers=True)`` → ``xl(%P2%,headers=True)`` for pythonScripts.xml."""

    def repl(m: re.Match[str]) -> str:
        p_num = m.group(2)
        headers = m.group(3)
        if headers:
            # Normalize to MS sample style: no spaces around =.
            flag = m.group(4)
            return f"xl(%P{p_num}%,headers={flag})"
        return f"xl(%P{p_num}%)"

    return _QUOTED_P_TOKEN_RE.sub(repl, code or "")


def rewrite_dag_code_to_excel(
    code: str,
    data_args: list[str],
    *,
    header_modes: list[HeaderMode] | None = None,
    strip_object_suppress: bool = True,
) -> tuple[str, list[str], list[str]]:
    """Rewrite DAG code to Excel package ``xl(%Pn%)`` / ``xl(%Pn%, headers=…)``.

    Returns ``(excel_code, deps, issues)``. Only *data* args are returned as deps
    (ordering-only args must already be filtered by the caller).
    """
    issues: list[str] = []
    deps = list(data_args)
    modes: list[HeaderMode] = list(header_modes) if header_modes is not None else _omit_modes(len(deps))
    while len(modes) < len(deps):
        modes.append("omit")
    text = code or ""
    if strip_object_suppress:
        text = _OBJECT_SUPPRESS_RE.sub("", text)

    # New forward path keeps xl("%Pn%"); only unquote for the Excel package.
    if _dag_xl_already_bound(text):
        return _unquote_xl_binding_tokens(text), deps, issues

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

    def to_pandas_false_repl(m: re.Match[str]) -> str:
        idx_s = m.group(2)
        idx = int(idx_s) if idx_s is not None else 0
        if idx >= len(deps):
            issues.append(f"to_pandas pattern references index {idx} but only {len(deps)} deps")
            idx = 0
        return _xl_expr(idx, "false")

    def to_pandas_true_repl(m: re.Match[str]) -> str:
        idx_s = m.group(2)
        idx = int(idx_s) if idx_s is not None else 0
        if idx >= len(deps):
            issues.append(f"to_pandas pattern references index {idx} but only {len(deps)} deps")
            idx = 0
        return _xl_expr(idx, "true")

    # Newer CalcRange API first, then legacy DataFrame slicing, then bare data names.
    text = _TO_PANDAS_FALSE_RE.sub(to_pandas_false_repl, text)
    text = _TO_PANDAS_TRUE_RE.sub(to_pandas_true_repl, text)
    text = _DF_DATA_RE.sub(df_repl, text)

    # Token-position rewrite for data / data[i] / ranges[i] via AST when possible.
    rewritten, ast_issues = _rewrite_data_names_ast(text, deps, modes)
    issues.extend(ast_issues)
    if rewritten is not None:
        return rewritten, deps, issues

    # Regex fallback (data[i] / ranges[i]).
    def index_repl(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        if idx >= len(deps):
            issues.append(f"index [{idx}] with only {len(deps)} deps")
            return m.group(0)
        hm: HeaderMode = modes[idx] if idx < len(modes) else "omit"
        return _xl_expr(idx, hm)

    text = re.sub(r"\b(?:ranges|data)\[\s*(\d+)\s*\]", index_repl, text)

    # Bare ``data`` only when a single dep (multi-arg ``data`` is the ranges list).
    if len(deps) == 1:

        def bare_repl(_m: re.Match[str]) -> str:
            hm: HeaderMode = modes[0] if modes else "omit"
            return _xl_expr(0, hm)

        text = re.sub(r"(?<![\w.])data(?!\w)", bare_repl, text)

    return text, deps, issues


def _rewrite_data_names_ast(
    code: str,
    deps: list[str],
    modes: list[HeaderMode],
) -> tuple[str | None, list[str]]:
    """Rewrite ``data`` / ``data[i]`` / ``ranges[i]`` Name/Subscript nodes."""
    issues: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None, issues

    class _Hit:
        __slots__: ClassVar[tuple[str, ...]] = ("start", "end", "repl")
        start: int
        end: int
        repl: str

        def __init__(self, start: int, end: int, repl: str) -> None:
            self.start = start
            self.end = end
            self.repl = repl

    hits: list[_Hit] = []
    # Names that are already the target of ``data[...]`` / ``ranges[...]`` — skip bare rewrite.
    subscripted_data_names: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "data":
            subscripted_data_names.add(id(node.value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in ("data", "ranges"):
            sl = node.slice
            idx = None
            if isinstance(sl, ast.Constant) and isinstance(sl.value, int):
                idx = sl.value
            if idx is None:
                continue
            if idx >= len(deps):
                issues.append(f"{node.value.id}[{idx}] with only {len(deps)} deps")
                continue
            start = ast_source_offset(code, node.lineno, node.col_offset)
            end = ast_source_offset(code, node.end_lineno or node.lineno, node.end_col_offset or node.col_offset)
            if start < 0 or end < 0:
                continue
            hm: HeaderMode = modes[idx] if idx < len(modes) else "omit"
            hits.append(_Hit(start, end, _xl_expr(idx, hm)))
        elif isinstance(node, ast.Name) and node.id == "data" and isinstance(node.ctx, ast.Load):
            # Bare ``data`` is a CalcRange only for a single formula arg.
            if len(deps) != 1:
                continue
            if id(node) in subscripted_data_names:
                continue
            start = ast_source_offset(code, node.lineno, node.col_offset)
            end = ast_source_offset(code, node.end_lineno or node.lineno, node.end_col_offset or node.col_offset)
            if start < 0 or end < 0:
                continue
            hm0: HeaderMode = modes[0] if modes else "omit"
            hits.append(_Hit(start, end, _xl_expr(0, hm0)))

    if not hits:
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
    chunks = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        f'<pythonScripts xmlns="{_PY_NS}">\n',
    ]
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

    bindings_raw = meta.get("bindings")
    bindings: list[BindingInfo] = []
    if isinstance(bindings_raw, list):
        for b in bindings_raw:
            if isinstance(b, dict):
                raw = cast("dict[str, Any]", b)
                bindings.append(
                    BindingInfo(
                        a1=str(raw.get("a1") or ""),
                        header_mode=_as_header_mode(raw.get("header_mode") or "omit"),
                        role=_as_dep_role(raw.get("role") or "data"),
                        original_indices=list(raw.get("original_indices") or []),
                    )
                )

    # Header modes aligned to normalized data_args order
    modes: list[HeaderMode] = []
    if bindings:
        for b in bindings:
            if b.role == "ordering":
                continue
            modes.append(_as_header_mode(b.header_mode))
    while len(modes) < len(data_args):
        modes.append("omit")

    excel_code, deps, issues = rewrite_dag_code_to_excel(parts.code, data_args, header_modes=modes)
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
        converted=True,
        array_ref=array_ref,
    )
    assign_script_bank([out])
    return out


def convert_dag_cells_to_excel(
    formulas: Sequence[DagFormulaItem],
    *,
    return_type: int = 0,
    report_meta: dict[str, Any] | None = None,
) -> ConversionReport:
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
        report.cells.append(
            convert_dag_formula_to_excel(formula, sheet=sheet, cell=cell, return_type=return_type, meta=meta)
        )
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
                ConvertedCell(
                    sheet=cell.sheet,
                    cell=cell.cell,
                    direction="excel",
                    original_code=cell.converted_code or cell.original_code,
                    converted_code="",
                    issues=list(cell.issues) + ["skipped: not converted on DAG pass"],
                    converted=False,
                    return_type=cell.return_type,
                    array_ref=cell.array_ref,
                )
            )
            continue
        modes: list[HeaderMode] = []
        if cell.bindings:
            for b in cell.bindings:
                if b.role == "ordering":
                    continue
                modes.append(_as_header_mode(b.header_mode))
        while len(modes) < len(cell.data_args):
            modes.append("omit")
        excel_code, deps, issues = rewrite_dag_code_to_excel(
            cell.converted_code,
            list(cell.data_args),
            header_modes=modes,
        )
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
                converted=True,
                array_ref=cell.array_ref,
            )
        )
    assign_script_bank(out.cells)
    return out
