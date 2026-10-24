# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bad inputs for calc helpers whose names start with I–M.

Each row is text, blank (None), zero, or out of range, and only when that
call is not already asserted elsewhere. Helpers report a spreadsheet error
either as NaN (#NUM! / #DIV/0! / #VALUE!, depending on the function) or as a
``#...`` token. Rows in ``_BAD_INPUTS`` still return NaN or a ``#...`` token. Rows in ``_RAISES`` used to return NaN and now propagate. A
finite number or a boolean is the accepted result for that input.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
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
    ("imabs-zero", "imabs", (0,), 0.0),
    ("imabs-inf", "imabs", (_INF,), _NAN),
    ("imaginary-zero", "imaginary", (0,), 0.0),
    ("imaginary-inf", "imaginary", (_INF,), 0.0),
    ("imargument-zero", "imargument", (0,), _NAN),
    ("imargument-inf", "imargument", (_INF,), 0.0),
    ("imreal-zero", "imreal", (0,), 0.0),
    ("imreal-inf", "imreal", (_INF,), _NAN),
    # Complex string helpers return #VALUE! when the text is not a complex number.
    ("imconjugate-text", "imconjugate", ("text",), "#VALUE!"),
    ("imconjugate-blank", "imconjugate", (None,), "#VALUE!"),
    ("imconjugate-zero", "imconjugate", (0,), 0.0),
    ("imconjugate-inf", "imconjugate", (_INF,), "#VALUE!"),
    ("imcos-text", "imcos", ("text",), "#VALUE!"),
    ("imcos-blank", "imcos", (None,), "#VALUE!"),
    ("imcos-zero", "imcos", (0,), 1.0),
    ("imcos-inf", "imcos", (_INF,), "#VALUE!"),
    ("imcosh-text", "imcosh", ("text",), "#VALUE!"),
    ("imcosh-blank", "imcosh", (None,), "#VALUE!"),
    ("imcosh-zero", "imcosh", (0,), 1.0),
    ("imcosh-inf", "imcosh", (_INF,), "#VALUE!"),
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
    ("imexp-inf", "imexp", (_INF,), "#VALUE!"),
    ("imln-text", "imln", ("text",), "#VALUE!"),
    ("imln-blank", "imln", (None,), "#VALUE!"),
    ("imln-inf", "imln", (_INF,), "#VALUE!"),
    ("imlog10-text", "imlog10", ("text",), "#VALUE!"),
    ("imlog10-blank", "imlog10", (None,), "#VALUE!"),
    ("imlog10-inf", "imlog10", (_INF,), "#VALUE!"),
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
    ("imsinh-inf", "imsinh", (_INF,), "#VALUE!"),
    ("imsqrt-text", "imsqrt", ("text",), "#VALUE!"),
    ("imsqrt-blank", "imsqrt", (None,), "#VALUE!"),
    ("imsqrt-zero", "imsqrt", (0,), 0.0),
    ("imsqrt-inf", "imsqrt", (_INF,), "#VALUE!"),
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
    ("imsub-inf", "imsub", ("1", _INF), "#VALUE!"),
    # 0**negative and a finite base to inf used to raise ZeroDivisionError.
    ("impower-text", "impower", ("text", 2), "#VALUE!"),
    ("impower-blank", "impower", (None, 2), "#VALUE!"),
    ("impower-zero-neg", "impower", (0, -1), "#VALUE!"),
    ("impower-inf", "impower", ("1+i", _INF), "#VALUE!"),
    ("improduct-text", "improduct", ("text",), "#VALUE!"),
    # An empty ComplexList is Complex(0) for both the sum and the product.
    ("improduct-blank", "improduct", (None,), 0.0),
    ("improduct-zero", "improduct", (0,), 0.0),
    ("improduct-inf", "improduct", (_INF,), "#VALUE!"),
    ("imsum-text", "imsum", ("text",), "#VALUE!"),
    ("imsum-blank", "imsum", (None,), 0.0),
    ("imsum-zero", "imsum", (0,), 0.0),
    ("imsum-inf", "imsum", (_INF,), "#VALUE!"),
    ("intercept-text", "intercept", (["text", "b"], [1.0, 2.0]), _NAN),
    ("intercept-blank", "intercept", ([None, None], [1.0, 2.0]), _NAN),
    ("intercept-zero", "intercept", ([0.0, 0.0], [0.0, 0.0]), _NAN),
    ("intercept-inf", "intercept", ([_INF, _INF], [1.0, 2.0]), _NAN),
    ("intrate-zero-investment", "intrate", (_DATE[0], _DATE[1], 0, 105), _NAN),
    ("intrate-inf-redemption", "intrate", (_DATE[0], _DATE[1], 100, _INF), _NAN),
    ("ipmt-zero-per", "ipmt", (0.1, 0, 3, 8000), _NAN),
    ("ipmt-type-inf", "ipmt", (0.1, 1, 3, 8000, 0, _INF), _NAN),
    ("irr-blank", "irr", ([None, None],), _NAN),
    # All zeros: f and df are 0, so ScIRR never shrinks the step and
    # pushes NoConvergence. The old NPV test returned the guess 0.1.
    ("irr-zero", "irr", ([0, 0, 0],), _NAN),
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
    ("linest-text", "linest", (["text", "b"],), "#VALUE!"),
    ("linest-blank", "linest", ([None, None],), "#VALUE!"),
    ("linest-zero", "linest", ([0, 0],), [0.0, 0.0]),
    ("linest-inf", "linest", ([_INF, 1],), [_NAN, _NAN]),
    ("logest-blank", "logest", ([None, None],), "#VALUE!"),
    ("logest-inf", "logest", ([_INF, 1],), [_NAN, _NAN]),
    ("loginv-zero", "loginv", (0, 0, 1), _NAN),
    ("loginv-inf", "loginv", (_INF, 0, 1), _NAN),
    ("loginv-sd-zero", "loginv", (0.5, 0, 0), _NAN),
    ("lognormdist-zero", "lognormdist", (0, 0, 1), 0.0),
    ("lognormdist-inf", "lognormdist", (_INF, 0, 1), 1.0),
    ("lognormdist-sd-zero", "lognormdist", (1, 0, 0), _NAN),
    ("lookup-text", "lookup", ("text", [1, 2, 3]), "#N/A"),
    ("lookup-blank", "lookup", (None, [1, 2, 3]), "#N/A"),
    ("lookup-oor", "lookup", (99, [1, 2, 3]), 3),
    ("lookup-vector-blank", "lookup", (1, [None, None]), "#N/A"),
    ("match-text", "match_criteria", ("text", ">0"), False),
    ("match-blank", "match_criteria", (None, ">0"), False),
    ("match-zero", "match_criteria", (0, ">0"), False),
    ("match-inf", "match_criteria", (_INF, ">0"), True),
    ("maxa-text", "maxa", ("text",), 0.0),
    ("maxa-blank", "maxa", (None,), 0.0),
    ("maxa-zero", "maxa", (0,), 0.0),
    ("maxa-inf", "maxa", (_INF,), _INF),
    ("mdeterm-blank", "mdeterm", ([[None, None], [None, None]],), _NAN),
    ("mdeterm-zero", "mdeterm", ([[0, 0], [0, 0]],), 0.0),
    ("mdeterm-inf", "mdeterm", ([[_INF, 1], [1, 1]],), _INF),
    ("mduration-zero-frequency", "mduration", (_DATE[0], _DATE[1], 0.08, 0.09, 0), _NAN),
    ("mina-text", "mina", ("text",), 0.0),
    ("mina-blank", "mina", (None,), 0.0),
    ("mina-zero", "mina", (0,), 0.0),
    ("mina-inf", "mina", (_INF,), _INF),
    ("minverse-text", "minverse", ([["text", 1], [1, 1]],), "#VALUE!"),
    ("minverse-blank", "minverse", ([[None, None], [None, None]],), [[_NAN, _NAN], [_NAN, _NAN]]),
    ("minverse-zero", "minverse", ([[0, 0], [0, 0]],), "#VALUE!"),
    ("minverse-rect", "minverse", ([[1, 2, 3]],), "#VALUE!"),
    ("mirr-zero", "mirr", ([0, 0, 0], 0.1, 0.1), _NAN),
    ("mirr-inf", "mirr", ([_INF, -1], 0.1, 0.1), _NAN),
    ("mirr-rate-neg-one", "mirr", ([-100, 39, 59], -1, 0.1), _NAN),
    ("mmult-text", "mmult", ([["text"]], [[1]]), "#VALUE!"),
    ("mmult-blank", "mmult", ([[None]], [[1]]), [[_NAN]]),
    ("mmult-zero", "mmult", ([[0]], [[0]]), [[0.0]]),
    ("mmult-inf", "mmult", ([[_INF]], [[1]]), [[_INF]]),
    ("mmult-shape", "mmult", ([[1, 2]], [[1, 2]]), "#VALUE!"),
    ("mode-text", "mode", (["text", "text"],), _NAN),
    ("mode-blank", "mode", ([None, None],), _NAN),
    ("mode-zero", "mode", ([0, 0, 1],), 0),
    ("mode-inf", "mode", ([_INF, _INF, 1],), _INF),
    # Non-finite MROUND used to raise from math.floor. Zero multiple is already tested.
    ("mround-inf", "mround", (_INF, 1), _NAN),
    ("mround-nan", "mround", (float("nan"), 1), _NAN),
    ("mtrans-text", "mtrans", ([["text", "b"], ["c", "d"]],), [["text", "c"], ["b", "d"]]),
    ("mtrans-blank", "mtrans", ([[None, None], [None, None]],), [[None, None], [None, None]]),
    ("mtrans-zero", "mtrans", ([[0, 0], [0, 0]],), [[0, 0], [0, 0]]),
    ("mtrans-scalar", "mtrans", (0,), 0),
    ("multinomial-zero", "multinomial", (0,), 1.0),
    ("multinomial-neg", "multinomial", (-1,), _NAN),
    ("munit-text", "munit", ("text",), "#VALUE!"),
    ("munit-blank", "munit", (None,), "#VALUE!"),
    ("munit-zero", "munit", (0,), "#VALUE!"),
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


_RAISES: list[tuple[str, str, tuple[object, ...], type[BaseException]]] = [
    # Native complex() ValueError for text/blank; the worker maps it to #VALUE! centrally (#1577).
    ("imabs-text", "imabs", ("text",), ValueError),
    ("imabs-blank", "imabs", (None,), ValueError),
    ("imaginary-text", "imaginary", ("text",), ValueError),
    ("imaginary-blank", "imaginary", (None,), ValueError),
    ("imargument-text", "imargument", ("text",), ValueError),
    ("imargument-blank", "imargument", (None,), ValueError),
    ("imreal-text", "imreal", ("text",), ValueError),
    ("imreal-blank", "imreal", (None,), ValueError),
    ("intrate-text", "intrate", ("text", _DATE[1], 100, 105), ValueError),
    ("intrate-blank", "intrate", (None, _DATE[1], 100, 105), TypeError),
    ("intrate-basis-inf", "intrate", (_DATE[0], _DATE[1], 100, 105, _INF), OverflowError),
    ("ipmt-text", "ipmt", ("text", 1, 3, 8000), ValueError),
    ("ipmt-blank", "ipmt", (None, 1, 3, 8000), TypeError),
    ("ipmt-per-inf", "ipmt", (0.1, _INF, 3, 8000), OverflowError),
    ("ispmt-text", "ispmt", ("text", 1, 12, 1000), ValueError),
    ("ispmt-blank", "ispmt", (None, 1, 12, 1000), TypeError),
    ("large-inf-k", "large", ([1, 2, 3], _INF), OverflowError),
    ("loginv-text", "loginv", ("text", 0, 1), ValueError),
    ("loginv-blank", "loginv", (None, 0, 1), TypeError),
    ("lognormdist-text", "lognormdist", ("text", 0, 1), ValueError),
    ("lognormdist-blank", "lognormdist", (None, 0, 1), TypeError),
    ("mdeterm-text", "mdeterm", ([["text", "b"], ["c", "d"]],), ValueError),
    ("mduration-text", "mduration", ("text", _DATE[1], 0.08, 0.09, 2), ValueError),
    ("mduration-blank", "mduration", (None, _DATE[1], 0.08, 0.09, 2), TypeError),
    ("mduration-basis-inf", "mduration", (_DATE[0], _DATE[1], 0.08, 0.09, 2, _INF), OverflowError),
    ("mirr-text", "mirr", (["text", 10], 0.1, 0.1), ValueError),
    ("mirr-blank", "mirr", ([None, None], 0.1, 0.1), TypeError),
    ("mround-huge", "mround", (1e308, 1e-308), OverflowError),
    ("multinomial-text", "multinomial", ("text",), ValueError),
    ("multinomial-blank", "multinomial", (None,), TypeError),
    ("multinomial-inf", "multinomial", (_INF,), OverflowError),
]


@pytest.mark.parametrize(
    "label,name,args,exc",
    _RAISES,
    ids=[row[0] for row in _RAISES],
)
def test_i_m_bad_inputs_raise(
    label: str, name: str, args: tuple[object, ...], exc: type[BaseException]
) -> None:
    with pytest.raises(exc):
        getattr(calc, name)(*args)


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


def test_mround_zero_multiple():
    # Regression R3: mround(n, 0) should return 0.0 (Excel and LibreOffice ScMRound)
    assert calc.mround(5, 0) == 0.0
    assert calc.mround(-5, 0) == 0.0
    assert calc.mround(0, 0) == 0.0


def test_error_predicates_i_m():
    # Bug Z7: iserror, iserr, istext, isnumber, iferror
    assert calc.iserror(float("nan")) is True
    assert calc.iserror(np.nan) is True
    assert calc.iserror("#VALUE!") is True
    assert calc.iserror("#N/A") is True
    assert calc.iserror("#hashtag") is False
    assert calc.iserror("normal") is False

    # iserr excludes #N/A (and NaN which represents NA())
    assert calc.iserr(float("nan")) is False
    assert calc.iserr("#VALUE!") is True
    assert calc.iserr("#N/A") is False
    assert calc.iserr("#hashtag") is False

    assert calc.istext("#hashtag") is True
    assert calc.istext("#VALUE!") is False
    assert calc.istext("hello") is True
    assert calc.istext(123) is False

    assert calc.isnumber(np.int64(10)) is True
    assert calc.isnumber(np.float64(3.14)) is True
    assert calc.isnumber(True) is False
    assert calc.isnumber(np.bool_(True)) is False
    assert calc.islogical(True) is True
    assert calc.islogical(False) is True
    assert calc.islogical(np.bool_(True)) is True
    assert calc.islogical(np.bool_(False)) is True
    assert calc.islogical(1) is False
    assert calc.isnumber(10) is True

    # iferror on token strings and NaN
    assert calc.iferror(lambda: "#VALUE!", 99) == 99
    assert calc.iferror(lambda: float("nan"), 99) == 99
    assert calc.iferror(lambda: "not an error", 99) == "not an error"


def test_large_numeric_extraction():
    # Bug Z15: large ignores text, bools, and NaN
    assert calc.large([1, float("nan"), 3], 1) == 3.0
    assert calc.large([1, "5", 3], 1) == 3.0
    assert calc.large([1, True, 3], 1) == 3.0
    assert calc.large([1, "text", 3], 2) == 1.0


def test_linest_order_and_const():
    # Bug Z12: linest multi-X reverse order [mk, ..., m1, b] and const=False
    # y = 2*x1 + 3*x2 + 5
    x1 = [1, 2, 3, 4]
    x2 = [2, 1, 4, 3]
    y = [2 * a + 3 * b + 5 for a, b in zip(x1, x2)]
    # known_x as columns: [[1, 2], [2, 1], [3, 4], [4, 3]]
    known_x = list(zip(x1, x2))
    res = calc.linest(y, known_x)
    assert isinstance(res, list)
    # Excel order: [m2, m1, b] -> [3.0, 2.0, 5.0]
    assert np.allclose(res, [3.0, 2.0, 5.0], atol=1e-6)

    # const=False: y = 2*x1 + 3*x2 (b=0.0)
    y_noconst = [2 * a + 3 * b for a, b in zip(x1, x2)]
    res_noconst = calc.linest(y_noconst, known_x, const=False)
    assert isinstance(res_noconst, list)
    assert np.allclose(res_noconst, [3.0, 2.0, 0.0], atol=1e-6)


def test_mode_mixed_types():
    # Bug Z2: mode with mixed types does not coerce numbers to strings.
    # Text is not a candidate. A bool is the number 1, so it merges with 1.
    assert calc.mode([1, 1, 2, "a"]) == 1.0
    assert calc.mode(1, 2, 2) == 2.0
    assert calc.mode([1, True, True]) == 1.0
    assert math.isnan(calc.mode(["text", "text"]))


def test_irr_guess_minus_one_and_overflow():
    # ScIRR starts Newton at 0.1 when the guess is -1. [-1, 1] solves at 0.
    assert calc.irr([-1, 1], -1) == pytest.approx(0.0, abs=1e-6)
    with pytest.raises(OverflowError):
        calc.irr([-1, 1], 1e308)
    assert math.isnan(calc.irr([-100] + [0] * 40 + [110], 1e6))
    # abs(NPV) < 1e-7 never finishes at this scale: the residual sticks
    # just above 1e-7. ScIRR stops on the rate step, so the rate matches
    # the unscaled series. A guess of 0 within 1e-7 of zero snaps to 0.
    small = calc.irr([-100, 30, 40, 50])
    assert calc.irr([-1e9, 3e8, 4e8, 5e8]) == pytest.approx(small)
    assert calc.irr([-1, 1], 0) == 0.0


def test_loginv_bounds_and_lognormdist_nonpositive():
    assert math.isnan(calc.loginv(0, 0, 1))
    assert math.isnan(calc.loginv(1, 0, 1))
    with pytest.raises(OverflowError):
        calc.loginv(0.5, 1e308, 1)
    assert calc.loginv(0.5, 0, 1) == pytest.approx(1.0)
    # Cumulative x <= 0 is 0. The density is NaN. A huge mean does not raise.
    assert calc.lognormdist(0, 0, 1) == 0.0
    assert calc.lognormdist(-1, 0, 1) == 0.0
    assert math.isnan(calc.lognormdist(0, 0, 1, False))
    # ScLogNormDist uses integralPhi((ln x - mean) / sd). A huge mean is the
    # far tail, 0. scipy's exp(mean) used to overflow into NaN.
    assert calc.lognormdist(1, 1e308, 1) == 0.0
    assert calc.lognormdist(1, 0, 1) == pytest.approx(0.5)
    assert calc.lognormdist(1, 0, 1, False) == pytest.approx(1.0 / math.sqrt(2.0 * math.pi))


def test_im_suffix_blanks_and_nonfinite():
    assert calc.imsum("1+2j", "3+j") == "4+3j"
    # First imaginary unit wins. Mixed i then j is not #VALUE!.
    assert calc.imsum("1+2i", "3+j") == "4+3i"
    assert calc.imsum([None, "1+i", ""]) == "1+i"
    assert calc.improduct([None, "2+j"]) == "2+j"
    assert calc.improduct("j") == "j"
    assert calc.imargument(0) is not None and math.isnan(calc.imargument(0))
    assert math.isnan(calc.imargument("0+0i"))
    assert calc.improduct("1e308", "1e308") == "#VALUE!"
    assert calc.imsum("nan") == "#VALUE!"


def test_isnontext_matches_istext():
    assert calc.isnontext("#hashtag") is False
    assert calc.isnontext("") is False
    assert calc.istext("#hashtag") is True
    assert calc.istext("") is True
    assert calc.isnontext("#VALUE!") is True
    assert calc.isnontext(None) is True


def test_lookup_rejects_cross_type_and_miss_is_na():
    assert calc.lookup(20, ["10x"]) == "#N/A"
    assert calc.lookup("text", [1, 2, 3]) == "#N/A"
    assert calc.lookup("b", ["a", "b", "c"]) == "b"
    assert calc.lookup(2, [1, 2, 3]) == 2


def test_linest_const_text_and_blank_known_x():
    y = [6, 7, 8]
    x = [1, 2, 3]
    # "0" and "FALSE" used to stay truthy, so the intercept stayed 5.
    assert np.allclose(calc.linest(y, x, "0"), [3.1428571428571423, 0.0], atol=1e-9)
    assert np.allclose(calc.linest(y, x, "FALSE"), [3.1428571428571423, 0.0], atol=1e-9)
    assert np.allclose(calc.linest(y, x, "TRUE"), [1.0, 5.0], atol=1e-9)
    # Same const coercion on LOGEST. "FALSE" forces the multiplicative intercept to 1.
    through_origin = calc.logest([1, 2, 4], x, "FALSE")
    assert isinstance(through_origin, list)
    assert through_origin[-1] == pytest.approx(1.0)
    # A present blank known_x is not the omitted argument.
    assert calc.linest(y, None) == "#VALUE!"
    omitted = calc.linest([1, 2, 3])
    assert isinstance(omitted, list)
    assert np.allclose(omitted, [1.0, 0.0], atol=1e-9)


def test_mround_zero_and_negative_multiple_is_positive_zero():
    result = calc.mround(0, -1)
    assert result == 0.0
    assert math.copysign(1.0, result) == 1.0


def test_intercept_drops_text_pairs():
    # slope keeps (1, 1) and (5, 3). The old dtype=float parse raised.
    assert calc.intercept([1.0, "text", 5.0], [1.0, 2.0, 3.0]) == pytest.approx(-1.0)


def test_jis_integral_float_has_no_decimal():
    assert calc.jis(1.0) == "1"
    assert calc.jis(1.5) == "1.5"
    assert calc.jis(None) == ""


def test_ipmt_any_nonzero_type_is_beginning():
    # GetBool() is GetDouble() != 0. int(type) == 1 used to treat these as end.
    beginning = calc.ipmt(0.1, 1, 3, 8000, 0, 1)
    assert beginning == 0.0
    assert calc.ipmt(0.1, 1, 3, 8000, 0, 2) == beginning
    assert calc.ipmt(0.1, 1, 3, 8000, 0, -1) == beginning
    assert calc.ipmt(0.1, 1, 3, 8000, 0, 0.5) == beginning
    assert calc.ipmt(0.1, 1, 3, 8000, 0, 0) == pytest.approx(-800.0)
    assert math.isnan(calc.ipmt(0.1, 1, 3, 8000, 0, float("inf")))


def test_lookup_two_dimensional_uses_first_and_last():
    table = [[1, 10], [2, 20], [3, 30]]
    assert calc.lookup(2, table) == 20
    assert calc.lookup(2, table, [100, 200, 300]) == 200
    # More columns than rows: search the first row, return the last row.
    assert calc.lookup(2, [[1, 2, 3], [10, 20, 30]]) == 20
    assert calc.lookup(2, [1, 2, 3], [10, 20, 30]) == 20
    assert calc.lookup(2, table, [[100, 200], [300, 400]]) == "#VALUE!"


def test_matrix_scalar_and_flat_list():
    assert calc.mdeterm(5) == pytest.approx(5.0)
    assert calc.mdeterm([5]) == pytest.approx(5.0)
    with pytest.raises(np.linalg.LinAlgError):
        calc.mdeterm([1, 2, 3])
    assert calc.mmult([1, 2], [3, 4]) == "#VALUE!"
    assert calc.mmult([[1, 2]], [[3], [4]]) == [[11.0]]
    assert calc.mtrans([1, 2, 3]) == [[1], [2], [3]]
    assert calc.mtrans(0) == 0
    assert calc.minverse(5) == [[0.2]]
    # Singular stays #VALUE!. Calc's code is Err:502, not #NUM!.
    assert calc.minverse([[1, 2], [2, 4]]) == "#VALUE!"


def test_munit_zero_and_cap():
    assert calc.munit(0) == "#VALUE!"
    assert calc.munit(0.9) == "#VALUE!"
    assert calc.munit(1025) == "#VALUE!"
    assert calc.munit(2.9) == [[1.0, 0.0], [0.0, 1.0]]


def test_multinomial_bounds_float_range():
    # comb(2e9, 1e9) must not run. A sum cap would reject the finite cases below.
    assert math.isnan(calc.multinomial(1e9, 1e9))
    assert calc.multinomial(1e6, 1) == 1000001.0
    assert calc.multinomial(1e308) == 1.0
    assert calc.multinomial(1e308, 1) == 1e308
    assert calc.multinomial(2, 3, 4) == 1260.0
    assert math.isfinite(calc.multinomial(500, 500))
    assert math.isnan(calc.multinomial(600, 600))
    # Each C(600, 100), C(500, 100), ... fits; the product does not.
    assert math.isnan(calc.multinomial(*([100] * 6)))


def test_ispmt_and_mirr_huge_int_overflows():
    huge = 10**10000
    with pytest.raises(OverflowError):
        calc.ispmt(huge, 1, 12, 100)
    with pytest.raises(OverflowError):
        calc.mirr([-100, 10], huge, 0.1)


def test_iseven_bool_is_numeric_and_mround_half_ulp():
    # Calc stores TRUE as 1. Excel's #VALUE! for a boolean is not copied.
    assert calc.iseven(True) is False
    assert calc.isodd(True) is True
    # floor(q + 0.5) is 1 here, and getMround's approxValue path is also 1.
    assert calc.mround(math.nextafter(0.5, 0.0), 1) == 1.0


def test_isoweeknum_date_matches_serial():
    day = dt.date(2023, 1, 1)
    assert calc.isoweeknum(day) == calc.isoweeknum(day.toordinal() - 693594)


