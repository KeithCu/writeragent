# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc formula parity helpers (plugin.scripting.calc_functions / xl)."""

from __future__ import annotations

import plugin.scripting.calc_functions as xl


def test_conditional_aggregates():
    assert xl.sumif([5.0, 12.0, 3.0, 15.0, 8.0], ">10", [1.0, 2.0, 3.0, 4.0, 5.0]) == 6.0

    assert (
        xl.sumifs(
            [1.0, 2.0, 3.0, 4.0, 5.0],
            [5.0, 12.0, 3.0, 15.0, 8.0],
            ">5",
            [5.0, 12.0, 3.0, 15.0, 8.0],
            "<=12",
        )
        == 7.0
    )

    assert xl.countif([5.0, 12.0, 3.0, 15.0, 8.0], "<=5") == 2.0

    assert (
        xl.countifs(
            [5.0, 12.0, 3.0, 15.0, 8.0],
            ">5",
            [1.0, 2.0, 3.0, 4.0, 5.0],
            "<10",
        )
        == 3.0
    )

    assert abs(xl.averageif([5.0, 12.0, 3.0, 15.0, 8.0], ">5", [1.0, 2.0, 3.0, 4.0, 5.0]) - 11.0 / 3.0) < 1e-9

    assert abs(xl.averageifs([1.0, 2.0, 3.0, 4.0, 5.0], [5.0, 12.0, 3.0, 15.0, 8.0], ">5") - 11.0 / 3.0) < 1e-9


def test_lookup_text_date():
    assert xl.xlookup("apple", ["pear", "apple", "banana"], [10.0, 20.0, 30.0], "Not Found") == 20.0
    assert xl.xlookup("orange", ["pear", "apple", "banana"], [10.0, 20.0, 30.0], "Not Found") == "Not Found"

    assert xl.textjoin(", ", True, ["apple", "", "banana"]) == "apple, banana"

    assert xl.regex("123-456", "[0-9]+", "XXX", "g") == "XXX-XXX"

    assert xl.eomonth(46182, 1) == 46234.0
    assert xl.networkdays(46181, 46185) == 5.0


def test_tier_abc_helpers():
    assert xl.subtotal(9, [1.0, 2.0, 3.0, 4.0, 5.0]) == 15.0

    assert xl.isblank("") is True
    assert xl.isblank(5.0) is False

    assert xl.isnumber(5.0) is True
    assert xl.isnumber("x") is False

    assert xl.sumproduct([1.0, 2.0, 3.0], [4.0, 5.0, 6.0]) == 32.0
    assert xl.datedif(46181, 46185, "D") == 4.0

    assert xl.istext("hello") is True
    assert xl.large([1.0, 5.0, 3.0, 4.0, 2.0], 2) == 4.0
    assert xl.small([1.0, 5.0, 3.0, 4.0, 2.0], 2) == 2.0
    assert xl.averagea([10.0, "", 20.0]) == 10.0
    assert xl.even(3.0) == 4.0
    assert xl.xmatch("b", ["a", "b", "c"]) == 2.0

    assert xl.filter([1.0, 2.0, 3.0, 4.0, 5.0], [True, False, True, False, True]) == [1.0, 3.0, 5.0]
    assert xl.sort([3.0, 1.0, 2.0], 1, -1) == [3.0, 2.0, 1.0]
    assert xl.unique([1.0, 2.0, 1.0, 3.0, 2.0]) == [1.0, 2.0, 3.0]


def test_error_handlers():
    assert xl.iferror(lambda: 1 / 0, 0) == 0
    assert xl.iferror(lambda: 5.0, 0) == 5.0
    assert xl.ifna(lambda: None, 1) == 1
    assert xl.ifna(lambda: 2.0, 1) == 2.0


def test_always_injected_xl_does_not_resolve_bare_x():
    """Auto-imported ``xl`` must not make undefined bare ``x`` silently succeed."""
    from plugin.contrib.smolagents.local_python_executor import InterpreterError
    from plugin.scripting.config_limits import python_exec_timeout_default
    from plugin.scripting.venv_sandbox import _new_executor, inject_auto_imports

    executor = _new_executor(python_exec_timeout_default())
    inject_auto_imports(executor, "result = x")
    assert "xl" in executor.state
    try:
        executor("result = x")
    except InterpreterError as exc:
        assert "xl" in str(exc)
    else:
        raise AssertionError("bare x must raise InterpreterError when undefined")


def test_helper_names_complete():
    from plugin.scripting.calc_functions_common import HELPER_NAMES

    exported = {name for name in dir(xl) if not name.startswith("_") and callable(getattr(xl, name))}
    assert HELPER_NAMES <= exported


def test_tier_d_helpers():
    # Financial
    # PMT(0.05/12, 60, 10000) approx -188.71
    assert abs(xl.pmt(0.05 / 12, 60, 10000) - (-188.712336)) < 1e-2
    # FV(0.05/12, 60, -200, -10000) approx 26434.80
    assert abs(xl.fv(0.05 / 12, 60, -200, -10000) - 26434.80) < 1.0
    # PV(0.05/12, 60, -200, 26434.80) should be approx -10000
    assert abs(xl.pv(0.05 / 12, 60, -200, 26434.80) - (-10000.0)) < 1.0

    # Math
    assert xl.mround(1.23, 0.5) == 1.0
    assert xl.sumsq([3.0, 4.0]) == 25.0

    # Information
    assert xl.iseven(4) is True
    assert xl.iseven(3) is False
    assert xl.isodd(3) is True
    assert xl.isodd(4) is False

    # Date/Time
    assert xl.days(46185, 46181) == 4.0
    assert xl.time(12, 0, 0) == 0.5
    assert xl.trimmean([1.0, 2.0, 3.0, 4.0, 5.0], 0.2) == 3.0
    assert xl.forecast(6, [1.0, 2.0, 3.0, 4.0, 5.0], [1.0, 2.0, 3.0, 4.0, 5.0]) == 6.0


def test_15_more_helpers():
    # Lookup
    assert xl.choose(2, "a", "b", "c") == "b"
    assert xl.address(1, 1) == "$A$1"
    assert xl.address(1, 1, 4) == "A1"
    assert xl.areas("any") == 1.0

    # Date & Time
    assert abs(xl.yearfrac(44927, 45292, 1) - 1.0) < 0.1
    assert xl.days360(44927, 45292) == 360.0
    assert xl.networkdays_intl(46181, 46185, 1) == 5.0
    assert xl.workday_intl(46181, 4, 1) == 46185.0

    # Logical/Text
    assert xl.xor(True, False, True) is False
    assert xl.xor(True, False, False) is True
    assert xl.char(65) == "A"
    assert xl.code("A") == 65.0

    # Database
    db = [
        ["Tree", "Height", "Age", "Yield", "Profit"],
        ["Apple", 18.0, 20.0, 14.0, 105.0],
        ["Pear", 12.0, 12.0, 10.0, 96.0],
        ["Cherry", 13.0, 7.0, 8.0, 105.0],
        ["Apple", 14.0, 15.0, 10.0, 75.0],
        ["Pear", 9.0, 8.0, 8.0, 77.0],
        ["Apple", 8.0, 9.0, 6.0, 45.0],
    ]
    crit = [["Tree", "Height"], ["Apple", ">10"]]
    assert xl.dcount(db, "Yield", crit) == 2.0
    assert xl.dsum(db, "Profit", crit) == 180.0
    assert xl.daverage(db, "Yield", crit) == 12.0
    assert xl.dmax(db, "Height", crit) == 18.0
    assert xl.dmin(db, "Height", crit) == 14.0


def test_norminv():
    from plugin.scripting.calc_functions import norminv
    import math
    res = norminv(0.5, 0, 1)
    assert math.isclose(res, 0.0, abs_tol=1e-5)
    assert math.isnan(norminv(-0.1, 0, 1))

def test_normsdist():
    from plugin.scripting.calc_functions import normsdist
    import math
    res = normsdist(0)
    assert math.isclose(res, 0.5, abs_tol=1e-5)

def test_normsinv():
    from plugin.scripting.calc_functions import normsinv
    import math
    res = normsinv(0.5)
    assert math.isclose(res, 0.0, abs_tol=1e-5)

def test_pearson():
    from plugin.scripting.calc_functions import pearson
    import math
    res = pearson([1, 2, 3], [1, 2, 3])
    assert math.isclose(res, 1.0, abs_tol=1e-5)
    assert math.isnan(pearson([1], [1]))

def test_percentrank():
    from plugin.scripting.calc_functions import percentrank
    import math
    res = percentrank([1, 2, 3, 4], 3)
    assert math.isclose(res, 0.666, abs_tol=1e-2)
    assert math.isnan(percentrank([1, 2, 3, 4], 5))

def test_permut():
    from plugin.scripting.calc_functions import permut
    import math
    res = permut(5, 2)
    assert res == 20.0
    assert math.isnan(permut(2, 5))

def test_poisson():
    from plugin.scripting.calc_functions import poisson
    import math
    res_pmf = poisson(2, 2, False)
    assert math.isclose(res_pmf, 0.27067, abs_tol=1e-4)
    res_cdf = poisson(2, 2, True)
    assert math.isclose(res_cdf, 0.67667, abs_tol=1e-4)

def test_prob():
    from plugin.scripting.calc_functions import prob
    import math
    res = prob([1, 2, 3], [0.2, 0.3, 0.5], 2)
    assert math.isclose(res, 0.3, abs_tol=1e-5)
    res2 = prob([1, 2, 3], [0.2, 0.3, 0.5], 1, 2)
    assert math.isclose(res2, 0.5, abs_tol=1e-5)

def test_standardize():
    from plugin.scripting.calc_functions import standardize
    import math
    res = standardize(42, 40, 1.5)
    assert math.isclose(res, 1.33333, abs_tol=1e-4)

def test_tdist():
    from plugin.scripting.calc_functions import tdist
    import math
    res = tdist(1.96, 60, 2)
    assert math.isclose(res, 0.0546, abs_tol=1e-4)

def test_tinv():
    from plugin.scripting.calc_functions import tinv
    import math
    res = tinv(0.0546, 60)
    assert math.isclose(res, 1.96, abs_tol=1e-2)

def test_ttest():
    from plugin.scripting.calc_functions import ttest
    import math
    res = ttest([1, 2, 3], [1.1, 2.1, 3.1], 2, 1)
    assert not math.isnan(res)

def test_weibull():
    from plugin.scripting.calc_functions import weibull
    import math
    res = weibull(105, 20, 100, True)
    assert math.isclose(res, 0.9295, abs_tol=1e-4)

def test_ztest():
    from plugin.scripting.calc_functions import ztest
    import math
    res = ztest([3, 6, 7, 8, 6, 5, 4, 2, 1, 9], 4)
    assert math.isclose(res, 0.0905, abs_tol=1e-4)

def test_asc():
    from plugin.scripting.calc_functions import asc
    res = asc("Ｅｘｃｅｌ　Ｐｙｔｈｏｎ")
    assert res == "Excel Python"
