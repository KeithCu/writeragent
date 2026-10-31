# SPDX-License-Identifier: GPL-3.0-or-later
"""Excel ``xl(%Pn%)`` → runnable DAG ``xl("%Pn%")`` + formula range args.

What the converter does
-----------------------
Script/formula **shape** only — not a runtime. It does **not** rewrite
pandas/seaborn/plot logic.

Excel stores Python separately from the cell formula:

* ``xl/pythonScripts.xml`` — e.g. ``df = xl(%P2%, headers=True)``
  (``%P2%`` = first trailing dep; ``headers=`` is an ``xl()`` kwarg, not a
  ``_xlws.PY`` formula arg; arg 2 of ``_xlws.PY`` is returnType)
* cell ``_xlfn._xlws.PY(scriptIndex, returnType, A1:B10, ...)`` — trailing args
  fill ``%P2%``, ``%P3%``, …

Microsoft does not productize true dynamic ``xl(variable)`` / ``xl(f"…")``;
those fail closed here as defense and because they are not DAG-safe.

Bare ``%Pn%`` tokens are not valid Python, so before ``ast.parse`` we rewrite
them (outside strings/comments) to equal-length ``_Pn_`` sentinels. Call sites
are found only via AST; there is no regex ``xl(`` scanner.

Per cell we do two paired steps:

1. **Code:** keep ``xl(...)`` call sites; emit runnable ``xl("%Pn%", …)`` string
   refs (remap indices after dep dedup). The sandbox injects a binding-only
   ``xl`` that looks up formula ``data`` / ``ranges`` ranges — see :mod:`plugin.scripting.excel_xl`.
2. **Formula:** emit ``=PY("…"; resolved_ranges)`` with deduplicated data args
   only (every trailing arg is a real binding). The converter does **not**
   append prior PY cells for shared-kernel order — enable shared session and
   manage run order separately. Tables / ``ANCHORARRAY`` are snapped to A1 at
   convert time.

Fail-closed: unresolved deps, dynamic ``xl()``, or syntax errors leave the cell
unconverted (no ``dag_formula``) unless the caller opts into best-effort mode.

Statement-form ``xl("%Pn%")`` (including under ``if``) is left in place — the
sandbox binding shim makes those calls valid. Unsupported literals / dynamics
fail closed rather than being silently stripped.
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
from plugin.calc.excel_py_convert.resolve_refs import ResolvedDep, resolve_dep
from plugin.calc.excel_py_convert.script_bank import formula_for_converted_cell
from plugin.calc.python.xl_static_rewrite import iter_xl_calls
from plugin.framework.ast_stmt_edit import ast_source_offset, line_starts_exact
from plugin.framework.deal_shim import (
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_PLACEHOLDER_INDEX,
    DEAL_MAX_SOURCE,
    DEAL_MAX_XL_EXPR,
    ascii_bounded,
    deal,
    inverse_ensure,
    str_bounded,
)

_P_TOKEN_RE = re.compile(r"^%P(\d+)%$", re.IGNORECASE)
# Bare Excel placeholder in source (not anchored); same length as ``_Pn_`` sentinel.
_P_BARE_RE = re.compile(r"%P(\d+)%", re.IGNORECASE)
# Equal-length stand-in so ``ast.parse`` accepts Excel scripts: ``%P2%`` → ``_P2_``.
_P_SENTINEL_RE = re.compile(r"^_P(\d+)_$", re.IGNORECASE)
_OBJECT_SUPPRESS = "\n# excel_py: returnType=1 (Object) — cell value egress suppressed until object cards ship\nresult = None"


@dataclass
class _XlCall:
    """One ``xl(...)`` call site in source."""

    start: int
    end: int
    p_num: int | None  # None → dynamic / literal / unsupported
    header_mode: str  # HeaderMode values; str so CrossHair can proxy (not Literal)


@dataclass
class _XlAnalysis:
    """AST analysis of all xl() call sites in a script."""

    calls: list[_XlCall]
    issues: list[str]
    header_modes: dict[int, str]
    fatal: bool


_RESOLVED_KINDS = frozenset(("range", "unresolved", "table_snapshot", "anchor_snapshot"))


def _deal_excel_src_ok(src: object) -> bool:
    return str_bounded(src, DEAL_MAX_SOURCE)


def _deal_convert_scripts_ok(model: object) -> bool:
    # Scripts share _deal_excel_src_ok with _find_xl_calls.
    scripts = getattr(model, "scripts", None)
    return isinstance(scripts, list) and len(scripts) <= DEAL_MAX_CMD_ARGS and all(_deal_excel_src_ok(s) for s in scripts)


def _deal_convert_cell_ok(cell: object) -> bool:
    """Cell fields ``convert_cell_to_dag`` requires; script_index range is body-checked."""
    script_index = getattr(cell, "script_index", None)
    deps = getattr(cell, "deps", None)
    return type(script_index) is int and isinstance(deps, list) and len(deps) <= DEAL_MAX_CMD_ARGS and all(isinstance(d, str) and ascii_bounded(d, DEAL_MAX_SOURCE) for d in deps)


@deal.pre(lambda p_num, *_unused, **__: isinstance(p_num, int) and 2 <= p_num <= 2 + DEAL_MAX_PLACEHOLDER_INDEX)
@deal.post(lambda result: isinstance(result, int) and 0 <= result <= DEAL_MAX_PLACEHOLDER_INDEX)
def _placeholder_to_data_index(p_num: int) -> int:
    """Map Excel ``%Pk%`` to 0-based original dep index: ``%P2%`` → 0, ``%P3%`` → 1."""
    return p_num - 2


@deal.pre(lambda index, *_unused, **__: isinstance(index, int) and 0 <= index <= DEAL_MAX_PLACEHOLDER_INDEX)
@deal.post(lambda result: isinstance(result, str) and result.startswith("xl(") and result.endswith(")") and '"%P' in result and len(result) <= DEAL_MAX_XL_EXPR)
def _xl_binding_expr(index: int, header_mode: str) -> str:
    """Runnable DAG ``xl("%Pn%", …)`` (quoted token; MS package uses bare ``%Pn%``)."""
    tok = f'"%P{index + 2}%"'
    # Match Microsoft samples: no space after the comma in ``headers=…``.
    suffix = {"true": ",headers=True", "false": ",headers=False"}.get(header_mode, "")
    return f"xl({tok}{suffix})"


def _header_mode_from_keywords(node: ast.Call) -> tuple[HeaderMode, str | None]:
    """Extract HeaderMode from call keywords or return an error issue if invalid."""
    # A non-constant headers argument (headers=flag, headers=1) and any
    # extra keyword are fatal. Looking only for a constant headers= and
    # returning "omit" strips the rest of the call.
    header_mode: HeaderMode = "omit"
    for kw in node.keywords:
        if kw.arg is None:
            return "omit", "unsupported **kwargs in xl() call"
        if kw.arg != "headers":
            return "omit", f"unsupported keyword argument '{kw.arg}' in xl() call"
        if not (isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, bool)):
            return "omit", "xl() headers argument must be a True or False constant"
        header_mode = "true" if kw.value.value is True else "false"
    return header_mode, None


@deal.pre(lambda src, i, *_unused, **__: str_bounded(src, DEAL_MAX_SOURCE) and type(i) is int and 0 <= i < len(src))
def _skip_string(src: str, i: int) -> int:
    """Return index just past a string literal starting at *i* (quote char)."""
    quote = src[i]
    i += 1
    n = len(src)
    # Triple quotes
    if i + 1 < n and src[i] == quote and src[i + 1] == quote:
        i += 2
        # Skip an escaped character inside a triple-quoted string before
        # looking for the closer. Three quotes after a backslash are not
        # the end of the string.
        while i + 2 < n:
            if src[i] == "\\":
                i += 2
                continue
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


@deal.pre(lambda src, *_unused, **__: _deal_excel_src_ok(src))
@inverse_ensure(lambda *args, result="", **kwargs: len(result) == len(args[0]))
def _normalize_excel_placeholders(src: str) -> str:
    # crosshair: off
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


def _p_num_from_arg(arg0: ast.AST) -> int | None:
    """Interpret ``xl`` first arg → p_num or None (sentinel ``_Pn_`` or quoted ``%Pn%``)."""
    if isinstance(arg0, ast.Name):
        text, pattern = arg0.id, _P_SENTINEL_RE
    elif isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
        text, pattern = arg0.value, _P_TOKEN_RE
    else:
        return None
    matched = pattern.match(text)
    return int(matched.group(1)) if matched else None


def format_python_syntax_issue(exc: BaseException) -> str:
    """Format an AST parse exception as a stable issue string with line/col location."""
    msg = getattr(exc, "msg", str(exc))
    lineno = getattr(exc, "lineno", None)
    offset = getattr(exc, "offset", None)
    loc = f"line {lineno}" if lineno is not None else "unknown line"
    if offset is not None:
        loc = f"{loc}:{offset}"
    return f"Python syntax error at {loc}: {msg}"


@deal.pre(lambda code, *_unused, **__: _deal_excel_src_ok(code or ""))
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
    except (SyntaxError, TypeError, MemoryError, RecursionError) as exc:
        # Fail closed — do not guess call sites with a hand-rolled scanner.
        issues.append(format_python_syntax_issue(exc))
        return [], issues

    line_starts = line_starts_exact(src)
    calls: list[_XlCall] = []
    for node, start, end in iter_xl_calls(tree, src, line_starts, issues):
        header_mode, kw_err = _header_mode_from_keywords(node)
        if kw_err:
            issues.append(kw_err)

        # xl() takes one positional. xl(%P2%, True) fails the conversion.
        # Inspecting only the first argument drops the rest.
        if len(node.args) > 1:
            issues.append("xl() does not accept positional arguments beyond the first")

        p_num = _p_num_from_arg(node.args[0]) if node.args else None
        calls.append(_XlCall(start=start, end=end, p_num=p_num, header_mode=header_mode))

    # Overlapping xl() spans fail the conversion. Rewriting the inner call
    # first (xl(%P2%, headers=xl(%P3%))) changes the source length, so the
    # outer end offset is stale.
    calls.sort(key=lambda c: (c.start, -c.end))
    for idx in range(len(calls) - 1):
        if calls[idx + 1].start < calls[idx].end:
            issues.append("nested or overlapping xl() calls are not supported")
            break

    for name_node in ast.walk(tree):
        if not (isinstance(name_node, ast.Name) and _P_SENTINEL_RE.match(name_node.id)):
            continue
        off = ast_source_offset(src, name_node.lineno, name_node.col_offset, line_starts=line_starts)
        if off < 0:
            issues.append("placeholder token without reliable source position")
            continue
        token = src[off : off + len(name_node.id)]
        if token.startswith(("%P", "%p")):
            if not any(c.p_num is not None and c.start <= off < c.end for c in calls):
                issues.append(f"placeholder {token} outside xl()")
        else:
            if any(c.p_num is not None and c.start <= off < c.end for c in calls):
                issues.append(f"xl() argument {name_node.id} collides with the placeholder sentinel; rename it")

    return calls, issues


def analyze_xl_calls(code: str, *, num_deps: int) -> _XlAnalysis:
    """Perform AST analysis and validation of xl() calls, extracting header modes and fatal issues."""
    calls, find_issues = _find_xl_calls(code)
    issues = list(find_issues)
    fatal = bool(find_issues)

    header_modes: dict[int, str] = {}

    for call in calls:
        if call.p_num is None:
            issues.append("dynamic xl() reference (not a %Pn% placeholder)")
            fatal = True
            continue
        # Check p_num before _placeholder_to_data_index. Its precondition
        # requires 2 <= p_num <= 2 + DEAL_MAX_PLACEHOLDER_INDEX, so %P0%,
        # %P1%, and a too-large %Pn% raise PreconditionFailed under deal,
        # or stay as a bare placeholder without it.
        if call.p_num < 2 or call.p_num > 2 + DEAL_MAX_PLACEHOLDER_INDEX:
            issues.append(f"invalid placeholder %P{call.p_num}% (must be %P2%..%P{2 + DEAL_MAX_PLACEHOLDER_INDEX}%)")
            fatal = True
            continue
        idx = _placeholder_to_data_index(call.p_num)
        # idx >= num_deps is fatal even when num_deps is 0. Guarding that
        # test with ``if num_deps`` skipped the empty-deps case, and
        # convert_cell_to_dag never marked a missing formula dep fatal.
        if idx >= num_deps:
            issues.append(f"%P{call.p_num}% has no matching formula dep (need {idx + 1} deps, have {num_deps})")
            fatal = True
            continue
        # First seen header mode wins for a given original index; conflict → warn.
        prev = header_modes.get(idx)
        if prev is None:
            header_modes[idx] = call.header_mode
        elif prev != call.header_mode and call.header_mode != "omit":
            issues.append(f"conflicting headers mode for %P{call.p_num}%: {prev} vs {call.header_mode}")

    return _XlAnalysis(calls=calls, issues=issues, header_modes=header_modes, fatal=fatal)


def apply_rewrite(src: str, calls: list[_XlCall], *, index_map: dict[int, int]) -> str:
    """Rewrite binding call sites in *src* to quoted xl("%Pn%", ...) with remapped indices."""
    new_code = src
    for call in sorted(calls, key=lambda c: c.start, reverse=True):
        if call.p_num is None or not 2 <= call.p_num <= 2 + DEAL_MAX_PLACEHOLDER_INDEX:
            continue
        orig_idx = _placeholder_to_data_index(call.p_num)
        norm_idx = index_map.get(orig_idx, orig_idx)
        repl = _xl_binding_expr(norm_idx, call.header_mode)
        new_code = new_code[: call.start] + repl + new_code[call.end :]
    return new_code


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


@deal.pre(lambda current, candidate: ascii_bounded(current or "", DEAL_MAX_SOURCE) and ascii_bounded(candidate or "", DEAL_MAX_SOURCE))
def _prefer_excel_dep_token(current: str, candidate: str) -> str:
    # crosshair: off
    """When merging deps that snap to the same A1, keep the Excel-native token if any.

    See ``plugin.calc.excel_py_convert.models`` module doc; fidelity only, not Calc semantics.
    """
    cand = (candidate or "").strip()
    cur = (current or "").strip()
    if not cand:
        return cur
    if "[#" in cand or "ANCHORARRAY" in cand.upper():
        return cand
    return cur or cand


@deal.pre(
    lambda resolved, header_modes: (
        type(resolved) is list
        and len(resolved) <= DEAL_MAX_CMD_ARGS
        and all(isinstance(r, ResolvedDep) and r.kind in _RESOLVED_KINDS and str_bounded(r.note or "", DEAL_MAX_SOURCE) and ascii_bounded(r.a1, DEAL_MAX_SOURCE) and (r.original is None or ascii_bounded(r.original, DEAL_MAX_SOURCE)) for r in resolved)
        and type(header_modes) is dict
        and len(header_modes) <= DEAL_MAX_CMD_ARGS
        and all(type(k) is int and 0 <= k <= DEAL_MAX_CMD_ARGS and isinstance(v, str) and v in ("omit", "true", "false") for k, v in header_modes.items())
    )
)
def _normalize_bindings(resolved: list[ResolvedDep], header_modes: dict[int, str]) -> tuple[list[BindingInfo], dict[int, int], list[str], list[str]]:
    """Deduplicate resolved A1s; map original indices → normalized data indices.

    Returns ``(bindings, index_map, excel_deps, issues)``.
    ``excel_deps`` is parallel to ``bindings`` (original Excel tokens for export fidelity).
    Unresolved deps produce issues and an empty a1 — caller must fail-closed.
    """
    # crosshair: off
    issues: list[str] = []
    bindings: list[BindingInfo] = []
    index_map: dict[int, int] = {}
    a1_to_norm: dict[str, int] = {}
    excel_deps: list[str] = []

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
            excel_deps[norm] = _prefer_excel_dep_token(excel_deps[norm], r.original)
            # Prefer explicit headers=True over omit/false when merging.
            hm = header_modes.get(orig_i, "omit")
            if hm == "true":
                bindings[norm].header_mode = "true"
            continue
        norm = len(bindings)
        a1_to_norm[key] = norm
        index_map[orig_i] = norm
        excel_deps.append((r.original or key).strip() or key)
        bindings.append(BindingInfo(a1=key, header_mode=header_modes.get(orig_i, "omit"), role="data", original_indices=[orig_i]))
    return bindings, index_map, excel_deps, issues


@deal.pre(lambda model, cell, *_unused, **__: _deal_convert_scripts_ok(model) and _deal_convert_cell_ok(cell))
def convert_cell_to_dag(model: ExcelWorkbookModel, cell: ExcelPyCell, *, has_prior: bool = False, best_effort: bool = False) -> ConvertedCell:
    # crosshair: off
    """Convert one Excel PY cell: rewrite ``xl`` in code + attach ranges on ``=PY``."""
    if cell.script_index < 0 or cell.script_index >= len(model.scripts):
        return ConvertedCell(
            sheet=cell.sheet,
            cell=cell.cell,
            direction="dag",
            original_code="",
            converted_code="",
            return_type=cell.return_type,
            array_ref=cell.array_ref,
            script_index=cell.script_index,
            converted=False,
            issues=[f"script_index {cell.script_index} out of range ({len(model.scripts)} scripts)"],
        )

    original = model.scripts[cell.script_index]
    resolved = [resolve_dep(d, model, sheet_hint=cell.sheet) for d in cell.deps]

    # Parse once: analyze xl() calls, placeholders, header modes, and fatal errors in one pass.
    analysis = analyze_xl_calls(original, num_deps=len(cell.deps))
    bindings, index_map, excel_deps, bind_issues = _normalize_bindings(resolved, analysis.header_modes)
    data_args = [b.a1 for b in bindings]

    issues = list(bind_issues)
    issues.extend(analysis.issues)

    snapshot_notes = [r.note for r in resolved if r.kind in ("table_snapshot", "anchor_snapshot") and r.note]

    fatal = analysis.fatal or bool(bind_issues)

    # Second rewrite step with dedup index map when all deps resolved and no fatal issues.
    # Merge every analysis issue. When deps are unresolved, dropping
    # analyze_xl_calls issues hides syntax errors and bad placeholders.
    # In best_effort mode say that placeholder remapping was skipped. The
    # old warning claimed shifted data indices were refused in both modes.
    if bind_issues:
        if best_effort:
            issues.append("unresolved or dropped dependency; skipped placeholder remapping in best-effort mode")
        else:
            issues.append("unresolved or dropped dependency; refusing to emit shifted data indices")
    if fatal:
        new_code = original
    else:
        new_code = apply_rewrite(original, analysis.calls, index_map=index_map)

    # Advisory only: multi-cell Excel workbooks often need shared-kernel mode.
    # We do not inject prior-PY formula args for Calc ordering.
    shared_kernel = has_prior or (not cell.deps and "xl(" not in original.replace(" ", ""))
    if has_prior:
        issues.append("shared-kernel workbook: enable shared session; converter does not add order edges")

    if cell.return_type == 1:
        new_code = new_code + _OBJECT_SUPPRESS
        issues.append("returnType=1 (Object): suppressed cell value egress (shared object kept in script)")

    failed = fatal and not best_effort
    out = ConvertedCell(
        sheet=cell.sheet,
        cell=cell.cell,
        direction="dag",
        original_code=original,
        converted_code=original if failed else new_code,
        return_type=cell.return_type,
        array_ref=cell.array_ref,
        script_index=cell.script_index,
        converted=not failed,
        data_args=data_args,
        excel_deps=excel_deps,
        bindings=bindings,
        issues=issues,
        shared_kernel=shared_kernel,
        snapshot_deps=snapshot_notes,
    )
    if not failed:
        try:
            out.dag_formula = formula_for_converted_cell(out, separator=";")
        except ValueError as exc:
            out.converted = False
            out.converted_code = original
            out.issues.append(str(exc))
    out.issues = list(dict.fromkeys(out.issues))
    return out


@deal.pre(
    lambda model, *_unused, **__: (
        _deal_convert_scripts_ok(model)
        and isinstance(model.cells, list)
        and len(model.cells) <= DEAL_MAX_CMD_ARGS
        # convert_cell_to_dag's cell.deps bound is tighter than this wrapper used to be
        # (empty scripts + a 3-dep cell passed here, then PreconditionFailed inside).
        and all(_deal_convert_cell_ok(c) for c in model.cells)
    )
)
def convert_model_to_dag(model: ExcelWorkbookModel, *, best_effort: bool = False) -> ConversionReport:
    """Convert every PY cell in *model* to DAG-style ``=PY`` formulas."""
    report = ConversionReport(direction="dag", source_path=model.source_path)
    if model.issues:
        report.issues.extend(model.issues)
    if not model.scripts:
        report.issues.append("no pythonScripts found")
    ordered = _excel_execution_order(model)
    # Convert in Excel sheet/row order so shared_kernel advisory matches stage order.
    converted_by_key: dict[tuple[str, str], ConvertedCell] = {}
    for index, cell in enumerate(ordered):
        converted = convert_cell_to_dag(model, cell, has_prior=index > 0, best_effort=best_effort)
        converted_by_key[(cell.sheet, cell.cell)] = converted
    # Preserve original model.cells order in the report for stable fixtures.
    for cell in model.cells:
        report.cells.append(converted_by_key[(cell.sheet, cell.cell)])
    if not report.ok:
        report.issues.append("one or more cells failed conversion (fail-closed)")
    return report
