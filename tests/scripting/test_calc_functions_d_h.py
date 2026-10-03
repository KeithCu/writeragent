# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Edge cases for calc helpers in calc_functions_d_h.

Expected values were checked against LibreOffice Calc in this environment
and against Excel's published DOLLARDE / EFFECT / DAYS360 examples.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

import pytest

import plugin.scripting.calc_functions as calc


def _serial(year: int, month: int, day: int) -> int:
    return dt.date(year, month, day).toordinal() - 693594


def test_effect_non_positive_nominal_is_num_error():
    # Excel and Calc reject nominal_rate <= 0 (#NUM!). The algebra at 0 is 0.
    assert math.isnan(calc.effect(0, 4))
    assert math.isnan(calc.effect(-0.1, 4))
    assert math.isclose(calc.effect(0.0525, 4), 0.05354266737075819, rel_tol=1e-9)


def test_dollarde_dollarfr_match_excel_calc_scale():
    # Denominator sets the digit count via ceil(log10), not the typed decimals.
    # DOLLARDE(1.02, 4) is 1.05 in Calc, not 1.5. DOLLARDE(1.1, 32) is Excel's
    # documented 1.3125 (1 and 10/32).
    assert math.isclose(calc.dollarde(1.02, 16), 1.125)
    assert math.isclose(calc.dollarde(1.1, 32), 1.3125)
    assert math.isclose(calc.dollarde(1.02, 4), 1.05)
    assert math.isclose(calc.dollarde(1.03, 3), 1.1)
    assert math.isclose(calc.dollarde(1.02, 1), 1.02)
    assert math.isclose(calc.dollarde(-1.02, 16), -1.125)
    assert math.isclose(calc.dollarfr(1.125, 16), 1.02)
    assert math.isclose(calc.dollarfr(1.5, 4), 1.2)
    assert math.isclose(calc.dollarfr(1.3125, 32), 1.1)
    assert math.isclose(calc.dollarfr(1.02, 1), 1.02)


def test_db_period_past_final_stub_is_nan():
    # life+1 is still a real period. Past that, Calc returns #NUM!.
    partial = calc.db(1_000_000, 100_000, 6, 7, 7)
    assert math.isclose(partial, 15845.098473848071, rel_tol=1e-9)
    assert calc.db(10000, 1000, 5, 6, 12) == 0.0
    assert math.isnan(calc.db(10000, 1000, 5, 7, 12))
    assert math.isnan(calc.db(1_000_000, 100_000, 6, 8, 7))
    assert not math.isnan(calc.db(10000, 1000, 5, 1))


def test_disc_zero_redemption_is_nan():
    assert math.isnan(calc.disc(43831, 43983, 95, 0))
    assert math.isfinite(calc.disc(43831, 43983, 95, 100))


def test_dollar_and_fixed_negative_decimals_round_left():
    assert calc.dollar(12345, -2) == "$12,300"
    assert calc.dollar(1234.567) == "$1,234.57"
    assert calc.dollar(1234.567, 1) == "$1,234.6"
    assert calc.fixed(12345, -2) == "12,300"
    assert calc.fixed(12345, -2, True) == "12300"
    assert calc.fixed(-12345.6, -1) == "-12,350"
    assert calc.fixed(1234.567) == "1,234.57"
    assert calc.fixed(1234.567, 1, True) == "1234.6"


def test_duration_zero_yield_is_the_closed_form_limit():
    # 2018-01-01 to 2023-01-01, semiannual: 10 periods. Zero coupon is n/f.
    # An 8% coupon at 0% yield is the undiscounted cash-flow average, 4.357...
    start = _serial(2018, 1, 1)
    end = _serial(2023, 1, 1)
    assert math.isclose(calc.duration(start, end, 0, 0, 2, 0), 5.0, rel_tol=1e-12)
    assert math.isclose(calc.duration(start, end, 0.08, 0, 2, 0), 30.5 / 7.0, rel_tol=1e-12)
    assert math.isfinite(calc.duration(start, end, 0.08, 0.09, 2, 0))


def test_growth_rejects_non_positive_known_y():
    assert calc.growth([1.0, 2.0, -1.0], [1.0, 2.0, 3.0], 4) == []
    assert calc.growth([1.0, 0.0, 3.0], [1.0, 2.0, 3.0], 4) == []
    fitted = calc.growth([1.0, 2.0, 4.0], [1.0, 2.0, 3.0], 4)
    assert math.isclose(fitted[0], 8.0, rel_tol=1e-9)
    through_origin = calc.growth([1.0, 2.0, 4.0], [1.0, 2.0, 3.0], 4, False)
    assert len(through_origin) == 1
    assert math.isfinite(through_origin[0])


def test_days360_us_february_month_end():
    # US NASD treats a February month-end start as day 30. European does not.
    assert calc.days360(_serial(2023, 2, 28), _serial(2023, 3, 1), False) == 1.0
    assert calc.days360(_serial(2024, 2, 29), _serial(2024, 3, 1), False) == 1.0
    assert calc.days360(_serial(2024, 2, 28), _serial(2024, 3, 1), False) == 3.0
    assert calc.days360(_serial(2023, 2, 28), _serial(2023, 3, 31), False) == 30.0
    assert calc.days360(_serial(2023, 1, 31), _serial(2023, 2, 28), False) == 28.0
    assert calc.days360(_serial(2023, 2, 28), _serial(2024, 2, 28), False) == 358.0
    assert calc.days360(_serial(2024, 2, 29), _serial(2025, 2, 28), False) == 358.0
    assert calc.days360(_serial(2023, 2, 28), _serial(2023, 3, 1), True) == 3.0
    assert calc.days360(_serial(2024, 2, 29), _serial(2024, 3, 1), True) == 2.0
    assert calc.days360(44927, 45292) == 360.0


# One row per bad input these tests did not already cover.
# "nan" is the helper's #NUM! / #VALUE! / #DIV/0! stand-in. A quoted token
# is the string the helper returns. geomean and harmean already have this
# table. DCOUNT, DCOUNTA, DPRODUCT, DSUM, ENCODEURL, FMT, and FREQUENCY do
# not return NaN or a spreadsheet error for these inputs. GROWTH's
# non-positive cases are already asserted as an empty result.
_DB = [["Tree", "Height"], ["Apple", 18.0], ["Pear", 12.0], ["Apple", 8.0]]
_CRIT = [["Tree"], ["Apple"]]
_NAN = "nan"

_D_H_BAD_INPUTS = [
    ("datedif-blank", "datedif", ("", 44927, "D"), _NAN),
    ("datedif-oor", "datedif", (1e20, 1e20, "D"), _NAN),
    ("datevalue-text", "datevalue", ("not-a-date",), _NAN),
    ("datevalue-blank", "datevalue", ("",), _NAN),
    ("datevalue-zero", "datevalue", (0,), _NAN),
    ("datevalue-oor", "datevalue", ("9999-99-99",), _NAN),
    ("daverage-text", "daverage", ("text", "Height", _CRIT), _NAN),
    ("daverage-blank", "daverage", (None, "Height", _CRIT), _NAN),
    ("daverage-zero", "daverage", (_DB, 0, _CRIT), _NAN),
    ("daverage-oor", "daverage", (_DB, 99, _CRIT), _NAN),
    ("days-text", "days", ("text", 1), _NAN),
    ("days-blank", "days", ("", ""), _NAN),
    ("days360-text", "days360", ("text", 44927), _NAN),
    ("days360-blank", "days360", ("", ""), _NAN),
    ("days360-oor", "days360", (1e12, 1e12), _NAN),
    ("db-text", "db", ("text", 1000, 5, 1), _NAN),
    ("db-blank", "db", ("", 1000, 5, 1), _NAN),
    ("ddb-text", "ddb", ("x", 1000, 5, 1), _NAN),
    ("ddb-blank", "ddb", ("", 1000, 5, 1), _NAN),
    ("ddb-zero", "ddb", (10000, 1000, 0, 1), _NAN),
    ("decimal-text", "decimal", ("zz", 16), _NAN),
    ("decimal-blank", "decimal", ("", 10), _NAN),
    ("decimal-zero", "decimal", ("0", 0), _NAN),
    ("decimal-oor", "decimal", ("10", 1), _NAN),
    ("delta-text", "delta", ("x", 1), _NAN),
    ("delta-blank", "delta", ("", 0), _NAN),
    ("devsq-text", "devsq", (["a", "b"],), _NAN),
    ("devsq-oor", "devsq", ([float("inf"), 1.0],), _NAN),
    ("dget-text", "dget", ("text", "Height", _CRIT), "#VALUE!"),
    ("dget-blank", "dget", (None, "Height", _CRIT), "#VALUE!"),
    ("dget-zero", "dget", (_DB, 0, _CRIT), "#VALUE!"),
    ("dget-oor", "dget", (_DB, "Height", _CRIT), "#NUM!"),
    ("disc-text", "disc", ("x", 43983, 95, 100), _NAN),
    ("disc-blank", "disc", ("", 43983, 95, 100), _NAN),
    ("disc-oor", "disc", (43831, 43983, 95, 100, 9), _NAN),
    ("dmax-text", "dmax", ("text", "Height", _CRIT), _NAN),
    ("dmax-blank", "dmax", (None, "Height", _CRIT), _NAN),
    ("dmax-zero", "dmax", (_DB, 0, _CRIT), _NAN),
    ("dmax-oor", "dmax", (_DB, 99, _CRIT), _NAN),
    ("dmin-text", "dmin", ("text", "Height", _CRIT), _NAN),
    ("dmin-blank", "dmin", (None, "Height", _CRIT), _NAN),
    ("dmin-zero", "dmin", (_DB, 0, _CRIT), _NAN),
    ("dmin-oor", "dmin", (_DB, 99, _CRIT), _NAN),
    ("dollar-text", "dollar", ("abc",), _NAN),
    ("dollar-blank", "dollar", ("",), _NAN),
    ("dollar-oor", "dollar", (1, float("inf")), _NAN),
    ("dollarde-text", "dollarde", ("x", 16), _NAN),
    ("dollarde-blank", "dollarde", ("", 16), _NAN),
    ("dollarde-zero", "dollarde", (1.02, 0), _NAN),
    ("dollarde-oor", "dollarde", (1.02, float("inf")), _NAN),
    ("dollarfr-text", "dollarfr", ("x", 16), _NAN),
    ("dollarfr-blank", "dollarfr", ("", 16), _NAN),
    ("dollarfr-zero", "dollarfr", (1.5, 0), _NAN),
    ("dollarfr-oor", "dollarfr", (1.5, float("inf")), _NAN),
    ("dstdev-text", "dstdev", ("text", "Height", _CRIT), _NAN),
    ("dstdev-blank", "dstdev", (None, "Height", _CRIT), _NAN),
    ("dstdev-zero", "dstdev", (_DB, 0, _CRIT), _NAN),
    ("dstdev-oor", "dstdev", (_DB, 99, _CRIT), _NAN),
    ("dstdevp-text", "dstdevp", ("text", "Height", _CRIT), _NAN),
    ("dstdevp-blank", "dstdevp", (None, "Height", _CRIT), _NAN),
    ("dstdevp-zero", "dstdevp", (_DB, 0, _CRIT), _NAN),
    ("dstdevp-oor", "dstdevp", (_DB, 99, _CRIT), _NAN),
    ("duration-text", "duration", ("x", 45000, 0.05, 0.06, 2), _NAN),
    ("duration-blank", "duration", ("", 45000, 0.05, 0.06, 2), _NAN),
    ("duration-zero", "duration", (43831, 45000, 0.05, 0.06, 0), _NAN),
    ("duration-oor", "duration", (43831, 45000, 0.05, 0.06, 2, float("inf")), _NAN),
    ("dvar-text", "dvar", ("text", "Height", _CRIT), _NAN),
    ("dvar-blank", "dvar", (None, "Height", _CRIT), _NAN),
    ("dvar-zero", "dvar", (_DB, 0, _CRIT), _NAN),
    ("dvar-oor", "dvar", (_DB, 99, _CRIT), _NAN),
    ("dvarp-text", "dvarp", ("text", "Height", _CRIT), _NAN),
    ("dvarp-blank", "dvarp", (None, "Height", _CRIT), _NAN),
    ("dvarp-zero", "dvarp", (_DB, 0, _CRIT), _NAN),
    ("dvarp-oor", "dvarp", (_DB, 99, _CRIT), _NAN),
    ("edate-text", "edate", ("text", 1), _NAN),
    ("edate-oor", "edate", (44927, 100000), _NAN),
    ("effect-text", "effect", ("x", 4), _NAN),
    ("effect-blank", "effect", ("", 4), _NAN),
    ("effect-oor", "effect", (0.05, float("inf")), _NAN),
    ("eomonth-text", "eomonth", ("text", 1), _NAN),
    ("eomonth-oor", "eomonth", (44927, 100000), _NAN),
    ("erf-text", "erf", ("x",), _NAN),
    ("erf-blank", "erf", ("",), _NAN),
    ("erfc-text", "erfc", ("x",), _NAN),
    ("erfc-blank", "erfc", ("",), _NAN),
    ("euroconvert-text", "euroconvert", ("x", "EUR", "DEM"), _NAN),
    ("euroconvert-blank", "euroconvert", ("", "EUR", "DEM"), _NAN),
    ("euroconvert-oor", "euroconvert", (1e308, "EUR", "BEF"), _NAN),
    ("euroconvert-precision", "euroconvert", (100, "DEM", "ATS", False, float("inf")), _NAN),
    ("even-blank", "even", ("",), _NAN),
    ("expondist-text", "expondist", ("x", 1), _NAN),
    ("expondist-blank", "expondist", ("", 1), _NAN),
    ("expondist-zero", "expondist", (1, 0), _NAN),
    ("expondist-oor", "expondist", (-1, 1), _NAN),
    ("fact-text", "fact", ("x",), _NAN),
    ("fact-blank", "fact", ("",), _NAN),
    ("fact-oor", "fact", (-1,), _NAN),
    ("fact-huge", "fact", (171,), _NAN),
    ("factdouble-text", "factdouble", ("x",), _NAN),
    ("factdouble-blank", "factdouble", ("",), _NAN),
    ("factdouble-oor", "factdouble", (-1,), _NAN),
    ("factdouble-huge", "factdouble", (1000,), _NAN),
    ("fdist-text", "fdist", ("x", 2, 5), _NAN),
    ("fdist-blank", "fdist", ("", 2, 5), _NAN),
    ("fdist-zero", "fdist", (1, 0, 5), _NAN),
    ("fdist-oor", "fdist", (-1, 2, 5), _NAN),
    ("filter-text", "filter", ("text", [True]), "#VALUE!"),
    ("filter-blank", "filter", ([], []), "#VALUE!"),
    ("finv-text", "finv", ("x", 2, 5), _NAN),
    ("finv-blank", "finv", ("", 2, 5), _NAN),
    ("finv-zero", "finv", (0.5, 0, 5), _NAN),
    ("finv-oor", "finv", (1.5, 2, 5), _NAN),
    ("fisher-text", "fisher", ("x",), _NAN),
    ("fisher-blank", "fisher", ("",), _NAN),
    ("fisher-oor", "fisher", (1,), _NAN),
    ("fisherinv-text", "fisherinv", ("x",), _NAN),
    ("fisherinv-blank", "fisherinv", ("",), _NAN),
    ("fixed-text", "fixed", ("abc",), _NAN),
    ("fixed-blank", "fixed", ("",), _NAN),
    ("fixed-oor", "fixed", (1, float("inf")), _NAN),
    ("forecast-blank", "forecast", ("", [1.0, 2.0], [1.0, 2.0]), _NAN),
    ("forecast-zero", "forecast", (6, [1.0, 2.0], [0.0, 0.0]), _NAN),
    ("fv-blank", "fv", ("", 12, -100), _NAN),
    ("fv-oor", "fv", (0.01, 12, -100, 0, float("inf")), _NAN),
    ("fvschedule-text", "fvschedule", ("x", [0.1]), _NAN),
    ("fvschedule-blank", "fvschedule", ("", [0.1]), _NAN),
    ("fvschedule-oor", "fvschedule", (100, ["a"]), _NAN),
    ("gamma-text", "gamma", ("x",), _NAN),
    ("gamma-blank", "gamma", ("",), _NAN),
    ("gamma-zero", "gamma", (0,), _NAN),
    ("gamma-oor", "gamma", (200,), _NAN),
    ("gammadist-text", "gammadist", ("x", 2, 1), _NAN),
    ("gammadist-blank", "gammadist", ("", 2, 1), _NAN),
    ("gammadist-zero", "gammadist", (1, 0, 1), _NAN),
    ("gammadist-oor", "gammadist", (-1, 2, 1), _NAN),
    ("gammainv-text", "gammainv", ("x", 2, 1), _NAN),
    ("gammainv-blank", "gammainv", ("", 2, 1), _NAN),
    ("gammainv-zero", "gammainv", (0.5, 0, 1), _NAN),
    ("gammainv-oor", "gammainv", (1.5, 2, 1), _NAN),
    ("gammaln-text", "gammaln", ("x",), _NAN),
    ("gammaln-blank", "gammaln", ("",), _NAN),
    ("gammaln-zero", "gammaln", (0,), _NAN),
    ("gammaln-oor", "gammaln", (-1,), _NAN),
    ("gauss-text", "gauss", ("x",), _NAN),
    ("gauss-blank", "gauss", ("",), _NAN),
    ("gestep-text", "gestep", ("x", 0), _NAN),
    ("gestep-blank", "gestep", ("", 0), _NAN),
    ("hypgeomdist-text", "hypgeomdist", ("x", 5, 3, 10), _NAN),
    ("hypgeomdist-blank", "hypgeomdist", ("", 5, 3, 10), _NAN),
    ("hypgeomdist-zero", "hypgeomdist", (0, 0, 0, 0), _NAN),
    ("hypgeomdist-oor", "hypgeomdist", (5, 3, 2, 10), _NAN),
    ("hypgeomdist-inf", "hypgeomdist", (float("inf"), 5, 3, 10), _NAN),
]


@pytest.mark.parametrize(
    ("label", "name", "args", "expected"),
    _D_H_BAD_INPUTS,
    ids=[row[0] for row in _D_H_BAD_INPUTS],
)
def test_d_h_bad_inputs(label: str, name: str, args: tuple[Any, ...], expected: str) -> None:
    result = getattr(calc, name)(*args)
    if expected == _NAN:
        assert isinstance(result, float) and math.isnan(result), (label, result)
    else:
        assert result == expected, (label, result)
