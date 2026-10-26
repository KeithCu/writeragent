# SPDX-License-Identifier: GPL-3.0-or-later
"""Save-time sugar: static ``xl("A1:…")`` → ``=PY`` data args + polymorphic ``data``.

Gated by ``scripting.xl_static_rewrite`` (internal, default off). No live sheet reads —
ranges become normal formula precedents; call sites become ``data`` (one binding) or
``data[i]`` (two or more). On the one-to-many transition only, prior sugar-shaped bare
``data`` uses are migrated to ``data[0]`` (skipped when the script binds or imports ``data``).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

from plugin.calc.address_utils import MAX_COL, format_range, parse_range_string, split_sheet_prefix
from plugin.framework.ast_stmt_edit import ast_source_offset, line_starts_exact
from plugin.framework.deal_shim import DEAL_MAX_SHAPE_DIM, DEAL_MAX_SOURCE, str_bounded, deal

# A1 endpoint shape gate only (1-3 letters, optional $). Bounds and the rest of the
# validation come from plugin.calc.address_utils; unbounded letters would raise
# PreContractError inside column_to_index before a range check could reject them.
_A1_ENDPOINT_RE = re.compile(r"^\$?[A-Za-z]{1,3}\$?[0-9]{1,7}$")

# Excel binding tokens are not A1 literals — leave them for the binding-only shim.
_P_TOKEN_RE = re.compile(r"^%P(\d+)%$", re.IGNORECASE)


@dataclass(frozen=True)
class _XlLiteralCall:
    start: int
    end: int
    address: str
    key: tuple[str | None, str]
    header_mode: str  # "omit" | "true" | "false"


@dataclass
class XlStaticRewriteResult:
    """Outcome of a static ``xl("A1")`` → ``data`` rewrite attempt."""

    code: str
    data_args: list[str]
    issues: list[str]
    changed: bool


def _header_mode_from_call(node: ast.Call) -> str:
    mode = "omit"
    for kw in node.keywords:
        if kw.arg != "headers":
            return "bad"
        if not (isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, bool)):
            return "bad"
        mode = "true" if kw.value.value is True else "false"
    return mode


def _canonical_address_key(ref: str) -> tuple[str | None, str] | None:
    """Canonical dedup key for an address/range, or None when *ref* is not one."""
    s = (ref or "").strip()
    sheet, a1 = split_sheet_prefix(s)
    parts = a1.split(":")
    if len(parts) > 2 or not all(_A1_ENDPOINT_RE.match(p) for p in parts):
        return None
    try:
        start, end = parse_range_string(a1)
    except ValueError:
        return None
    # 3-letter columns run past XFD; parse_range_string bounds rows only.
    if end[0] > MAX_COL:
        return None
    body = format_range(start[0], start[1], end[0], end[1], collapse_single=True)
    return (sheet.casefold() if sheet else None, body)


@deal.post(lambda result: isinstance(result, bool))
def is_static_a1_literal(ref: str) -> bool:
    """True when *ref* looks like a formula-static cell/range address."""
    # Address parsing is skipped under CrossHair (same class as parse_address).
    # crosshair: off
    s = (ref or "").strip()
    if not s or _P_TOKEN_RE.match(s):
        return False
    return _canonical_address_key(s) is not None


def _data_expr(index: int, header_mode: str, *, n_bindings: int) -> str:
    """Emit polymorphic ``data`` / ``data[i]`` for the binding count at save time."""
    if n_bindings <= 1:
        base = "data"
    else:
        base = f"data[{index}]"
    if header_mode == "true":
        return f"{base}.to_pandas()"
    if header_mode == "false":
        return f"{base}.to_pandas(header_row=None)"
    return base


def _binds_name(tree: ast.AST, name: str) -> bool:
    """True when the script binds or imports *name* (sugar assumes the injected global)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name and not isinstance(node.ctx, ast.Load):
            return True
        if isinstance(node, ast.arg) and node.arg == name:
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            return True
        if isinstance(node, ast.alias) and (node.asname or node.name.split(".")[0]) == name:
            return True
        if isinstance(node, ast.ExceptHandler) and node.name == name:
            return True
    return False


def _bare_data_load_spans(src: str, tree: ast.AST, line_starts: list[int]) -> list[tuple[int, int]]:
    skip_names: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "data":
            skip_names.add(id(node.value))

    hits: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or node.id != "data":
            continue
        if not isinstance(node.ctx, ast.Load):
            continue
        if id(node) in skip_names:
            continue
        start = ast_source_offset(src, node.lineno, node.col_offset, line_starts=line_starts)
        end = ast_source_offset(src, node.end_lineno or node.lineno, node.end_col_offset or node.col_offset, line_starts=line_starts)
        if start < 0 or end < 0 or end <= start:
            continue
        hits.append((start, end))
    return hits


def iter_xl_calls(
    tree: ast.AST,
    src: str,
    line_starts: list[int],
    issues: list[str],
) -> Iterator[tuple[ast.Call, int, int]]:
    """Yield direct ``xl(...)`` Call nodes and their start/end offsets in *src*."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id != "xl":
            continue
        if getattr(node, "lineno", None) is None:
            continue
        start = ast_source_offset(src, node.lineno, node.col_offset, line_starts=line_starts)
        end = ast_source_offset(src, node.end_lineno or node.lineno, node.end_col_offset or node.col_offset, line_starts=line_starts)
        if start < 0 or end < 0 or end <= start:
            issues.append("xl() call without reliable source positions")
            continue
        yield node, start, end


def _find_static_xl_literals(code: str) -> tuple[list[_XlLiteralCall], list[str], list[tuple[int, int]]]:
    """Locate direct ``xl("A1…")`` calls; report dynamic / invalid forms as issues."""
    issues: list[str] = []
    src = code or ""
    if not src.strip():
        return [], issues, []
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        loc = f"line {exc.lineno}" if exc.lineno is not None else "unknown line"
        if exc.offset is not None:
            loc = f"{loc}:{exc.offset}"
        issues.append(f"Python syntax error at {loc}: {exc.msg}")
        return [], issues, []
    except (TypeError, ValueError) as exc:
        # NUL bytes: CPython ValueError. CrossHair symbolic str: TypeError from compile().
        issues.append(f"Python source is not parseable: {exc}")
        return [], issues, []

    line_starts = line_starts_exact(src)
    if _binds_name(tree, "xl"):
        return [], [], []
    bare_spans = [] if _binds_name(tree, "data") else _bare_data_load_spans(src, tree, line_starts)

    calls: list[_XlLiteralCall] = []
    for node, start, end in iter_xl_calls(tree, src, line_starts, issues):
        header_mode = _header_mode_from_call(node)
        if header_mode == "bad":
            issues.append("xl() accepts only a headers=True/False keyword")
            continue

        if not node.args:
            issues.append("dynamic xl() reference (empty args)")
            continue
        if len(node.args) > 1:
            issues.append("xl() does not accept positional arguments beyond the first")
            continue
        arg0 = node.args[0]
        if isinstance(arg0, ast.JoinedStr):
            issues.append("dynamic xl() reference (f-string)")
            continue
        if not isinstance(arg0, ast.Constant) or not isinstance(arg0.value, str):
            issues.append("dynamic xl() reference (not a string literal)")
            continue
        ref = arg0.value.strip()
        if _P_TOKEN_RE.match(ref):
            # Already an Excel binding token — leave for excel_xl shim; not sugar.
            continue
        key = _canonical_address_key(ref)
        if key is None:
            issues.append(f"xl({ref!r}) is not a static A1/range address")
            continue
        calls.append(_XlLiteralCall(start=start, end=end, address=ref, key=key, header_mode=header_mode))
    calls.sort(key=lambda c: c.start)
    return calls, issues, bare_spans


@deal.pre(lambda code, existing_data_args=None: str_bounded(code, DEAL_MAX_SOURCE) and (existing_data_args is None or (isinstance(existing_data_args, list) and len(existing_data_args) <= DEAL_MAX_SHAPE_DIM and all(str_bounded(x, DEAL_MAX_SOURCE) for x in existing_data_args))))
@deal.post(lambda result: isinstance(result, XlStaticRewriteResult) and isinstance(result.code, str) and isinstance(result.data_args, list))
def apply_xl_static_rewrite(code: str, existing_data_args: list[str] | None = None) -> XlStaticRewriteResult:
    """Lift static ``xl("A1")`` literals onto data args and rewrite to ``data`` / ``data[i]``.

    Explicit *existing_data_args* (Monaco **Data:** field) stay first; new addresses append.
    Duplicate addresses share one binding. On any issue, *changed* is False and code is
    unchanged (caller should fail the save).
    """
    # ast.parse + A1 regex hang under deep check even at DEAL_MAX_SOURCE.
    # crosshair: off
    existing = [a.strip() for a in (existing_data_args or []) if a and str(a).strip()]
    calls, issues, bare_spans = _find_static_xl_literals(code)
    if issues:
        return XlStaticRewriteResult(code=code, data_args=list(existing), issues=issues, changed=False)

    if not calls:
        return XlStaticRewriteResult(code=code, data_args=list(existing), issues=[], changed=False)

    # Preserve explicit Data: order; append newly seen addresses from xl() calls.
    data_args: list[str] = []
    key_to_index: dict[tuple[str | None, str], int] = {}
    for addr in existing:
        key = _canonical_address_key(addr)
        if key is not None and key in key_to_index:
            continue
        if key is not None:
            key_to_index[key] = len(data_args)
        data_args.append(addr)

    n_existing = len(data_args)

    for call in calls:
        if call.key not in key_to_index:
            key_to_index[call.key] = len(data_args)
            data_args.append(call.address.strip())

    n_bindings = len(data_args)
    replacements: list[tuple[int, int, str]] = []
    for call in calls:
        idx = key_to_index[call.key]
        repl = _data_expr(idx, call.header_mode, n_bindings=n_bindings)
        replacements.append((call.start, call.end, repl))

    # Growing 1→N on this save: prior sugar left bare ``data``. Only the injected
    # global is migrated; a shadowed name is skipped in _find_static_xl_literals.
    if n_existing <= 1 and n_bindings >= 2:
        replacements.extend((span_start, span_end, "data[0]") for span_start, span_end in bare_spans)

    new_code = code
    for start, end, repl in sorted(replacements, key=lambda r: r[0], reverse=True):
        new_code = new_code[:start] + repl + new_code[end:]

    changed = new_code != code or data_args != existing
    return XlStaticRewriteResult(code=new_code, data_args=data_args, issues=[], changed=changed)
