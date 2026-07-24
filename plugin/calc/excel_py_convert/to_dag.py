# SPDX-License-Identifier: GPL-3.0-or-later
"""Excel ``xl(%Pn%)`` → DAG-style ``data`` / ``data[i]`` (code + formula args).

What the converter does
-----------------------
Script/formula **shape** only — not a runtime. It does **not** rewrite
pandas/seaborn/plot logic.

Excel stores Python separately from the cell formula:

* ``xl/pythonScripts.xml`` — e.g. ``df = xl(%P2%, headers=True)``
* cell ``_xlfn._xlws.PY(scriptIndex, returnType, A1:B10, ...)`` — trailing args
  fill ``%P2%``, ``%P3%``, …

Bare ``%Pn%`` tokens are not valid Python, so before ``ast.parse`` we rewrite
them (outside strings/comments) to equal-length ``_Pn_`` sentinels. Call sites
are found only via AST; there is no regex ``xl(`` scanner.

Per cell we do two paired steps:

1. **Code:** rewrite each direct ``xl(...)`` *call* (AST positions) to ``data`` /
   ``data[i]`` / ``.to_pandas()`` for header modes. Unrelated source
   (strings, comments) is left intact where possible.
2. **Formula:** emit ``=PY("…rewritten…"; resolved_ranges)`` with deduplicated
   data args, then append **ordering-only** edges for prior PY cells in Excel
   workbook sheet/row order (shared kernel). Tables / ``ANCHORARRAY`` are
   snapped to A1 at convert time.

Fail-closed: unresolved deps, dynamic ``xl()``, or syntax errors leave the cell
unconverted (no ``dag_formula``) unless the caller opts into best-effort mode.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from plugin.calc.excel_py_convert.models import (
    BindingInfo,
    ConvertedCell,
    ConversionReport,
    ExcelPyCell,
    ExcelWorkbookModel,
    HeaderMode,
)
from plugin.calc.excel_py_convert.resolve_refs import ResolvedDep, resolve_deps

_P_TOKEN_RE = re.compile(r"^%P(\d+)%$", re.IGNORECASE)
# Bare Excel placeholder in source (not anchored); same length as ``_Pn_`` sentinel.
_P_BARE_RE = re.compile(r"%P(\d+)%", re.IGNORECASE)
# Equal-length stand-in so ``ast.parse`` accepts Excel scripts: ``%P2%`` → ``_P2_``.
_P_SENTINEL_RE = re.compile(r"^_P(\d+)_$", re.IGNORECASE)
_OBJECT_SUPPRESS = (
    "\n# excel_py: returnType=1 (Object) — cell value egress suppressed until object cards ship\n"
    "result = None"
)


@dataclass
class _XlCall:
    """One ``xl(...)`` call site in source."""

    start: int
    end: int
    p_num: int | None  # None → dynamic / literal / unsupported
    header_mode: HeaderMode
    literal: str | None = None
    dynamic: bool = False
    raw: str = ""


def _placeholder_to_data_index(p_num: int) -> int:
    """Map Excel ``%Pk%`` to 0-based original dep index: ``%P2%`` → 0, ``%P3%`` → 1."""
    return p_num - 2


def _data_expr(index: int, *, multi: bool) -> str:
    if index == 0:
        return "data"
    return f"inputs[{index}]"


def _headers_dataframe_expr(data_expr: str) -> str:
    return f"{data_expr}.to_pandas()"


def _no_headers_dataframe_expr(data_expr: str) -> str:
    return f"{data_expr}.to_pandas(header_row=None)"


def _header_mode_from_keywords(node: ast.Call) -> HeaderMode:
    for kw in node.keywords:
        if kw.arg and kw.arg.lower() == "headers":
            if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                return "true"
            if isinstance(kw.value, ast.Constant) and kw.value.value is False:
                return "false"
            if isinstance(kw.value, ast.Name) and kw.value.id in ("True", "False"):
                return "true" if kw.value.id == "True" else "false"
    return "omit"


def _skip_string(src: str, i: int) -> int:
    """Return index just past a string literal starting at *i* (quote char)."""
    quote = src[i]
    i += 1
    n = len(src)
    # Triple quotes
    if i + 1 < n and src[i] == quote and src[i + 1] == quote:
        i += 2
        while i + 2 < n:
            if src[i] == quote and src[i + 1] == quote and src[i + 2] == quote:
                return i + 3
            i += 1
        return n
    while i < n:
        # Escapes apply in both quote styles (needed so \" mid-string is not a closer).
        if src[i] == "\\":
            i += 2
            continue
        if src[i] == quote:
            return i + 1
        i += 1
    return n


def _normalize_excel_placeholders(src: str) -> str:
    """Rewrite bare ``%Pn%`` to equal-length ``_Pn_`` so ``ast.parse`` accepts Excel scripts.

    Placeholders inside strings and comments are left untouched so quoted
    ``xl("%P2%")`` stays a string constant on the AST path. Length is preserved
    (``%`` ↔ ``_``) so AST byte/character offsets still index the original source.
    """
    out: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if ch in ("'", '"'):
            end = _skip_string(src, i)
            out.append(src[i:end])
            i = end
            continue
        if ch == "#":
            j = i
            while j < n and src[j] != "\n":
                j += 1
            out.append(src[i:j])
            i = j
            continue
        m = _P_BARE_RE.match(src, i)
        if m:
            # ``%P12%`` and ``_P12_`` are the same length — offsets stay aligned.
            out.append(f"_P{m.group(1)}_")
            i = m.end()
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _p_num_from_arg(arg0: ast.AST) -> tuple[int | None, str | None, bool]:
    """Interpret ``xl`` first arg → ``(p_num, literal, dynamic)``."""
    if isinstance(arg0, ast.Name):
        # Sentinel from ``_normalize_excel_placeholders``: ``_P2_`` ↔ ``%P2%``.
        m = _P_SENTINEL_RE.match(arg0.id)
        if m:
            return int(m.group(1)), None, False
        # xl(name) / xl(P2) — not a formula-static placeholder.
        return None, None, True
    if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
        m = _P_TOKEN_RE.match(arg0.value)
        if m:
            return int(m.group(1)), None, False
        # literal xl("A1") without formula dep binding
        return None, arg0.value, True
    if isinstance(arg0, (ast.JoinedStr, ast.BinOp)):
        return None, None, True
    return None, None, True


def _find_xl_calls(code: str) -> tuple[list[_XlCall], list[str]]:
    """Locate direct ``xl(...)`` call expressions via AST after placeholder normalization."""
    issues: list[str] = []
    src = code or ""
    if not src.strip():
        return [], issues
    # Excel ``%Pn%`` is not valid Python; equal-length ``_Pn_`` lets us parse with AST.
    normalized = _normalize_excel_placeholders(src)
    try:
        tree = ast.parse(normalized)
    except SyntaxError as exc:
        # Fail closed — do not guess call sites with a hand-rolled scanner.
        issues.append(f"Python syntax error in script: {exc.msg}")
        return [], issues

    calls: list[_XlCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Direct ``xl(...)`` only — ``obj.xl(...)`` is not the Excel data bridge.
        if not isinstance(func, ast.Name) or func.id != "xl":
            continue
        if getattr(node, "lineno", None) is None:
            continue
        # Offsets are valid on *src*: placeholder rewrites keep the same length.
        start = _offset(src, node.lineno, node.col_offset)
        end = _offset(src, node.end_lineno or node.lineno, node.end_col_offset or node.col_offset)
        if start < 0 or end < 0 or end <= start:
            issues.append("xl() call without reliable source positions")
            continue
        header_mode = _header_mode_from_keywords(node)
        p_num: int | None = None
        literal: str | None = None
        dynamic = False
        if not node.args:
            dynamic = True
        else:
            p_num, literal, dynamic = _p_num_from_arg(node.args[0])
        calls.append(
            _XlCall(
                start=start,
                end=end,
                p_num=p_num,
                header_mode=header_mode,
                literal=literal,
                dynamic=dynamic,
                raw=src[start:end],
            )
        )
    calls.sort(key=lambda c: c.start)
    return calls, issues


def _offset(src: str, lineno: int, col: int) -> int:
    """Map AST ``(lineno, col_offset)`` to an absolute character index in *src*.

    On Python 3.8+, ``col_offset`` / ``end_col_offset`` are UTF-8 *byte* offsets
    within the line — not Unicode character indices. Convert before slicing *src*
    so a non-ASCII prefix cannot shift the rewrite window.
    """
    if lineno < 1 or col < 0:
        return -1
    lines = src.splitlines(keepends=True)
    if lineno > len(lines):
        return -1
    line_start = sum(len(lines[i]) for i in range(lineno - 1))
    line = lines[lineno - 1]
    raw = line.encode("utf-8")
    if col > len(raw):
        return -1
    # If *col* landed mid-codepoint, back up to a valid UTF-8 boundary.
    while col > 0 and col < len(raw) and (raw[col] & 0xC0) == 0x80:
        col -= 1
    return line_start + len(raw[:col].decode("utf-8"))


def rewrite_excel_code(
    code: str,
    *,
    num_deps: int,
    index_map: dict[int, int] | None = None,
) -> tuple[str, list[str], list[str], dict[int, HeaderMode]]:
    """Replace ``xl(...)`` call expressions only; leave the rest of the script intact.

    *index_map* maps original 0-based dep index → normalized ``data`` index after dedup.
    Returns ``(new_code, issues, used_original_indices, header_modes_by_original_index)``.
    """
    issues: list[str] = []
    src = code or ""
    calls, find_issues = _find_xl_calls(src)
    issues.extend(find_issues)

    used: set[int] = set()
    header_modes: dict[int, HeaderMode] = {}
    imap = index_map or {}

    for call in calls:
        if call.dynamic and call.p_num is None:
            issues.append("dynamic xl() reference (not a %Pn% placeholder)")
            continue
        if call.p_num is None:
            continue
        idx = _placeholder_to_data_index(call.p_num)
        if idx < 0:
            issues.append(f"invalid placeholder %P{call.p_num}%")
            continue
        if num_deps and idx >= num_deps:
            issues.append(f"%P{call.p_num}% has no matching formula dep (need {idx + 1} deps, have {num_deps})")
        used.add(idx)
        # First seen header mode wins for a given original index; conflict → warn.
        prev = header_modes.get(idx)
        if prev is None:
            header_modes[idx] = call.header_mode
        elif prev != call.header_mode and call.header_mode != "omit":
            issues.append(f"conflicting headers mode for %P{call.p_num}%: {prev} vs {call.header_mode}")

    # How many distinct normalized data slots will appear?
    norm_indices = sorted({imap.get(i, i) for i in used}) if imap else sorted(used)
    multi = len(norm_indices) > 1 or (num_deps > 1 and not imap)

    # Apply replacements from end → start so offsets stay valid.
    new_code = src
    for call in sorted(calls, key=lambda c: c.start, reverse=True):
        if call.dynamic and call.p_num is None:
            continue
        if call.p_num is None:
            continue
        orig_idx = _placeholder_to_data_index(call.p_num)
        if orig_idx < 0:
            continue
        norm_idx = imap.get(orig_idx, orig_idx)
        # multi relative to final normalized data arity
        expr_multi = multi if imap else (num_deps > 1 or len(used) > 1)
        expr = _data_expr(norm_idx, multi=expr_multi)
        hm = call.header_mode
        if hm == "true":
            repl = _headers_dataframe_expr(expr)
        elif hm == "false":
            repl = _no_headers_dataframe_expr(expr)
        else:
            # omitted → bare CalcRange; scripts that need a DataFrame call to_pandas()
            repl = expr
        new_code = new_code[: call.start] + repl + new_code[call.end :]

    return new_code, issues, [str(i) for i in sorted(used)], header_modes


def _excel_execution_order(model: ExcelWorkbookModel) -> list[ExcelPyCell]:
    """Workbook sheet order, then row, then column (Excel's documented PY order)."""
    order_map = model.sheet_order_map()
    # Unknown sheets sort after known ones, stable by first appearance.
    unknown: dict[str, int] = {}

    def sheet_key(title: str) -> int:
        if title in order_map:
            return order_map[title]
        if title not in unknown:
            unknown[title] = len(order_map) + len(unknown)
        return unknown[title]

    cells = list(model.cells)
    cells.sort(key=lambda c: (sheet_key(c.sheet), c.row or 10**9, c.col or 10**9, c.cell))
    return cells


def _cell_addr(cell: ExcelPyCell) -> str:
    """Sheet-qualified A1 for cross-sheet ordering edges."""
    return f"{cell.sheet}!{cell.cell}" if cell.sheet else cell.cell


def _normalize_bindings(
    resolved: list[ResolvedDep],
    header_modes: dict[int, HeaderMode],
) -> tuple[list[BindingInfo], dict[int, int], list[str], list[str]]:
    """Deduplicate resolved A1s; map original indices → normalized data indices.

    Returns ``(bindings, index_map, data_args, issues)``. Unresolved deps produce
    issues and an empty a1 — caller must fail-closed.
    """
    issues: list[str] = []
    bindings: list[BindingInfo] = []
    index_map: dict[int, int] = {}
    a1_to_norm: dict[str, int] = {}
    data_args: list[str] = []

    for orig_i, r in enumerate(resolved):
        if r.kind == "unresolved" or not r.a1:
            issues.append(r.note or f"unresolved {r.original}")
            # Keep positional integrity until reject — do not shift later indices.
            continue
        key = r.a1
        if key in a1_to_norm:
            norm = a1_to_norm[key]
            index_map[orig_i] = norm
            bindings[norm].original_indices.append(orig_i)
            # Prefer explicit headers=True over omit/false when merging.
            hm = header_modes.get(orig_i, "omit")
            if hm == "true":
                bindings[norm].header_mode = "true"
            continue
        norm = len(data_args)
        a1_to_norm[key] = norm
        index_map[orig_i] = norm
        data_args.append(key)
        bindings.append(
            BindingInfo(
                a1=key,
                header_mode=header_modes.get(orig_i, "omit"),
                role="data",
                original_indices=[orig_i],
            )
        )
    return bindings, index_map, data_args, issues


def convert_cell_to_dag(
    model: ExcelWorkbookModel,
    cell: ExcelPyCell,
    *,
    prior_in_order: list[ExcelPyCell] | None = None,
    best_effort: bool = False,
) -> ConvertedCell:
    """Convert one Excel PY cell: rewrite ``xl`` in code + attach ranges on ``=PY``."""
    base = ConvertedCell(
        sheet=cell.sheet,
        cell=cell.cell,
        direction="dag",
        original_code="",
        converted_code="",
        return_type=cell.return_type,
        array_ref=cell.array_ref,
        script_index=cell.script_index,
        converted=False,
    )
    if cell.script_index < 0 or cell.script_index >= len(model.scripts):
        base.issues = [f"script_index {cell.script_index} out of range ({len(model.scripts)} scripts)"]
        return base

    original = model.scripts[cell.script_index]
    base.original_code = original

    resolved = resolve_deps(cell.deps, model, sheet_hint=cell.sheet)
    # Discover header modes from xl() calls against original arity (no index remap yet).
    _code0, rewrite_issues0, _used0, header_modes = rewrite_excel_code(original, num_deps=len(cell.deps))
    bindings, index_map, data_args, bind_issues = _normalize_bindings(resolved, header_modes)
    issues: list[str] = list(bind_issues)

    snapshot_notes = [r.note for r in resolved if r.kind in ("table_snapshot", "anchor_snapshot") and r.note]

    unresolved = len(index_map) != len(cell.deps)
    dynamic = any("dynamic xl()" in i for i in rewrite_issues0)
    # Syntax errors are always fatal now that placeholders are normalized for AST
    # (previously ``%P`` scripts skipped AST and used a regex scanner).
    syntax_fatal = any("syntax error" in i for i in rewrite_issues0)

    # Second rewrite with dedup index map when every original dep resolved.
    if unresolved:
        issues.append("unresolved or dropped dependency; refusing to emit shifted data indices")
        new_code = original
        rewrite_issues = list(rewrite_issues0)
    else:
        new_code, rewrite_issues, _used, _hm2 = rewrite_excel_code(
            original,
            num_deps=len(cell.deps),
            index_map=index_map if index_map else None,
        )
        issues.extend(i for i in rewrite_issues if i not in issues)
        dynamic = dynamic or any("dynamic xl()" in i for i in rewrite_issues)

    # Ordering-only: immediate previous PY cell in Excel workbook sheet/row order.
    prior = prior_in_order or []
    ordering_args: list[str] = []
    if prior:
        prev = prior[-1]
        addr = prev.cell if prev.sheet == cell.sheet else _cell_addr(prev)
        if addr not in data_args:
            ordering_args.append(addr)
            issues.append("added prior PY stage as a DAG ordering edge (shared kernel)")

    shared_kernel = bool(prior) or (not cell.deps and "xl(" not in original.replace(" ", ""))

    if cell.return_type == 1:
        new_code = (new_code or "") + _OBJECT_SUPPRESS
        issues.append("returnType=1 (Object): suppressed cell value egress (shared object kept in script)")

    fatal = unresolved or dynamic or syntax_fatal

    if fatal and not best_effort:
        base.converted_code = original
        base.data_args = data_args
        base.ordering_args = ordering_args
        base.bindings = bindings
        base.issues = list(dict.fromkeys(issues + (["dynamic xl()"] if dynamic else []) + (["unresolved dependency"] if unresolved else [])))
        base.shared_kernel = shared_kernel
        base.snapshot_deps = snapshot_notes
        base.dag_formula = ""
        base.converted = False
        return base

    dag_formula = ""
    if new_code is not None:
        from plugin.calc.excel_py_convert.script_bank import formula_for_converted_cell

        # Placeholder ConvertedCell for formula builder (fields already on base below).
        base.converted_code = new_code
        base.data_args = data_args
        base.ordering_args = ordering_args
        dag_formula = formula_for_converted_cell(base, separator=";", use_script_bank=True)

    base.converted_code = new_code
    base.data_args = data_args
    base.ordering_args = ordering_args
    base.bindings = bindings
    base.dag_formula = dag_formula
    base.issues = list(dict.fromkeys(issues))
    base.shared_kernel = shared_kernel
    base.snapshot_deps = snapshot_notes
    base.converted = True
    return base


def convert_model_to_dag(model: ExcelWorkbookModel, *, best_effort: bool = False) -> ConversionReport:
    """Convert every PY cell in *model* to DAG-style ``=PY`` formulas."""
    report = ConversionReport(direction="dag", source_path=model.source_path)
    if not model.scripts:
        report.issues.append("no pythonScripts found")
    ordered = _excel_execution_order(model)
    prior: list[ExcelPyCell] = []
    # Convert in execution order so ordering edges follow Excel.
    converted_by_key: dict[tuple[str, str], ConvertedCell] = {}
    for cell in ordered:
        converted = convert_cell_to_dag(model, cell, prior_in_order=prior, best_effort=best_effort)
        converted_by_key[(cell.sheet, cell.cell)] = converted
        prior.append(cell)
    # Preserve original model.cells order in the report for stable fixtures.
    for cell in model.cells:
        report.cells.append(converted_by_key[(cell.sheet, cell.cell)])
    if not report.ok:
        report.issues.append("one or more cells failed conversion (fail-closed)")
    return report
