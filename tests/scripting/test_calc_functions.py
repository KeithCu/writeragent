# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc formula parity helpers (plugin.scripting.calc_functions / calc)."""

from __future__ import annotations
import math

import plugin.scripting.calc_functions as calc


def test_conditional_aggregates():
    assert calc.sumif([5.0, 12.0, 3.0, 15.0, 8.0], ">10", [1.0, 2.0, 3.0, 4.0, 5.0]) == 6.0

    assert (
        calc.sumifs(
            [1.0, 2.0, 3.0, 4.0, 5.0],
            [5.0, 12.0, 3.0, 15.0, 8.0],
            ">5",
            [5.0, 12.0, 3.0, 15.0, 8.0],
            "<=12",
        )
        == 7.0
    )

    assert calc.countif([5.0, 12.0, 3.0, 15.0, 8.0], "<=5") == 2.0

    assert (
        calc.countifs(
            [5.0, 12.0, 3.0, 15.0, 8.0],
            ">5",
            [1.0, 2.0, 3.0, 4.0, 5.0],
            "<10",
        )
        == 3.0
    )

    assert abs(calc.averageif([5.0, 12.0, 3.0, 15.0, 8.0], ">5", [1.0, 2.0, 3.0, 4.0, 5.0]) - 11.0 / 3.0) < 1e-9

    assert abs(calc.averageifs([1.0, 2.0, 3.0, 4.0, 5.0], [5.0, 12.0, 3.0, 15.0, 8.0], ">5") - 11.0 / 3.0) < 1e-9


def test_lookup_text_date():
    assert calc.xlookup("apple", ["pear", "apple", "banana"], [10.0, 20.0, 30.0], "Not Found") == 20.0
    assert calc.xlookup("orange", ["pear", "apple", "banana"], [10.0, 20.0, 30.0], "Not Found") == "Not Found"

    assert calc.textjoin(", ", True, ["apple", "", "banana"]) == "apple, banana"

    assert calc.regex("123-456", "[0-9]+", "XXX", "g") == "XXX-XXX"

    assert calc.eomonth(46182, 1) == 46234.0
    assert calc.networkdays(46181, 46185) == 5.0


def test_tier_abc_helpers():
    assert calc.subtotal(9, [1.0, 2.0, 3.0, 4.0, 5.0]) == 15.0

    assert calc.isblank("") is True
    assert calc.isblank(5.0) is False

    assert calc.isnumber(5.0) is True
    assert calc.isnumber("x") is False

    assert calc.sumproduct([1.0, 2.0, 3.0], [4.0, 5.0, 6.0]) == 32.0
    assert calc.datedif(46181, 46185, "D") == 4.0

    assert calc.istext("hello") is True
    assert calc.large([1.0, 5.0, 3.0, 4.0, 2.0], 2) == 4.0
    assert calc.small([1.0, 5.0, 3.0, 4.0, 2.0], 2) == 2.0
    assert calc.averagea([10.0, "", 20.0]) == 10.0
    assert calc.even(3.0) == 4.0
    assert calc.xmatch("b", ["a", "b", "c"]) == 2.0
    # TEXT() import calls calc.fmt. 46181 is 2026-06-08.
    assert calc.fmt(46181, "MMMM") == "June"

    assert calc.filter([1.0, 2.0, 3.0, 4.0, 5.0], [True, False, True, False, True]) == [1.0, 3.0, 5.0]
    assert calc.sort([3.0, 1.0, 2.0], 1, -1) == [3.0, 2.0, 1.0]
    assert calc.unique([1.0, 2.0, 1.0, 3.0, 2.0]) == [1.0, 2.0, 3.0]


def test_error_handlers():
    assert calc.iferror(lambda: 1 / 0, 0) == 0
    assert calc.iferror(lambda: 5.0, 0) == 5.0
    # None is blank, not #N/A. IFNA only substitutes NA.
    assert calc.ifna(lambda: None, 1) is None
    assert calc.ifna(lambda: 2.0, 1) == 2.0


def test_always_injected_calc_does_not_resolve_bare_x():
    """Auto-imported ``calc`` must not make undefined bare ``x`` silently succeed.

    (Previously the helpers alias was ``xl``, which smolagents could fuzzy-match from ``x``.)
    """
    from plugin.contrib.smolagents.local_python_executor import InterpreterError
    from plugin.scripting.config_limits import python_exec_timeout_default
    from plugin.scripting.venv.venv_sandbox import _new_executor, inject_auto_imports

    executor = _new_executor(python_exec_timeout_default())
    inject_auto_imports(executor, "result = x")
    assert "calc" in executor.state
    assert "xl" not in executor.state
    try:
        executor("result = x")
    except InterpreterError:
        pass
    else:
        raise AssertionError("bare x must raise InterpreterError when undefined")


def test_auto_imports_inject_st_dt_plt_aliases():
    """Sandbox gets st/dt/plt aliases without explicit imports when packages exist."""
    from plugin.framework.constants import AUTO_IMPORTS
    from plugin.scripting.config_limits import python_exec_timeout_default
    from plugin.scripting.venv.venv_sandbox import _new_executor, apply_auto_imports, inject_auto_imports, optional_module

    assert AUTO_IMPORTS["scipy.stats"] == "import scipy.stats as st"
    assert AUTO_IMPORTS["datetime"] == "import datetime as dt"
    assert AUTO_IMPORTS["matplotlib.pyplot"] == "import matplotlib.pyplot as plt"
    assert "seaborn" not in AUTO_IMPORTS

    code, _lines = apply_auto_imports("result = 1")
    assert "import datetime as dt" in code
    if optional_module("scipy.stats") is not None:
        assert "import scipy.stats as st" in code
    if optional_module("matplotlib.pyplot") is not None:
        assert "import matplotlib.pyplot as plt" in code

    executor = _new_executor(python_exec_timeout_default())
    inject_auto_imports(executor, "result = 1")
    assert "dt" in executor.state
    assert "datetime" not in executor.state
    assert executor("result = dt.date(2020, 1, 1).isoformat()").output == "2020-01-01"
    if optional_module("scipy.stats") is not None:
        assert "st" in executor.state
        assert executor("result = float(st.norm.cdf(0))").output == 0.5
    if optional_module("matplotlib.pyplot") is not None:
        assert "plt" in executor.state
        assert callable(executor.state["plt"].plot)


def test_helper_names_complete():
    from plugin.scripting.calc_functions_common import HELPER_NAMES

    exported = {name for name in dir(calc) if not name.startswith("_") and callable(getattr(calc, name))}
    assert HELPER_NAMES <= exported


def test_tier_d_helpers():
    # Financial
    # PMT(0.05/12, 60, 10000) approx -188.71
    assert abs(calc.pmt(0.05 / 12, 60, 10000) - (-188.712336)) < 1e-2
    # FV(0.05/12, 60, -200, -10000) approx 26434.80
    assert abs(calc.fv(0.05 / 12, 60, -200, -10000) - 26434.80) < 1.0
    # PV(0.05/12, 60, -200, 26434.80) should be approx -10000
    assert abs(calc.pv(0.05 / 12, 60, -200, 26434.80) - (-10000.0)) < 1.0

    # Math
    assert calc.mround(1.23, 0.5) == 1.0
    assert calc.sumsq([3.0, 4.0]) == 25.0

    # Information
    assert calc.iseven(4) is True
    assert calc.iseven(3) is False
    assert calc.isodd(3) is True
    assert calc.isodd(4) is False

    # Date/Time
    assert calc.days(46185, 46181) == 4.0
    assert calc.time(12, 0, 0) == 0.5
    assert calc.trimmean([1.0, 2.0, 3.0, 4.0, 5.0], 0.2) == 3.0
    assert calc.forecast(6, [1.0, 2.0, 3.0, 4.0, 5.0], [1.0, 2.0, 3.0, 4.0, 5.0]) == 6.0


def test_15_more_helpers():
    # Lookup
    assert calc.choose(2, "a", "b", "c") == "b"
    assert calc.address(1, 1) == "$A$1"
    assert calc.address(1, 1, 4) == "A1"
    assert calc.areas("any") == 1.0

    # Date & Time
    assert abs(calc.yearfrac(44927, 45292, 1) - 1.0) < 0.1
    assert calc.days360(44927, 45292) == 360.0
    assert calc.networkdays_intl(46181, 46185, 1) == 5.0
    assert calc.workday_intl(46181, 4, 1) == 46185.0

    # Logical/Text
    assert calc.xor(True, False, True) is False
    assert calc.xor(True, False, False) is True
    assert calc.char(65) == "A"
    assert calc.code("A") == 65.0

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
    assert calc.dcount(db, "Yield", crit) == 2.0
    assert calc.dsum(db, "Profit", crit) == 180.0
    assert calc.daverage(db, "Yield", crit) == 12.0
    assert calc.dmax(db, "Height", crit) == 18.0
    assert calc.dmin(db, "Height", crit) == 14.0


def test_dsum_numeric_looking_header_is_a_name():
    """A header 2024 (Calc float) and field \"2024\" name the column.

    int(float(\"2024\"))-1 used to index off the grid, and str(2024.0) was
    \"2024.0\", so the name missed and DSUM returned 0.
    """
    db = [[2024.0, "Name"], [5.0, "A"], [15.0, "B"]]
    crit = [["2024"], [">10"]]
    assert calc.dsum(db, "2024", crit) == 15.0
    assert calc.dsum(db, 1, crit) == 15.0
    named = [["2024", "Name"], [5.0, "A"], [15.0, "B"]]
    assert calc.dsum(named, "2024", crit) == 15.0

def test_financial_group_a():
    # Basic math validation - dates represented as strings/floats are accepted
    assert not math.isnan(calc.accrint(43831, 43862, 43891, 0.05, 1000, 2))
    assert not math.isnan(calc.accrintm(43831, 43891, 0.05, 1000))
    assert not math.isnan(calc.amordegrc(1000, 43831, 43983, 100, 1, 0.1))
    assert calc.amorlinc(1000, 43831, 43983, 100, 1, 0.1) == 100.0

    assert not math.isnan(calc.coupdaybs(43831, 43983, 2))
    assert not math.isnan(calc.coupdays(43831, 43983, 2))
    assert not math.isnan(calc.coupdaysnc(43831, 43983, 2))

    # coupncd returns a date ordinal (which we stubbed as nan)
    assert calc.coupncd(43831, 43983, 2) == 43983.0
    assert not math.isnan(calc.coupnum(43831, 43983, 2))

    # couppcd returns a date ordinal (which we stubbed as nan for simplified implementation)
    assert calc.couppcd(43831, 43983, 2) == 43803.0

    assert abs(calc.cumipmt(0.09 / 12, 360, 125000, 1, 12, 0) - (-11215.34288)) < 1e-2
    assert abs(calc.cumprinc(0.09 / 12, 360, 125000, 1, 12, 0) - (-853.99637)) < 1e-2
    # XNPV: cash flows at dates
    assert abs(calc.xnpv(0.1, [-10000, 2750, 4250, 3250, 2750], [43831, 43900, 44000, 44100, 44200]) - 2294.3573) < 1e-2

    assert not math.isnan(calc.db(10000, 1000, 5, 1))
    assert not math.isnan(calc.ddb(10000, 1000, 5, 1))

    assert not math.isnan(calc.disc(43831, 43983, 95, 100))

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
def test_bahttext():
    assert "Baht" in calc.bahttext(123)

def test_clean():
    assert calc.clean("A" + chr(7) + "B" + chr(10)) == "AB"
    assert isinstance(calc.clean(float("nan")), float) and math.isnan(calc.clean(float("nan")))

def test_dollar():
    assert calc.dollar(1234.567) == "$1,234.57"
    assert calc.dollar(1234.567, 1) == "$1,234.6"

def test_encodeurl():
    assert calc.encodeurl("http://example.com") == "http%3A%2F%2Fexample.com"

def test_fixed():
    assert calc.fixed(1234.567) == "1,234.57"
    assert calc.fixed(1234.567, 1, True) == "1234.6"

def test_jis():
    assert calc.jis("test") == "test"

def test_numbervalue():
    assert calc.numbervalue("1,234.56") == 1234.56
    assert calc.numbervalue("1.234,56", ",", ".") == 1234.56

def test_t():
    assert calc.t("test") == "test"
    assert calc.t(123) == ""

def test_textafter():
    assert calc.textafter("a-b-c", "-") == "b-c"
    assert calc.textafter("a-b-c", "-", 2) == "c"

def test_textbefore():
    assert calc.textbefore("a-b-c", "-") == "a"
    assert calc.textbefore("a-b-c", "-", 2) == "a-b"

def test_textsplit():
    assert calc.textsplit("a-b-c", "-") == [["a", "b", "c"]]
    assert calc.textsplit("a-b;c-d", "-", ";") == [["a", "b"], ["c", "d"]]

def test_unichar():
    assert calc.unichar(65) == "A"
    assert math.isnan(calc.unichar(-1))

def test_unicode():
    assert calc.unicode("A") == 65
    assert math.isnan(calc.unicode(""))

def test_besseli():
    import scipy.special
    assert math.isclose(calc.besseli(1.5, 1), scipy.special.iv(1, 1.5))
    assert math.isnan(calc.besseli(1.5, -1))

def test_besselj():
    import scipy.special
    assert math.isclose(calc.besselj(1.5, 1), scipy.special.jv(1, 1.5))
    assert math.isnan(calc.besselj(1.5, -1))


def test_group_i_functions():
    # Bessel K and Y
    assert not math.isnan(calc.besselk(1.5, 1))
    assert not math.isnan(calc.bessely(1.5, 1))
    assert math.isnan(calc.besselk(-1.0, 1))
    assert math.isnan(calc.bessely(-1.0, 1))

    # Euroconvert
    assert calc.euroconvert(100.0, "ATS", "ATS") == 100.0
    assert calc.euroconvert(100.0, "ATS", "EUR") == 7.27
    assert calc.euroconvert(100.0, "EUR", "DEM") == 195.58
    assert calc.euroconvert(100.0, "DEM", "ATS") == 703.55
    assert calc.euroconvert(100.0, "DEM", "ATS", False, 3) == 703.15
    assert abs(calc.euroconvert(100.0, "DEM", "ATS", True) - 703.55296) < 1e-3
    assert math.isnan(calc.euroconvert(100.0, "INVALID", "ATS"))

    # Complex functions
    assert calc.imcosh("1+2i") != "#VALUE!"
    assert calc.imsinh("1+2i") != "#VALUE!"
    assert calc.imtanh("1+2i") != "#VALUE!"
    assert calc.imtan("1+2i") != "#VALUE!"
    assert calc.imcot("1+2i") != "#VALUE!"
    assert calc.imcsc("1+2i") != "#VALUE!"
    assert calc.imcsch("1+2i") != "#VALUE!"
    assert calc.imsec("1+2i") != "#VALUE!"
    assert calc.imsech("1+2i") != "#VALUE!"
    assert calc.imsqrt("1+2i") != "#VALUE!"
    assert calc.imsub("1+2i", "3+4i") == "-2.0-2.0i"
    assert calc.imsum("1+2i", "3+4i") == "4.0+6.0i"


def test_isblank_isna_ifna_are_not_the_same_check():
    assert calc.isblank(None) is True
    assert calc.isblank("") is True
    assert calc.isblank("  ") is True
    assert calc.isblank("#VALUE!") is False
    assert calc.isblank("#N/A") is False
    assert calc.isblank(float("nan")) is False
    assert calc.isna("#N/A") is True
    assert calc.isna("#n/a") is True
    assert calc.isna(float("nan")) is True
    assert calc.isna("") is False
    assert calc.isna(None) is False
    assert calc.isna("#VALUE!") is False
    assert calc.ifna(lambda: "#VALUE!", "alt") == "#VALUE!"
    assert calc.ifna(lambda: "", "alt") == ""
    assert calc.ifna(lambda: None, "alt") is None
    assert calc.ifna(lambda: "#N/A", "alt") == "alt"
    assert calc.ifna(lambda: float("nan"), "alt") == "alt"


def test_yearfrac_basis_matches_days360_and_can_be_negative():
    import datetime as dt

    start, end = 44927, 45292
    assert calc.yearfrac(start, end, 0) == calc.days360(start, end, False) / 360.0
    assert calc.yearfrac(start, end, 4) == calc.days360(start, end, True) / 360.0
    sd = dt.date.fromordinal(int(start) + 693594)
    ed = dt.date.fromordinal(int(end) + 693594)
    actual = (ed - sd).days
    assert calc.yearfrac(start, end, 2) == actual / 360.0
    assert calc.yearfrac(start, end, 3) == actual / 365.0
    assert calc.yearfrac(end, start, 0) == -calc.yearfrac(start, end, 0)
    from plugin.scripting.venv.calc_functions_a_c import _year_frac

    assert _year_frac(float(start), float(end), 0) == calc.yearfrac(start, end, 0)


def test_avedev_ignores_text_and_logicals():
    # Mean of 1,2,3 is 2; mean absolute deviation is 2/3. Text and TRUE are ignored.
    assert calc.avedev([1.0, 2.0, 3.0]) == 2.0 / 3.0
    assert calc.avedev([1.0, "x", "", 2.0, True, 3.0]) == 2.0 / 3.0
    assert math.isnan(calc.avedev(["x", True, ""]))


def test_address_bad_input_returns_value_error():
    assert calc.address(1, 1) == "$A$1"
    assert calc.address("x", 1) == "#VALUE!"
    assert calc.address(1, float("inf")) == "#VALUE!"
    assert calc.address(float("nan"), 1) == "#VALUE!"
    assert calc.address(1, 1, float("inf")) == "#VALUE!"


def test_averageifs_and_countifs_reject_odd_predicates():
    assert calc.averageifs([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == "#VALUE!"
    assert calc.countifs([1.0, 2.0], ">0", [1.0, 2.0]) == "#VALUE!"
    assert calc.countifs([1.0, 2.0, 3.0], ">1") == 2.0
    assert calc.averageifs([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], ">1") == 2.5


def test_bit_char_choose_combin_inf_does_not_raise():
    assert math.isnan(calc.bitand(float("inf"), 1))
    assert math.isnan(calc.bitlshift(1, float("inf")))
    assert math.isnan(calc.bitor(1, float("inf")))
    assert math.isnan(calc.bitrshift(float("inf"), 1))
    assert math.isnan(calc.bitxor(float("inf"), 1))
    assert calc.bitand(5, 3) == 1.0
    assert calc.char(float("inf")) == "#VALUE!"
    assert calc.char(256) == "#VALUE!"
    assert calc.char("nope") == "#VALUE!"
    assert calc.char(65) == "A"
    assert calc.choose(float("inf"), "a", "b") is None
    assert calc.choose(2, "a", "b") == "b"
    assert math.isnan(calc.combin(float("inf"), 2))
    assert math.isnan(calc.combina(5, float("inf")))
    assert calc.combin(5, 2) == 10.0


def test_aggregate_error_options_and_text():
    data = [1.0, 2.0, float("nan"), 3.0]
    # Ignore-error options are 2, 3, 6, 7 (Microsoft AGGREGATE). Sum is 6.
    for opt in (2, 3, 6, 7):
        assert calc.aggregate(9, opt, data) == 6.0
    # Hidden-row-only options 1 and 5 do not ignore errors.
    for opt in (1, 4, 5):
        assert math.isnan(calc.aggregate(9, opt, data))
    # Text must not fail the whole call. SUM ignores it; COUNTA counts it.
    assert calc.aggregate(9, 4, [1.0, "x", "", 3.0]) == 4.0
    assert calc.aggregate(3, 4, [1.0, "x", "", 3.0]) == 3.0
    assert calc.aggregate(3, 6, [1.0, "x", float("nan")]) == 2.0
    assert calc.aggregate(3, 4, [1.0, "x", float("nan")]) == 3.0
    assert calc.aggregate(9, 4, [1.0, 2.0, 3.0]) == 6.0


def test_base_returns_num_error_token():
    assert calc.base(255, 16) == "FF"
    assert calc.base(-1, 10) == "#NUM!"
    assert calc.base(10, 1) == "#NUM!"
    assert calc.base("x", 10) == "#NUM!"
    assert calc.base(float("inf"), 10) == "#NUM!"
    assert calc.base(15, 16, 4) == "000F"


def _excel_serial(year: int, month: int, day: int) -> float:
    import datetime as dt

    return float(dt.date(year, month, day).toordinal() - 693594)


def test_datedif_units_match_libreoffice_and_do_not_raise():
    # YD used to call datetime.date(end.year, start.month, start.day) outside
    # the try. Feb 29 into a non-leap end year raised ValueError.
    start = _excel_serial(2020, 2, 29)
    end = _excel_serial(2021, 3, 1)
    assert calc.datedif(start, end, "YD") == 0.0
    # Anniversary after the end date in that year counts across the boundary.
    assert calc.datedif(_excel_serial(2020, 12, 31), _excel_serial(2021, 1, 15), "YD") == 15.0

    # M drops the incomplete month (day-of-month not yet reached).
    assert calc.datedif(_excel_serial(2020, 1, 31), _excel_serial(2020, 2, 28), "M") == 0.0
    assert calc.datedif(_excel_serial(2020, 1, 15), _excel_serial(2020, 2, 15), "M") == 1.0

    # YM wraps across the year instead of going negative.
    assert calc.datedif(_excel_serial(2020, 3, 15), _excel_serial(2021, 2, 10), "YM") == 10.0
    assert calc.datedif(_excel_serial(2020, 12, 31), _excel_serial(2021, 1, 15), "YM") == 0.0

    # MD borrows the previous month (ScGetDateDif). Naive day subtraction
    # was negative whenever the end day was smaller.
    assert calc.datedif(_excel_serial(2012, 1, 28), _excel_serial(2012, 3, 1), "MD") == 2.0
    assert calc.datedif(_excel_serial(2011, 1, 29), _excel_serial(2011, 3, 1), "MD") == 0.0
    assert calc.datedif(_excel_serial(2023, 1, 15), _excel_serial(2023, 3, 10), "MD") == 23.0
    assert calc.datedif(_excel_serial(2021, 1, 31), _excel_serial(2021, 2, 28), "MD") == 28.0
    # Day 31 rolled through a short February still matches LO (can be negative).
    assert calc.datedif(_excel_serial(2021, 1, 31), _excel_serial(2021, 3, 1), "MD") == -2.0

    assert calc.datedif(start, end, "D") == (_excel_serial(2021, 3, 1) - start)
    assert math.isnan(calc.datedif("bad", end, "D"))
    assert math.isnan(calc.datedif(end, start, "D"))


def test_even_rounds_away_from_zero_and_rejects_text():
    assert calc.even(2.5) == 4.0
    assert calc.even(-2.5) == -4.0
    assert calc.even(2) == 2.0
    assert calc.even(-2) == -2.0
    assert calc.even(3.0) == 4.0
    assert calc.even(-3) == -4.0
    assert calc.even(0.1) == 2.0
    assert calc.even(-0.1) == -2.0
    assert math.isnan(calc.even("x"))
    assert math.isnan(calc.even(float("nan")))


def test_filter_shape_mismatch_is_value_error():
    assert calc.filter([1.0, 2.0, 3.0, 4.0, 5.0], [True, False, True, False, True]) == [1.0, 3.0, 5.0]
    # Shorter include used to IndexError on the boolean index.
    assert calc.filter([1.0, 2.0, 3.0], [True, False]) == "#VALUE!"
    # Column-shaped include against a wider range is the same IndexError.
    assert calc.filter([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]], [[True], [False], [True]]) == "#VALUE!"


def test_numeric_coercions_return_nan_not_raise():
    assert math.isnan(calc.forecast("x", [1.0, 2.0], [1.0, 2.0]))
    assert math.isnan(calc.forecast(6, ["a", "b"], [1.0, 2.0]))
    assert calc.forecast(6, [1.0, 2.0, 3.0, 4.0, 5.0], [1.0, 2.0, 3.0, 4.0, 5.0]) == 6.0

    assert math.isnan(calc.fv("bad", 12, -100))
    assert abs(calc.fv(0.05 / 12, 60, -200, -10000) - 26434.80) < 1.0

    assert math.isnan(calc.geomean(["a", "b"]))
    assert abs(calc.geomean([4.0, 9.0]) - 6.0) < 1e-9
    assert math.isnan(calc.harmean(["a", "b"]))
    assert abs(calc.harmean([1.0, 4.0]) - 1.6) < 1e-9


def test_large_text_cells_and_bad_k_return_nan():
    # float() on a text cell used to raise ValueError out of LARGE.
    assert math.isnan(calc.large(["a", "b"], 1))
    assert calc.large(["a", 5.0, "b", 1.0], 1) == 5.0
    assert calc.large([1.0, 5.0, 3.0, 4.0, 2.0], 2) == 4.0
    assert math.isnan(calc.large([1.0, 2.0], "k"))


def test_complex_overflow_returns_value_error():
    # cmath / complex ** raise OverflowError, which the helpers did not catch.
    assert calc.imexp("1000") == "#VALUE!"
    assert calc.imsinh("1000") == "#VALUE!"
    assert calc.imcosh("1000") == "#VALUE!"
    assert calc.imsin("1000i") == "#VALUE!"
    assert calc.imcos("1000i") == "#VALUE!"
    # Secant/cosecant call the same cmath functions, then take a reciprocal.
    # OverflowError used to escape before the division.
    assert calc.imcsc("1000i") == "#VALUE!"
    assert calc.imcsch("1000") == "#VALUE!"
    assert calc.imsec("1000i") == "#VALUE!"
    assert calc.imsech("1000") == "#VALUE!"
    assert calc.impower("2", 10000) == "#VALUE!"
    assert calc.imexp("0") == "1.0"
    assert calc.imsin("0") == "0.0"
    assert calc.imcsc("1") != "#VALUE!"
    assert calc.imsec("1") != "#VALUE!"


def test_lookup_short_result_vector_is_na():
    # Index 2 into a length-2 result vector used to raise IndexError.
    assert calc.lookup(3, [1, 2, 3], [10, 20]) == "#N/A"
    assert calc.lookup(2, [1, 2, 3], [10, 20]) == 20
    assert calc.lookup(0, [1, 2, 3], [10, 20]) is None


def test_ipmt_beginning_of_period_matches_calc():
    # GetIpmt pay-in-advance: period 1 is 0; later periods use FV(per-2, advance).
    # The old formula agreed through per=2 and drifted from per=3.
    assert calc.ipmt(0.1, 1, 3, 8000, 0, 1) == 0.0
    assert math.isclose(calc.ipmt(0.1, 2, 3, 8000, 0, 1), -507.5528700906347)
    assert math.isclose(calc.ipmt(0.1, 3, 3, 8000, 0, 1), -265.86102719033266)
    # End-of-period first interest is still -pv*rate.
    assert math.isclose(calc.ipmt(0.1, 1, 3, 8000, 0, 0), -800.0)


def test_numpy_financial_annuity_keeps_excel_errors():
    # numpy-financial ipmt returns 0 when per > nper; Excel/Calc are #NUM!.
    assert math.isnan(calc.ipmt(0.1, 4, 3, 8000))
    # Fractional per is truncated, not interpolated.
    assert math.isclose(calc.ipmt(0.1, 1.9, 3, 8000), calc.ipmt(0.1, 1, 3, 8000))
    # No payment, or a payment that never amortizes, is #NUM! rather than ±inf.
    assert math.isnan(calc.nper(0, 0, 1000))
    assert math.isnan(calc.nper(0.01, 0, 1000))
    # nper == 0 used to raise ZeroDivisionError. numpy-financial returns ±inf.
    assert math.isnan(calc.pmt(0.1, 0, 1000))
    assert math.isnan(calc.pmt("x", 12, 1000))


def test_irr_honors_guess_when_two_real_roots():
    # numpy-financial 1.1 ignores guess and returns ~0.089 for both starts.
    values = [-5, 10.5, 1, -8, 1]
    low = calc.irr(values, 0.1)
    high = calc.irr(values, 0.5)
    assert math.isclose(low, 0.08859833852, rel_tol=1e-6)
    assert math.isclose(high, 0.70955952768, rel_tol=1e-6)


def test_countif_numeric_operator_does_not_count_text():
    # "abc" > "5" is lexicographic True; numeric ">" must not count text.
    assert calc.countif(["abc"], ">5") == 0.0
    assert calc.countif(["abc", 6, 4], ">5") == 1.0
    assert calc.countif(["abc"], "<>5") == 1.0
    assert calc.countif(["zzz"], ">aaa") == 1.0


def test_mround_halves_away_from_zero():
    assert calc.mround(2.5, 1) == 3.0
    assert calc.mround(-2.5, -1) == -3.0
    assert calc.mround(1.5, 1) == 2.0
    assert calc.mround(1.23, 0.5) == 1.0


def test_mround_zero_multiple_is_nan():
    # Excel/Calc MROUND(n, 0) is #DIV/0!. The helper used to return 0.0.
    assert math.isnan(calc.mround(10, 0))
    assert math.isnan(calc.mround(0, 0))
    assert math.isnan(calc.mround(-4, 0.0))
    assert calc.mround(10, 2) == 10.0


def test_logest_nonpositive_y_is_value_error():
    # np.log(y<=0) is -inf/nan and used to fit garbage coefficients.
    assert calc.logest([1, 0, 4]) == "#VALUE!"
    assert calc.logest([1, -2, 4]) == "#VALUE!"
    assert calc.logest([0]) == "#VALUE!"
    assert calc.logest([[1.0], [0.0], [4.0]]) == "#VALUE!"
    assert calc.logest(["1", "2", "4"]) == "#VALUE!"
    coeffs = calc.logest([1, 2, 4, 8, 16])
    assert math.isclose(coeffs[0], 2.0)
    assert math.isclose(coeffs[1], 0.5)


def test_mode_na_without_duplicates_and_lowest_tie():
    assert math.isnan(calc.mode([1, 2, 3]))
    assert calc.isna(calc.mode([1, 2, 3])) is True
    assert calc.mode([2, 2, 1, 1]) == 1
    assert calc.mode([1, 1, 2]) == 1


def test_imlog2_zero_is_value_error():
    # cmath.log(0, 2) is (-inf+nanj), which used to stringify as '-infnani'.
    assert calc.imlog2("0") == "#VALUE!"
    assert calc.imln("0") == "#VALUE!"
    assert calc.imlog10("0") == "#VALUE!"
    assert calc.imlog2("8") == "3.0"


def _calc_serial(year: int, month: int, day: int) -> int:
    import datetime as dt

    return dt.date(year, month, day).toordinal() - 693594


def test_text_numeric_formats_round_half_away_and_keep_separators():
    assert calc.text(1234.5, "0.00") == "1234.50"
    assert calc.text(1234.5, "0") == "1235"
    assert calc.text(1234.5, "#,##0") == "1,235"
    from plugin.scripting.venv.calc_functions_t_z import fmt

    assert fmt(1234.5, "0.00") == "1234.50"
    assert calc.text(1.5, "0") == "2"
    assert calc.text(2.5, "0") == "3"
    assert calc.text(-1.5, "0") == "-2"
    assert calc.text(-1234.5, "0.00") == "-1234.50"
    assert calc.text(-1234.5, "#,##0") == "-1,235"
    assert calc.text(0.4, "0") == "0"
    assert calc.text(-0.4, "0") == "0"
    assert calc.text(1234.567, "0.00") == "1234.57"
    assert calc.text(999.5, "#,##0") == "1,000"
    assert calc.text("abc", "0") == "abc"
    # Non-numeric specs are unchanged. 45292 is 2024-01-01 on the Calc epoch.
    assert calc.text(45292, "MMMM") == "January"


def test_type_errors_are_16_and_hash_text_is_text():
    assert calc.type(1) == 1.0
    assert calc.type(True) == 4.0
    assert calc.type("hello") == 2.0
    assert calc.type("") == 2.0
    assert calc.type("#hashtag") == 2.0
    assert calc.type(None) == 1.0
    for token in ("#N/A", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#NULL!"):
        assert calc.type(token) == 16.0
    assert calc.type(float("nan")) == 16.0
    assert calc.type([1.0, 2.0]) == 64.0


def test_time_wraps_modulo_one_day_and_rejects_negative_remainder():
    assert calc.time(24, 0, 0) == 0.0
    assert calc.time(48, 0, 0) == 0.0
    assert calc.time(0, 0, 0) == 0.0
    assert math.isclose(calc.time(25, 0, 0), 1.0 / 24.0)
    assert math.isclose(calc.time(12, 0, 0), 0.5)
    assert math.isclose(calc.time(1, -30, 0), 0.5 / 24.0)
    assert math.isclose(calc.time(0, 90, 0), 1.5 / 24.0)
    assert calc.time(23, 59, 60) == 0.0
    assert math.isclose(calc.time(-1, 120, 0), 1.0 / 24.0)
    assert calc.time(-0.5, 30, 0) == 0.0
    assert math.isclose(calc.time(1.9, 0, 0), 1.9 / 24.0)
    assert math.isnan(calc.time(-1, 0, 0))
    assert math.isnan(calc.time(0, -1, 0))
    assert math.isnan(calc.time(0, 0, -0.5))
    assert math.isnan(calc.time("x", 0, 0))


def test_trimmean_text_or_bad_percent_is_nan():
    assert math.isnan(calc.trimmean(["a", 1.0, 2.0], 0.2))
    assert math.isnan(calc.trimmean([1.0, "#VALUE!", 3.0], 0.2))
    assert math.isnan(calc.trimmean([1.0, 2.0, 3.0], "bad"))
    assert calc.trimmean([1.0, 2.0, 3.0, 4.0, 5.0], 0.4) == 3.0


def test_weekday_return_types_and_weeknum_modes():
    sunday = _calc_serial(2023, 1, 1)
    monday = _calc_serial(2023, 1, 2)
    assert calc.weekday(sunday, 1) == 1.0
    assert calc.weekday(sunday, 2) == 7.0
    assert calc.weekday(sunday, 3) == 6.0
    assert calc.weekday(monday, 3) == 0.0
    assert calc.weekday(sunday, 11) == 7.0
    assert calc.weekday(sunday, 12) == 6.0
    assert calc.weekday(sunday, 13) == 5.0
    assert calc.weekday(sunday, 17) == 1.0
    assert calc.weekday(_calc_serial(2023, 1, 7), 16) == 1.0
    assert math.isnan(calc.weekday(sunday, 4))

    jan1_1995 = _calc_serial(1995, 1, 1)
    assert calc.weeknum(jan1_1995, 1) == 1.0
    assert calc.weeknum(jan1_1995, 2) == 1.0
    assert calc.weeknum(jan1_1995, 21) == 52.0
    assert calc.weeknum(jan1_1995, 150) == calc.isoweeknum(jan1_1995)
    assert calc.weeknum(_calc_serial(1999, 1, 1), 21) == 53.0
    dec_2023 = _calc_serial(2023, 12, 31)
    assert calc.weeknum(dec_2023, 1) == 1.0
    assert calc.weeknum(dec_2023, 2) == 53.0
    assert calc.weeknum(dec_2023, 21) == 52.0
    dec_2016 = _calc_serial(2016, 12, 31)
    assert calc.weeknum(dec_2016, 1) == 53.0
    assert calc.weeknum(dec_2016, 2) == 1.0
    # 2024-01-07 is a Sunday. Sunday-start (1) is week 2; Monday-start (2) is week 1.
    # Tuesday-start (12) is also week 2: Jan 1 (Monday) still falls in that week.
    jan7_2024 = _calc_serial(2024, 1, 7)
    assert calc.weeknum(jan7_2024, 1) == 2.0
    assert calc.weeknum(jan7_2024, 2) == 1.0
    assert calc.weeknum(jan7_2024, 12) == 2.0
    assert math.isnan(calc.weeknum(jan1_1995, 3))


def test_unique_by_col_and_exactly_once():
    assert calc.unique([1.0, 2.0, 1.0, 3.0, 2.0]) == [1.0, 2.0, 3.0]
    assert calc.unique([1.0, 2.0, 1.0, 3.0, 2.0], unique_only=True) == [3.0]
    # One row is one row. The old falsy by_col path flattened the cells.
    assert calc.unique([[1, 2, 1, 3]], by_col=False) == [[1, 2, 1, 3]]
    assert calc.unique([[1, 2], [3, 4], [1, 2], [5, 6]]) == [[1, 2], [3, 4], [5, 6]]
    assert calc.unique([[1, 2], [3, 4], [1, 2], [5, 6]], unique_only=True) == [[3, 4], [5, 6]]
    assert calc.unique([[1], [1], [2]]) == [[1], [2]]
    columns = [[1, 3, 1, 5], [2, 4, 2, 6]]
    assert calc.unique(columns, by_col=True) == [[1, 3, 5], [2, 4, 6]]
    assert calc.unique(columns, by_col=True, unique_only=True) == [[3, 5], [4, 6]]
    assert calc.unique(columns, 1, 1) == [[3, 5], [4, 6]]


def test_xmatch_wildcards_and_xlookup_horizontal_scalar():
    names = ["apple", "pear", "apricot", "banana"]
    assert calc.xmatch("ap*", names, 2) == 1.0
    assert calc.xmatch("a?ple", names, 2) == 1.0
    assert calc.xmatch("a*", names, 2, -1) == 3.0
    assert math.isnan(calc.xmatch("z*", names, 2))
    # Same wildcard helper as xlookup, including its case-sensitive match.
    assert calc.xlookup("ap*", names, [10, 20, 30, 40], "missing", 2) == 10
    assert math.isnan(calc.xmatch("Ap*", names, 2))
    assert calc.xlookup("Ap*", names, [10, 20, 30, 40], "missing", 2) == "missing"
    assert calc.xlookup("b", [["a", "b", "c"]], [[10, 20, 30]]) == 20
    assert calc.xlookup("b", [["a", "b", "c"]], [[10, 20, 30], [40, 50, 60]]) == [20, 50]
    assert calc.xlookup("b", [["a"], ["b"], ["c"]], [[10], [20], [30]]) == 20
    assert calc.xmatch("b", ["a", "b", "c"]) == 2.0


def test_xmatch_and_xlookup_approximate_modes():
    nums = [10, 20, 30, 40]
    vals = [100, 200, 300, 400]
    # Exact match
    assert calc.xmatch(20, nums) == 2.0
    assert calc.xlookup(20, nums, vals) == 200

    # match_mode -1: exact or next smaller
    assert calc.xmatch(25, nums, -1) == 2.0
    assert calc.xlookup(25, nums, vals, match_mode=-1) == 200
    assert math.isnan(calc.xmatch(5, nums, -1))
    assert calc.xlookup(5, nums, vals, if_not_found="none", match_mode=-1) == "none"

    # match_mode 1: exact or next larger
    assert calc.xmatch(25, nums, 1) == 3.0
    assert calc.xlookup(25, nums, vals, match_mode=1) == 300
    assert math.isnan(calc.xmatch(45, nums, 1))
    assert calc.xlookup(45, nums, vals, if_not_found="none", match_mode=1) == "none"

    # search_mode -1 (reverse search)
    dup_nums = [10, 20, 20, 30]
    dup_vals = [1, 2, 3, 4]
    assert calc.xmatch(20, dup_nums, 0, 1) == 2.0
    assert calc.xmatch(20, dup_nums, 0, -1) == 3.0
    assert calc.xlookup(20, dup_nums, dup_vals, search_mode=1) == 2
    assert calc.xlookup(20, dup_nums, dup_vals, search_mode=-1) == 3



def test_xor_flattens_ranges_and_does_not_crash_on_arrays():
    import numpy as np

    assert calc.xor(True, False) is True
    assert calc.xor(True, True) is False
    assert calc.xor(1, 0, 1) is False
    assert calc.xor(-1, 0) is True
    assert calc.xor([True, False, True]) is False
    assert calc.xor([1, "x", 0]) is True
    assert calc.xor([1, "", 0]) is True
    assert calc.xor(1, "a", 0) == "#VALUE!"
    assert calc.xor("") == "#VALUE!"
    assert calc.xor([1, "#DIV/0!", 0]) == "#DIV/0!"
    assert math.isnan(calc.xor([1, float("nan"), 0]))
    assert calc.xor(np.array([1, 0, 1])) is False
    assert calc.xor(np.array([[True, False], [True, False]])) is False
    assert calc.xor() == "#VALUE!"


def test_yield_stubs_stay_nan():
    assert math.isnan(calc.yield_calc(1, 2, 0.05, 95, 100, 2))
    assert math.isnan(calc.yielddisc(1, 2, 95, 100))
    assert math.isnan(calc.yieldmat(1, 2, 0, 0.05, 95))


def test_sort_by_col_uses_row_key_and_keeps_shape():
    data = [[3, 1, 2], [6, 5, 4]]
    # sort_index 1 is the first row [3, 1, 2]: columns reorder to 1, 2, 0.
    assert calc.sort(data, 1, 1, True) == [[1, 2, 3], [5, 4, 6]]
    # sort_index 2 is the second row [6, 5, 4].
    assert calc.sort(data, 2, 1, True) == [[2, 1, 3], [4, 5, 6]]
    assert calc.sort(data, 1, -1, True) == [[3, 2, 1], [6, 4, 5]]
    # Row sort (by_col false) still reorders rows and keeps the 2-d shape.
    rows = [[3, 9], [1, 8], [2, 7]]
    assert calc.sort(rows, 1, 1, False) == [[1, 8], [2, 7], [3, 9]]


def test_sumproduct_unequal_shape_is_nan():
    assert math.isnan(calc.sumproduct([1.0, 2.0], [3.0, 4.0, 5.0]))
    assert math.isnan(calc.sumproduct([[1.0, 2.0, 3.0]], [[1.0], [2.0], [3.0]]))
    assert calc.sumproduct([[1.0, 2.0], [3.0, 4.0]], [[1.0, 1.0], [1.0, 1.0]]) == 10.0


def test_sortby_secondary_keys_and_short_key():
    rows = [[1], [2], [3], [4]]
    by1 = [2, 1, 2, 1]
    by2 = [20, 20, 10, 10]
    # Ascending by1, then by2 breaks the ties the other way from a single-key sort.
    assert calc.sortby(rows, by1, 1, by2, 1) == [[4], [2], [3], [1]]
    # Omitted sort_order: the next array is by_array2, not a direction.
    assert calc.sortby(rows, by1, by2) == [[4], [2], [3], [1]]
    assert calc.sortby([3, 1, 2], [2, 1, 3]) == [1, 3, 2]
    assert calc.sortby([1, 2, 3], [1, 2, 3], -1) == [3, 2, 1]
    assert math.isnan(calc.sortby([[1], [2], [3]], [10, 20]))
    assert math.isnan(calc.sortby([3, 1, 2], [2, 1]))
    assert math.isnan(calc.sortby([3, 1], [1, 2, 3]))
    assert math.isnan(calc.sortby([[1], [2], [3]], [3, 1, 2], 1, [1, 0]))


def test_quartile_rejects_bad_quart():
    data = [1.0, 2.0, 3.0, 4.0]
    assert math.isnan(calc.quartile(data, 5))
    assert math.isnan(calc.quartile(data, -1))
    assert math.isnan(calc.quartile(data, "x"))
    assert calc.quartile(data, 0) == 1.0
    assert calc.quartile(data, 4) == 4.0
    assert calc.quartile(data, 2) == 2.5
    # Excel truncates a non-integer quart before the 0..4 check.
    assert calc.quartile(data, 1.9) == calc.quartile(data, 1)


def test_regex_invalid_pattern_is_nan():
    assert math.isnan(calc.regex("abc", "["))
    assert math.isnan(calc.regex("ab", "(a)", r"\2"))
    assert calc.regex("abc", "b") == "b"


def test_n_s_text_cells_return_nan():
    assert math.isnan(calc.npv("bad", [100.0, 200.0]))
    assert math.isnan(calc.npv(-1, [100.0, 200.0]))
    assert abs(calc.npv(0.1, 100.0, 200.0) - (100.0 / 1.1 + 200.0 / 1.21)) < 1e-9
    assert math.isnan(calc.pmt("x", 12, 1000))
    assert math.isnan(calc.pv("x", 12, -100))
    assert math.isnan(calc.odd("x"))
    assert calc.odd(2) == 3.0
    assert calc.odd(-2) == -3.0
    assert math.isnan(calc.rank(1, [1, "a", 3]))
    assert calc.rank(2, [1, 2, 3]) == 2.0
    assert calc.rank(1, [1, "", 2]) == 2.0
    assert math.isnan(calc.small([1, "a", 3], 1))
    assert math.isnan(calc.small([1.0, 5.0, 3.0], "k"))
    assert calc.small([1.0, 5.0, 3.0], 1) == 1.0
    assert math.isnan(calc.rsq([1.0, "a"], [1.0, 2.0]))
    assert abs(calc.rsq([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) - 1.0) < 1e-9
    assert math.isnan(calc.slope([1.0, "a"], [1.0, 2.0]))
    assert abs(calc.slope([2.0, 4.0, 6.0], [1.0, 2.0, 3.0]) - 2.0) < 1e-9
    assert math.isnan(calc.steyx([1.0, "a", 3.0], [1.0, 2.0, 3.0]))
    assert abs(calc.steyx([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])) < 1e-9


def test_sumifs_unpaired_criteria_is_nan():
    assert math.isnan(calc.sumifs([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]))
    assert math.isnan(calc.sumifs([1.0, 2.0], [1.0, 2.0], ">0", [1.0, 2.0]))
    assert calc.sumifs([1.0, 2.0], [1.0, 2.0], ">1") == 2.0


def test_irr_subtotal_workday_iseven_bad_input_does_not_raise():
    assert math.isnan(calc.irr(["a", "b"]))
    assert math.isnan(calc.irr([-100.0, 110.0], guess="nope"))
    assert math.isnan(calc.subtotal("nope", [1.0, 2.0, 3.0]))
    assert calc.subtotal(9, [1.0, 2.0, 3.0]) == 6.0
    assert math.isnan(calc.workday(46181, "nope"))
    assert calc.workday(46181, 1) == 46182.0
    assert math.isnan(calc.workday_intl(46181, "nope", 1))
    assert math.isnan(calc.workday_intl(46181, 1, None))
    assert calc.iseven(float("inf")) is False
    assert calc.isodd(1e309) is False
    assert calc.iseven(4) is True
    assert calc.isodd(3) is True


def test_networkdays_intl_invalid_weekend_is_nan():
    assert math.isnan(calc.networkdays_intl(46181, 46185, 8))
    assert math.isnan(calc.networkdays_intl(46181, 46185, 0))
    assert math.isnan(calc.networkdays_intl(46181, 46185, 18))
    assert calc.networkdays_intl(46181, 46185, 1) == 5.0
    assert calc.networkdays_intl(46181, 46185, 17) == 5.0
    assert calc.networkdays_intl(46181, 46185, "0000011") == 5.0


def test_workday_intl_invalid_weekend_is_nan():
    assert math.isnan(calc.workday_intl(46181, 1, 8))
    assert math.isnan(calc.workday_intl(46181, 1, 0))
    assert math.isnan(calc.workday_intl(46181, 1, 18))
    assert calc.workday_intl(46181, 1, 1) == calc.workday(46181, 1)
    assert calc.workday_intl(46181, 1, "0000011") == calc.workday(46181, 1)


def test_coupon_nonpositive_frequency_is_nan():
    assert math.isnan(calc.coupdays(43831, 43983, 0))
    assert math.isnan(calc.coupdays(43831, 43983, -1))
    assert math.isnan(calc.coupdaybs(43831, 43983, -2))
    assert math.isnan(calc.coupdaysnc(43831, 43983, -1))
    assert math.isnan(calc.coupncd(43831, 43983, 0))
    assert math.isnan(calc.couppcd(43831, 43983, -4))
    assert not math.isnan(calc.coupdaybs(43831, 43983, 2))


def test_edate_eomonth_text_months_are_nan():
    assert math.isnan(calc.edate(46182, "x"))
    assert math.isnan(calc.edate(46182, ""))
    assert math.isnan(calc.edate(46182, None))
    assert math.isnan(calc.eomonth(46182, "x"))
    assert math.isnan(calc.eomonth(46182, ""))
    assert math.isnan(calc.eomonth(46182, None))
    assert calc.eomonth(46182, 1) == 46234.0
    assert not math.isnan(calc.edate(46182, 1))


def test_ispmt_zero_nper_and_mround_sort_text_are_nan():
    assert math.isnan(calc.ispmt(0.1, 1, 0, 1000))
    assert not math.isnan(calc.ispmt(0.1, 1, 12, 1000))
    assert math.isnan(calc.mround("x", 1))
    assert math.isnan(calc.mround(2.5, ""))
    assert math.isnan(calc.mround(None, 1))
    assert calc.mround(2.5, 1) == 3.0
    assert math.isnan(calc.sort([[3, 1], [2, 4]], "a", 1))
    assert math.isnan(calc.sort([[3, 1], [2, 4]], 1, ""))
    assert math.isnan(calc.sort([[3, 1], [2, 4]], None, 1))
    assert calc.sort([3.0, 1.0, 2.0], 1, -1) == [3.0, 2.0, 1.0]


def test_rept_negative_count_is_nan():
    assert math.isnan(calc.rept("ab", -1))
    assert math.isnan(calc.rept("ab", -1.2))
    assert calc.rept("ab", -0.1) == ""
    assert calc.rept("ab", 0) == ""
    assert calc.rept("ab", 3) == "ababab"


def test_odd_price_zero_frequency_is_nan():
    assert math.isnan(calc.oddfprice(40000, 41000, 39900, 40100, 0.05, 0.06, 100, 0))
    assert math.isnan(calc.oddlprice(40000, 41000, 39000, 0.05, 0.06, 100, 0))
    assert not math.isnan(calc.oddfprice(40000, 41000, 39900, 40100, 0.05, 0.06, 100, 2))
    assert not math.isnan(calc.oddlprice(40000, 41000, 39000, 0.05, 0.06, 100, 2))


def test_kurt_and_skew():
    data = [1.0, 2.0, 4.0, 7.0, 11.0, 16.0]
    k = calc.kurt(data)
    s = calc.skew(data)
    assert not math.isnan(k)
    assert not math.isnan(s)
    # Check text and booleans are ignored
    data_with_noise = [1.0, "ignore", True, 2.0, 4.0, 7.0, 11.0, 16.0]
    assert abs(calc.kurt(data_with_noise) - k) < 1e-12
    assert abs(calc.skew(data_with_noise) - s) < 1e-12

    # Insufficient elements or zero variance
    assert math.isnan(calc.kurt([1.0, 2.0, 3.0]))
    assert math.isnan(calc.kurt([5.0, 5.0, 5.0, 5.0]))
    assert math.isnan(calc.skew([1.0, 2.0]))
    assert math.isnan(calc.skew([5.0, 5.0, 5.0]))


def test_devsq_geomean_harmean():
    data = [2.0, 4.0, 8.0]
    # mean=4.6666667, devsq = (2-14/3)^2 + (4-14/3)^2 + (8-14/3)^2 = 64/9 + 4/9 + 100/9 = 168/9 = 18.6666667
    assert abs(calc.devsq(data) - 18.666666666666668) < 1e-12
    assert abs(calc.devsq(2.0, 4.0, "text", 8.0) - 18.666666666666668) < 1e-12
    assert math.isnan(calc.devsq([]))

    # geomean: (2*4*8)^(1/3) = 64^(1/3) = 4.0
    assert abs(calc.geomean(data) - 4.0) < 1e-12
    assert abs(calc.geomean(2.0, 4.0, 8.0) - 4.0) < 1e-12
    # Non-positive or empty returns nan
    assert math.isnan(calc.geomean([2.0, 0.0, 8.0]))
    assert math.isnan(calc.geomean([2.0, -4.0, 8.0]))
    assert math.isnan(calc.geomean([]))

    # harmean: 3 / (1/2 + 1/4 + 1/8) = 3 / (7/8) = 24/7 = 3.4285714...
    assert abs(calc.harmean(data) - (24.0 / 7.0)) < 1e-12
    assert abs(calc.harmean(2.0, 4.0, 8.0) - (24.0 / 7.0)) < 1e-12
    assert math.isnan(calc.harmean([2.0, 0.0, 8.0]))
    assert math.isnan(calc.harmean([2.0, -4.0, 8.0]))
    assert math.isnan(calc.harmean([]))


