# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Translate P1 Calc formulas to ``=PY()`` Python source via vendored AST."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.contrib.calc_formula_parser import FunctionNode, OperandNode, OperatorNode, RangeNode, parse_formula
from plugin.calc.python.formula_edit import sanitize_inline_py_code
from plugin.calc.spreadsheet_import.models import TranslationResult
from plugin.calc.spreadsheet_import.preprocess import normalize_lo_formula_for_parse
from plugin.calc.address_utils import parse_address, parse_range_string, split_sheet_prefix


class TranslationError(ValueError):
    """Base error for formula translation failures."""

    reason: str = "PARSE_ERROR"


class UnsupportedFunction(TranslationError):
    """Raised when encountering a function with no Python translation."""

    reason: str = "UNSUPPORTED_FUNCTION"


class BadArity(TranslationError):
    """Raised when a function receives too few or too many arguments."""

    reason: str = "UNSUPPORTED_ARITY"


class UnsupportedRef(TranslationError):
    """Raised when encountering an unsupported reference structure."""

    reason: str = "UNSUPPORTED_FUNCTION"


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
    s = str(addr).strip()
    # Keep the quotes and the sheet name's case. Strip $ from the reference
    # and uppercase the cell address. Uppercasing the whole address turned
    # 'My Sheet'.A1 into MY SHEET.A1.
    sheet, cell = split_sheet_prefix(s)
    if sheet is not None:
        clean_s = s.lstrip("$")
        if clean_s.startswith("'"):
            end_quote = clean_s.find("'", 1)
            while end_quote != -1 and end_quote + 1 < len(clean_s) and clean_s[end_quote + 1] == "'":
                end_quote = clean_s.find("'", end_quote + 2)
            if end_quote != -1:
                quoted_sheet = clean_s[:end_quote + 1]
                rest = clean_s[end_quote + 1:].lstrip()
                sep = rest[0] if rest and rest[0] in (".", "!") else "."
                cell_part = rest[1:].strip().replace("$", "").upper()
                return f"{quoted_sheet}{sep}{cell_part}"
        sep = "!" if "!" in s else "."
        return f"{sheet.upper()}{sep}{cell.replace('$', '').upper()}"
    return s.replace("$", "").upper()


def _walk_ranges(node: Any, state: _CodegenState) -> None:
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
        raise TranslationError("error literal")
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


def _emit_expr(node: Any, state: _CodegenState, cell_addr: str | None = None) -> str:
    if isinstance(node, RangeNode):
        return state.ref_expr(node.address)
    if isinstance(node, OperandNode):
        return _emit_operand(node)
    if isinstance(node, OperatorNode):
        return _emit_operator(node, state, cell_addr)
    if isinstance(node, FunctionNode):
        return _emit_function(node, state, cell_addr)
    raise TranslationError(f"unknown node {type(node)}")


def _emit_operator(node: OperatorNode, state: _CodegenState, cell_addr: str | None = None) -> str:
    if node.ttype == "operator-prefix":
        rhs = _emit_expr(node.right, state, cell_addr)
        if node.tvalue == "-":
            return f"(-{rhs})"
        if node.tvalue == "+":
            return rhs
        raise TranslationError("unsupported prefix op")
    if node.ttype == "operator-postfix":
        # Emit (expr * 0.01), parenthesized. Postfix % binds tighter than
        # ^ and * / (check -50% and 2^50%). The parser builds a postfix
        # operator node; rejecting that node drops a legal formula.
        if node.tvalue == "%":
            expr = _emit_expr(node.left, state, cell_addr)
            return f"({expr} * 0.01)"
        raise TranslationError(f"unsupported postfix op {node.tvalue}")
    if node.ttype != "operator-infix":
        raise TranslationError("unsupported operator type")
    left = _emit_expr(node.left, state, cell_addr)
    right = _emit_expr(node.right, state, cell_addr)
    op = node.tvalue
    if op == "^":
        return f"({left} ** {right})"
    if op == "=":
        return f"({left} == {right})"
    if op == "<>":
        return f"({left} != {right})"
    if op == "&":
        return f"(calc.py_str({left}) + calc.py_str({right}))"
    return f"({left} {op} {right})"


def _emit_row_col(axis: int, is_count: bool, node: FunctionNode, state: _CodegenState, cell_addr: str | None = None) -> str:
    """Consolidated emitter for ROW/COLUMN (is_count=False) and ROWS/COLUMNS (is_count=True)."""
    if is_count:
        if not node.args:
            raise BadArity("ROWS/COLUMNS requires 1 argument")
        arg = node.args[0]
        if isinstance(arg, RangeNode):
            _sheet, bare = split_sheet_prefix(arg.address)
            clean_ref = bare.replace("$", "")
            try:
                (sc, sr), (ec, er) = parse_range_string(clean_ref)
                return f"float({abs(er - sr) + 1})" if axis == 0 else f"float({abs(ec - sc) + 1})"
            except ValueError:
                pass
        expr = _emit_expr(arg, state, cell_addr)
        return f"float(np.asarray({expr}).shape[{axis}])"

    # ROW / COLUMN:
    if not node.args:
        if not cell_addr:
            # Without a cell address, ROW() and COLUMN() cannot name a row
            # or a column, so fail as unsupported. Falling through to
            # float(1) counted as a successful translation.
            raise UnsupportedRef("ROW/COLUMN without reference requires cell_addr")
        try:
            col_idx, row_idx = parse_address(cell_addr.replace("$", ""))
            return f"float({row_idx + 1})" if axis == 0 else f"float({col_idx + 1})"
        except ValueError as exc:
            raise UnsupportedRef(f"Cannot parse cell address {cell_addr}") from exc

    arg = node.args[0]
    if not isinstance(arg, RangeNode):
        raise UnsupportedRef("ROW/COLUMN argument must be a range")

    # Strip the sheet prefix and $ so parse_range_string can read the
    # coordinates. Anything else raises UnsupportedRef. parse_range_string
    # rejects a prefix and $, and the ValueError handler returned float(1),
    # so =ROW($A$5) and =ROW(Sheet2.A1:A3) emitted (1)+0.0.
    _sheet, bare = split_sheet_prefix(arg.address)
    clean_ref = bare.replace("$", "")
    try:
        (sc, sr), (ec, er) = parse_range_string(clean_ref)
    except ValueError as exc:
        raise UnsupportedRef(f"Invalid range in ROW/COLUMN: {arg.address}") from exc

    if axis == 0:
        if sr == er:
            return f"float({sr + 1})"
        rows = [float(r) for r in range(sr + 1, er + 2)]
        return f"np.array({rows}, dtype=float)"
    else:
        if sc == ec:
            return f"float({sc + 1})"
        cols = [float(c) for c in range(sc + 1, ec + 2)]
        return f"np.array({cols}, dtype=float)"


def _emit_switch(args: list[str]) -> str:
    if len(args) < 2:
        raise BadArity("SWITCH arity")
    expr = args[0]
    pairs = args[1:]
    if len(pairs) % 2 == 1:
        default = pairs[-1]
        cases = pairs[:-1]
    else:
        default = "None"
        cases = pairs
    res = default
    for i in range(len(cases) - 2, -1, -2):
        val = cases[i]
        ret = cases[i + 1]
        res = f"({ret} if {expr} == {val} else {res})"
    return res


def _emit_ifs(args: list[str]) -> str:
    if len(args) < 2 or len(args) % 2 != 0:
        raise BadArity("IFS arity")
    res = "None"
    for i in range(len(args) - 2, -1, -2):
        cond = args[i]
        ret = args[i + 1]
        res = f"({ret} if {cond} else {res})"
    return res


def _emit_function(node: FunctionNode, state: _CodegenState, cell_addr: str | None = None) -> str:
    name = str(node.tvalue).upper().replace("_XLFN.", "")
    if name == "ROW":
        return _emit_row_col(0, False, node, state, cell_addr)
    if name == "COLUMN":
        return _emit_row_col(1, False, node, state, cell_addr)
    if name == "ROWS":
        return _emit_row_col(0, True, node, state, cell_addr)
    if name == "COLUMNS":
        return _emit_row_col(1, True, node, state, cell_addr)
    args = [_emit_expr(arg, state, cell_addr) for arg in (node.args or [])]
    if name == "SWITCH":
        return _emit_switch(args)
    if name == "IFS":
        return _emit_ifs(args)
    emitted = _P1_FUNCTION_EMITTERS.get(name)
    if emitted is None:
        raise UnsupportedFunction(f"unsupported function {name}")
    try:
        return emitted(args)
    except IndexError as exc:
        # Catch IndexError and raise BadArity (UNSUPPORTED_ARITY). The
        # emitters index args directly, so too few arguments crash out of
        # translate_formula.
        raise BadArity(f"insufficient arguments for {name}") from exc


def _emit_if(args: list[str]) -> str:
    # Excel and Calc return FALSE when IF's condition is false and the else
    # branch is omitted. Requiring three arguments reports a legal
    # IF(cond; val) as PARSE_ERROR.
    if len(args) == 2:
        return f"({args[1]} if {args[0]} else False)"
    if len(args) == 3:
        return f"({args[1]} if {args[0]} else {args[2]})"
    raise BadArity("IF expects 2 or 3 arguments")


def _bad_arity(name: str, expected: int | str, got: int) -> str:
    raise BadArity(f"{name} expects {expected} args, got {got}")


def _calc(name: str, min_args: int = 1, max_args: int | None = None) -> Callable[[list[str]], str]:
    def emitter(args: list[str]) -> str:
        if len(args) < min_args or (max_args is not None and len(args) > max_args):
            expected = str(min_args) if max_args == min_args else f"{min_args}-{max_args}" if max_args is not None else f">={min_args}"
            _bad_arity(name, expected, len(args))
        return f"calc.{name}({', '.join(args)})"

    return emitter


# P1 function emitters: args are already Python sub-expressions using data[i].
_P1_FUNCTION_EMITTERS: dict[str, Callable[[list[str]], str]] = {
    "ACCRINT": _calc("accrint", 6, 8),
    "ACCRINTM": _calc("accrintm", 4, 6),
    "AMORDEGRC": _calc("amordegrc", 6, 7),
    "AMORLINC": _calc("amorlinc", 6, 7),
    "COUPDAYBS": _calc("coupdaybs", 3, 4),
    "COUPDAYS": _calc("coupdays", 3, 4),
    "COUPDAYSNC": _calc("coupdaysnc", 3, 4),
    "COUPNCD": _calc("coupncd", 3, 4),
    "COUPNUM": _calc("coupnum", 3, 4),
    "COUPPCD": _calc("couppcd", 3, 4),
    "CUMIPMT": _calc("cumipmt", 6, 6),
    "CUMPRINC": _calc("cumprinc", 6, 6),
    "DB": _calc("db", 4, 5),
    "DDB": _calc("ddb", 4, 5),
    "DISC": _calc("disc", 4, 5),
    # SUM: not translated — keep native =SUM(); inline np.sum(data) is lexer-safe but blank/text semantics differ from Calc.
    "AVERAGE": lambda a: (f"np.mean({a[0]})" if len(a) == 1 else f"np.mean(np.concatenate([np.asarray(x).ravel() for x in [{', '.join(a)}]]))") if len(a) >= 1 else _bad_arity("AVERAGE", ">=1", 0),
    "PRODUCT": lambda a: (f"np.prod({a[0]})" if len(a) == 1 else f"np.prod([np.prod(x) for x in [{', '.join(a)}]])") if len(a) >= 1 else _bad_arity("PRODUCT", ">=1", 0),
    "MAX": lambda a: (f"np.nanmax({a[0]})" if len(a) == 1 else f"np.nanmax([np.nanmax(x) for x in [{', '.join(a)}]])") if len(a) >= 1 else _bad_arity("MAX", ">=1", 0),
    "MIN": lambda a: (f"np.nanmin({a[0]})" if len(a) == 1 else f"np.nanmin([np.nanmin(x) for x in [{', '.join(a)}]])") if len(a) >= 1 else _bad_arity("MIN", ">=1", 0),
    "COUNT": lambda a: (f"np.sum(np.isfinite(np.asarray({a[0]}, dtype=float).ravel()))" if len(a) == 1 else f"sum(np.sum(np.isfinite(np.asarray(x, dtype=float).ravel())) for x in [{', '.join(a)}])") if len(a) >= 1 else _bad_arity("COUNT", ">=1", 0),
    "COUNTA": lambda a: (f"sum(1 for x in np.asarray({a[0]}).ravel() if x is not None and str(x) != '')" if len(a) == 1 else f"sum(sum(1 for val in np.asarray(x).ravel() if val is not None and str(val) != '') for x in [{', '.join(a)}])") if len(a) >= 1 else _bad_arity("COUNTA", ">=1", 0),
    "ABS": lambda a: f"np.abs({a[0]})" if len(a) == 1 else _bad_arity("ABS", 1, len(a)),
    "SQRT": lambda a: f"np.sqrt({a[0]})" if len(a) == 1 else _bad_arity("SQRT", 1, len(a)),
    "SIGN": lambda a: f"np.sign({a[0]})" if len(a) == 1 else _bad_arity("SIGN", 1, len(a)),
    "INT": lambda a: f"np.floor({a[0]})" if len(a) == 1 else _bad_arity("INT", 1, len(a)),
    "TRUNC": lambda a: f"np.trunc({a[0]})" if len(a) == 1 else _bad_arity("TRUNC", 1, len(a)),
    "EXP": lambda a: f"np.exp({a[0]})" if len(a) == 1 else _bad_arity("EXP", 1, len(a)),
    "LN": lambda a: f"np.log({a[0]})" if len(a) == 1 else _bad_arity("LN", 1, len(a)),
    "LOG10": lambda a: f"np.log10({a[0]})" if len(a) == 1 else _bad_arity("LOG10", 1, len(a)),
    # Parenthesize every compound or infix emitter (MOD, POWER, QUOTIENT,
    # and the rest). Without the outer parentheses, =2*MOD(A1;B1) emits
    # (2 * a % b), which Python evaluates as (2*a)%b, and 1/LOG(...)
    # becomes (1/np.log(a)/np.log(b)).
    "MOD": lambda a: f"({a[0]} % {a[1]})" if len(a) == 2 else _bad_arity("MOD", 2, len(a)),
    "POWER": lambda a: f"({a[0]} ** {a[1]})" if len(a) == 2 else _bad_arity("POWER", 2, len(a)),
    "ROUND": lambda a: (f"np.round({a[0]}, {a[1]})" if len(a) > 1 else f"np.round({a[0]})") if 1 <= len(a) <= 2 else _bad_arity("ROUND", "1-2", len(a)),
    "SIN": lambda a: f"np.sin({a[0]})" if len(a) == 1 else _bad_arity("SIN", 1, len(a)),
    "COS": lambda a: f"np.cos({a[0]})" if len(a) == 1 else _bad_arity("COS", 1, len(a)),
    "TAN": lambda a: f"np.tan({a[0]})" if len(a) == 1 else _bad_arity("TAN", 1, len(a)),
    "NOT": lambda a: f"(not {a[0]})" if len(a) == 1 else _bad_arity("NOT", 1, len(a)),
    "TRUE": lambda _a: "True",
    "FALSE": lambda _a: "False",
    "PI": lambda _a: "math.pi",
    "IF": _emit_if,
    "AND": lambda a: f"all([{', '.join(a)}])" if len(a) >= 1 else _bad_arity("AND", ">=1", 0),
    "OR": lambda a: f"any([{', '.join(a)}])" if len(a) >= 1 else _bad_arity("OR", ">=1", 0),
    # Text (P2)
    "CONCATENATE": lambda a: f'"".join(str(x) for x in [{", ".join(a)}])',
    "CONCAT": lambda a: f'"".join(str(x) for x in np.asarray([{", ".join(a)}]).ravel())',
    "LEFT": lambda a: (f"str({a[0]})[:int({a[1]})]" if len(a) > 1 else f"str({a[0]})[:1]") if 1 <= len(a) <= 2 else _bad_arity("LEFT", "1-2", len(a)),
    "RIGHT": lambda a: (f"str({a[0]})[-int({a[1]}):]" if len(a) > 1 else f"str({a[0]})[-1:]") if 1 <= len(a) <= 2 else _bad_arity("RIGHT", "1-2", len(a)),
    "MID": lambda a: f"str({a[0]})[max(0, int({a[1]})-1) : max(0, int({a[1]})-1) + int({a[2]})]" if len(a) == 3 else _bad_arity("MID", 3, len(a)),
    "LEN": lambda a: f"float(len(str({a[0]})))" if len(a) == 1 else _bad_arity("LEN", 1, len(a)),
    "LOWER": lambda a: f"str({a[0]}).lower()" if len(a) == 1 else _bad_arity("LOWER", 1, len(a)),
    "UPPER": lambda a: f"str({a[0]}).upper()" if len(a) == 1 else _bad_arity("UPPER", 1, len(a)),
    "PROPER": lambda a: f"str({a[0]}).title()" if len(a) == 1 else _bad_arity("PROPER", 1, len(a)),
    "TRIM": lambda a: f"str({a[0]}).strip()" if len(a) == 1 else _bad_arity("TRIM", 1, len(a)),
    "SUBSTITUTE": lambda a: (f"str({a[0]}).replace(str({a[1]}), str({a[2]}))" if len(a) > 2 else f'str({a[0]}).replace(str({a[1]}), "")') if 2 <= len(a) <= 4 else _bad_arity("SUBSTITUTE", "2-4", len(a)),
    "REPLACE": lambda a: f"(str({a[0]})[:max(0, int({a[1]})-1)] + str({a[3]}) + str({a[0]})[max(0, int({a[1]})-1) + int({a[2]}):])" if len(a) == 4 else _bad_arity("REPLACE", 4, len(a)),
    "FIND": lambda a: f"float(str({a[1]}).find(str({a[0]})) + 1)" if 2 <= len(a) <= 3 else _bad_arity("FIND", "2-3", len(a)),
    "SEARCH": lambda a: f"float(str({a[1]}).lower().find(str({a[0]}).lower()) + 1)" if 2 <= len(a) <= 3 else _bad_arity("SEARCH", "2-3", len(a)),
    "VALUE": lambda a: f"float({a[0]})" if len(a) == 1 else _bad_arity("VALUE", 1, len(a)),
    # Date & Time (P2) — use auto-imported ``dt`` (datetime as dt)
    "TODAY": lambda _a: "float(dt.date.today().toordinal() - 693594)",
    "NOW": lambda _a: "float(dt.datetime.now().toordinal() - 693594)",
    "YEAR": lambda a: f"float(dt.date.fromordinal(int({a[0]}) + 693594).year)" if len(a) == 1 else _bad_arity("YEAR", 1, len(a)),
    "MONTH": lambda a: f"float(dt.date.fromordinal(int({a[0]}) + 693594).month)" if len(a) == 1 else _bad_arity("MONTH", 1, len(a)),
    "DAY": lambda a: f"float(dt.date.fromordinal(int({a[0]}) + 693594).day)" if len(a) == 1 else _bad_arity("DAY", 1, len(a)),
    # Statistical (P2)
    "STDEV": lambda a: f"np.std({a[0]}, ddof=1)" if len(a) >= 1 else _bad_arity("STDEV", ">=1", 0),
    "STDEVP": lambda a: f"np.std({a[0]}, ddof=0)" if len(a) >= 1 else _bad_arity("STDEVP", ">=1", 0),
    "VAR": lambda a: f"np.var({a[0]}, ddof=1)" if len(a) >= 1 else _bad_arity("VAR", ">=1", 0),
    "VARP": lambda a: f"np.var({a[0]}, ddof=0)" if len(a) >= 1 else _bad_arity("VARP", ">=1", 0),
    "TRANSPOSE": lambda a: f"np.asarray({a[0]}).T.tolist()" if len(a) == 1 else _bad_arity("TRANSPOSE", 1, len(a)),
    # Lookup & Reference (P2)
    "VLOOKUP": lambda a: f"next((r[int({a[2]})-1] for r in np.asarray({a[1]}) if r[0] == {a[0]}), None)" if 3 <= len(a) <= 4 else _bad_arity("VLOOKUP", "3-4", len(a)),
    "HLOOKUP": lambda a: f"next((np.asarray({a[1]})[int({a[2]})-1, i] for i, val in enumerate(np.asarray({a[1]})[0]) if val == {a[0]}), None)" if 3 <= len(a) <= 4 else _bad_arity("HLOOKUP", "3-4", len(a)),
    "INDEX": lambda a: (f"np.asarray({a[0]})[int({a[1]})-1, int({a[2]})-1]" if len(a) > 2 else f"np.asarray({a[0]})[int({a[1]})-1]") if 2 <= len(a) <= 4 else _bad_arity("INDEX", "2-4", len(a)),
    "MATCH": lambda a: f"float(next((i+1 for i, val in enumerate(np.asarray({a[1]}).ravel()) if val == {a[0]}), -1))" if 2 <= len(a) <= 3 else _bad_arity("MATCH", "2-3", len(a)),
    # Logical (P2)
    "IFERROR": lambda a: f"calc.iferror(lambda: {a[0]}, {a[1]})" if len(a) == 2 else _bad_arity("IFERROR", 2, len(a)),
    "IFNA": lambda a: f"calc.ifna(lambda: {a[0]}, {a[1]})" if len(a) == 2 else _bad_arity("IFNA", 2, len(a)),
    # Math & Trig (P2)
    "ASIN": lambda a: f"np.arcsin({a[0]})" if len(a) == 1 else _bad_arity("ASIN", 1, len(a)),
    "ACOS": lambda a: f"np.arccos({a[0]})" if len(a) == 1 else _bad_arity("ACOS", 1, len(a)),
    "ATAN": lambda a: f"np.arctan({a[0]})" if len(a) == 1 else _bad_arity("ATAN", 1, len(a)),
    "ATAN2": lambda a: f"np.arctan2({a[1]}, {a[0]})" if len(a) == 2 else _bad_arity("ATAN2", 2, len(a)),
    "ACOSH": lambda a: f"np.arccosh({a[0]})" if len(a) == 1 else _bad_arity("ACOSH", 1, len(a)),
    "ASINH": lambda a: f"np.arcsinh({a[0]})" if len(a) == 1 else _bad_arity("ASINH", 1, len(a)),
    "ATANH": lambda a: f"np.arctanh({a[0]})" if len(a) == 1 else _bad_arity("ATANH", 1, len(a)),
    "COSH": lambda a: f"np.cosh({a[0]})" if len(a) == 1 else _bad_arity("COSH", 1, len(a)),
    "SINH": lambda a: f"np.sinh({a[0]})" if len(a) == 1 else _bad_arity("SINH", 1, len(a)),
    "TANH": lambda a: f"np.tanh({a[0]})" if len(a) == 1 else _bad_arity("TANH", 1, len(a)),
    "DEGREES": lambda a: f"np.degrees({a[0]})" if len(a) == 1 else _bad_arity("DEGREES", 1, len(a)),
    "RADIANS": lambda a: f"np.radians({a[0]})" if len(a) == 1 else _bad_arity("RADIANS", 1, len(a)),
    "GCD": lambda a: f"math.gcd({', '.join(a)})" if len(a) > 1 else (f"math.gcd({a[0]}, 0)" if len(a) == 1 else _bad_arity("GCD", ">=1", 0)),
    "LCM": lambda a: f"math.lcm({', '.join(a)})" if len(a) > 1 else (f"int({a[0]})" if len(a) == 1 else _bad_arity("LCM", ">=1", 0)),
    "FACT": _calc("fact", 1, 1),
    "COMBIN": _calc("combin", 2, 2),
    "REPT": _calc("rept", 2, 2),
    "EXACT": lambda a: f"(str({a[0]}) == str({a[1]}))" if len(a) == 2 else _bad_arity("EXACT", 2, len(a)),
    "ARABIC": _calc("arabic", 1, 1),
    "BAHTTEXT": _calc("bahttext", 1, 1),
    "CLEAN": _calc("clean", 1, 1),
    "DOLLAR": _calc("dollar", 1, 2),
    "ENCODEURL": _calc("encodeurl", 1, 1),
    "FIXED": _calc("fixed", 1, 3),
    "JIS": _calc("jis", 1, 1),
    "NUMBERVALUE": _calc("numbervalue", 1, 3),
    "T": _calc("t", 1, 1),
    "TEXTAFTER": _calc("textafter", 2, 6),
    "TEXTBEFORE": _calc("textbefore", 2, 6),
    "TEXTSPLIT": _calc("textsplit", 2, 6),
    "UNICHAR": _calc("unichar", 1, 1),
    "UNICODE": _calc("unicode", 1, 1),
    "BESSELI": _calc("besseli", 2, 2),
    "BESSELJ": _calc("besselj", 2, 2),
    # Date & Time (P2)
    "DATE": lambda a: f"float(dt.date(int({a[0]}), int({a[1]}), int({a[2]})).toordinal() - 693594)" if len(a) == 3 else _bad_arity("DATE", 3, len(a)),
    "HOUR": lambda a: f"float((dt.datetime.fromordinal(693594) + dt.timedelta(days=float({a[0]}))).hour)" if len(a) == 1 else _bad_arity("HOUR", 1, len(a)),
    "MINUTE": lambda a: f"float((dt.datetime.fromordinal(693594) + dt.timedelta(days=float({a[0]}))).minute)" if len(a) == 1 else _bad_arity("MINUTE", 1, len(a)),
    "SECOND": lambda a: f"float((dt.datetime.fromordinal(693594) + dt.timedelta(days=float({a[0]}))).second)" if len(a) == 1 else _bad_arity("SECOND", 1, len(a)),
    "DATEVALUE": _calc("datevalue", 1, 1),
    "TIMEVALUE": _calc("timevalue", 1, 1),
    # Conditional Aggregates
    "SUMIF": _calc("sumif", 2, 3),
    "SUMIFS": _calc("sumifs", 3),
    "COUNTIF": _calc("countif", 2, 2),
    "COUNTIFS": _calc("countifs", 2),
    "AVERAGEIF": _calc("averageif", 2, 3),
    "AVERAGEIFS": _calc("averageifs", 3),
    "N": _calc("n", 1, 1),
    "TYPE": _calc("type", 1, 1),
    # Lookup & Reference (XLOOKUP)
    "XLOOKUP": _calc("xlookup", 3, 6),
    # Text (TEXTJOIN, REGEX)
    "TEXTJOIN": _calc("textjoin", 3),
    "REGEX": _calc("regex", 2, 4),
    # Date & Time (EOMONTH, NETWORKDAYS)
    "EOMONTH": _calc("eomonth", 2, 2),
    "NETWORKDAYS": _calc("networkdays", 2, 3),
    # Tier A — high-frequency gaps
    "SUBTOTAL": lambda a: (f"calc.subtotal({a[0]}, {a[1]})" if len(a) > 1 else f"calc.subtotal(9, {a[0]})") if 1 <= len(a) <= 2 else _bad_arity("SUBTOTAL", "1-2", len(a)),
    "ISBLANK": _calc("isblank", 1, 1),
    "ISNUMBER": _calc("isnumber", 1, 1),
    "ISNA": _calc("isna", 1, 1),
    "ISERROR": _calc("iserror", 1, 1),
    "LOOKUP": _calc("lookup", 2, 3),
    "MEDIAN": lambda a: f"np.median({a[0]})" if len(a) >= 1 else _bad_arity("MEDIAN", ">=1", 0),
    "COUNTBLANK": lambda a: f"sum(1 for x in np.asarray({a[0]}).ravel() if x is None or x == '')" if len(a) == 1 else _bad_arity("COUNTBLANK", 1, len(a)),
    "ROUNDUP": lambda a: (f"(np.ceil({a[0]} * 10**int({a[1]})) / 10**int({a[1]}))" if len(a) > 1 else f"np.ceil({a[0]})") if 1 <= len(a) <= 2 else _bad_arity("ROUNDUP", "1-2", len(a)),
    "ROUNDDOWN": lambda a: (f"(np.floor({a[0]} * 10**int({a[1]})) / 10**int({a[1]}))" if len(a) > 1 else f"np.floor({a[0]})") if 1 <= len(a) <= 2 else _bad_arity("ROUNDDOWN", "1-2", len(a)),
    "CEILING": lambda a: (f"np.ceil({a[0]})" if len(a) == 1 else f"(np.ceil({a[0]} / {a[1]}) * {a[1]})") if 1 <= len(a) <= 2 else _bad_arity("CEILING", "1-2", len(a)),
    "FLOOR": lambda a: (f"np.floor({a[0]})" if len(a) == 1 else f"(np.floor({a[0]} / {a[1]}) * {a[1]})") if 1 <= len(a) <= 2 else _bad_arity("FLOOR", "1-2", len(a)),
    "LOG": lambda a: (f"(np.log({a[0]}) / np.log({a[1]}))" if len(a) > 1 else f"np.log10({a[0]})") if 1 <= len(a) <= 2 else _bad_arity("LOG", "1-2", len(a)),
    "QUOTIENT": lambda a: f"({a[0]} // {a[1]})" if len(a) == 2 else _bad_arity("QUOTIENT", 2, len(a)),
    "EDATE": _calc("edate", 2, 2),
    "DATEDIF": _calc("datedif", 3, 3),
    "SUMPRODUCT": _calc("sumproduct", 1),
    # Tier B — info, stats, text, misc
    "ISTEXT": _calc("istext", 1, 1),
    "ISLOGICAL": _calc("islogical", 1, 1),
    "ISERR": _calc("iserr", 1, 1),
    "ISNONTEXT": _calc("isnontext", 1, 1),
    "PERCENTILE": lambda a: f"np.percentile(np.asarray({a[0]}, dtype=float).ravel(), float({a[1]}) * 100)" if len(a) == 2 else _bad_arity("PERCENTILE", 2, len(a)),
    "QUARTILE": _calc("quartile", 2, 2),
    "RANK": _calc("rank", 2, 3),
    "LARGE": _calc("large", 2, 2),
    "SMALL": _calc("small", 2, 2),
    "CORREL": lambda a: f"np.corrcoef(np.asarray({a[0]}).ravel(), np.asarray({a[1]}).ravel())[0, 1]" if len(a) == 2 else _bad_arity("CORREL", 2, len(a)),
    "COVAR": lambda a: f"np.cov(np.asarray({a[0]}).ravel(), np.asarray({a[1]}).ravel())[0, 1]" if len(a) == 2 else _bad_arity("COVAR", 2, len(a)),
    "MODE": _calc("mode", 1),
    "AVERAGEA": _calc("averagea", 1),
    "TEXT": lambda a: (f"calc.fmt({a[0]}, {a[1]})" if len(a) > 1 else f"calc.py_str({a[0]})") if 1 <= len(a) <= 2 else _bad_arity("TEXT", "1-2", len(a)),
    "EVEN": _calc("even", 1, 1),
    "ODD": _calc("odd", 1, 1),
    "RAND": lambda _a: "float(np.random.random())",
    "RANDBETWEEN": lambda a: f"float(np.random.randint(int({a[0]}), int({a[1]}) + 1))" if len(a) == 2 else _bad_arity("RANDBETWEEN", 2, len(a)),
    "XMATCH": _calc("xmatch", 2, 4),
    "WEEKDAY": _calc("weekday", 1, 2),
    "WEEKNUM": _calc("weeknum", 1, 2),
    "WORKDAY": _calc("workday", 2, 3),
    # Group B — Financial 2
    "DOLLARDE": _calc("dollarde", 2, 2),
    "DOLLARFR": _calc("dollarfr", 2, 2),
    "DURATION": _calc("duration", 5, 6),
    "EFFECT": _calc("effect", 2, 2),
    "FVSCHEDULE": _calc("fvschedule", 2, 2),
    "INTRATE": _calc("intrate", 4, 5),
    "IPMT": _calc("ipmt", 4, 6),
    "ISPMT": _calc("ispmt", 4, 4),
    "MDURATION": _calc("mduration", 5, 6),
    "MIRR": _calc("mirr", 3, 3),
    "NOMINAL": _calc("nominal", 2, 2),
    "NPER": _calc("nper", 3, 5),
    "ODDFPRICE": _calc("oddfprice", 8, 9),
    "ODDFYIELD": _calc("oddfyield", 8, 9),
    "ODDLPRICE": _calc("oddlprice", 7, 8),
    # Tier C — dynamic array helpers (LO 24.8+)
    "FILTER": _calc("filter", 2, 3),
    "SORT": _calc("sort", 1, 3),
    "UNIQUE": _calc("unique", 1, 3),
    "SORTBY": _calc("sortby", 2),
    "PMT": _calc("pmt", 3, 5),
    "FV": _calc("fv", 3, 5),
    "PV": _calc("pv", 3, 5),
    "MROUND": _calc("mround", 2, 2),
    "SUMSQ": _calc("sumsq", 1),
    "ISEVEN": _calc("iseven", 1, 1),
    "ISODD": _calc("isodd", 1, 1),
    "DAYS": _calc("days", 2, 2),
    "TIME": _calc("time", 3, 3),
    "TRIMMEAN": _calc("trimmean", 2, 2),
    "FORECAST": _calc("forecast", 3, 3),
    "CHOOSE": _calc("choose", 2),
    "ADDRESS": _calc("address", 2, 5),
    "YEARFRAC": _calc("yearfrac", 2, 3),
    "DAYS360": _calc("days360", 2, 3),
    "NETWORKDAYS.INTL": _calc("networkdays_intl", 2, 4),
    "WORKDAY.INTL": _calc("workday_intl", 2, 4),
    "XOR": _calc("xor", 1),
    "XIRR": _calc("xirr", 2, 3),
    "XNPV": _calc("xnpv", 3, 3),
    "YIELD": _calc("yield_calc", 6, 7),
    "YIELDDISC": _calc("yielddisc", 4, 5),
    "YIELDMAT": _calc("yieldmat", 5, 6),
    "ISFORMULA": _calc("isformula", 1, 1),
    "ISREF": _calc("isref", 1, 1),
    "NA": lambda _a: "calc.na()",
    "AGGREGATE": _calc("aggregate", 3),
    "BASE": _calc("base", 2, 3),
    "DECIMAL": _calc("decimal", 2, 2),
    "MULTINOMIAL": _calc("multinomial", 1),
    "SERIESSUM": _calc("seriessum", 4, 4),
    "FREQUENCY": _calc("frequency", 2, 2),
    "GROWTH": _calc("growth", 1, 4),
    "AREAS": _calc("areas", 1, 1),
    "CHAR": _calc("char", 1, 1),
    "CODE": _calc("code", 1, 1),
    "DAVERAGE": _calc("daverage", 3, 3),
    "DCOUNT": _calc("dcount", 3, 3),
    "DMAX": _calc("dmax", 3, 3),
    "DMIN": _calc("dmin", 3, 3),
    "DSUM": _calc("dsum", 3, 3),
    "DCOUNTA": _calc("dcounta", 3, 3),
    "DGET": _calc("dget", 3, 3),
    "DPRODUCT": _calc("dproduct", 3, 3),
    "DSTDEV": _calc("dstdev", 3, 3),
    "DSTDEVP": _calc("dstdevp", 3, 3),
    "DVAR": _calc("dvar", 3, 3),
    "DVARP": _calc("dvarp", 3, 3),
    "ISOWEEKNUM": _calc("isoweeknum", 1, 1),
    "FACTDOUBLE": _calc("factdouble", 1, 1),
    "COMBINA": _calc("combina", 2, 2),
    "AVEDEV": _calc("avedev", 1),
    "GEOMEAN": _calc("geomean", 1),
    "HARMEAN": _calc("harmean", 1),
    "NPV": _calc("npv", 2),
    "IRR": _calc("irr", 1, 2),
    "DEVSQ": _calc("devsq", 1),
    "KURT": _calc("kurt", 1),
    "SKEW": _calc("skew", 1),
    "SLOPE": _calc("slope", 2, 2),
    "INTERCEPT": _calc("intercept", 2, 2),
    "RSQ": _calc("rsq", 2, 2),
    "STEYX": _calc("steyx", 2, 2),
    "ACOT": _calc("acot", 1, 1),
    "ACOTH": _calc("acoth", 1, 1),
    "COT": _calc("cot", 1, 1),
    "COTH": _calc("coth", 1, 1),
    "CSC": _calc("csc", 1, 1),
    "CSCH": _calc("csch", 1, 1),
    "SEC": _calc("sec", 1, 1),
    "SECH": _calc("sech", 1, 1),
    "STDEVA": _calc("stdeva", 1),
    "STDEVPA": _calc("stdevpa", 1),
    "VARA": _calc("vara", 1),
    "VARPA": _calc("varpa", 1),
    "MAXA": _calc("maxa", 1),
    "MINA": _calc("mina", 1),
    "EXPONDIST": _calc("expondist", 2, 3),
    "FDIST": _calc("fdist", 3, 3),
    "FINV": _calc("finv", 3, 3),
    "FISHER": _calc("fisher", 1, 1),
    "FISHERINV": _calc("fisherinv", 1, 1),
    "GAMMA": _calc("gamma", 1, 1),
    "GAMMADIST": _calc("gammadist", 3, 4),
    "GAMMAINV": _calc("gammainv", 3, 3),
    "GAMMALN": _calc("gammaln", 1, 1),
    "GAUSS": _calc("gauss", 1, 1),
    "HYPGEOMDIST": _calc("hypgeomdist", 4, 4),
    "LOGINV": _calc("loginv", 3, 3),
    "LOGNORMDIST": _calc("lognormdist", 3, 4),
    "NEGBINOMDIST": _calc("negbinomdist", 3, 3),
    "NORMDIST": _calc("normdist", 3, 4),
    "ERF": _calc("erf", 1, 2),
    "ERFC": _calc("erfc", 1, 1),
    "DELTA": _calc("delta", 1, 2),
    "GESTEP": _calc("gestep", 1, 2),
    "SQRTPI": _calc("sqrtpi", 1, 1),
    "BITAND": _calc("bitand", 2, 2),
    "BITOR": _calc("bitor", 2, 2),
    "BITXOR": _calc("bitxor", 2, 2),
    "BITLSHIFT": _calc("bitlshift", 2, 2),
    "BITRSHIFT": _calc("bitrshift", 2, 2),
    "COMPLEX": _calc("complex", 2, 3),
    "IMABS": _calc("imabs", 1, 1),
    "IMAGINARY": _calc("imaginary", 1, 1),
    "IMARGUMENT": _calc("imargument", 1, 1),
    "IMCONJUGATE": _calc("imconjugate", 1, 1),
    "IMCOS": _calc("imcos", 1, 1),
    "IMDIV": _calc("imdiv", 2, 2),
    "IMEXP": _calc("imexp", 1, 1),
    "IMLN": _calc("imln", 1, 1),
    "IMLOG10": _calc("imlog10", 1, 1),
    "IMLOG2": _calc("imlog2", 1, 1),
    "IMPOWER": _calc("impower", 2, 2),
    "IMPRODUCT": _calc("improduct", 1),
    "IMREAL": _calc("imreal", 1, 1),
    "IMSIN": _calc("imsin", 1, 1),
    "BESSELK": _calc("besselk", 2, 2),
    "BESSELY": _calc("bessely", 2, 2),
    "EUROCONVERT": _calc("euroconvert", 3, 5),
    "IMCOSH": _calc("imcosh", 1, 1),
    "IMCOT": _calc("imcot", 1, 1),
    "IMCSC": _calc("imcsc", 1, 1),
    "IMCSCH": _calc("imcsch", 1, 1),
    "IMSEC": _calc("imsec", 1, 1),
    "IMSECH": _calc("imsech", 1, 1),
    "IMSINH": _calc("imsinh", 1, 1),
    "IMSQRT": _calc("imsqrt", 1, 1),
    "IMSUB": _calc("imsub", 2, 2),
    "IMSUM": _calc("imsum", 1),
    "IMTAN": _calc("imtan", 1, 1),
    "IMTANH": _calc("imtanh", 1, 1),
    # Group E
    "LINEST": _calc("linest", 1, 4),
    "LOGEST": _calc("logest", 1, 4),
    "MDETERM": _calc("mdeterm", 1, 1),
    "MINVERSE": _calc("minverse", 1, 1),
    "MMULT": _calc("mmult", 2, 2),
    "MTRANS": _calc("mtrans", 1, 1),
    "MUNIT": _calc("munit", 1, 1),
    "TREND": _calc("trend", 1, 4),
    "BETADIST": _calc("betadist", 3, 5),
    "BETAINV": _calc("betainv", 3, 5),
    "BINOMDIST": _calc("binomdist", 4, 4),
    "CHIDIST": _calc("chidist", 2, 2),
    "CHIINV": _calc("chiinv", 2, 2),
    "CONFIDENCE": _calc("confidence", 3, 3),
    "CRITBINOM": _calc("critbinom", 3, 3),
    "NORMINV": _calc("norminv", 3, 3),
    "NORMSDIST": _calc("normsdist", 1, 1),
    "NORMSINV": _calc("normsinv", 1, 1),
    "PEARSON": _calc("pearson", 2, 2),
    "PERCENTRANK": _calc("percentrank", 2, 3),
    "PERMUT": _calc("permut", 2, 2),
    "POISSON": lambda a: (f"calc.poisson({a[0]}, {a[1]}, {a[2] if len(a) > 2 else 'False'})") if 2 <= len(a) <= 3 else _bad_arity("POISSON", "2-3", len(a)),
    "PROB": _calc("prob", 3, 4),
    "STANDARDIZE": _calc("standardize", 3, 3),
    "TDIST": _calc("tdist", 3, 3),
    "TINV": _calc("tinv", 2, 2),
    "TTEST": _calc("ttest", 4, 4),
    "WEIBULL": _calc("weibull", 3, 4),
    "ZTEST": _calc("ztest", 2, 3),
    "ASC": _calc("asc", 1, 1),
}


def translate_formula(formula: str, cell_addr: str | None = None) -> TranslationResult:
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
        body = _emit_expr(ast, state, cell_addr)
    except TranslationError as exc:
        return TranslationResult(ok=False, reason=exc.reason)
    except IndexError:
        return TranslationResult(ok=False, reason="UNSUPPORTED_ARITY")
    except (ValueError, TypeError):
        return TranslationResult(ok=False, reason="PARSE_ERROR")

    return TranslationResult(ok=True, code=sanitize_inline_py_code(body), data_ranges=list(state.ranges))
