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


def test_dollarde_dollarfr_nonfinite_is_nan():
    # math.floor(inf) and int(inf) raise OverflowError. That escaped the
    # (ValueError, TypeError) handler and crashed the formula.
    assert math.isnan(calc.dollarde(1.02, float("inf")))
    assert math.isnan(calc.dollarde(1.02, float("-inf")))
    assert math.isnan(calc.dollarfr(float("inf"), 4))
    assert math.isnan(calc.dollarfr(float("-inf"), 4))
    assert math.isnan(calc.dollarde(float("nan"), 4))
    assert math.isnan(calc.dollarfr(1.5, float("nan")))
    assert math.isclose(calc.dollarde(1.02, 4), 1.05)
    assert math.isclose(calc.dollarfr(1.5, 4), 1.2)


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
