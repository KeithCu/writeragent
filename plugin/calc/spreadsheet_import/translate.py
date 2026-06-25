# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Translate P1 Calc formulas to ``=PY()`` Python source via vendored AST."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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


def _emit_expr(node, state: _CodegenState, cell_addr: str | None = None) -> str:
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
        return f"(xl.py_str({left}) + xl.py_str({right}))"
    return f"({left} {op} {right})"


def _emit_row_func(node: FunctionNode, state: _CodegenState, cell_addr: str | None = None) -> str:
    if not node.args:
        if cell_addr:
            try:
                _, r = parse_address(cell_addr)
                return f"float({r + 1})"
            except ValueError:
                pass
        return "float(1)"
    arg = node.args[0]
    if isinstance(arg, RangeNode):
        try:
            (sc, sr), (ec, er) = parse_range_string(arg.address)
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
                c, _ = parse_address(cell_addr)
                return f"float({c + 1})"
            except ValueError:
                pass
        return "float(1)"
    arg = node.args[0]
    if isinstance(arg, RangeNode):
        try:
            (sc, sr), (ec, er) = parse_range_string(arg.address)
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
            (sc, sr), (ec, er) = parse_range_string(arg.address)
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
            (sc, sr), (ec, er) = parse_range_string(arg.address)
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
    "xl.iferror(",
    "xl.ifna(",
    "xl.sumif(",
    "xl.sumifs(",
    "xl.countif(",
    "xl.countifs(",
    "xl.averageif(",
    "xl.averageifs(",
    "xl.xlookup(",
    "xl.textjoin(",
    "xl.eomonth(",
    "xl.networkdays(",
    "xl.regex(",
    "xl.subtotal(",
    "xl.lookup(",
    "xl.edate(",
    "xl.datedif(",
    "xl.sumproduct(",
    "xl.averagea(",
    "xl.fmt(",
    "xl.bahttext(",
    "xl.clean(",
    "xl.dollar(",
    "xl.encodeurl(",
    "xl.fixed(",
    "xl.jis(",
    "xl.numbervalue(",
    "xl.t(",
    "xl.textafter(",
    "xl.textbefore(",
    "xl.textsplit(",
    "xl.unichar(",
    "xl.unicode(",
    "xl.besseli(",
    "xl.besselj(",
    "xl.xmatch(",
    "xl.workday(",
    "xl.filter(",
    "xl.sort(",
    "xl.unique(",
    "xl.sortby(",
    "xl.rank(",
    "xl.large(",
    "xl.small(",
    "xl.mode(",
    "xl.choose(",
    "xl.address(",
    "xl.char(",
    "xl.xor(",
    "xl.areas(",
    "xl.code(",
    "xl.yearfrac(",
    "xl.days360(",
    "xl.networkdays_intl(",
    "xl.workday_intl(",
    "xl.daverage(",
    "xl.dcount(",
    "xl.dcounta(",
    "xl.dget(",
    "xl.dmax(",
    "xl.dmin(",
    "xl.dproduct(",
    "xl.dstdev(",
    "xl.dstdevp(",
    "xl.dsum(",
    "xl.dvar(",
    "xl.dvarp(",
    "xl.isoweeknum(",
    "xl.factdouble(",
    "xl.combina(",
    "xl.avedev(",
    "xl.geomean(",
    "xl.harmean(",
    "xl.npv(",
    "xl.irr(",
    "xl.devsq(",
    "xl.kurt(",
    "xl.skew(",
    "xl.slope(",
    "xl.intercept(",
    "xl.rsq(",
    "xl.steyx(",
    "xl.acot(",
    "xl.acoth(",
    "xl.cot(",
    "xl.coth(",
    "xl.csc(",
    "xl.csch(",
    "xl.sec(",
    "xl.sech(",
    "xl.stdeva(",
    "xl.stdevpa(",
    "xl.vara(",
    "xl.varpa(",
    "xl.maxa(",
    "xl.mina(",
    "xl.erf(",
    "xl.erfc(",
    "xl.delta(",
    "xl.gestep(",
    "xl.sqrtpi(",
    "xl.bitand(",
    "xl.bitor(",
    "xl.bitxor(",
    "xl.bitlshift(",
    "xl.bitrshift(",
    "xl.complex(",
    "xl.imabs(",
    "xl.imaginary(",
    "xl.imargument(",
    "xl.imconjugate(",
    "xl.imcos(",
    "xl.imdiv(",
    "xl.imexp(",
    "xl.imln(",
    "xl.imlog10(",
    "xl.imlog2(",
    "xl.impower(",
    "xl.improduct(",
    "xl.imreal(",
    "xl.imsin(",
    "xl.besselk(",
    "xl.bessely(",
    "xl.euroconvert(",
    "xl.imcosh(",
    "xl.imcot(",
    "xl.imcsc(",
    "xl.imcsch(",
    "xl.imsec(",
    "xl.imsech(",
    "xl.imsinh(",
    "xl.imsqrt(",
    "xl.imsub(",
    "xl.imsum(",
    "xl.imtan(",
    "xl.imtanh(",
    "xl.xirr(",
    "xl.xnpv(",
    "xl.yield_calc(",
    "xl.yielddisc(",
    "xl.yieldmat(",
    "xl.na(",
    "xl.aggregate(",
    "xl.base(",
    "xl.decimal(",
    "xl.multinomial(",
    "xl.seriessum(",
    "xl.frequency(",
    "xl.growth(",
    "xl.norminv(",
    "xl.normsdist(",
    "xl.normsinv(",
    "xl.pearson(",
    "xl.percentrank(",
    "xl.permut(",
    "xl.poisson(",
    "xl.prob(",
    "xl.standardize(",
    "xl.tdist(",
    "xl.tinv(",
    "xl.ttest(",
    "xl.weibull(",
    "xl.ztest(",
    "xl.asc(",
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


def _scalar(expr: str) -> str:
    """Coerce to scalar without ``float(`` (Calc formula lexer treats it as #NAME?)."""
    return f"({expr})+0.0"


def _py_index(expr: str) -> str:
    """Integer index without ``int(`` token."""
    return f"(({expr})//1)"


def _emit_if(args: list[str]) -> str:
    if len(args) != 3:
        raise ValueError("IF arity")
    return f"({args[1]} if {args[0]} else {args[2]})"


# P1 function emitters: args are already Python sub-expressions using data[i].
_P1_FUNCTION_EMITTERS: dict[str, Callable[[list[str]], str]] = {
    "ACCRINT": lambda a: f"xl.accrint({', '.join(a)})",
    "ACCRINTM": lambda a: f"xl.accrintm({', '.join(a)})",
    "AMORDEGRC": lambda a: f"xl.amordegrc({', '.join(a)})",
    "AMORLINC": lambda a: f"xl.amorlinc({', '.join(a)})",
    "COUPDAYBS": lambda a: f"xl.coupdaybs({', '.join(a)})",
    "COUPDAYS": lambda a: f"xl.coupdays({', '.join(a)})",
    "COUPDAYSNC": lambda a: f"xl.coupdaysnc({', '.join(a)})",
    "COUPNCD": lambda a: f"xl.coupncd({', '.join(a)})",
    "COUPNUM": lambda a: f"xl.coupnum({', '.join(a)})",
    "COUPPCD": lambda a: f"xl.couppcd({', '.join(a)})",
    "CUMIPMT": lambda a: f"xl.cumipmt({', '.join(a)})",
    "CUMPRINC": lambda a: f"xl.cumprinc({', '.join(a)})",
    "DB": lambda a: f"xl.db({', '.join(a)})",
    "DDB": lambda a: f"xl.ddb({', '.join(a)})",
    "DISC": lambda a: f"xl.disc({', '.join(a)})",
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
    # Date & Time (P2)
    "TODAY": lambda _a: 'float(datetime.date.today().toordinal() - 693594)',
    "NOW": lambda _a: 'float(datetime.datetime.now().toordinal() - 693594)',
    "YEAR": lambda a: f'float(datetime.date.fromordinal(int({a[0]}) + 693594).year)',
    "MONTH": lambda a: f'float(datetime.date.fromordinal(int({a[0]}) + 693594).month)',
    "DAY": lambda a: f'float(datetime.date.fromordinal(int({a[0]}) + 693594).day)',
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
    "IFERROR": lambda a: f"xl.iferror(lambda: {a[0]}, {a[1]})",
    "IFNA": lambda a: f"xl.ifna(lambda: {a[0]}, {a[1]})",
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
    "FACT": lambda a: f"xl.fact({a[0]})",
    "COMBIN": lambda a: f"xl.combin({a[0]}, {a[1]})",
    "REPT": lambda a: f"xl.rept({a[0]}, {a[1]})",
    "EXACT": lambda a: f"(str({a[0]}) == str({a[1]}))",
    "ARABIC": lambda a: f"xl.arabic({a[0]})",

    "BAHTTEXT": lambda a: f"xl.bahttext({a[0]})",
    "CLEAN": lambda a: f"xl.clean({a[0]})",
    "DOLLAR": lambda a: f"xl.dollar({', '.join(a)})",
    "ENCODEURL": lambda a: f"xl.encodeurl({a[0]})",
    "FIXED": lambda a: f"xl.fixed({', '.join(a)})",
    "JIS": lambda a: f"xl.jis({a[0]})",
    "NUMBERVALUE": lambda a: f"xl.numbervalue({', '.join(a)})",
    "T": lambda a: f"xl.t({a[0]})",
    "TEXTAFTER": lambda a: f"xl.textafter({', '.join(a)})",
    "TEXTBEFORE": lambda a: f"xl.textbefore({', '.join(a)})",
    "TEXTSPLIT": lambda a: f"xl.textsplit({', '.join(a)})",
    "UNICHAR": lambda a: f"xl.unichar({a[0]})",
    "UNICODE": lambda a: f"xl.unicode({a[0]})",
    "BESSELI": lambda a: f"xl.besseli({', '.join(a)})",
    "BESSELJ": lambda a: f"xl.besselj({', '.join(a)})",
    # Date & Time (P2)
    "DATE": lambda a: f"float(datetime.date(int({a[0]}), int({a[1]}), int({a[2]})).toordinal() - 693594)",
    "HOUR": lambda a: f"float((datetime.datetime.fromordinal(693594) + datetime.timedelta(days=float({a[0]}))).hour)",
    "MINUTE": lambda a: f"float((datetime.datetime.fromordinal(693594) + datetime.timedelta(days=float({a[0]}))).minute)",
    "SECOND": lambda a: f"float((datetime.datetime.fromordinal(693594) + datetime.timedelta(days=float({a[0]}))).second)",
    "DATEVALUE": lambda a: f"xl.datevalue({a[0]})",
    "TIMEVALUE": lambda a: f"xl.timevalue({a[0]})",
    # Conditional Aggregates
    "SUMIF": lambda a: f"xl.sumif({a[0]}, {a[1]}, {a[2]})" if len(a) > 2 else f"xl.sumif({a[0]}, {a[1]})",
    "SUMIFS": lambda a: f"xl.sumifs({a[0]}, {', '.join(a[1:])})",
    "COUNTIF": lambda a: f"xl.countif({a[0]}, {a[1]})",
    "COUNTIFS": lambda a: f"xl.countifs({', '.join(a)})",
    "AVERAGEIF": lambda a: f"xl.averageif({a[0]}, {a[1]}, {a[2]})" if len(a) > 2 else f"xl.averageif({a[0]}, {a[1]})",
    "AVERAGEIFS": lambda a: f"xl.averageifs({a[0]}, {', '.join(a[1:])})",
    "N": lambda a: f"xl.n({a[0]})",
    "TYPE": lambda a: f"xl.type({a[0]})",
    # Lookup & Reference (XLOOKUP)
    "XLOOKUP": lambda a: f"xl.xlookup({', '.join(a)})",
    # Text (TEXTJOIN, REGEX)
    "TEXTJOIN": lambda a: f"xl.textjoin({', '.join(a)})",
    "REGEX": lambda a: f"xl.regex({', '.join(a)})",
    # Date & Time (EOMONTH, NETWORKDAYS)
    "EOMONTH": lambda a: f"xl.eomonth({a[0]}, {a[1]})",
    "NETWORKDAYS": lambda a: f"xl.networkdays({', '.join(a)})",
    # Tier A — high-frequency gaps
    "SUBTOTAL": lambda a: f"xl.subtotal({a[0]}, {a[1]})" if len(a) > 1 else f"xl.subtotal(9, {a[0]})",
    "ISBLANK": lambda a: f"xl.isblank({a[0]})",
    "ISNUMBER": lambda a: f"xl.isnumber({a[0]})",
    "ISNA": lambda a: f"xl.isna({a[0]})",
    "ISERROR": lambda a: f"xl.iserror({a[0]})",
    "LOOKUP": lambda a: f"xl.lookup({', '.join(a)})",
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
    "EDATE": lambda a: f"xl.edate({a[0]}, {a[1]})",
    "DATEDIF": lambda a: f"xl.datedif({', '.join(a)})",
    "SUMPRODUCT": lambda a: f"xl.sumproduct({', '.join(a)})",
    # Tier B — info, stats, text, misc
    "ISTEXT": lambda a: f"xl.istext({a[0]})",
    "ISLOGICAL": lambda a: f"xl.islogical({a[0]})",
    "ISERR": lambda a: f"xl.iserr({a[0]})",
    "ISNONTEXT": lambda a: f"xl.isnontext({a[0]})",
    "PERCENTILE": lambda a: f"np.percentile(np.asarray({a[0]}, dtype=float).ravel(), float({a[1]}) * 100)",
    "QUARTILE": lambda a: f"xl.quartile({a[0]}, {a[1]})",
    "RANK": lambda a: f"xl.rank({', '.join(a)})",
    "LARGE": lambda a: f"xl.large({a[0]}, {a[1]})",
    "SMALL": lambda a: f"xl.small({a[0]}, {a[1]})",
    "CORREL": lambda a: f"np.corrcoef(np.asarray({a[0]}).ravel(), np.asarray({a[1]}).ravel())[0, 1]",
    "COVAR": lambda a: f"np.cov(np.asarray({a[0]}).ravel(), np.asarray({a[1]}).ravel())[0, 1]",
    "MODE": lambda a: f"xl.mode({a[0]})",
    "AVERAGEA": lambda a: f"xl.averagea({a[0]})",
    "TEXT": lambda a: f"xl.fmt({a[0]}, {a[1]})" if len(a) > 1 else f"xl.py_str({a[0]})",
    "EVEN": lambda a: f"xl.even({a[0]})",
    "ODD": lambda a: f"xl.odd({a[0]})",
    "RAND": lambda _a: "float(np.random.random())",
    "RANDBETWEEN": lambda a: f"float(np.random.randint(int({a[0]}), int({a[1]}) + 1))",
    "XMATCH": lambda a: f"xl.xmatch({', '.join(a)})",
    "WEEKDAY": lambda a: f"xl.weekday({a[0]})" if len(a) == 1 else f"xl.weekday({a[0]}, {a[1]})",
    "WEEKNUM": lambda a: f"xl.weeknum({', '.join(a)})",
    "WORKDAY": lambda a: f"xl.workday({', '.join(a)})",
    # Group B — Financial 2
    "DOLLARDE": lambda a: f"xl.dollarde({a[0]}, {a[1]})",
    "DOLLARFR": lambda a: f"xl.dollarfr({a[0]}, {a[1]})",
    "DURATION": lambda a: f"xl.duration({', '.join(a)})",
    "EFFECT": lambda a: f"xl.effect({a[0]}, {a[1]})",
    "FVSCHEDULE": lambda a: f"xl.fvschedule({a[0]}, {a[1]})",
    "INTRATE": lambda a: f"xl.intrate({', '.join(a)})",
    "IPMT": lambda a: f"xl.ipmt({', '.join(a)})",
    "ISPMT": lambda a: f"xl.ispmt({', '.join(a)})",
    "MDURATION": lambda a: f"xl.mduration({', '.join(a)})",
    "MIRR": lambda a: f"xl.mirr({', '.join(a)})",
    "NOMINAL": lambda a: f"xl.nominal({a[0]}, {a[1]})",
    "NPER": lambda a: f"xl.nper({', '.join(a)})",
    "ODDFPRICE": lambda a: f"xl.oddfprice({', '.join(a)})",
    "ODDFYIELD": lambda a: f"xl.oddfyield({', '.join(a)})",
    "ODDLPRICE": lambda a: f"xl.oddlprice({', '.join(a)})",
    # Tier C — dynamic array helpers (LO 24.8+)
    "FILTER": lambda a: f"xl.filter({', '.join(a)})",
    "SORT": lambda a: f"xl.sort({', '.join(a)})",
    "UNIQUE": lambda a: f"xl.unique({', '.join(a)})",
    "SORTBY": lambda a: f"xl.sortby({', '.join(a)})",
    "PMT": lambda a: f"xl.pmt({', '.join(a)})",
    "FV": lambda a: f"xl.fv({', '.join(a)})",
    "PV": lambda a: f"xl.pv({', '.join(a)})",
    "MROUND": lambda a: f"xl.mround({a[0]}, {a[1]})",
    "SUMSQ": lambda a: f"xl.sumsq({', '.join(a)})",
    "ISEVEN": lambda a: f"xl.iseven({a[0]})",
    "ISODD": lambda a: f"xl.isodd({a[0]})",
    "DAYS": lambda a: f"xl.days({a[0]}, {a[1]})",
    "TIME": lambda a: f"xl.time({a[0]}, {a[1]}, {a[2]})",
    "TRIMMEAN": lambda a: f"xl.trimmean({a[0]}, {a[1]})",
    "FORECAST": lambda a: f"xl.forecast({a[0]}, {a[1]}, {a[2]})",
    "CHOOSE": lambda a: f"xl.choose({a[0]}, {', '.join(a[1:])})",
    "ADDRESS": lambda a: f"xl.address({', '.join(a)})",
    "YEARFRAC": lambda a: f"xl.yearfrac({', '.join(a)})",
    "DAYS360": lambda a: f"xl.days360({', '.join(a)})",
    "NETWORKDAYS.INTL": lambda a: f"xl.networkdays_intl({', '.join(a)})",
    "WORKDAY.INTL": lambda a: f"xl.workday_intl({', '.join(a)})",
    "XOR": lambda a: f"xl.xor({', '.join(a)})",
    "XIRR": lambda a: f"xl.xirr({a[0]}, {a[1]})" if len(a) == 2 else f"xl.xirr({a[0]}, {a[1]}, {a[2]})",
    "XNPV": lambda a: f"xl.xnpv({a[0]}, {a[1]}, {a[2]})",
    "YIELD": lambda a: f"xl.yield_calc({', '.join(a)})",
    "YIELDDISC": lambda a: f"xl.yielddisc({', '.join(a)})",
    "YIELDMAT": lambda a: f"xl.yieldmat({', '.join(a)})",
    "ISFORMULA": lambda a: f"xl.isformula({a[0]})",
    "ISREF": lambda a: f"xl.isref({a[0]})",
    "NA": lambda _a: "xl.na()",
    "AGGREGATE": lambda a: f"xl.aggregate({a[0]}, {a[1]}, {', '.join(a[2:])})",
    "BASE": lambda a: f"xl.base({', '.join(a)})",
    "DECIMAL": lambda a: f"xl.decimal({a[0]}, {a[1]})",
    "MULTINOMIAL": lambda a: f"xl.multinomial({', '.join(a)})",
    "SERIESSUM": lambda a: f"xl.seriessum({a[0]}, {a[1]}, {a[2]}, {a[3]})",
    "FREQUENCY": lambda a: f"xl.frequency({a[0]}, {a[1]})",
    "GROWTH": lambda a: f"xl.growth({', '.join(a)})",
    "AREAS": lambda a: f"xl.areas({a[0]})",
    "CHAR": lambda a: f"xl.char({a[0]})",
    "CODE": lambda a: f"xl.code({a[0]})",
    "DAVERAGE": lambda a: f"xl.daverage({a[0]}, {a[1]}, {a[2]})",
    "DCOUNT": lambda a: f"xl.dcount({a[0]}, {a[1]}, {a[2]})",
    "DMAX": lambda a: f"xl.dmax({a[0]}, {a[1]}, {a[2]})",
    "DMIN": lambda a: f"xl.dmin({a[0]}, {a[1]}, {a[2]})",
    "DSUM": lambda a: f"xl.dsum({a[0]}, {a[1]}, {a[2]})",
    "DCOUNTA": lambda a: f"xl.dcounta({a[0]}, {a[1]}, {a[2]})",
    "DGET": lambda a: f"xl.dget({a[0]}, {a[1]}, {a[2]})",
    "DPRODUCT": lambda a: f"xl.dproduct({a[0]}, {a[1]}, {a[2]})",
    "DSTDEV": lambda a: f"xl.dstdev({a[0]}, {a[1]}, {a[2]})",
    "DSTDEVP": lambda a: f"xl.dstdevp({a[0]}, {a[1]}, {a[2]})",
    "DVAR": lambda a: f"xl.dvar({a[0]}, {a[1]}, {a[2]})",
    "DVARP": lambda a: f"xl.dvarp({a[0]}, {a[1]}, {a[2]})",
    "ISOWEEKNUM": lambda a: f"xl.isoweeknum({a[0]})",
    "FACTDOUBLE": lambda a: f"xl.factdouble({a[0]})",
    "COMBINA": lambda a: f"xl.combina({a[0]}, {a[1]})",
    "AVEDEV": lambda a: f"xl.avedev({a[0]})",
    "GEOMEAN": lambda a: f"xl.geomean({a[0]})",
    "HARMEAN": lambda a: f"xl.harmean({a[0]})",
    "NPV": lambda a: f"xl.npv({a[0]}, {', '.join(a[1:])})",
    "IRR": lambda a: f"xl.irr({a[0]})" if len(a) == 1 else f"xl.irr({a[0]}, {a[1]})",
    "DEVSQ": lambda a: f"xl.devsq({', '.join(a)})",
    "KURT": lambda a: f"xl.kurt({', '.join(a)})",
    "SKEW": lambda a: f"xl.skew({', '.join(a)})",
    "SLOPE": lambda a: f"xl.slope({a[0]}, {a[1]})",
    "INTERCEPT": lambda a: f"xl.intercept({a[0]}, {a[1]})",
    "RSQ": lambda a: f"xl.rsq({a[0]}, {a[1]})",
    "STEYX": lambda a: f"xl.steyx({a[0]}, {a[1]})",
    "ACOT": lambda a: f"xl.acot({a[0]})",
    "ACOTH": lambda a: f"xl.acoth({a[0]})",
    "COT": lambda a: f"xl.cot({a[0]})",
    "COTH": lambda a: f"xl.coth({a[0]})",
    "CSC": lambda a: f"xl.csc({a[0]})",
    "CSCH": lambda a: f"xl.csch({a[0]})",
    "SEC": lambda a: f"xl.sec({a[0]})",
    "SECH": lambda a: f"xl.sech({a[0]})",
    "STDEVA": lambda a: f"xl.stdeva({', '.join(a)})",
    "STDEVPA": lambda a: f"xl.stdevpa({', '.join(a)})",
    "VARA": lambda a: f"xl.vara({', '.join(a)})",
    "VARPA": lambda a: f"xl.varpa({', '.join(a)})",
    "MAXA": lambda a: f"xl.maxa({', '.join(a)})",
    "MINA": lambda a: f"xl.mina({', '.join(a)})",
    "EXPONDIST": lambda a: f"xl.expondist({', '.join(a)})",
    "FDIST": lambda a: f"xl.fdist({', '.join(a)})",
    "FINV": lambda a: f"xl.finv({', '.join(a)})",
    "FISHER": lambda a: f"xl.fisher({a[0]})",
    "FISHERINV": lambda a: f"xl.fisherinv({a[0]})",
    "GAMMA": lambda a: f"xl.gamma({a[0]})",
    "GAMMADIST": lambda a: f"xl.gammadist({', '.join(a)})",
    "GAMMAINV": lambda a: f"xl.gammainv({', '.join(a)})",
    "GAMMALN": lambda a: f"xl.gammaln({a[0]})",
    "GAUSS": lambda a: f"xl.gauss({a[0]})",
    "HYPGEOMDIST": lambda a: f"xl.hypgeomdist({', '.join(a)})",
    "LOGINV": lambda a: f"xl.loginv({', '.join(a)})",
    "LOGNORMDIST": lambda a: f"xl.lognormdist({', '.join(a)})",
    "NEGBINOMDIST": lambda a: f"xl.negbinomdist({', '.join(a)})",
    "NORMDIST": lambda a: f"xl.normdist({', '.join(a)})",
    "ERF": lambda a: f"xl.erf({', '.join(a)})",
    "ERFC": lambda a: f"xl.erfc({a[0]})",
    "DELTA": lambda a: f"xl.delta({', '.join(a)})",
    "GESTEP": lambda a: f"xl.gestep({', '.join(a)})",
    "SQRTPI": lambda a: f"xl.sqrtpi({a[0]})",
    "BITAND": lambda a: f"xl.bitand({a[0]}, {a[1]})",
    "BITOR": lambda a: f"xl.bitor({a[0]}, {a[1]})",
    "BITXOR": lambda a: f"xl.bitxor({a[0]}, {a[1]})",
    "BITLSHIFT": lambda a: f"xl.bitlshift({a[0]}, {a[1]})",
    "BITRSHIFT": lambda a: f"xl.bitrshift({a[0]}, {a[1]})",
    "COMPLEX": lambda a: f"xl.complex({', '.join(a)})",
    "IMABS": lambda a: f"xl.imabs({a[0]})",
    "IMAGINARY": lambda a: f"xl.imaginary({a[0]})",
    "IMARGUMENT": lambda a: f"xl.imargument({a[0]})",
    "IMCONJUGATE": lambda a: f"xl.imconjugate({a[0]})",
    "IMCOS": lambda a: f"xl.imcos({a[0]})",
    "IMDIV": lambda a: f"xl.imdiv({a[0]}, {a[1]})",
    "IMEXP": lambda a: f"xl.imexp({a[0]})",
    "IMLN": lambda a: f"xl.imln({a[0]})",
    "IMLOG10": lambda a: f"xl.imlog10({a[0]})",
    "IMLOG2": lambda a: f"xl.imlog2({a[0]})",
    "IMPOWER": lambda a: f"xl.impower({a[0]}, {a[1]})",
    "IMPRODUCT": lambda a: f"xl.improduct({', '.join(a)})",
    "IMREAL": lambda a: f"xl.imreal({a[0]})",
    "IMSIN": lambda a: f"xl.imsin({a[0]})",
    "BESSELK": lambda a: f"xl.besselk({a[0]}, {a[1]})",
    "BESSELY": lambda a: f"xl.bessely({a[0]}, {a[1]})",
    "EUROCONVERT": lambda a: f"xl.euroconvert({', '.join(a)})",
    "IMCOSH": lambda a: f"xl.imcosh({a[0]})",
    "IMCOT": lambda a: f"xl.imcot({a[0]})",
    "IMCSC": lambda a: f"xl.imcsc({a[0]})",
    "IMCSCH": lambda a: f"xl.imcsch({a[0]})",
    "IMSEC": lambda a: f"xl.imsec({a[0]})",
    "IMSECH": lambda a: f"xl.imsech({a[0]})",
    "IMSINH": lambda a: f"xl.imsinh({a[0]})",
    "IMSQRT": lambda a: f"xl.imsqrt({a[0]})",
    "IMSUB": lambda a: f"xl.imsub({a[0]}, {a[1]})",
    "IMSUM": lambda a: f"xl.imsum({', '.join(a)})",
    "IMTAN": lambda a: f"xl.imtan({a[0]})",
    "IMTANH": lambda a: f"xl.imtanh({a[0]})",
    "ODDLYIELD": lambda a: f"xl.oddlyield({', '.join(a)})",
    "PDURATION": lambda a: f"xl.pduration({a[0]}, {a[1]}, {a[2]})",
    "PPMT": lambda a: f"xl.ppmt({', '.join(a)})",
    "PRICE": lambda a: f"xl.price({', '.join(a)})",
    "PRICEDISC": lambda a: f"xl.pricedisc({', '.join(a)})",
    "PRICEMAT": lambda a: f"xl.pricemat({', '.join(a)})",
    "RATE": lambda a: f"xl.rate({', '.join(a)})",
    "RECEIVED": lambda a: f"xl.received({', '.join(a)})",
    "RRI": lambda a: f"xl.rri({a[0]}, {a[1]}, {a[2]})",
    "SLN": lambda a: f"xl.sln({a[0]}, {a[1]}, {a[2]})",
    "SYD": lambda a: f"xl.syd({a[0]}, {a[1]}, {a[2]}, {a[3]})",
    "TBILLEQ": lambda a: f"xl.tbilleq({a[0]}, {a[1]}, {a[2]})",
    "TBILLPRICE": lambda a: f"xl.tbillprice({a[0]}, {a[1]}, {a[2]})",
    "TBILLYIELD": lambda a: f"xl.tbillyield({a[0]}, {a[1]}, {a[2]})",
    "VDB": lambda a: f"xl.vdb({', '.join(a)})",
    # Group E
    "LINEST": lambda a: f"xl.linest({', '.join(a)})",
    "LOGEST": lambda a: f"xl.logest({', '.join(a)})",
    "MDETERM": lambda a: f"xl.mdeterm({a[0]})",
    "MINVERSE": lambda a: f"xl.minverse({a[0]})",
    "MMULT": lambda a: f"xl.mmult({a[0]}, {a[1]})",
    "MTRANS": lambda a: f"xl.mtrans({a[0]})",
    "MUNIT": lambda a: f"xl.munit({a[0]})",
    "TREND": lambda a: f"xl.trend({', '.join(a)})",
    "BETADIST": lambda a: f"xl.betadist({', '.join(a)})",
    "BETAINV": lambda a: f"xl.betainv({', '.join(a)})",
    "BINOMDIST": lambda a: f"xl.binomdist({', '.join(a)})",
    "CHIDIST": lambda a: f"xl.chidist({a[0]}, {a[1]})",
    "CHIINV": lambda a: f"xl.chiinv({a[0]}, {a[1]})",
    "CONFIDENCE": lambda a: f"xl.confidence({a[0]}, {a[1]}, {a[2]})",
    "CRITBINOM": lambda a: f"xl.critbinom({a[0]}, {a[1]}, {a[2]})",
    "NORMINV": lambda a: f"xl.norminv({a[0]}, {a[1]}, {a[2]})",
    "NORMSDIST": lambda a: f"xl.normsdist({a[0]})",
    "NORMSINV": lambda a: f"xl.normsinv({a[0]})",
    "PEARSON": lambda a: f"xl.pearson({a[0]}, {a[1]})",
    "PERCENTRANK": lambda a: f"xl.percentrank({a[0]}, {a[1]}{', ' + a[2] if len(a) > 2 else ''})",
    "PERMUT": lambda a: f"xl.permut({a[0]}, {a[1]})",
    "POISSON": lambda a: f"xl.poisson({a[0]}, {a[1]}, {a[2] if len(a) > 2 else 'False'})",
    "PROB": lambda a: f"xl.prob({a[0]}, {a[1]}, {a[2]}{', ' + a[3] if len(a) > 3 else ''})",
    "STANDARDIZE": lambda a: f"xl.standardize({a[0]}, {a[1]}, {a[2]})",
    "TDIST": lambda a: f"xl.tdist({a[0]}, {a[1]}, {a[2]})",
    "TINV": lambda a: f"xl.tinv({a[0]}, {a[1]})",
    "TTEST": lambda a: f"xl.ttest({a[0]}, {a[1]}, {a[2]}, {a[3]})",
    "WEIBULL": lambda a: f"xl.weibull({a[0]}, {a[1]}, {a[2]}{', ' + a[3] if len(a) > 3 else ''})",
    "ZTEST": lambda a: f"xl.ztest({a[0]}, {a[1]}{', ' + a[2] if len(a) > 2 else ''})",
    "ASC": lambda a: f"xl.asc({a[0]})",
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


