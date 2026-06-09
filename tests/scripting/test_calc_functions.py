# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc formula parity helpers (plugin.scripting.calc_functions / xl)."""

from __future__ import annotations
import math

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

def test_bahttext():
    assert "Baht" in xl.bahttext(123)

def test_clean():
    assert xl.clean("A" + chr(7) + "B" + chr(10)) == "AB"
    assert isinstance(xl.clean(float("nan")), float) and math.isnan(xl.clean(float("nan")))

def test_dollar():
    assert xl.dollar(1234.567) == "$1,234.57"
    assert xl.dollar(1234.567, 1) == "$1,234.6"

def test_encodeurl():
    assert xl.encodeurl("http://example.com") == "http%3A%2F%2Fexample.com"

def test_fixed():
    assert xl.fixed(1234.567) == "1,234.57"
    assert xl.fixed(1234.567, 1, True) == "1234.6"

def test_jis():
    assert xl.jis("test") == "test"

def test_numbervalue():
    assert xl.numbervalue("1,234.56") == 1234.56
    assert xl.numbervalue("1.234,56", ",", ".") == 1234.56

def test_t():
    assert xl.t("test") == "test"
    assert xl.t(123) == ""

def test_textafter():
    assert xl.textafter("a-b-c", "-") == "b-c"
    assert xl.textafter("a-b-c", "-", 2) == "c"

def test_textbefore():
    assert xl.textbefore("a-b-c", "-") == "a"
    assert xl.textbefore("a-b-c", "-", 2) == "a-b"

def test_textsplit():
    assert xl.textsplit("a-b-c", "-") == [["a", "b", "c"]]
    assert xl.textsplit("a-b;c-d", "-", ";") == [["a", "b"], ["c", "d"]]

def test_unichar():
    assert xl.unichar(65) == "A"
    assert math.isnan(xl.unichar(-1))

def test_unicode():
    assert xl.unicode("A") == 65
    assert math.isnan(xl.unicode(""))

def test_besseli():
    import scipy.special
    assert math.isclose(xl.besseli(1.5, 1), scipy.special.iv(1, 1.5))
    assert math.isnan(xl.besseli(1.5, -1))

def test_besselj():
    import scipy.special
    assert math.isclose(xl.besselj(1.5, 1), scipy.special.jv(1, 1.5))
    assert math.isnan(xl.besselj(1.5, -1))
