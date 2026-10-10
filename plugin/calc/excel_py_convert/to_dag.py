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
from dataclasses import dataclass, field

from plugin.calc.excel_py_convert.models import (
    BindingInfo,
    ConvertedCell,
    ConversionReport,
    ExcelPyCell,
    ExcelWorkbookModel,
    HeaderMode,
)
from plugin.calc.excel_py_convert.resolve_refs import ResolvedDep, resolve_deps
from plugin.calc.excel_py_convert.script_bank import formula_for_converted_cell
from plugin.calc.excel_py_convert.to_dag_contracts import (
    _DEAL_BINDING_A1_LEN,
    _DEAL_CONVERT_LIST,
    _DEAL_NOTE_LEN,
    _DEAL_RESOLVED_LEN,
    _DEAL_REWRITE_SRC,
    _RESOLVED_KINDS,
    _deal_convert_cell_ok,
    _deal_convert_scripts_ok,
    _deal_excel_src_ok,
    _deal_note_ok,
)
from plugin.doc.text_helpers import ast_source_offset, line_starts_exact
from plugin.framework.deal_shim import (
    DEAL_MAX_PLACEHOLDER_INDEX,
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
    literal: str | None = None
    dynamic: bool = False
    raw: str = ""


@dataclass
class RewriteResult:
    """Outcome of rewriting Excel xl() bindings for DAG."""

    code: str
    issues: list[str]
    header_modes: dict[int, str]
    fatal: bool = False
    used: list[str] = field(default_factory=list)

    def __iter__(self):
        """Enable tuple unpacking (code, issues, used, header_modes) for backward compatibility."""
        return iter((self.code, self.issues, self.used, self.header_modes))


@dataclass
class _XlAnalysis:
    """AST analysis of all xl() call sites in a script."""

    calls: list[_XlCall]
    issues: list[str]
    header_modes: dict[int, str]
    used: set[int]
    fatal: bool


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
    if header_mode == "true":
        return f"xl({tok},headers=True)"
    if header_mode == "false":
        return f"xl({tok},headers=False)"
    return f"xl({tok})"


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


@deal.pre(lambda src, i, *_unused, **__: str_bounded(src, _DEAL_REWRITE_SRC) and type(i) is int and 0 <= i < len(src))
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
    # regex/char-walk on symbolic src; 82331 examples / 119m on cover-all 32987767383 after alphabet pre.
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
        msg = getattr(exc, "msg", str(exc))
        lineno = getattr(exc, "lineno", None)
        offset = getattr(exc, "offset", None)
        loc = f"line {lineno}" if lineno is not None else "unknown line"
        if offset is not None:
            loc = f"{loc}:{offset}"
        issues.append(f"Python syntax error at {loc}: {msg}")
        return [], issues

    line_starts = line_starts_exact(src)
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
        start = ast_source_offset(src, node.lineno, node.col_offset, line_starts=line_starts)
        end = ast_source_offset(src, node.end_lineno or node.lineno, node.end_col_offset or node.col_offset, line_starts=line_starts)
        if start < 0 or end < 0 or end <= start:
            issues.append("xl() call without reliable source positions")
            continue

        header_mode, kw_err = _header_mode_from_keywords(node)
        if kw_err:
            issues.append(kw_err)

        # xl() takes one positional. xl(%P2%, True) fails the conversion.
        # Inspecting only the first argument drops the rest.
        if len(node.args) > 1:
            issues.append("xl() does not accept positional arguments beyond the first")

        p_num: int | None = None
        literal: str | None = None
        dynamic = False
        if not node.args:
            dynamic = True
        else:
            p_num, literal, dynamic = _p_num_from_arg(node.args[0])
        calls.append(_XlCall(start=start, end=end, p_num=p_num, header_mode=header_mode, literal=literal, dynamic=dynamic, raw=src[start:end]))

    # Overlapping xl() spans fail the conversion. Rewriting the inner call
    # first (xl(%P2%, headers=xl(%P3%))) changes the source length, so the
    # outer end offset is stale.
    calls.sort(key=lambda c: (c.start, -c.end))
    for idx in range(len(calls) - 1):
        if calls[idx + 1].start < calls[idx].end:
            issues.append("nested or overlapping xl() calls are not supported")
            break

    return calls, issues


def analyze_xl_calls(code: str, *, num_deps: int) -> _XlAnalysis:
    """Perform AST analysis and validation of xl() calls, extracting header modes and fatal issues."""
    calls, find_issues = _find_xl_calls(code)
    issues = list(find_issues)
    fatal = bool(find_issues)

    used: set[int] = set()
    header_modes: dict[int, str] = {}

    for call in calls:
        if call.dynamic and call.p_num is None:
            issues.append("dynamic xl() reference (not a %Pn% placeholder)")
            fatal = True
            continue
        if call.p_num is None:
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
        used.add(idx)
        # First seen header mode wins for a given original index; conflict → warn.
        prev = header_modes.get(idx)
        if prev is None:
            header_modes[idx] = call.header_mode
        elif prev != call.header_mode and call.header_mode != "omit":
            issues.append(f"conflicting headers mode for %P{call.p_num}%: {prev} vs {call.header_mode}")

    return _XlAnalysis(calls=calls, issues=issues, header_modes=header_modes, used=used, fatal=fatal)


def apply_rewrite(src: str, calls: list[_XlCall], *, index_map: dict[int, int] | None = None) -> str:
    """Rewrite binding call sites in *src* to quoted xl("%Pn%", ...) with remapped indices."""
    imap = index_map or {}
    new_code = src
    for call in sorted(calls, key=lambda c: c.start, reverse=True):
        if call.dynamic and call.p_num is None:
            continue
        if call.p_num is None:
            continue
        if call.p_num < 2 or call.p_num > 2 + DEAL_MAX_PLACEHOLDER_INDEX:
            continue
        orig_idx = _placeholder_to_data_index(call.p_num)
        norm_idx = imap.get(orig_idx, orig_idx)
        repl = _xl_binding_expr(norm_idx, call.header_mode)
        new_code = new_code[: call.start] + repl + new_code[call.end :]
    return new_code


@deal.pre(
    lambda code, num_deps, index_map=None, *_unused, **__: (
        _deal_excel_src_ok(code or "")
        and type(num_deps) is int
        and 0 <= num_deps <= _DEAL_CONVERT_LIST
        and (index_map is None or (isinstance(index_map, dict) and len(index_map) <= _DEAL_CONVERT_LIST and all(type(k) is int and type(v) is int and 0 <= k <= _DEAL_CONVERT_LIST and 0 <= v <= _DEAL_CONVERT_LIST for k, v in index_map.items())))
    )
)
def rewrite_excel_code(code: str, *, num_deps: int, index_map: dict[int, int] | None = None) -> RewriteResult:
    """Normalize ``xl(...)`` bindings to runnable ``xl("%Pn%", …)``; leave other code intact.

    *index_map* maps original 0-based dep index → normalized binding index after dedup.
    Returns a ``RewriteResult`` dataclass that also unpacks as
    ``(new_code, issues, used_original_indices, header_modes_by_original_index)``
    for backward compatibility.

    Statement-form ``xl`` (e.g. under ``if``) is kept and quoted like any other
    binding site. Literal / dynamic ``xl(...)`` is reported and fail-closed by
    the converter — not silently deleted.
    """
    src = code or ""
    analysis = analyze_xl_calls(src, num_deps=num_deps)
    if analysis.fatal:
        return RewriteResult(
            code=src,
            issues=analysis.issues,
            header_modes=analysis.header_modes,
            fatal=True,
            used=[str(i) for i in sorted(analysis.used)],
        )
    new_code = apply_rewrite(src, analysis.calls, index_map=index_map)
    return RewriteResult(
        code=new_code,
        issues=analysis.issues,
        header_modes=analysis.header_modes,
        fatal=False,
        used=[str(i) for i in sorted(analysis.used)],
    )


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


@deal.pre(lambda current, candidate: ascii_bounded(current or "", _DEAL_BINDING_A1_LEN) and ascii_bounded(candidate or "", _DEAL_BINDING_A1_LEN))
def _prefer_excel_dep_token(current: str, candidate: str) -> str:
    # crosshair: off
    # string branch leftover (cover-all 33569420452: ~977s est / 1812 ex despite ascii_bounded A1). Doable later.
    """When merging deps that snap to the same A1, keep the Excel-native token if any.

    See ``EXCEL_DEP_TOKEN_FIDELITY`` / models module doc — fidelity only, not Calc semantics.
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
        and len(resolved) <= _DEAL_RESOLVED_LEN
        and all(isinstance(r, ResolvedDep) and r.kind in _RESOLVED_KINDS and _deal_note_ok(r.note or "", _DEAL_NOTE_LEN) and ascii_bounded(r.a1, _DEAL_BINDING_A1_LEN) and (r.original is None or ascii_bounded(r.original, _DEAL_BINDING_A1_LEN)) for r in resolved)
        and type(header_modes) is dict
        and len(header_modes) <= _DEAL_RESOLVED_LEN
        and all(type(k) is int and 0 <= k <= _DEAL_RESOLVED_LEN and isinstance(v, str) and v in ("omit", "true", "false") for k, v in header_modes.items())
    )
)
def _normalize_bindings(resolved: list[ResolvedDep], header_modes: dict[int, str]) -> tuple[list[BindingInfo], dict[int, int], list[str], list[str], list[str]]:
    """Deduplicate resolved A1s; map original indices → normalized data indices.

    Returns ``(bindings, index_map, data_args, excel_deps, issues)``.
    ``excel_deps`` is parallel to ``data_args`` (original Excel tokens for export fidelity).
    Unresolved deps produce issues and an empty a1 — caller must fail-closed.
    """
    # crosshair: off
    # ResolvedDep objects still explode SMT; tiny pre is not enough (cover-all 33258921875: 255k lines). Doable later.
    issues: list[str] = []
    bindings: list[BindingInfo] = []
    index_map: dict[int, int] = {}
    a1_to_norm: dict[str, int] = {}
    data_args: list[str] = []
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
        norm = len(data_args)
        a1_to_norm[key] = norm
        index_map[orig_i] = norm
        data_args.append(key)
        excel_deps.append((r.original or key).strip() or key)
        bindings.append(BindingInfo(a1=key, header_mode=header_modes.get(orig_i, "omit"), role="data", original_indices=[orig_i]))
    return bindings, index_map, data_args, excel_deps, issues


@deal.pre(lambda model, cell, *_unused, **__: _deal_convert_scripts_ok(model) and _deal_convert_cell_ok(cell))
def convert_cell_to_dag(model: ExcelWorkbookModel, cell: ExcelPyCell, *, prior_in_order: list[ExcelPyCell] | None = None, best_effort: bool = False) -> ConvertedCell:
    # crosshair: off
    # ExcelWorkbookModel + resolve_deps/rewrite stack (cover-all 33569420452: ~1195s est / 2217 ex despite tiny list deals). Doable later.
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
    resolved = resolve_deps(cell.deps, model, sheet_hint=cell.sheet)

    # Parse once: analyze xl() calls, placeholders, header modes, and fatal errors in one pass.
    analysis = analyze_xl_calls(original, num_deps=len(cell.deps))
    bindings, index_map, data_args, excel_deps, bind_issues = _normalize_bindings(resolved, analysis.header_modes)

    issues: list[str] = list(bind_issues)
    for issue in analysis.issues:
        if issue not in issues:
            issues.append(issue)

    snapshot_notes = [r.note for r in resolved if r.kind in ("table_snapshot", "anchor_snapshot") and r.note]

    unresolved = len(index_map) != len(cell.deps)
    fatal = analysis.fatal or unresolved

    # Second rewrite step with dedup index map when all deps resolved and no fatal issues.
    # Merge every analysis issue. When deps are unresolved, dropping
    # rewrite_excel_code issues hides syntax errors and bad placeholders.
    # In best_effort mode say that placeholder remapping was skipped. The
    # old warning claimed shifted data indices were refused in both modes.
    if unresolved:
        if best_effort:
            issues.append("unresolved or dropped dependency; skipped placeholder remapping in best-effort mode")
        else:
            issues.append("unresolved or dropped dependency; refusing to emit shifted data indices")
        new_code = original
    elif fatal:
        new_code = original
    else:
        new_code = apply_rewrite(original, analysis.calls, index_map=index_map if index_map else None)

    # Advisory only: multi-cell Excel workbooks often need shared-kernel mode.
    # We do not inject prior-PY formula args for Calc ordering.
    prior = prior_in_order or []
    shared_kernel = bool(prior) or (not cell.deps and "xl(" not in original.replace(" ", ""))
    if shared_kernel and prior:
        issues.append("shared-kernel workbook: enable shared session; converter does not add order edges")

    if cell.return_type == 1:
        new_code = new_code + _OBJECT_SUPPRESS
        issues.append("returnType=1 (Object): suppressed cell value egress (shared object kept in script)")

    if fatal and not best_effort:
        # Fail closed: converted stays False and the original code is kept.
        # A missing fatal flag for a bad placeholder, an extra argument, or
        # a dep mismatch writes a partial formula.
        return ConvertedCell(
            sheet=cell.sheet,
            cell=cell.cell,
            direction="dag",
            original_code=original,
            converted_code=original,
            return_type=cell.return_type,
            array_ref=cell.array_ref,
            script_index=cell.script_index,
            converted=False,
            data_args=data_args,
            excel_deps=excel_deps,
            ordering_args=[],  # TODO: ordering_args is legacy; converter relies on shared-kernel session
            bindings=bindings,
            issues=list(dict.fromkeys(issues)),
            shared_kernel=shared_kernel,
            snapshot_deps=snapshot_notes,
            dag_formula="",
        )

    base = ConvertedCell(
        sheet=cell.sheet,
        cell=cell.cell,
        direction="dag",
        original_code=original,
        converted_code=new_code,
        return_type=cell.return_type,
        array_ref=cell.array_ref,
        script_index=cell.script_index,
        converted=True,
        data_args=data_args,
        excel_deps=excel_deps,
        ordering_args=[],  # TODO: ordering_args is legacy; converter relies on shared-kernel session
        bindings=bindings,
        issues=list(dict.fromkeys(issues)),
        shared_kernel=shared_kernel,
        snapshot_deps=snapshot_notes,
        dag_formula="",
    )
    base.dag_formula = formula_for_converted_cell(base, separator=";", use_script_bank=True)
    return base


@deal.pre(
    lambda model, *_unused, **__: (
        _deal_convert_scripts_ok(model)
        and isinstance(model.cells, list)
        and len(model.cells) <= _DEAL_CONVERT_LIST
        # convert_cell_to_dag's cell.deps bound is tighter than this wrapper used to be
        # (empty scripts + a 3-dep cell passed here, then PreconditionFailed inside).
        and all(_deal_convert_cell_ok(c) for c in model.cells)
    )
)
def convert_model_to_dag(model: ExcelWorkbookModel, *, best_effort: bool = False) -> ConversionReport:
    """Convert every PY cell in *model* to DAG-style ``=PY`` formulas."""
    report = ConversionReport(direction="dag", source_path=model.source_path)
    if not model.scripts:
        report.issues.append("no pythonScripts found")
    ordered = _excel_execution_order(model)
    prior: list[ExcelPyCell] = []
    # Convert in Excel sheet/row order so shared_kernel advisory matches stage order.
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
