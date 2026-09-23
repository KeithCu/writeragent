# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Translate P1 Calc formulas to ``=PY()`` Python source via vendored AST."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.contrib.calc_formula_parser import (
    FunctionNode,
    OperandNode,
    OperatorNode,
    RangeNode,
    parse_formula,
)
from plugin.calc.python.formula_edit import sanitize_inline_py_code
from plugin.calc.spreadsheet_import.models import TranslationResult
from plugin.calc.spreadsheet_import.preprocess import normalize_lo_formula_for_parse
from plugin.calc.address_utils import parse_address, parse_range_string

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


def _emit_expr(node: Any, state: _CodegenState, cell_addr: str | None = None) -> str:
    if isinstance(node, RangeNode):
        return state.ref_expr(node.address)
    if isinstance(node, OperandNode):
        return _emit_operand(node)
    if isinstance(node, OperatorNode):
        return _emit_operator(node, state, cell_addr)
    if isinstance(node, FunctionNode):
        return _emit_function(node, state, cell_addr)
    raise ValueError(f"unknown node {type(node)}")


def _emit_operator(node: OperatorNode, state: _CodegenState, cell_addr: str | None = None) -> str:
    if node.ttype == "operator-prefix":
        rhs = _emit_expr(node.right, state, cell_addr)
        if node.tvalue == "-":
            return f"(-{rhs})"
        if node.tvalue == "+":
            return rhs
        raise ValueError("unsupported prefix op")
    if node.ttype != "operator-infix":
        raise ValueError("unsupported operator type")
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


def _emit_row_func(node: FunctionNode, state: _CodegenState, cell_addr: str | None = None) -> str:
    if not node.args:
        if cell_addr:
            try:
                _unused, r = parse_address(cell_addr)
                return f"float({r + 1})"
            except ValueError:
                pass
        return "float(1)"
    arg = node.args[0]
    if isinstance(arg, RangeNode):
        try:
            (_sc, sr), (_ec, er) = parse_range_string(arg.address)
            if sr == er:
                return f"float({sr + 1})"
            rows = [float(r) for r in range(sr + 1, er + 2)]
            return f"np.array({rows}, dtype=float)"
        except ValueError:
            pass
    return "float(1)"


def _emit_col_func(node: FunctionNode, state: _CodegenState, cell_addr: str | None = None) -> str:
    if not node.args:
        if cell_addr:
            try:
                c, _unused = parse_address(cell_addr)
                return f"float({c + 1})"
            except ValueError:
                pass
        return "float(1)"
    arg = node.args[0]
    if isinstance(arg, RangeNode):
        try:
            (sc, _sr), (ec, _er) = parse_range_string(arg.address)
            if sc == ec:
                return f"float({sc + 1})"
            cols = [float(c) for c in range(sc + 1, ec + 2)]
            return f"np.array({cols}, dtype=float)"
        except ValueError:
            pass
    return "float(1)"


def _emit_rows_func(node: FunctionNode, state: _CodegenState) -> str:
    if not node.args:
        raise ValueError("ROWS arity")
    arg = node.args[0]
    if isinstance(arg, RangeNode):
        try:
            (_sc, sr), (_ec, er) = parse_range_string(arg.address)
            return f"float({abs(er - sr) + 1})"
        except ValueError:
            pass
    expr = _emit_expr(arg, state)
    return f"float(np.asarray({expr}).shape[0])"


def _emit_columns_func(node: FunctionNode, state: _CodegenState) -> str:
    if not node.args:
        raise ValueError("COLUMNS arity")
    arg = node.args[0]
    if isinstance(arg, RangeNode):
        try:
            (sc, _sr), (ec, _er) = parse_range_string(arg.address)
            return f"float({abs(ec - sc) + 1})"
        except ValueError:
            pass
    expr = _emit_expr(arg, state)
    return f"float(np.asarray({expr}).shape[1])" if "np.asarray" in expr or "data" in expr else "float(1.0)"


def _emit_switch(args: list[str]) -> str:
    if len(args) < 2:
        raise ValueError("SWITCH arity")
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
        ret = cases[i+1]
        res = f"({ret} if {expr} == {val} else {res})"
    return res


def _emit_ifs(args: list[str]) -> str:
    if len(args) < 2 or len(args) % 2 != 0:
        raise ValueError("IFS arity")
    res = "None"
    for i in range(len(args) - 2, -1, -2):
        cond = args[i]
        ret = args[i + 1]
        res = f"({ret} if {cond} else {res})"
    return res


# Functions that return arbitrary types — skip scalar float() wrap in translate_formula.
_NO_SCALAR_WRAP_FUNCTIONS = frozenset(
    {
        "TRUE",
        "FALSE",
        "IF",
        "IFS",
        "SWITCH",
        "AND",
        "OR",
        "NOT",
        "ISBLANK",
        "ISNUMBER",
        "ISNA",
        "ISERROR",
        "ISTEXT",
        "ISLOGICAL",
        "ISERR",
        "ISNONTEXT",
        "ISFORMULA",
        "ISREF",
        "LINEST",
        "LOGEST",
        "MINVERSE",
        "MMULT",
        "MTRANS",
        "MUNIT",
        "TREND",
    }
)

# Helpers and array-returning emitters — skip scalar float() wrap.
_NO_FLOAT_WRAP_PREFIXES = (
    "calc.iferror(",
    "calc.ifna(",
    "calc.sumif(",
    "calc.sumifs(",
    "calc.countif(",
    "calc.countifs(",
    "calc.averageif(",
    "calc.averageifs(",
    "calc.xlookup(",
    "calc.textjoin(",
    "calc.eomonth(",
    "calc.networkdays(",
    "calc.regex(",
    "calc.subtotal(",
    "calc.lookup(",
    "calc.edate(",
    "calc.datedif(",
    "calc.sumproduct(",
    "calc.averagea(",
    "calc.fmt(",
    "calc.bahttext(",
    "calc.clean(",
    "calc.dollar(",
    "calc.encodeurl(",
    "calc.fixed(",
    "calc.jis(",
    "calc.numbervalue(",
    "calc.t(",
    "calc.textafter(",
    "calc.textbefore(",
    "calc.textsplit(",
    "calc.unichar(",
    "calc.unicode(",
    "calc.besseli(",
    "calc.besselj(",
    "calc.xmatch(",
    "calc.workday(",
    "calc.filter(",
    "calc.sort(",
    "calc.unique(",
    "calc.sortby(",
    "calc.rank(",
    "calc.large(",
    "calc.small(",
    "calc.mode(",
    "calc.choose(",
    "calc.address(",
    "calc.char(",
    "calc.xor(",
    "calc.areas(",
    "calc.code(",
    "calc.yearfrac(",
    "calc.days360(",
    "calc.networkdays_intl(",
    "calc.workday_intl(",
    "calc.daverage(",
    "calc.dcount(",
    "calc.dcounta(",
    "calc.dget(",
    "calc.dmax(",
    "calc.dmin(",
    "calc.dproduct(",
    "calc.dstdev(",
    "calc.dstdevp(",
    "calc.dsum(",
    "calc.dvar(",
    "calc.dvarp(",
    "calc.isoweeknum(",
    "calc.factdouble(",
    "calc.combina(",
    "calc.avedev(",
    "calc.geomean(",
    "calc.harmean(",
    "calc.npv(",
    "calc.irr(",
    "calc.devsq(",
    "calc.kurt(",
    "calc.skew(",
    "calc.slope(",
    "calc.intercept(",
    "calc.rsq(",
    "calc.steyx(",
    "calc.acot(",
    "calc.acoth(",
    "calc.cot(",
    "calc.coth(",
    "calc.csc(",
    "calc.csch(",
    "calc.sec(",
    "calc.sech(",
    "calc.stdeva(",
    "calc.stdevpa(",
    "calc.vara(",
    "calc.varpa(",
    "calc.maxa(",
    "calc.mina(",
    "calc.erf(",
    "calc.erfc(",
    "calc.delta(",
    "calc.gestep(",
    "calc.sqrtpi(",
    "calc.bitand(",
    "calc.bitor(",
    "calc.bitxor(",
    "calc.bitlshift(",
    "calc.bitrshift(",
    "calc.complex(",
    "calc.imabs(",
    "calc.imaginary(",
    "calc.imargument(",
    "calc.imconjugate(",
    "calc.imcos(",
    "calc.imdiv(",
    "calc.imexp(",
    "calc.imln(",
    "calc.imlog10(",
    "calc.imlog2(",
    "calc.impower(",
    "calc.improduct(",
    "calc.imreal(",
    "calc.imsin(",
    "calc.besselk(",
    "calc.bessely(",
    "calc.euroconvert(",
    "calc.imcosh(",
    "calc.imcot(",
    "calc.imcsc(",
    "calc.imcsch(",
    "calc.imsec(",
    "calc.imsech(",
    "calc.imsinh(",
    "calc.imsqrt(",
    "calc.imsub(",
    "calc.imsum(",
    "calc.imtan(",
    "calc.imtanh(",
    "calc.xirr(",
    "calc.xnpv(",
    "calc.yield_calc(",
    "calc.yielddisc(",
    "calc.yieldmat(",
    "calc.na(",
    "calc.aggregate(",
    "calc.base(",
    "calc.decimal(",
    "calc.multinomial(",
    "calc.seriessum(",
    "calc.frequency(",
    "calc.growth(",
    "calc.norminv(",
    "calc.normsdist(",
    "calc.normsinv(",
    "calc.pearson(",
    "calc.percentrank(",
    "calc.permut(",
    "calc.poisson(",
    "calc.prob(",
    "calc.standardize(",
    "calc.tdist(",
    "calc.tinv(",
    "calc.ttest(",
    "calc.weibull(",
    "calc.ztest(",
    "calc.asc(",
)


def _emit_function(node: FunctionNode, state: _CodegenState, cell_addr: str | None = None) -> str:
    name = str(node.tvalue).upper().replace("_XLFN.", "")
    if name == "ROW":
        return _emit_row_func(node, state, cell_addr)
    if name == "COLUMN":
        return _emit_col_func(node, state, cell_addr)
    if name == "ROWS":
        return _emit_rows_func(node, state)
    if name == "COLUMNS":
        return _emit_columns_func(node, state)
    args = [_emit_expr(arg, state, cell_addr) for arg in (node.args or [])]
    if name == "SWITCH":
        return _emit_switch(args)
    if name == "IFS":
        return _emit_ifs(args)
    emitted = _P1_FUNCTION_EMITTERS.get(name)
    if emitted is None:
        raise ValueError(f"unsupported function {name}")
    return emitted(args)


def _emit_if(args: list[str]) -> str:
    if len(args) != 3:
        raise ValueError("IF arity")
    return f"({args[1]} if {args[0]} else {args[2]})"


# P1 function emitters: args are already Python sub-expressions using data[i].
_P1_FUNCTION_EMITTERS: dict[str, Callable[[list[str]], str]] = {
    "ACCRINT": lambda a: f"calc.accrint({', '.join(a)})",
    "ACCRINTM": lambda a: f"calc.accrintm({', '.join(a)})",
    "AMORDEGRC": lambda a: f"calc.amordegrc({', '.join(a)})",
    "AMORLINC": lambda a: f"calc.amorlinc({', '.join(a)})",
    "COUPDAYBS": lambda a: f"calc.coupdaybs({', '.join(a)})",
    "COUPDAYS": lambda a: f"calc.coupdays({', '.join(a)})",
    "COUPDAYSNC": lambda a: f"calc.coupdaysnc({', '.join(a)})",
    "COUPNCD": lambda a: f"calc.coupncd({', '.join(a)})",
    "COUPNUM": lambda a: f"calc.coupnum({', '.join(a)})",
    "COUPPCD": lambda a: f"calc.couppcd({', '.join(a)})",
    "CUMIPMT": lambda a: f"calc.cumipmt({', '.join(a)})",
    "CUMPRINC": lambda a: f"calc.cumprinc({', '.join(a)})",
    "DB": lambda a: f"calc.db({', '.join(a)})",
    "DDB": lambda a: f"calc.ddb({', '.join(a)})",
    "DISC": lambda a: f"calc.disc({', '.join(a)})",
    # SUM: not translated — keep native =SUM(); inline np.sum(data) is lexer-safe but blank/text semantics differ from Calc.
    "AVERAGE": lambda a: f"np.mean({a[0]})" if len(a) == 1 else f"np.mean(np.concatenate([np.asarray(x).ravel() for x in [{', '.join(a)}]]))",
    "PRODUCT": lambda a: f"np.prod({a[0]})" if len(a) == 1 else f"np.prod([np.prod(x) for x in [{', '.join(a)}]])",
    "MAX": lambda a: f"np.nanmax({a[0]})" if len(a) == 1 else f"np.nanmax([np.nanmax(x) for x in [{', '.join(a)}]])",
    "MIN": lambda a: f"np.nanmin({a[0]})" if len(a) == 1 else f"np.nanmin([np.nanmin(x) for x in [{', '.join(a)}]])",
    "COUNT": lambda a: f"np.sum(np.isfinite(np.asarray({a[0]}, dtype=float).ravel()))" if len(a) == 1 else f"sum(np.sum(np.isfinite(np.asarray(x, dtype=float).ravel())) for x in [{', '.join(a)}])",
    "COUNTA": lambda a: f"sum(1 for x in np.asarray({a[0]}).ravel() if x is not None and str(x) != '')" if len(a) == 1 else f"sum(sum(1 for val in np.asarray(x).ravel() if val is not None and str(val) != '') for x in [{', '.join(a)}])",
    "ABS": lambda a: f"np.abs({a[0]})",
    "SQRT": lambda a: f"np.sqrt({a[0]})",
    "SIGN": lambda a: f"np.sign({a[0]})",
    "INT": lambda a: f"np.floor({a[0]})",
    "TRUNC": lambda a: f"np.trunc({a[0]})",
    "EXP": lambda a: f"np.exp({a[0]})",
    "LN": lambda a: f"np.log({a[0]})",
    "LOG10": lambda a: f"np.log10({a[0]})",
    "MOD": lambda a: f"{a[0]} % {a[1]}",
    "POWER": lambda a: f"{a[0]} ** {a[1]}",
    "ROUND": lambda a: f"np.round({a[0]}, {a[1]})" if len(a) > 1 else f"np.round({a[0]})",
    "SIN": lambda a: f"np.sin({a[0]})",
    "COS": lambda a: f"np.cos({a[0]})",
    "TAN": lambda a: f"np.tan({a[0]})",
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
    # Date & Time (P2) — use auto-imported ``dt`` (datetime as dt)
    "TODAY": lambda _a: 'float(dt.date.today().toordinal() - 693594)',
    "NOW": lambda _a: 'float(dt.datetime.now().toordinal() - 693594)',
    "YEAR": lambda a: f'float(dt.date.fromordinal(int({a[0]}) + 693594).year)',
    "MONTH": lambda a: f'float(dt.date.fromordinal(int({a[0]}) + 693594).month)',
    "DAY": lambda a: f'float(dt.date.fromordinal(int({a[0]}) + 693594).day)',
    # Statistical (P2)
    "STDEV": lambda a: f"np.std({a[0]}, ddof=1)",
    "STDEVP": lambda a: f"np.std({a[0]}, ddof=0)",
    "VAR": lambda a: f"np.var({a[0]}, ddof=1)",
    "VARP": lambda a: f"np.var({a[0]}, ddof=0)",
    "TRANSPOSE": lambda a: f"np.asarray({a[0]}).T.tolist()",
    # Lookup & Reference (P2)
    "VLOOKUP": lambda a: f'next((r[int({a[2]})-1] for r in np.asarray({a[1]}) if r[0] == {a[0]}), None)',
    "HLOOKUP": lambda a: f'next((np.asarray({a[1]})[int({a[2]})-1, i] for i, val in enumerate(np.asarray({a[1]})[0]) if val == {a[0]}), None)',
    "INDEX": lambda a: f'np.asarray({a[0]})[int({a[1]})-1, int({a[2]})-1]' if len(a) > 2 else f'np.asarray({a[0]})[int({a[1]})-1]',
    "MATCH": lambda a: f'float(next((i+1 for i, val in enumerate(np.asarray({a[1]}).ravel()) if val == {a[0]}), -1))',
    # Logical (P2)
    "IFERROR": lambda a: f"calc.iferror(lambda: {a[0]}, {a[1]})",
    "IFNA": lambda a: f"calc.ifna(lambda: {a[0]}, {a[1]})",
    # Math & Trig (P2)
    "ASIN": lambda a: f"np.arcsin({a[0]})",
    "ACOS": lambda a: f"np.arccos({a[0]})",
    "ATAN": lambda a: f"np.arctan({a[0]})",
    "ATAN2": lambda a: f"np.arctan2({a[1]}, {a[0]})",
    "ACOSH": lambda a: f"np.arccosh({a[0]})",
    "ASINH": lambda a: f"np.arcsinh({a[0]})",
    "ATANH": lambda a: f"np.arctanh({a[0]})",
    "COSH": lambda a: f"np.cosh({a[0]})",
    "SINH": lambda a: f"np.sinh({a[0]})",
    "TANH": lambda a: f"np.tanh({a[0]})",
    "DEGREES": lambda a: f"np.degrees({a[0]})",
    "RADIANS": lambda a: f"np.radians({a[0]})",
    "GCD": lambda a: f"math.gcd({', '.join(a)})" if len(a) > 1 else f"math.gcd({a[0]}, 0)",
    "LCM": lambda a: f"math.lcm({', '.join(a)})" if len(a) > 1 else f"int({a[0]})",
    "FACT": lambda a: f"calc.fact({a[0]})",
    "COMBIN": lambda a: f"calc.combin({a[0]}, {a[1]})",
    "REPT": lambda a: f"calc.rept({a[0]}, {a[1]})",
    "EXACT": lambda a: f"(str({a[0]}) == str({a[1]}))",
    "ARABIC": lambda a: f"calc.arabic({a[0]})",

    "BAHTTEXT": lambda a: f"calc.bahttext({a[0]})",
    "CLEAN": lambda a: f"calc.clean({a[0]})",
    "DOLLAR": lambda a: f"calc.dollar({', '.join(a)})",
    "ENCODEURL": lambda a: f"calc.encodeurl({a[0]})",
    "FIXED": lambda a: f"calc.fixed({', '.join(a)})",
    "JIS": lambda a: f"calc.jis({a[0]})",
    "NUMBERVALUE": lambda a: f"calc.numbervalue({', '.join(a)})",
    "T": lambda a: f"calc.t({a[0]})",
    "TEXTAFTER": lambda a: f"calc.textafter({', '.join(a)})",
    "TEXTBEFORE": lambda a: f"calc.textbefore({', '.join(a)})",
    "TEXTSPLIT": lambda a: f"calc.textsplit({', '.join(a)})",
    "UNICHAR": lambda a: f"calc.unichar({a[0]})",
    "UNICODE": lambda a: f"calc.unicode({a[0]})",
    "BESSELI": lambda a: f"calc.besseli({', '.join(a)})",
    "BESSELJ": lambda a: f"calc.besselj({', '.join(a)})",
    # Date & Time (P2)
    "DATE": lambda a: f"float(dt.date(int({a[0]}), int({a[1]}), int({a[2]})).toordinal() - 693594)",
    "HOUR": lambda a: f"float((dt.datetime.fromordinal(693594) + dt.timedelta(days=float({a[0]}))).hour)",
    "MINUTE": lambda a: f"float((dt.datetime.fromordinal(693594) + dt.timedelta(days=float({a[0]}))).minute)",
    "SECOND": lambda a: f"float((dt.datetime.fromordinal(693594) + dt.timedelta(days=float({a[0]}))).second)",
    "DATEVALUE": lambda a: f"calc.datevalue({a[0]})",
    "TIMEVALUE": lambda a: f"calc.timevalue({a[0]})",
    # Conditional Aggregates
    "SUMIF": lambda a: f"calc.sumif({a[0]}, {a[1]}, {a[2]})" if len(a) > 2 else f"calc.sumif({a[0]}, {a[1]})",
    "SUMIFS": lambda a: f"calc.sumifs({a[0]}, {', '.join(a[1:])})",
    "COUNTIF": lambda a: f"calc.countif({a[0]}, {a[1]})",
    "COUNTIFS": lambda a: f"calc.countifs({', '.join(a)})",
    "AVERAGEIF": lambda a: f"calc.averageif({a[0]}, {a[1]}, {a[2]})" if len(a) > 2 else f"calc.averageif({a[0]}, {a[1]})",
    "AVERAGEIFS": lambda a: f"calc.averageifs({a[0]}, {', '.join(a[1:])})",
    "N": lambda a: f"calc.n({a[0]})",
    "TYPE": lambda a: f"calc.type({a[0]})",
    # Lookup & Reference (XLOOKUP)
    "XLOOKUP": lambda a: f"calc.xlookup({', '.join(a)})",
    # Text (TEXTJOIN, REGEX)
    "TEXTJOIN": lambda a: f"calc.textjoin({', '.join(a)})",
    "REGEX": lambda a: f"calc.regex({', '.join(a)})",
    # Date & Time (EOMONTH, NETWORKDAYS)
    "EOMONTH": lambda a: f"calc.eomonth({a[0]}, {a[1]})",
    "NETWORKDAYS": lambda a: f"calc.networkdays({', '.join(a)})",
    # Tier A — high-frequency gaps
    "SUBTOTAL": lambda a: f"calc.subtotal({a[0]}, {a[1]})" if len(a) > 1 else f"calc.subtotal(9, {a[0]})",
    "ISBLANK": lambda a: f"calc.isblank({a[0]})",
    "ISNUMBER": lambda a: f"calc.isnumber({a[0]})",
    "ISNA": lambda a: f"calc.isna({a[0]})",
    "ISERROR": lambda a: f"calc.iserror({a[0]})",
    "LOOKUP": lambda a: f"calc.lookup({', '.join(a)})",
    "MEDIAN": lambda a: f"np.median({a[0]})",
    "COUNTBLANK": lambda a: f"sum(1 for x in np.asarray({a[0]}).ravel() if x is None or x == '')",
    "ROUNDUP": lambda a: f"np.ceil({a[0]} * 10**int({a[1]})) / 10**int({a[1]})"
    if len(a) > 1
    else f"np.ceil({a[0]})",
    "ROUNDDOWN": lambda a: f"np.floor({a[0]} * 10**int({a[1]})) / 10**int({a[1]})"
    if len(a) > 1
    else f"np.floor({a[0]})",
    "CEILING": lambda a: f"np.ceil({a[0]})"
    if len(a) == 1
    else f"np.ceil({a[0]} / {a[1]}) * {a[1]}",
    "FLOOR": lambda a: f"np.floor({a[0]})"
    if len(a) == 1
    else f"np.floor({a[0]} / {a[1]}) * {a[1]}",
    "LOG": lambda a: f"np.log({a[0]}) / np.log({a[1]})"
    if len(a) > 1
    else f"np.log10({a[0]})",
    "QUOTIENT": lambda a: f"{a[0]} // {a[1]}",
    "EDATE": lambda a: f"calc.edate({a[0]}, {a[1]})",
    "DATEDIF": lambda a: f"calc.datedif({', '.join(a)})",
    "SUMPRODUCT": lambda a: f"calc.sumproduct({', '.join(a)})",
    # Tier B — info, stats, text, misc
    "ISTEXT": lambda a: f"calc.istext({a[0]})",
    "ISLOGICAL": lambda a: f"calc.islogical({a[0]})",
    "ISERR": lambda a: f"calc.iserr({a[0]})",
    "ISNONTEXT": lambda a: f"calc.isnontext({a[0]})",
    "PERCENTILE": lambda a: f"np.percentile(np.asarray({a[0]}, dtype=float).ravel(), float({a[1]}) * 100)",
    "QUARTILE": lambda a: f"calc.quartile({a[0]}, {a[1]})",
    "RANK": lambda a: f"calc.rank({', '.join(a)})",
    "LARGE": lambda a: f"calc.large({a[0]}, {a[1]})",
    "SMALL": lambda a: f"calc.small({a[0]}, {a[1]})",
    "CORREL": lambda a: f"np.corrcoef(np.asarray({a[0]}).ravel(), np.asarray({a[1]}).ravel())[0, 1]",
    "COVAR": lambda a: f"np.cov(np.asarray({a[0]}).ravel(), np.asarray({a[1]}).ravel())[0, 1]",
    "MODE": lambda a: f"calc.mode({a[0]})",
    "AVERAGEA": lambda a: f"calc.averagea({a[0]})",
    "TEXT": lambda a: f"calc.fmt({a[0]}, {a[1]})" if len(a) > 1 else f"calc.py_str({a[0]})",
    "EVEN": lambda a: f"calc.even({a[0]})",
    "ODD": lambda a: f"calc.odd({a[0]})",
    "RAND": lambda _a: "float(np.random.random())",
    "RANDBETWEEN": lambda a: f"float(np.random.randint(int({a[0]}), int({a[1]}) + 1))",
    "XMATCH": lambda a: f"calc.xmatch({', '.join(a)})",
    "WEEKDAY": lambda a: f"calc.weekday({a[0]})" if len(a) == 1 else f"calc.weekday({a[0]}, {a[1]})",
    "WEEKNUM": lambda a: f"calc.weeknum({', '.join(a)})",
    "WORKDAY": lambda a: f"calc.workday({', '.join(a)})",
    # Group B — Financial 2
    "DOLLARDE": lambda a: f"calc.dollarde({a[0]}, {a[1]})",
    "DOLLARFR": lambda a: f"calc.dollarfr({a[0]}, {a[1]})",
    "DURATION": lambda a: f"calc.duration({', '.join(a)})",
    "EFFECT": lambda a: f"calc.effect({a[0]}, {a[1]})",
    "FVSCHEDULE": lambda a: f"calc.fvschedule({a[0]}, {a[1]})",
    "INTRATE": lambda a: f"calc.intrate({', '.join(a)})",
    "IPMT": lambda a: f"calc.ipmt({', '.join(a)})",
    "ISPMT": lambda a: f"calc.ispmt({', '.join(a)})",
    "MDURATION": lambda a: f"calc.mduration({', '.join(a)})",
    "MIRR": lambda a: f"calc.mirr({', '.join(a)})",
    "NOMINAL": lambda a: f"calc.nominal({a[0]}, {a[1]})",
    "NPER": lambda a: f"calc.nper({', '.join(a)})",
    "ODDFPRICE": lambda a: f"calc.oddfprice({', '.join(a)})",
    "ODDFYIELD": lambda a: f"calc.oddfyield({', '.join(a)})",
    "ODDLPRICE": lambda a: f"calc.oddlprice({', '.join(a)})",
    # Tier C — dynamic array helpers (LO 24.8+)
    "FILTER": lambda a: f"calc.filter({', '.join(a)})",
    "SORT": lambda a: f"calc.sort({', '.join(a)})",
    "UNIQUE": lambda a: f"calc.unique({', '.join(a)})",
    "SORTBY": lambda a: f"calc.sortby({', '.join(a)})",
    "PMT": lambda a: f"calc.pmt({', '.join(a)})",
    "FV": lambda a: f"calc.fv({', '.join(a)})",
    "PV": lambda a: f"calc.pv({', '.join(a)})",
    "MROUND": lambda a: f"calc.mround({a[0]}, {a[1]})",
    "SUMSQ": lambda a: f"calc.sumsq({', '.join(a)})",
    "ISEVEN": lambda a: f"calc.iseven({a[0]})",
    "ISODD": lambda a: f"calc.isodd({a[0]})",
    "DAYS": lambda a: f"calc.days({a[0]}, {a[1]})",
    "TIME": lambda a: f"calc.time({a[0]}, {a[1]}, {a[2]})",
    "TRIMMEAN": lambda a: f"calc.trimmean({a[0]}, {a[1]})",
    "FORECAST": lambda a: f"calc.forecast({a[0]}, {a[1]}, {a[2]})",
    "CHOOSE": lambda a: f"calc.choose({a[0]}, {', '.join(a[1:])})",
    "ADDRESS": lambda a: f"calc.address({', '.join(a)})",
    "YEARFRAC": lambda a: f"calc.yearfrac({', '.join(a)})",
    "DAYS360": lambda a: f"calc.days360({', '.join(a)})",
    "NETWORKDAYS.INTL": lambda a: f"calc.networkdays_intl({', '.join(a)})",
    "WORKDAY.INTL": lambda a: f"calc.workday_intl({', '.join(a)})",
    "XOR": lambda a: f"calc.xor({', '.join(a)})",
    "XIRR": lambda a: f"calc.xirr({a[0]}, {a[1]})" if len(a) == 2 else f"calc.xirr({a[0]}, {a[1]}, {a[2]})",
    "XNPV": lambda a: f"calc.xnpv({a[0]}, {a[1]}, {a[2]})",
    "YIELD": lambda a: f"calc.yield_calc({', '.join(a)})",
    "YIELDDISC": lambda a: f"calc.yielddisc({', '.join(a)})",
    "YIELDMAT": lambda a: f"calc.yieldmat({', '.join(a)})",
    "ISFORMULA": lambda a: f"calc.isformula({a[0]})",
    "ISREF": lambda a: f"calc.isref({a[0]})",
    "NA": lambda _a: "calc.na()",
    "AGGREGATE": lambda a: f"calc.aggregate({a[0]}, {a[1]}, {', '.join(a[2:])})",
    "BASE": lambda a: f"calc.base({', '.join(a)})",
    "DECIMAL": lambda a: f"calc.decimal({a[0]}, {a[1]})",
    "MULTINOMIAL": lambda a: f"calc.multinomial({', '.join(a)})",
    "SERIESSUM": lambda a: f"calc.seriessum({a[0]}, {a[1]}, {a[2]}, {a[3]})",
    "FREQUENCY": lambda a: f"calc.frequency({a[0]}, {a[1]})",
    "GROWTH": lambda a: f"calc.growth({', '.join(a)})",
    "AREAS": lambda a: f"calc.areas({a[0]})",
    "CHAR": lambda a: f"calc.char({a[0]})",
    "CODE": lambda a: f"calc.code({a[0]})",
    "DAVERAGE": lambda a: f"calc.daverage({a[0]}, {a[1]}, {a[2]})",
    "DCOUNT": lambda a: f"calc.dcount({a[0]}, {a[1]}, {a[2]})",
    "DMAX": lambda a: f"calc.dmax({a[0]}, {a[1]}, {a[2]})",
    "DMIN": lambda a: f"calc.dmin({a[0]}, {a[1]}, {a[2]})",
    "DSUM": lambda a: f"calc.dsum({a[0]}, {a[1]}, {a[2]})",
    "DCOUNTA": lambda a: f"calc.dcounta({a[0]}, {a[1]}, {a[2]})",
    "DGET": lambda a: f"calc.dget({a[0]}, {a[1]}, {a[2]})",
    "DPRODUCT": lambda a: f"calc.dproduct({a[0]}, {a[1]}, {a[2]})",
    "DSTDEV": lambda a: f"calc.dstdev({a[0]}, {a[1]}, {a[2]})",
    "DSTDEVP": lambda a: f"calc.dstdevp({a[0]}, {a[1]}, {a[2]})",
    "DVAR": lambda a: f"calc.dvar({a[0]}, {a[1]}, {a[2]})",
    "DVARP": lambda a: f"calc.dvarp({a[0]}, {a[1]}, {a[2]})",
    "ISOWEEKNUM": lambda a: f"calc.isoweeknum({a[0]})",
    "FACTDOUBLE": lambda a: f"calc.factdouble({a[0]})",
    "COMBINA": lambda a: f"calc.combina({a[0]}, {a[1]})",
    "AVEDEV": lambda a: f"calc.avedev({a[0]})",
    "GEOMEAN": lambda a: f"calc.geomean({a[0]})",
    "HARMEAN": lambda a: f"calc.harmean({a[0]})",
    "NPV": lambda a: f"calc.npv({a[0]}, {', '.join(a[1:])})",
    "IRR": lambda a: f"calc.irr({a[0]})" if len(a) == 1 else f"calc.irr({a[0]}, {a[1]})",
    "DEVSQ": lambda a: f"calc.devsq({', '.join(a)})",
    "KURT": lambda a: f"calc.kurt({', '.join(a)})",
    "SKEW": lambda a: f"calc.skew({', '.join(a)})",
    "SLOPE": lambda a: f"calc.slope({a[0]}, {a[1]})",
    "INTERCEPT": lambda a: f"calc.intercept({a[0]}, {a[1]})",
    "RSQ": lambda a: f"calc.rsq({a[0]}, {a[1]})",
    "STEYX": lambda a: f"calc.steyx({a[0]}, {a[1]})",
    "ACOT": lambda a: f"calc.acot({a[0]})",
    "ACOTH": lambda a: f"calc.acoth({a[0]})",
    "COT": lambda a: f"calc.cot({a[0]})",
    "COTH": lambda a: f"calc.coth({a[0]})",
    "CSC": lambda a: f"calc.csc({a[0]})",
    "CSCH": lambda a: f"calc.csch({a[0]})",
    "SEC": lambda a: f"calc.sec({a[0]})",
    "SECH": lambda a: f"calc.sech({a[0]})",
    "STDEVA": lambda a: f"calc.stdeva({', '.join(a)})",
    "STDEVPA": lambda a: f"calc.stdevpa({', '.join(a)})",
    "VARA": lambda a: f"calc.vara({', '.join(a)})",
    "VARPA": lambda a: f"calc.varpa({', '.join(a)})",
    "MAXA": lambda a: f"calc.maxa({', '.join(a)})",
    "MINA": lambda a: f"calc.mina({', '.join(a)})",
    "EXPONDIST": lambda a: f"calc.expondist({', '.join(a)})",
    "FDIST": lambda a: f"calc.fdist({', '.join(a)})",
    "FINV": lambda a: f"calc.finv({', '.join(a)})",
    "FISHER": lambda a: f"calc.fisher({a[0]})",
    "FISHERINV": lambda a: f"calc.fisherinv({a[0]})",
    "GAMMA": lambda a: f"calc.gamma({a[0]})",
    "GAMMADIST": lambda a: f"calc.gammadist({', '.join(a)})",
    "GAMMAINV": lambda a: f"calc.gammainv({', '.join(a)})",
    "GAMMALN": lambda a: f"calc.gammaln({a[0]})",
    "GAUSS": lambda a: f"calc.gauss({a[0]})",
    "HYPGEOMDIST": lambda a: f"calc.hypgeomdist({', '.join(a)})",
    "LOGINV": lambda a: f"calc.loginv({', '.join(a)})",
    "LOGNORMDIST": lambda a: f"calc.lognormdist({', '.join(a)})",
    "NEGBINOMDIST": lambda a: f"calc.negbinomdist({', '.join(a)})",
    "NORMDIST": lambda a: f"calc.normdist({', '.join(a)})",
    "ERF": lambda a: f"calc.erf({', '.join(a)})",
    "ERFC": lambda a: f"calc.erfc({a[0]})",
    "DELTA": lambda a: f"calc.delta({', '.join(a)})",
    "GESTEP": lambda a: f"calc.gestep({', '.join(a)})",
    "SQRTPI": lambda a: f"calc.sqrtpi({a[0]})",
    "BITAND": lambda a: f"calc.bitand({a[0]}, {a[1]})",
    "BITOR": lambda a: f"calc.bitor({a[0]}, {a[1]})",
    "BITXOR": lambda a: f"calc.bitxor({a[0]}, {a[1]})",
    "BITLSHIFT": lambda a: f"calc.bitlshift({a[0]}, {a[1]})",
    "BITRSHIFT": lambda a: f"calc.bitrshift({a[0]}, {a[1]})",
    "COMPLEX": lambda a: f"calc.complex({', '.join(a)})",
    "IMABS": lambda a: f"calc.imabs({a[0]})",
    "IMAGINARY": lambda a: f"calc.imaginary({a[0]})",
    "IMARGUMENT": lambda a: f"calc.imargument({a[0]})",
    "IMCONJUGATE": lambda a: f"calc.imconjugate({a[0]})",
    "IMCOS": lambda a: f"calc.imcos({a[0]})",
    "IMDIV": lambda a: f"calc.imdiv({a[0]}, {a[1]})",
    "IMEXP": lambda a: f"calc.imexp({a[0]})",
    "IMLN": lambda a: f"calc.imln({a[0]})",
    "IMLOG10": lambda a: f"calc.imlog10({a[0]})",
    "IMLOG2": lambda a: f"calc.imlog2({a[0]})",
    "IMPOWER": lambda a: f"calc.impower({a[0]}, {a[1]})",
    "IMPRODUCT": lambda a: f"calc.improduct({', '.join(a)})",
    "IMREAL": lambda a: f"calc.imreal({a[0]})",
    "IMSIN": lambda a: f"calc.imsin({a[0]})",
    "BESSELK": lambda a: f"calc.besselk({a[0]}, {a[1]})",
    "BESSELY": lambda a: f"calc.bessely({a[0]}, {a[1]})",
    "EUROCONVERT": lambda a: f"calc.euroconvert({', '.join(a)})",
    "IMCOSH": lambda a: f"calc.imcosh({a[0]})",
    "IMCOT": lambda a: f"calc.imcot({a[0]})",
    "IMCSC": lambda a: f"calc.imcsc({a[0]})",
    "IMCSCH": lambda a: f"calc.imcsch({a[0]})",
    "IMSEC": lambda a: f"calc.imsec({a[0]})",
    "IMSECH": lambda a: f"calc.imsech({a[0]})",
    "IMSINH": lambda a: f"calc.imsinh({a[0]})",
    "IMSQRT": lambda a: f"calc.imsqrt({a[0]})",
    "IMSUB": lambda a: f"calc.imsub({a[0]}, {a[1]})",
    "IMSUM": lambda a: f"calc.imsum({', '.join(a)})",
    "IMTAN": lambda a: f"calc.imtan({a[0]})",
    "IMTANH": lambda a: f"calc.imtanh({a[0]})",
    "ODDLYIELD": lambda a: f"calc.oddlyield({', '.join(a)})",
    "PDURATION": lambda a: f"calc.pduration({a[0]}, {a[1]}, {a[2]})",
    "PPMT": lambda a: f"calc.ppmt({', '.join(a)})",
    "PRICE": lambda a: f"calc.price({', '.join(a)})",
    "PRICEDISC": lambda a: f"calc.pricedisc({', '.join(a)})",
    "PRICEMAT": lambda a: f"calc.pricemat({', '.join(a)})",
    "RATE": lambda a: f"calc.rate({', '.join(a)})",
    "RECEIVED": lambda a: f"calc.received({', '.join(a)})",
    "RRI": lambda a: f"calc.rri({a[0]}, {a[1]}, {a[2]})",
    "SLN": lambda a: f"calc.sln({a[0]}, {a[1]}, {a[2]})",
    "SYD": lambda a: f"calc.syd({a[0]}, {a[1]}, {a[2]}, {a[3]})",
    "TBILLEQ": lambda a: f"calc.tbilleq({a[0]}, {a[1]}, {a[2]})",
    "TBILLPRICE": lambda a: f"calc.tbillprice({a[0]}, {a[1]}, {a[2]})",
    "TBILLYIELD": lambda a: f"calc.tbillyield({a[0]}, {a[1]}, {a[2]})",
    "VDB": lambda a: f"calc.vdb({', '.join(a)})",
    # Group E
    "LINEST": lambda a: f"calc.linest({', '.join(a)})",
    "LOGEST": lambda a: f"calc.logest({', '.join(a)})",
    "MDETERM": lambda a: f"calc.mdeterm({a[0]})",
    "MINVERSE": lambda a: f"calc.minverse({a[0]})",
    "MMULT": lambda a: f"calc.mmult({a[0]}, {a[1]})",
    "MTRANS": lambda a: f"calc.mtrans({a[0]})",
    "MUNIT": lambda a: f"calc.munit({a[0]})",
    "TREND": lambda a: f"calc.trend({', '.join(a)})",
    "BETADIST": lambda a: f"calc.betadist({', '.join(a)})",
    "BETAINV": lambda a: f"calc.betainv({', '.join(a)})",
    "BINOMDIST": lambda a: f"calc.binomdist({', '.join(a)})",
    "CHIDIST": lambda a: f"calc.chidist({a[0]}, {a[1]})",
    "CHIINV": lambda a: f"calc.chiinv({a[0]}, {a[1]})",
    "CONFIDENCE": lambda a: f"calc.confidence({a[0]}, {a[1]}, {a[2]})",
    "CRITBINOM": lambda a: f"calc.critbinom({a[0]}, {a[1]}, {a[2]})",
    "NORMINV": lambda a: f"calc.norminv({a[0]}, {a[1]}, {a[2]})",
    "NORMSDIST": lambda a: f"calc.normsdist({a[0]})",
    "NORMSINV": lambda a: f"calc.normsinv({a[0]})",
    "PEARSON": lambda a: f"calc.pearson({a[0]}, {a[1]})",
    "PERCENTRANK": lambda a: f"calc.percentrank({a[0]}, {a[1]}{', ' + a[2] if len(a) > 2 else ''})",
    "PERMUT": lambda a: f"calc.permut({a[0]}, {a[1]})",
    "POISSON": lambda a: f"calc.poisson({a[0]}, {a[1]}, {a[2] if len(a) > 2 else 'False'})",
    "PROB": lambda a: f"calc.prob({a[0]}, {a[1]}, {a[2]}{', ' + a[3] if len(a) > 3 else ''})",
    "STANDARDIZE": lambda a: f"calc.standardize({a[0]}, {a[1]}, {a[2]})",
    "TDIST": lambda a: f"calc.tdist({a[0]}, {a[1]}, {a[2]})",
    "TINV": lambda a: f"calc.tinv({a[0]}, {a[1]})",
    "TTEST": lambda a: f"calc.ttest({a[0]}, {a[1]}, {a[2]}, {a[3]})",
    "WEIBULL": lambda a: f"calc.weibull({a[0]}, {a[1]}, {a[2]}{', ' + a[3] if len(a) > 3 else ''})",
    "ZTEST": lambda a: f"calc.ztest({a[0]}, {a[1]}{', ' + a[2] if len(a) > 2 else ''})",
    "ASC": lambda a: f"calc.asc({a[0]})",
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
    except ValueError as exc:
        msg = str(exc)
        if "cross-sheet" in msg:
            return TranslationResult(ok=False, reason="CROSS_SHEET_REF")
        if msg.startswith("unsupported function"):
            return TranslationResult(ok=False, reason="UNSUPPORTED_FUNCTION")
        return TranslationResult(ok=False, reason="PARSE_ERROR")

    return TranslationResult(ok=True, code=sanitize_inline_py_code(body), data_ranges=list(state.ranges))


