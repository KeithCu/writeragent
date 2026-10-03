# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bad-input tables for public calc helpers whose names start with T–Z.

Each row is an input that was not already locked by an existing test. The
expected value is whatever that helper returns today: NaN, or a spreadsheet
error token. Text, blank, zero, or out-of-range inputs that return a normal
value are omitted — those are not errors.

Skipped because the four kinds are already tested, or they do not produce an
error: ``t``, ``text``, ``textjoin``, ``type``, ``unicode``, ``unique``, ``xor``.
``textafter`` lives in ``calc_functions_n_s`` but its name starts with T.
"""

from __future__ import annotations

import math

import pytest

import plugin.scripting.calc_functions as calc

_INF = float("inf")

# (function, kind, args, "nan" or an error token)
_BAD_INPUTS: list[tuple[str, str, tuple, str]] = [
    # tdist: zero x is a real probability. Zero df, zero tails, and a tail
    # count outside 1 or 2 are #NUM! (NaN here).
    ("tdist", "text", ("text", 10, 2), "nan"),
    ("tdist", "blank", (None, 10, 2), "nan"),
    ("tdist", "blank-empty", ("", 10, 2), "nan"),
    ("tdist", "zero-df", (1.96, 0, 2), "nan"),
    ("tdist", "zero-tails", (1.96, 10, 0), "nan"),
    ("tdist", "out-of-range-x", (-1, 10, 2), "nan"),
    ("tdist", "out-of-range-tails", (1, 10, 3), "nan"),
    ("tdist", "out-of-range-inf-tails", (1, 10, _INF), "nan"),
    # textafter / textbefore: instance 0 and a text instance are already tested.
    ("textafter", "blank", ("", "-", 1), "nan"),
    ("textafter", "blank-delimiter", ("a-b", "", 1), "nan"),
    ("textafter", "blank-instance", ("a-b", "-", None), "nan"),
    ("textafter", "out-of-range-instance", ("a-b", "-", 99), "nan"),
    ("textafter", "out-of-range-inf-instance", ("a-b", "-", _INF), "nan"),
    ("textbefore", "blank", ("", "-", 1), "nan"),
    ("textbefore", "blank-delimiter", ("a-b", "", 1), "nan"),
    ("textbefore", "blank-instance", ("a-b", "-", None), "nan"),
    ("textbefore", "out-of-range-instance", ("a-b", "-", 99), "nan"),
    ("textbefore", "out-of-range-inf-instance", ("a-b", "-", _INF), "nan"),
    # textsplit: text, zero, and a wild match_mode still split. An empty
    # delimiter is the blank that returns NaN.
    ("textsplit", "blank-delimiter", ("a-b", ""), "nan"),
    # time: text hour and a negative remainder are already tested. Zero is a
    # real time (0).
    ("time", "blank", (None, 0, 0), "nan"),
    ("time", "blank-empty", ("", 0, 0), "nan"),
    ("time", "out-of-range-inf", (_INF, 0, 0), "nan"),
    ("timevalue", "text", ("abc",), "nan"),
    ("timevalue", "blank", ("",), "nan"),
    ("timevalue", "blank-none", (None,), "nan"),
    ("timevalue", "zero", (0,), "nan"),
    ("timevalue", "out-of-range", ("25:00:00",), "nan"),
    ("tinv", "text", ("nope", 10), "nan"),
    ("tinv", "blank", (None, 10), "nan"),
    ("tinv", "blank-empty", ("", 10), "nan"),
    ("tinv", "zero-prob", (0, 10), "nan"),
    ("tinv", "zero-df", (0.05, 0), "nan"),
    ("tinv", "out-of-range-prob", (1.5, 10), "nan"),
    ("tinv", "out-of-range-df", (0.05, 0.5), "nan"),
    # trend: an empty known_y is already #VALUE!. A flat zero series fits.
    ("trend", "text", (["a", "b"],), "#VALUE!"),
    ("trend", "blank", ([None, None],), "#VALUE!"),
    ("trend", "blank-none", (None,), "#VALUE!"),
    ("trend", "out-of-range-shape", ([1.0, 2.0, 3.0], [1.0, 2.0]), "#VALUE!"),
    # trimmean: text cells, a blank cell, and a text percent are already NaN.
    # Percent 0 is the untrimmed mean.
    ("trimmean", "blank-empty", ([], 0.2), "nan"),
    ("trimmean", "out-of-range-percent", ([1.0, 2.0, 3.0, 4.0], 1), "nan"),
    ("trimmean", "out-of-range-negative", ([1.0, 2.0, 3.0, 4.0], -0.1), "nan"),
    ("ttest", "text", (["a", "b"], [1, 2, 3], 2, 2), "nan"),
    ("ttest", "blank", ([None, None], [1, 2, 3], 2, 2), "nan"),
    ("ttest", "zero-tails", ([1, 2, 3], [2, 3, 4], 0, 1), "nan"),
    ("ttest", "zero-type", ([1, 2, 3], [2, 3, 4], 2, 0), "nan"),
    ("ttest", "out-of-range-tails", ([1, 2, 3], [2, 3, 4], 3, 1), "nan"),
    ("ttest", "out-of-range-type", ([1, 2, 3], [2, 3, 4], 2, 4), "nan"),
    ("ttest", "out-of-range-inf-tails", ([1, 2, 3], [2, 3, 4], _INF, 1), "nan"),
    # unichar: a negative code point is already NaN.
    ("unichar", "text", ("A",), "nan"),
    ("unichar", "blank", (None,), "nan"),
    ("unichar", "blank-empty", ("",), "nan"),
    ("unichar", "zero", (0,), "nan"),
    ("unichar", "out-of-range", (0x110000,), "nan"),
    ("unichar", "out-of-range-inf", (_INF,), "nan"),
    # vara / varpa: text, blank, and zero are coerced to numbers (variance 0
    # when there are enough). Fewer than two samples is the error for VARA;
    # VARPA of one number is 0, and only the empty call is NaN.
    ("vara", "blank-empty", (), "nan"),
    ("vara", "out-of-range-count", (1,), "nan"),
    ("varpa", "blank-empty", (), "nan"),
    # weekday / weeknum: serial 0 is a real date. Return type 4 / 3 is already NaN.
    ("weekday", "text", ("nope",), "nan"),
    ("weekday", "blank", (None,), "nan"),
    ("weekday", "blank-empty", ("",), "nan"),
    ("weekday", "out-of-range-serial", (10**12,), "nan"),
    ("weekday", "out-of-range-negative", (-10**9,), "nan"),
    ("weekday", "out-of-range-inf", (_INF,), "nan"),
    ("weekday", "out-of-range-type", (44927, 0), "nan"),
    ("weekday", "text-type", (44927, "nope"), "nan"),
    ("weeknum", "text", ("nope",), "nan"),
    ("weeknum", "blank", (None,), "nan"),
    ("weeknum", "blank-empty", ("",), "nan"),
    ("weeknum", "out-of-range-serial", (10**12,), "nan"),
    ("weeknum", "out-of-range-negative", (-10**9,), "nan"),
    ("weeknum", "out-of-range-inf", (_INF,), "nan"),
    ("weeknum", "out-of-range-type", (44927, 0), "nan"),
    ("weeknum", "text-type", (44927, "x"), "nan"),
    # weibull: x of 0 is a real cdf. Zero or negative shape/scale is #NUM!.
    ("weibull", "text", ("x", 1, 1), "nan"),
    ("weibull", "blank", (None, 1, 1), "nan"),
    ("weibull", "blank-empty", ("", 1, 1), "nan"),
    ("weibull", "zero-alpha", (1, 0, 1), "nan"),
    ("weibull", "zero-beta", (1, 1, 0), "nan"),
    ("weibull", "out-of-range-x", (-1, 1, 1), "nan"),
    ("weibull", "out-of-range-alpha", (1, -1, 1), "nan"),
    # workday: a text day count is already NaN. Zero days and a negative
    # count are real dates. A holiday that does not parse is ignored.
    ("workday", "text-start", ("nope", 1), "nan"),
    ("workday", "blank-days", (46181, None), "nan"),
    ("workday", "blank-start", (None, 1), "nan"),
    ("workday", "blank-empty", ("", 1), "nan"),
    ("workday", "out-of-range-inf", (46181, _INF), "nan"),
    # workday_intl: text days, a blank weekend, and weekend codes 0/8/18
    # are already NaN.
    ("workday_intl", "text-start", ("nope", 1, 1), "nan"),
    ("workday_intl", "blank-days", (46181, None, 1), "nan"),
    ("workday_intl", "blank-start", (None, 1, 1), "nan"),
    ("workday_intl", "out-of-range-inf", (46181, _INF, 1), "nan"),
    # xirr: a zero guess still finds the root. The tiny-derivative case is
    # already NaN.
    ("xirr", "text", (["a", "b"], [1, 2]), "nan"),
    ("xirr", "blank", ([None, None], [1, 2]), "nan"),
    ("xirr", "blank-empty", ([], []), "nan"),
    ("xirr", "blank-none", (None, None), "nan"),
    ("xirr", "out-of-range-length", ([-100, 110], [0]), "nan"),
    ("xirr", "out-of-range-guess", ([-100, 110], [0, 365], -2), "nan"),
    ("xirr", "text-guess", ([-100, 110], [0, 365], "g"), "nan"),
    # xlookup: a bad match mode returns if_not_found, not an error. A return
    # array shorter than the match used to raise.
    ("xlookup", "out-of-range-return", ("b", ["a", "b"], [1]), "#VALUE!"),
    ("xlookup", "out-of-range-rows", ("c", ["a", "b", "c"], [["x"], ["y"]]), "#VALUE!"),
    ("xlookup", "out-of-range-column", ("b", [["a"], ["b"]], [[10]]), "#VALUE!"),
    # xmatch: a blank that occurs in the lookup vector is a hit, not an error.
    ("xmatch", "text", ("z", [1, 2, 3]), "nan"),
    ("xmatch", "zero", (0, [1, 2, 3]), "nan"),
    ("xmatch", "blank-empty", ("a", []), "nan"),
    ("xmatch", "out-of-range-mode", (1, [1, 2], 9), "nan"),
    ("xmatch", "text-mode", (1, [1], "bad"), "nan"),
    ("xmatch", "out-of-range-inf-mode", (1, [1], _INF), "nan"),
    # xnpv: rate <= -1 is already NaN. Rate 0 is a real present value.
    ("xnpv", "text", ("rate", [-100, 110], [0, 365]), "nan"),
    ("xnpv", "blank", (None, [-100, 110], [0, 365]), "nan"),
    ("xnpv", "blank-empty-rate", ("", [-100, 110], [0, 365]), "nan"),
    ("xnpv", "blank-values", (0.1, [], []), "nan"),
    ("xnpv", "text-values", (0.1, ["a", "b"], [0, 365]), "nan"),
    ("xnpv", "out-of-range-length", (0.1, [-100, 110], [0]), "nan"),
    # yearfrac: basis 0–4 on real dates is already tested. Equal serial 0 is 0.
    ("yearfrac", "text", ("nope", 45292), "nan"),
    ("yearfrac", "blank", (None, 45292), "nan"),
    ("yearfrac", "blank-empty", ("", 45292), "nan"),
    ("yearfrac", "out-of-range-basis", (44927, 45292, 5), "nan"),
    ("yearfrac", "out-of-range-negative-basis", (44927, 45292, -1), "nan"),
    ("yearfrac", "text-basis", (44927, 45292, "b"), "nan"),
    ("yearfrac", "out-of-range-inf", (_INF, 45292, 1), "nan"),
    # The YIELD* helpers are stubs: every input, including these, is NaN.
    ("yield_calc", "text", ("a", "b", "c", "d", "e", "f"), "nan"),
    ("yield_calc", "blank", (None, None, None, None, None, None), "nan"),
    ("yield_calc", "zero", (0, 0, 0, 0, 0, 0), "nan"),
    ("yield_calc", "out-of-range", (-1, -2, -3, 0, 0, 0), "nan"),
    ("yielddisc", "text", ("a", "b", "c", "d"), "nan"),
    ("yielddisc", "blank", (None, None, None, None), "nan"),
    ("yielddisc", "zero", (0, 0, 0, 0), "nan"),
    ("yielddisc", "out-of-range", (-1, -2, 0, 0), "nan"),
    ("yieldmat", "text", ("a", "b", "c", "d", "e"), "nan"),
    ("yieldmat", "blank", (None, None, None, None, None), "nan"),
    ("yieldmat", "zero", (0, 0, 0, 0, 0), "nan"),
    ("yieldmat", "out-of-range", (-1, -2, -3, 0, 0), "nan"),
    # ztest: a negative sigma is a real scale. Zero sigma and zero variance
    # are #DIV/0! (NaN here).
    ("ztest", "text", (["a", "b"], 1), "nan"),
    ("ztest", "blank", ([None, None], 1), "nan"),
    ("ztest", "blank-empty", ([], 1), "nan"),
    ("ztest", "blank-none", (None, 1), "nan"),
    ("ztest", "zero-sigma", ([1, 2, 3], 2, 0), "nan"),
    ("ztest", "zero-variance", ([5, 5, 5], 5), "nan"),
    ("ztest", "text-x", ([1, 2, 3], "x"), "nan"),
    ("ztest", "blank-x", ([1, 2, 3], None), "nan"),
    ("ztest", "text-sigma", ([1, 2, 3], 2, "s"), "nan"),
]


def _case_id(case: tuple[str, str, tuple, str]) -> str:
    name, kind, _unused_args, _unused_expected = case
    return f"{name}-{kind}"


@pytest.mark.parametrize(
    ("name", "kind", "args", "expected"),
    _BAD_INPUTS,
    ids=[_case_id(case) for case in _BAD_INPUTS],
)
def test_t_z_bad_input(name: str, kind: str, args: tuple, expected: str) -> None:
    """Text, blank, zero, and out-of-range inputs return the helper's error."""
    result = getattr(calc, name)(*args)
    if expected == "nan":
        assert isinstance(result, float) and math.isnan(result), (name, kind, result)
    else:
        assert result == expected, (name, kind, result)
