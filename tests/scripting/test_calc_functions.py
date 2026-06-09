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


def test_helper_names_complete():
    from plugin.scripting.calc_functions_common import HELPER_NAMES

    exported = {name for name in dir(xl) if not name.startswith("_") and callable(getattr(xl, name))}
    assert HELPER_NAMES <= exported
