# SPDX-License-Identifier: GPL-3.0-or-later
"""Save-time sugar: static ``xl("A1:…")`` → ``=PY`` data args + polymorphic ``data``.

Gated by ``scripting.xl_static_rewrite`` (internal, default off). No live sheet reads —
ranges become normal formula precedents; call sites become ``data`` (one binding) or
``data[i]`` (two or more). When a later save grows from one binding to many, prior
sugar-shaped bare ``data`` uses are migrated to ``data[0]``.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from plugin.calc.excel_py_convert.to_dag import ast_source_offset
from plugin.framework.deal_shim import DEAL_MAX_SHAPE_DIM, DEAL_MAX_SOURCE, str_bounded, deal

# Cell / range with optional $; optional sheet via Calc ``Sheet.`` or Excel ``Sheet!``.
# Quoted sheet names: 'My Sheet'.A1:B2
# Columns limited to 1–3 letters (Excel/Calc A…XFD) so names like ``Table1`` are not
# mistaken for A1 addresses.
_A1_RANGE_RE = re.compile(
    r"^(?:"
    r"(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_]*)[!.]"  # sheet qualifier
    r")?"
    r"\$?[A-Za-z]{1,3}\$?\d+"
    r"(?::\$?[A-Za-z]{1,3}\$?\d+)?"
    r"$"
)

# Excel binding tokens are not A1 literals — leave them for the binding-only shim.
_P_TOKEN_RE = re.compile(r"^%P(\d+)%$", re.IGNORECASE)


@dataclass(frozen=True)
class _XlLiteralCall:
    start: int
    end: int
    address: str
    header_mode: str  # "omit" | "true" | "false"


@dataclass
class XlStaticRewriteResult:
    """Outcome of a static ``xl("A1")`` → ``data`` rewrite attempt."""

    code: str
    data_args: list[str]
    issues: list[str]
    changed: bool


def _header_mode_from_call(node: ast.Call) -> str:
    for kw in node.keywords:
        if kw.arg and kw.arg.lower() == "headers":
            if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                return "true"
            if isinstance(kw.value, ast.Constant) and kw.value.value is False:
                return "false"
            if isinstance(kw.value, ast.Name) and kw.value.id in ("True", "False"):
                return "true" if kw.value.id == "True" else "false"
            return "bad"
    return "omit"


@deal.pre(lambda addr: str_bounded(addr, DEAL_MAX_SOURCE))
@deal.post(lambda result: isinstance(result, str))
def normalize_range_address(addr: str) -> str:
    """Normalize an address for dedup (strip ``$``, collapse sheet punctuation)."""
    s = (addr or "").strip().replace("$", "")
    if "!" in s and "." not in s.split("!")[0]:
        sheet, _unused, rest = s.partition("!")
        s = f"{sheet}.{rest}"
    return s


@deal.post(lambda result: isinstance(result, bool))
def is_static_a1_literal(ref: str) -> bool:
    """True when *ref* looks like a formula-static cell/range address."""
    # Sheet-prefix + A1 regex hang under deep check (same class as parse_address).
    # crosshair: off
    s = (ref or "").strip()
    if not s or _P_TOKEN_RE.match(s):
        return False
    return bool(_A1_RANGE_RE.match(s))


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


def migrate_bare_data_to_index0(code: str) -> str:
    """When bindings grow to 2+, rewrite bare Load ``data`` → ``data[0]`` (AST-safe).

    Skips ``data[...]`` (already indexed). Turns ``data.to_pandas()`` into
    ``data[0].to_pandas()`` by replacing only the Name span.
    """
    src = code or ""
    if not src.strip():
        return src
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src

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
        start = ast_source_offset(src, node.lineno, node.col_offset)
        end = ast_source_offset(
            src, node.end_lineno or node.lineno, node.end_col_offset or node.col_offset
        )
        if start < 0 or end < 0 or end <= start:
            continue
        hits.append((start, end))

    new_code = src
    for start, end in sorted(hits, key=lambda h: h[0], reverse=True):
        new_code = new_code[:start] + "data[0]" + new_code[end:]
    return new_code


def _find_static_xl_literals(code: str) -> tuple[list[_XlLiteralCall], list[str]]:
    """Locate direct ``xl("A1…")`` calls; report dynamic / invalid forms as issues."""
    issues: list[str] = []
    src = code or ""
    if not src.strip():
        return [], issues
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        loc = f"line {exc.lineno}" if exc.lineno is not None else "unknown line"
        if exc.offset is not None:
            loc = f"{loc}:{exc.offset}"
        issues.append(f"Python syntax error at {loc}: {exc.msg}")
        return [], issues
    except (TypeError, ValueError) as exc:
        # NUL bytes: CPython ValueError. CrossHair symbolic str: TypeError from compile().
        issues.append(f"Python source is not parseable: {exc}")
        return [], issues


    calls: list[_XlLiteralCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id != "xl":
            continue
        if getattr(node, "lineno", None) is None:
            continue
        start = ast_source_offset(src, node.lineno, node.col_offset)
        end = ast_source_offset(
            src, node.end_lineno or node.lineno, node.end_col_offset or node.col_offset
        )
        if start < 0 or end < 0 or end <= start:
            issues.append("xl() call without reliable source positions")
            continue

        header_mode = _header_mode_from_call(node)
        if header_mode == "bad":
            issues.append("xl() headers= must be True or False")
            continue

        if not node.args:
            issues.append("dynamic xl() reference (empty args)")
            continue
        arg0 = node.args[0]
        if isinstance(arg0, (ast.JoinedStr,)):
            issues.append("dynamic xl() reference (f-string)")
            continue
        if not isinstance(arg0, ast.Constant) or not isinstance(arg0.value, str):
            issues.append("dynamic xl() reference (not a string literal)")
            continue
        ref = arg0.value.strip()
        if _P_TOKEN_RE.match(ref):
            # Already an Excel binding token — leave for excel_xl shim; not sugar.
            continue
        if not is_static_a1_literal(ref):
            issues.append(f"xl({ref!r}) is not a static A1/range address")
            continue
        calls.append(
            _XlLiteralCall(start=start, end=end, address=ref, header_mode=header_mode)
        )
    calls.sort(key=lambda c: c.start)
    return calls, issues


@deal.pre(
    lambda code, existing_data_args=None: str_bounded(code, DEAL_MAX_SOURCE)
    and (
        existing_data_args is None
        or (
            isinstance(existing_data_args, list)
            and len(existing_data_args) <= DEAL_MAX_SHAPE_DIM
            and all(str_bounded(x, DEAL_MAX_SOURCE) for x in existing_data_args)
        )
    )
)
@deal.post(
    lambda result: isinstance(result, XlStaticRewriteResult)
    and isinstance(result.code, str)
    and isinstance(result.data_args, list)
)
def apply_xl_static_rewrite(
    code: str,
    existing_data_args: list[str] | None = None,
) -> XlStaticRewriteResult:
    """Lift static ``xl("A1")`` literals onto data args and rewrite to ``data`` / ``data[i]``.

    Explicit *existing_data_args* (Monaco **Data:** field) stay first; new addresses append.
    Duplicate addresses share one binding. On any issue, *changed* is False and code is
    unchanged (caller should fail the save).
    """
    # ast.parse + A1 regex hang under deep check even at DEAL_MAX_SOURCE.
    # crosshair: off
    existing = [a.strip() for a in (existing_data_args or []) if a and str(a).strip()]
    calls, issues = _find_static_xl_literals(code)
    if issues:
        return XlStaticRewriteResult(code=code, data_args=list(existing), issues=issues, changed=False)

    # Preserve explicit Data: order; append newly seen addresses from xl() calls.
    data_args: list[str] = list(existing)
    key_to_index: dict[str, int] = {}
    for i, addr in enumerate(data_args):
        key_to_index[normalize_range_address(addr)] = i

    for call in calls:
        key = normalize_range_address(call.address)
        if key not in key_to_index:
            key_to_index[key] = len(data_args)
            data_args.append(call.address.strip())

    if not calls:
        return XlStaticRewriteResult(code=code, data_args=list(existing), issues=[], changed=False)

    n_bindings = len(data_args)
    new_code = code
    for call in sorted(calls, key=lambda c: c.start, reverse=True):
        idx = key_to_index[normalize_range_address(call.address)]
        repl = _data_expr(idx, call.header_mode, n_bindings=n_bindings)
        new_code = new_code[: call.start] + repl + new_code[call.end :]

    # Growing 1→N: prior sugar left bare ``data`` / ``data.to_pandas()``; migrate.
    # Only when this save lifted at least one xl() into a multi-binding formula.
    if n_bindings >= 2:
        new_code = migrate_bare_data_to_index0(new_code)

    changed = new_code != code or data_args != existing
    return XlStaticRewriteResult(
        code=new_code,
        data_args=data_args,
        issues=[],
        changed=changed,
    )
