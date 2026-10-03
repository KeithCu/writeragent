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

    assert not math.isnan(calc.cumipmt(0.05/12, 60, 100000, 1, 12, 0))
    assert not math.isnan(calc.cumprinc(0.05/12, 60, 100000, 1, 12, 0))

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

