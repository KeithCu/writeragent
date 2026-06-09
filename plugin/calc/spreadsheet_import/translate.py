# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Translate P1 Calc formulas to ``=PY()`` Python source via vendored AST."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from plugin.contrib.calc_formula_parser import (
    FunctionNode,
    OperandNode,
    OperatorNode,
    RangeNode,
    parse_formula,
)
from plugin.calc.spreadsheet_import.models import TranslationResult
from plugin.calc.spreadsheet_import.preprocess import normalize_lo_formula_for_parse

_CROSS_SHEET_RE = re.compile(r"[!']")


@dataclass
class _CodegenState:
    ranges: list[str] = field(default_factory=list)
    _index: dict[str, int] = field(default_factory=dict)

    def add_range(self, addr: str) -> int:
        key = _canonical_range(addr)
        if key not in self._index:
            self._index[key] = len(self.ranges)
            self.ranges.append(key)
        return self._index[key]

    def ref_expr(self, addr: str) -> str:
        idx = self.add_range(addr)
        if len(self.ranges) == 1:
            return "data"
        return f"data[{idx}]"


def _canonical_range(addr: str) -> str:
    return str(addr).replace("$", "").upper()


def _walk_ranges(node, state: _CodegenState) -> None:
    if isinstance(node, RangeNode):
        state.add_range(node.address)
    elif isinstance(node, OperatorNode):
        if node.left is not None:
            _walk_ranges(node.left, state)
        if node.right is not None:
            _walk_ranges(node.right, state)
    elif isinstance(node, FunctionNode):
        for arg in node.args or []:
            _walk_ranges(arg, state)


def _emit_operand(node: OperandNode) -> str:
    if node.tsubtype == "logical":
        return "True" if str(node.tvalue).upper() == "TRUE" else "False"
    if node.tsubtype == "text":
        return repr(str(node.tvalue))
    if node.tsubtype == "error":
        raise ValueError("error literal")
    # number or none
    text = str(node.tvalue)
    if text.upper() in ("TRUE", "FALSE"):
        return "True" if text.upper() == "TRUE" else "False"
    try:
        val = float(text)
        if val.is_integer():
            return str(int(val)) if abs(val) < 1e15 else str(val)
        return str(val)
    except ValueError:
        return repr(text)


def _emit_expr(node, state: _CodegenState) -> str:
    if isinstance(node, RangeNode):
        if _CROSS_SHEET_RE.search(node.address):
            raise ValueError("cross-sheet ref")
        return state.ref_expr(node.address)
    if isinstance(node, OperandNode):
        return _emit_operand(node)
    if isinstance(node, OperatorNode):
        return _emit_operator(node, state)
    if isinstance(node, FunctionNode):
        return _emit_function(node, state)
    raise ValueError(f"unknown node {type(node)}")


def _emit_operator(node: OperatorNode, state: _CodegenState) -> str:
    if node.ttype == "operator-prefix":
        rhs = _emit_expr(node.right, state)
        if node.tvalue == "-":
            return f"(-{rhs})"
        if node.tvalue == "+":
            return rhs
        raise ValueError("unsupported prefix op")
    if node.ttype != "operator-infix":
        raise ValueError("unsupported operator type")
    left = _emit_expr(node.left, state)
    right = _emit_expr(node.right, state)
    op = node.tvalue
    if op == "^":
        return f"({left} ** {right})"
    if op == "=":
        return f"({left} == {right})"
    if op == "<>":
        return f"({left} != {right})"
    if op == "&":
        return f"(str({left}) + str({right}))"
    return f"({left} {op} {right})"


def _emit_function(node: FunctionNode, state: _CodegenState) -> str:
    name = str(node.tvalue).upper().replace("_XLFN.", "")
    args = [_emit_expr(arg, state) for arg in (node.args or [])]
    emitted = _P1_FUNCTION_EMITTERS.get(name)
    if emitted is None:
        raise ValueError(f"unsupported function {name}")
    return emitted(args)


def _float_wrap(expr: str) -> str:
    return f"float({expr})"


def _emit_if(args: list[str]) -> str:
    if len(args) != 3:
        raise ValueError("IF arity")
    return f"({args[1]} if {args[0]} else {args[2]})"


# P1 function emitters: args are already Python sub-expressions using data[i].
_P1_FUNCTION_EMITTERS: dict[str, Callable[[list[str]], str]] = {
    "SUM": lambda a: _float_wrap(f"np.sum({a[0]})"),
    "AVERAGE": lambda a: _float_wrap(f"np.mean({a[0]})"),
    "PRODUCT": lambda a: _float_wrap(f"np.prod({a[0]})"),
    "MAX": lambda a: _float_wrap(f"np.nanmax({a[0]})"),
    "MIN": lambda a: _float_wrap(f"np.nanmin({a[0]})"),
    "COUNT": lambda a: _float_wrap(f"np.sum(np.isfinite(np.asarray({a[0]}, dtype=float).ravel()))"),
    "COUNTA": lambda a: _float_wrap(
        f"sum(1 for x in np.asarray({a[0]}).ravel() if x is not None and str(x) != '')"
    ),
    "ABS": lambda a: _float_wrap(f"np.abs({a[0]})"),
    "SQRT": lambda a: _float_wrap(f"np.sqrt({a[0]})"),
    "SIGN": lambda a: _float_wrap(f"np.sign({a[0]})"),
    "INT": lambda a: _float_wrap(f"np.floor({a[0]})"),
    "TRUNC": lambda a: _float_wrap(f"np.trunc({a[0]})"),
    "EXP": lambda a: _float_wrap(f"np.exp({a[0]})"),
    "LN": lambda a: _float_wrap(f"np.log({a[0]})"),
    "LOG10": lambda a: _float_wrap(f"np.log10({a[0]})"),
    "MOD": lambda a: _float_wrap(f"{a[0]} % {a[1]}"),
    "POWER": lambda a: _float_wrap(f"{a[0]} ** {a[1]}"),
    "ROUND": lambda a: _float_wrap(f"np.round({a[0]}, {a[1]})") if len(a) > 1 else _float_wrap(f"np.round({a[0]})"),
    "SIN": lambda a: _float_wrap(f"np.sin({a[0]})"),
    "COS": lambda a: _float_wrap(f"np.cos({a[0]})"),
    "TAN": lambda a: _float_wrap(f"np.tan({a[0]})"),
    "NOT": lambda a: f"(not {a[0]})",
    "TRUE": lambda _a: "True",
    "FALSE": lambda _a: "False",
    "PI": lambda _a: "math.pi",
    "IF": _emit_if,
    "AND": lambda a: f"all([{', '.join(a)}])",
    "OR": lambda a: f"any([{', '.join(a)}])",
    # Text (P2)
    "CONCATENATE": lambda a: f'"".join(str(x) for x in [{", ".join(a)}])',
    "CONCAT": lambda a: f'"".join(str(x) for x in np.asarray([{", ".join(a)}]).ravel())',
    "LEFT": lambda a: f'str({a[0]})[:int({a[1]})]' if len(a) > 1 else f'str({a[0]})[:1]',
    "RIGHT": lambda a: f'str({a[0]})[-int({a[1]}):]' if len(a) > 1 else f'str({a[0]})[-1:]',
    "MID": lambda a: f'str({a[0]})[max(0, int({a[1]})-1) : max(0, int({a[1]})-1) + int({a[2]})]',
    "LEN": lambda a: f'float(len(str({a[0]})))',
    "LOWER": lambda a: f'str({a[0]}).lower()',
    "UPPER": lambda a: f'str({a[0]}).upper()',
    "PROPER": lambda a: f'str({a[0]}).title()',
    "TRIM": lambda a: f'str({a[0]}).strip()',
    "SUBSTITUTE": lambda a: f'str({a[0]}).replace(str({a[1]}), str({a[2]}))' if len(a) > 2 else f'str({a[0]}).replace(str({a[1]}), "")',
    "REPLACE": lambda a: f'str({a[0]})[:max(0, int({a[1]})-1)] + str({a[3]}) + str({a[0]})[max(0, int({a[1]})-1) + int({a[2]}):]',
    "FIND": lambda a: f'float(str({a[1]}).find(str({a[0]})) + 1)',
    "SEARCH": lambda a: f'float(str({a[1]}).lower().find(str({a[0]}).lower()) + 1)',
    "VALUE": lambda a: f'float({a[0]})',
    # Date & Time (P2)
    "TODAY": lambda _a: 'float(datetime.date.today().toordinal() - 693594)',
    "NOW": lambda _a: 'float(datetime.datetime.now().toordinal() - 693594)',
    "YEAR": lambda a: f'float(datetime.date.fromordinal(int({a[0]}) + 693594).year)',
    "MONTH": lambda a: f'float(datetime.date.fromordinal(int({a[0]}) + 693594).month)',
    "DAY": lambda a: f'float(datetime.date.fromordinal(int({a[0]}) + 693594).day)',
    # Statistical (P2)
    "STDEV": lambda a: _float_wrap(f"np.std({a[0]}, ddof=1)"),
    "STDEVP": lambda a: _float_wrap(f"np.std({a[0]}, ddof=0)"),
    "VAR": lambda a: _float_wrap(f"np.var({a[0]}, ddof=1)"),
    "VARP": lambda a: _float_wrap(f"np.var({a[0]}, ddof=0)"),
    "TRANSPOSE": lambda a: f"np.asarray({a[0]}).T.tolist()",
    # Lookup & Reference (P2)
    "VLOOKUP": lambda a: f'next((r[int({a[2]})-1] for r in np.asarray({a[1]}) if r[0] == {a[0]}), None)',
    "HLOOKUP": lambda a: f'next((np.asarray({a[1]})[int({a[2]})-1, i] for i, val in enumerate(np.asarray({a[1]})[0]) if val == {a[0]}), None)',
    "INDEX": lambda a: f'np.asarray({a[0]})[int({a[1]})-1, int({a[2]})-1]' if len(a) > 2 else f'np.asarray({a[0]})[int({a[1]})-1]',
    "MATCH": lambda a: f'float(next((i+1 for i, val in enumerate(np.asarray({a[1]}).ravel()) if val == {a[0]}), -1))',
}


def translate_formula(formula: str) -> TranslationResult:
    """Parse and codegen one Calc formula to ``result = …`` Python."""
    if not formula or not str(formula).strip().startswith("="):
        return TranslationResult(ok=False, reason="PARSE_ERROR")

    normalized = normalize_lo_formula_for_parse(formula)
    try:
        ast = parse_formula(normalized)
    except (SyntaxError, ValueError, IndexError):
        return TranslationResult(ok=False, reason="PARSE_ERROR")

    state = _CodegenState()
    try:
        _walk_ranges(ast, state)
        body = _emit_expr(ast, state)
    except ValueError as exc:
        msg = str(exc)
        if "cross-sheet" in msg:
            return TranslationResult(ok=False, reason="CROSS_SHEET_REF")
        if msg.startswith("unsupported function"):
            return TranslationResult(ok=False, reason="UNSUPPORTED_FUNCTION")
        return TranslationResult(ok=False, reason="PARSE_ERROR")

    if not state.ranges:
        # Literal-only (e.g. =PI() still has no ranges; =1+2 has none)
        pass
    else:
        # Scalar-wrap bare arithmetic for Calc double semantics.
        if isinstance(ast, OperatorNode) or (
            isinstance(ast, FunctionNode) and str(ast.tvalue).upper() not in ("TRUE", "FALSE", "IF", "AND", "OR", "NOT")
        ):
            if not body.startswith("float(") and body not in ("True", "False"):
                body = _float_wrap(body)

    return TranslationResult(ok=True, code=body, data_ranges=list(state.ranges))
