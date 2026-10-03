# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bad inputs for calc helpers whose names start with I–M.

Each row is text, blank (None), zero, or out of range, and only when that
call is not already asserted elsewhere. Helpers report a spreadsheet error
either as NaN (#NUM! / #DIV/0! / #VALUE!, depending on the function) or as a
``#...`` token. The table records which one the current code returns. A
finite number or a boolean is the accepted result for that input.
"""

from __future__ import annotations

import math

import pytest

import plugin.scripting.calc_functions as calc

_NAN = object()
_INF = float("inf")
_DATE = (43831, 43983)


def _plain_number(value: object) -> float | None:
    """Number behind a float, int, or a plain numeric string.

    Complex helpers stringify an integer-valued coefficient as ``1`` or ``0``
    (``_complex_coeff``). The table stores the number ``1.0`` / ``0.0``.
    Compare the value, not the spelling. Bool is an int subclass and is not
    a numeric cell. Error tokens such as ``#VALUE!`` are not numbers.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value and not value.startswith("#"):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _same(got: object, expected: object) -> bool:
    if expected is _NAN:
        number = _plain_number(got)
        return number is not None and math.isnan(number)
    if isinstance(expected, list):
        if not isinstance(got, list) or len(got) != len(expected):
            return False
        return all(_same(item, exp) for item, exp in zip(got, expected))
    left = _plain_number(got)
    right = _plain_number(expected)
    if left is not None and right is not None:
        if math.isnan(left) or math.isnan(right):
            return math.isnan(left) and math.isnan(right)
        if math.isinf(left) or math.isinf(right):
            return math.isinf(left) and math.isinf(right) and (left > 0) == (right > 0)
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-12)
    return bool(got == expected)


# (label, function, args, expected). _NAN is a non-finite float result.
_BAD_INPUTS: list[tuple[str, str, tuple[object, ...], object]] = [
    # Complex magnitude / parts return NaN for text and blank.
    ("imabs-text", "imabs", ("text",), _NAN),
    ("imabs-blank", "imabs", (None,), _NAN),
    ("imabs-zero", "imabs", (0,), 0.0),
    ("imabs-inf", "imabs", (_INF,), _INF),
    ("imaginary-text", "imaginary", ("text",), _NAN),
    ("imaginary-blank", "imaginary", (None,), _NAN),
    ("imaginary-zero", "imaginary", (0,), 0.0),
    ("imaginary-inf", "imaginary", (_INF,), 0.0),
    ("imargument-text", "imargument", ("text",), _NAN),
    ("imargument-blank", "imargument", (None,), _NAN),
    ("imargument-zero", "imargument", (0,), 0.0),
    ("imargument-inf", "imargument", (_INF,), 0.0),
    ("imreal-text", "imreal", ("text",), _NAN),
    ("imreal-blank", "imreal", (None,), _NAN),
    ("imreal-zero", "imreal", (0,), 0.0),
    ("imreal-inf", "imreal", (_INF,), _INF),
    # Complex string helpers return #VALUE! when the text is not a complex number.
    ("imconjugate-text", "imconjugate", ("text",), "#VALUE!"),
    ("imconjugate-blank", "imconjugate", (None,), "#VALUE!"),
    ("imconjugate-zero", "imconjugate", (0,), 0.0),
    ("imconjugate-inf", "imconjugate", (_INF,), "inf"),
    ("imcos-text", "imcos", ("text",), "#VALUE!"),
    ("imcos-blank", "imcos", (None,), "#VALUE!"),
    ("imcos-zero", "imcos", (0,), 1.0),
    ("imcos-inf", "imcos", (_INF,), "#VALUE!"),
    ("imcosh-text", "imcosh", ("text",), "#VALUE!"),
    ("imcosh-blank", "imcosh", (None,), "#VALUE!"),
    ("imcosh-zero", "imcosh", (0,), 1.0),
    ("imcosh-inf", "imcosh", (_INF,), "inf"),
    ("imcot-text", "imcot", ("text",), "#VALUE!"),
    ("imcot-blank", "imcot", (None,), "#VALUE!"),
    ("imcot-zero", "imcot", (0,), "#VALUE!"),
    ("imcot-inf", "imcot", (_INF,), "#VALUE!"),
    ("imcsc-text", "imcsc", ("text",), "#VALUE!"),
    ("imcsc-blank", "imcsc", (None,), "#VALUE!"),
    ("imcsc-zero", "imcsc", (0,), "#VALUE!"),
    ("imcsc-inf", "imcsc", (_INF,), "#VALUE!"),
    ("imcsch-text", "imcsch", ("text",), "#VALUE!"),
    ("imcsch-blank", "imcsch", (None,), "#VALUE!"),
    ("imcsch-zero", "imcsch", (0,), "#VALUE!"),
    ("imcsch-inf", "imcsch", (_INF,), 0.0),
    ("imexp-text", "imexp", ("text",), "#VALUE!"),
    ("imexp-blank", "imexp", (None,), "#VALUE!"),
    ("imexp-inf", "imexp", (_INF,), "inf"),
    ("imln-text", "imln", ("text",), "#VALUE!"),
    ("imln-blank", "imln", (None,), "#VALUE!"),
    ("imln-inf", "imln", (_INF,), "inf"),
    ("imlog10-text", "imlog10", ("text",), "#VALUE!"),
    ("imlog10-blank", "imlog10", (None,), "#VALUE!"),
    ("imlog10-inf", "imlog10", (_INF,), "inf"),
    ("imlog2-text", "imlog2", ("text",), "#VALUE!"),
    ("imlog2-blank", "imlog2", (None,), "#VALUE!"),
    ("imlog2-inf", "imlog2", (_INF,), "#VALUE!"),
    ("imsec-text", "imsec", ("text",), "#VALUE!"),
    ("imsec-blank", "imsec", (None,), "#VALUE!"),
    ("imsec-zero", "imsec", (0,), 1.0),
    ("imsec-inf", "imsec", (_INF,), "#VALUE!"),
    ("imsech-text", "imsech", ("text",), "#VALUE!"),
    ("imsech-blank", "imsech", (None,), "#VALUE!"),
    ("imsech-zero", "imsech", (0,), 1.0),
    ("imsech-inf", "imsech", (_INF,), 0.0),
    ("imsin-text", "imsin", ("text",), "#VALUE!"),
    ("imsin-blank", "imsin", (None,), "#VALUE!"),
    ("imsin-inf", "imsin", (_INF,), "#VALUE!"),
    ("imsinh-text", "imsinh", ("text",), "#VALUE!"),
    ("imsinh-blank", "imsinh", (None,), "#VALUE!"),
    ("imsinh-zero", "imsinh", (0,), 0.0),
    ("imsinh-inf", "imsinh", (_INF,), "inf"),
    ("imsqrt-text", "imsqrt", ("text",), "#VALUE!"),
    ("imsqrt-blank", "imsqrt", (None,), "#VALUE!"),
    ("imsqrt-zero", "imsqrt", (0,), 0.0),
    ("imsqrt-inf", "imsqrt", (_INF,), "inf"),
    ("imtan-text", "imtan", ("text",), "#VALUE!"),
    ("imtan-blank", "imtan", (None,), "#VALUE!"),
    ("imtan-zero", "imtan", (0,), 0.0),
    ("imtan-inf", "imtan", (_INF,), "#VALUE!"),
    ("imtanh-text", "imtanh", ("text",), "#VALUE!"),
    ("imtanh-blank", "imtanh", (None,), "#VALUE!"),
    ("imtanh-zero", "imtanh", (0,), 0.0),
    ("imtanh-inf", "imtanh", (_INF,), 1.0),
    ("imdiv-text", "imdiv", ("text", "1"), "#VALUE!"),
    ("imdiv-blank", "imdiv", (None, "1"), "#VALUE!"),
    ("imdiv-zero", "imdiv", ("1", 0), "#VALUE!"),
    ("imdiv-inf", "imdiv", ("1", _INF), 0.0),
    ("imsub-text", "imsub", ("text", "1"), "#VALUE!"),
    ("imsub-blank", "imsub", (None, "1"), "#VALUE!"),
    ("imsub-zero", "imsub", ("1", 0), 1.0),
    ("imsub-inf", "imsub", ("1", _INF), "-inf"),
    # 0**negative and a finite base to inf used to raise ZeroDivisionError.
    ("impower-text", "impower", ("text", 2), "#VALUE!"),
    ("impower-blank", "impower", (None, 2), "#VALUE!"),
    ("impower-zero-neg", "impower", (0, -1), "#VALUE!"),
    ("impower-inf", "impower", ("1+i", _INF), "#VALUE!"),
    ("improduct-text", "improduct", ("text",), "#VALUE!"),
    ("improduct-blank", "improduct", (None,), "#VALUE!"),
    ("improduct-zero", "improduct", (0,), 0.0),
    # A non-finite product does not raise. _from_complex stringifies it.
    ("improduct-inf", "improduct", (_INF,), "infnani"),
    ("imsum-text", "imsum", ("text",), "#VALUE!"),
    ("imsum-blank", "imsum", (None,), "#VALUE!"),
    ("imsum-zero", "imsum", (0,), 0.0),
    ("imsum-inf", "imsum", (_INF,), "inf"),
    ("intercept-text", "intercept", (["text", "b"], [1.0, 2.0]), _NAN),
    ("intercept-blank", "intercept", ([None, None], [1.0, 2.0]), _NAN),
    ("intercept-zero", "intercept", ([0.0, 0.0], [0.0, 0.0]), _NAN),
    ("intercept-inf", "intercept", ([_INF, _INF], [1.0, 2.0]), _NAN),
    ("intrate-text", "intrate", ("text", _DATE[1], 100, 105), _NAN),
    ("intrate-blank", "intrate", (None, _DATE[1], 100, 105), _NAN),
    ("intrate-zero-investment", "intrate", (_DATE[0], _DATE[1], 0, 105), _NAN),
    ("intrate-basis-inf", "intrate", (_DATE[0], _DATE[1], 100, 105, _INF), _NAN),
    ("ipmt-text", "ipmt", ("text", 1, 3, 8000), _NAN),
    ("ipmt-blank", "ipmt", (None, 1, 3, 8000), _NAN),
    ("ipmt-zero-per", "ipmt", (0.1, 0, 3, 8000), _NAN),
    ("ipmt-per-inf", "ipmt", (0.1, _INF, 3, 8000), _NAN),
    ("ipmt-type-inf", "ipmt", (0.1, 1, 3, 8000, 0, _INF), _NAN),
    ("irr-blank", "irr", ([None, None],), _NAN),
    ("irr-zero", "irr", ([0, 0, 0],), 0.1),
    ("irr-inf", "irr", ([_INF, -_INF],), _NAN),
    ("irr-guess-inf", "irr", ([-100, 110], _INF), _NAN),
    ("isblank-text", "isblank", ("text",), False),
    ("isblank-zero", "isblank", (0,), False),
    ("isblank-inf", "isblank", (_INF,), False),
    ("iserr-text", "iserr", ("text",), False),
    ("iserr-blank", "iserr", (None,), False),
    ("iserr-zero", "iserr", (0,), False),
    ("iserr-num", "iserr", ("#NUM!",), True),
    ("iserr-div0", "iserr", ("#DIV/0!",), True),
    ("iserr-value", "iserr", ("#VALUE!",), True),
    ("iserr-na", "iserr", ("#N/A",), False),
    ("iserror-text", "iserror", ("text",), False),
    ("iserror-blank", "iserror", (None,), False),
    ("iserror-zero", "iserror", (0,), False),
    ("iserror-value", "iserror", ("#VALUE!",), True),
    ("iserror-div0", "iserror", ("#DIV/0!",), True),
    ("iserror-num", "iserror", ("#NUM!",), True),
    ("iseven-text", "iseven", ("text",), False),
    ("iseven-blank", "iseven", (None,), False),
    ("iseven-zero", "iseven", (0,), True),
    ("isformula-text", "isformula", ("text",), False),
    ("isformula-blank", "isformula", (None,), False),
    ("isformula-zero", "isformula", (0,), False),
    ("isformula-inf", "isformula", (_INF,), False),
    ("islogical-text", "islogical", ("text",), False),
    ("islogical-blank", "islogical", (None,), False),
    ("islogical-zero", "islogical", (0,), False),
    ("islogical-inf", "islogical", (_INF,), False),
    ("isna-text", "isna", ("text",), False),
    ("isna-zero", "isna", (0,), False),
    ("isna-inf", "isna", (_INF,), False),
    ("isnontext-text", "isnontext", ("text",), False),
    ("isnontext-blank", "isnontext", (None,), True),
    ("isnontext-zero", "isnontext", (0,), True),
    ("isnontext-value", "isnontext", ("#VALUE!",), True),
    ("isnumber-text", "isnumber", ("text",), False),
    ("isnumber-blank", "isnumber", (None,), False),
    ("isnumber-zero", "isnumber", (0,), True),
    ("isnumber-inf", "isnumber", (_INF,), True),
    ("isodd-text", "isodd", ("text",), False),
    ("isodd-blank", "isodd", (None,), False),
    ("isodd-zero", "isodd", (0,), False),
    ("isoweeknum-text", "isoweeknum", ("text",), _NAN),
    ("isoweeknum-blank", "isoweeknum", (None,), _NAN),
    ("isoweeknum-zero", "isoweeknum", (0,), 52.0),
    ("isoweeknum-inf", "isoweeknum", (_INF,), _NAN),
    ("ispmt-text", "ispmt", ("text", 1, 12, 1000), _NAN),
    ("ispmt-blank", "ispmt", (None, 1, 12, 1000), _NAN),
    ("ispmt-zero-rate", "ispmt", (0, 1, 12, 1000), 0.0),
    ("ispmt-rate-inf", "ispmt", (_INF, 1, 12, 1000), -_INF),
    ("isref-text", "isref", ("text",), False),
    ("isref-blank", "isref", (None,), False),
    ("isref-zero", "isref", (0,), False),
    ("isref-inf", "isref", (_INF,), False),
    ("istext-text", "istext", ("text",), True),
    ("istext-blank", "istext", (None,), False),
    ("istext-zero", "istext", (0,), False),
    ("istext-value", "istext", ("#VALUE!",), False),
    ("jis-text", "jis", ("text",), "text"),
    ("jis-blank", "jis", (None,), ""),
    ("jis-zero", "jis", (0,), "0"),
    ("jis-inf", "jis", (_INF,), "inf"),
    ("kurt-blank", "kurt", ([None, None, None, None],), _NAN),
    ("kurt-inf", "kurt", ([_INF, 1, 2, 3],), _NAN),
    ("large-blank", "large", ([None, ""], 1), _NAN),
    ("large-zero-k", "large", ([1, 2, 3], 0), _NAN),
    ("large-oor-k", "large", ([1, 2, 3], 9), _NAN),
    ("large-inf-k", "large", ([1, 2, 3], _INF), _NAN),
    ("linest-text", "linest", (["text", "b"],), "#VALUE!"),
    ("linest-blank", "linest", ([None, None],), "#VALUE!"),
    ("linest-zero", "linest", ([0, 0],), [0.0, 0.0]),
    ("linest-inf", "linest", ([_INF, 1],), [_NAN, _NAN]),
    ("logest-blank", "logest", ([None, None],), "#VALUE!"),
    ("logest-inf", "logest", ([_INF, 1],), [_NAN, _NAN]),
    ("loginv-text", "loginv", ("text", 0, 1), _NAN),
    ("loginv-blank", "loginv", (None, 0, 1), _NAN),
    ("loginv-zero", "loginv", (0, 0, 1), 0.0),
    ("loginv-inf", "loginv", (_INF, 0, 1), _NAN),
    ("loginv-sd-zero", "loginv", (0.5, 0, 0), _NAN),
    ("lognormdist-text", "lognormdist", ("text", 0, 1), _NAN),
    ("lognormdist-blank", "lognormdist", (None, 0, 1), _NAN),
    ("lognormdist-zero", "lognormdist", (0, 0, 1), _NAN),
    ("lognormdist-inf", "lognormdist", (_INF, 0, 1), 1.0),
    ("lognormdist-sd-zero", "lognormdist", (1, 0, 0), _NAN),
    ("lookup-text", "lookup", ("text", [1, 2, 3]), 3),
    ("lookup-blank", "lookup", (None, [1, 2, 3]), 3),
    ("lookup-oor", "lookup", (99, [1, 2, 3]), 3),
    ("lookup-vector-blank", "lookup", (1, [None, None]), None),
    ("match-text", "match_criteria", ("text", ">0"), False),
    ("match-blank", "match_criteria", (None, ">0"), False),
    ("match-zero", "match_criteria", (0, ">0"), False),
    ("match-inf", "match_criteria", (_INF, ">0"), True),
    ("maxa-text", "maxa", ("text",), 0.0),
    ("maxa-blank", "maxa", (None,), 0.0),
    ("maxa-zero", "maxa", (0,), 0.0),
    ("maxa-inf", "maxa", (_INF,), _INF),
    ("mdeterm-text", "mdeterm", ([["text", "b"], ["c", "d"]],), _NAN),
    ("mdeterm-blank", "mdeterm", ([[None, None], [None, None]],), _NAN),
    ("mdeterm-zero", "mdeterm", ([[0, 0], [0, 0]],), 0.0),
    ("mdeterm-inf", "mdeterm", ([[_INF, 1], [1, 1]],), _INF),
    ("mduration-text", "mduration", ("text", _DATE[1], 0.08, 0.09, 2), _NAN),
    ("mduration-blank", "mduration", (None, _DATE[1], 0.08, 0.09, 2), _NAN),
    ("mduration-zero-frequency", "mduration", (_DATE[0], _DATE[1], 0.08, 0.09, 0), _NAN),
    ("mduration-basis-inf", "mduration", (_DATE[0], _DATE[1], 0.08, 0.09, 2, _INF), _NAN),
    ("mina-text", "mina", ("text",), 0.0),
    ("mina-blank", "mina", (None,), 0.0),
    ("mina-zero", "mina", (0,), 0.0),
    ("mina-inf", "mina", (_INF,), _INF),
    ("minverse-text", "minverse", ([["text", 1], [1, 1]],), "#VALUE!"),
    ("minverse-blank", "minverse", ([[None, None], [None, None]],), [[_NAN, _NAN], [_NAN, _NAN]]),
    ("minverse-zero", "minverse", ([[0, 0], [0, 0]],), "#VALUE!"),
    ("minverse-rect", "minverse", ([[1, 2, 3]],), "#VALUE!"),
    ("mirr-text", "mirr", (["text", 10], 0.1, 0.1), _NAN),
    ("mirr-blank", "mirr", ([None, None], 0.1, 0.1), _NAN),
    ("mirr-zero", "mirr", ([0, 0, 0], 0.1, 0.1), _NAN),
    ("mirr-inf", "mirr", ([_INF, -1], 0.1, 0.1), _NAN),
    ("mirr-rate-neg-one", "mirr", ([-100, 39, 59], -1, 0.1), _NAN),
    ("mmult-text", "mmult", ([["text"]], [[1]]), "#VALUE!"),
    ("mmult-blank", "mmult", ([[None]], [[1]]), [[_NAN]]),
    ("mmult-zero", "mmult", ([[0]], [[0]]), [[0.0]]),
    ("mmult-inf", "mmult", ([[_INF]], [[1]]), [[_INF]]),
    ("mmult-shape", "mmult", ([[1, 2]], [[1, 2]]), "#VALUE!"),
    ("mode-text", "mode", (["text", "text"],), "text"),
    ("mode-blank", "mode", ([None, None],), _NAN),
    ("mode-zero", "mode", ([0, 0, 1],), 0),
    ("mode-inf", "mode", ([_INF, _INF, 1],), _INF),
    # Non-finite MROUND used to raise from math.floor. Zero multiple is already tested.
    ("mround-inf", "mround", (_INF, 1), _NAN),
    ("mround-nan", "mround", (float("nan"), 1), _NAN),
    ("mround-huge", "mround", (1e308, 1e-308), _NAN),
    ("mtrans-text", "mtrans", ([["text", "b"], ["c", "d"]],), [["text", "c"], ["b", "d"]]),
    ("mtrans-blank", "mtrans", ([[None, None], [None, None]],), [[None, None], [None, None]]),
    ("mtrans-zero", "mtrans", ([[0, 0], [0, 0]],), [[0, 0], [0, 0]]),
    ("mtrans-scalar", "mtrans", (0,), 0),
    ("multinomial-text", "multinomial", ("text",), _NAN),
    ("multinomial-blank", "multinomial", (None,), _NAN),
    ("multinomial-zero", "multinomial", (0,), 1.0),
    ("multinomial-neg", "multinomial", (-1,), _NAN),
    ("multinomial-inf", "multinomial", (_INF,), _NAN),
    ("munit-text", "munit", ("text",), "#VALUE!"),
    ("munit-blank", "munit", (None,), "#VALUE!"),
    ("munit-zero", "munit", (0,), []),
    ("munit-neg", "munit", (-1,), "#VALUE!"),
    ("munit-inf", "munit", (_INF,), "#VALUE!"),
]


@pytest.mark.parametrize(
    "label,name,args,expected",
    _BAD_INPUTS,
    ids=[row[0] for row in _BAD_INPUTS],
)
def test_i_m_bad_inputs(label: str, name: str, args: tuple[object, ...], expected: object) -> None:
    got = getattr(calc, name)(*args)
    assert _same(got, expected), f"{label}: {name}{args!r} -> {got!r}"


def test_iferror_ifna_bad_inputs() -> None:
    def _raise() -> float:
        raise ValueError("bad")

    # A non-callable is text in the value slot. The call raises TypeError,
    # which both handlers already turn into the alternate.
    assert calc.iferror("text", "#VALUE!") == "#VALUE!"  # type: ignore[arg-type]
    assert calc.ifna("text", "#VALUE!") == "#VALUE!"  # type: ignore[arg-type]
    assert calc.iferror(lambda: None, "alt") is None
    assert calc.ifna(lambda: None, "alt") is None
    assert calc.iferror(lambda: 0, "alt") == 0
    assert calc.ifna(lambda: 0, "alt") == 0
    assert calc.iferror(lambda: _INF, "alt") == _INF
    assert math.isinf(calc.ifna(lambda: _INF, "alt"))
    assert calc.iferror(_raise, "#VALUE!") == "#VALUE!"
    assert calc.ifna(_raise, "#VALUE!") == "#VALUE!"
