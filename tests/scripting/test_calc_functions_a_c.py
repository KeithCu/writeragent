# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.venv.calc_functions_a_c."""

from __future__ import annotations

import math
import pytest

import plugin.scripting.calc_functions as calc


def test_address():
    assert calc.address(1, 1) == "$A$1"
    assert calc.address(1, 1, 1) == "$A$1"
    assert calc.address(1, 1, 2) == "A$1"
    assert calc.address(1, 1, 3) == "$A1"
    assert calc.address(1, 1, 4) == "A1"
    assert calc.address(2, 3, 4) == "C2"
    # Single quotes in sheet name are escaped as ''
    assert calc.address(1, 1, 1, True, "Sheet1") == "'Sheet1'!$A$1"
    assert calc.address(1, 1, 1, True, "Sheet's") == "'Sheet''s'!$A$1"

    # Out of bounds abs_num (must be 1..4)
    assert calc.address(1, 1, 0) == "#VALUE!"
    assert calc.address(1, 1, 5) == "#VALUE!"
    assert calc.address(0, 1, 1) == "#VALUE!"
    assert calc.address(1, 0, 1) == "#VALUE!"
    assert calc.address("invalid", 1) == "#VALUE!"


def test_aggregate_stubs_and_bools():
    # Functions 1..13 should work
    res = calc.aggregate(1, 4, [1.0, 2.0, 3.0])  # AVERAGE
    assert res == 2.0

    # Booleans in array should be ignored, not treated as 1 or 0
    res_bool = calc.aggregate(1, 4, [1.0, 2.0, True, False, 3.0])
    assert res_bool == 2.0

    # Functions 14..19 must raise NotImplementedError
    for fn in range(14, 20):
        with pytest.raises(NotImplementedError):
            calc.aggregate(fn, 4, [1, 2, 3])


def test_areas():
    assert calc.areas("A1:B2") == 1.0
    assert calc.areas([[1, 2], [3, 4]]) == 1.0
    assert calc.areas([[[1, 2]], [[3, 4]]]) == 2.0


def test_bahttext_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        calc.bahttext(123)


def test_accrint_and_accrintm():
    # Valid ACCRINT: issue=40000, first_interest=40180, settlement=40090, rate=0.1, par=1000, freq=2, basis=0
    val = calc.accrint(40000, 40180, 40090, 0.1, 1000, 2, 0)
    assert not math.isnan(val) and val > 0

    # ACCRINT validation
    assert math.isnan(calc.accrint(40000, 40180, 40090, -0.05, 1000, 2))  # rate <= 0
    assert math.isnan(calc.accrint(40000, 40180, 40090, 0.1, -1000, 2))  # par <= 0
    assert math.isnan(calc.accrint(40000, 40180, 40090, 0.1, 1000, 3))  # freq not 1, 2, 4
    assert math.isnan(calc.accrint(40000, 40180, 40090, 0.1, 1000, 2, 5))  # basis > 4
    assert math.isnan(calc.accrint(40100, 40180, 40090, 0.1, 1000, 2))  # issue >= settlement

    # Valid ACCRINTM
    val_m = calc.accrintm(40000, 40180, 0.1, 1000, 0)
    assert not math.isnan(val_m) and val_m > 0

    # ACCRINTM validation
    assert math.isnan(calc.accrintm(40000, 40180, -0.05, 1000))
    assert math.isnan(calc.accrintm(40000, 40180, 0.1, -1000))
    assert math.isnan(calc.accrintm(40180, 40000, 0.1, 1000))
    assert math.isnan(calc.accrintm(40000, 40180, 0.1, 1000, 5))


def test_amordegrc_and_amorlinc():
    # Valid AMORLINC: cost=1000, purchased=40000, first_period=40100, salvage=100, period=1, rate=0.15, basis=0
    dep_linc = calc.amorlinc(1000, 40000, 40100, 100, 1, 0.15, 0)
    assert not math.isnan(dep_linc) and dep_linc >= 0

    # AMORLINC validation
    assert math.isnan(calc.amorlinc(-1000, 40000, 40100, 100, 1, 0.15))  # cost <= 0
    assert math.isnan(calc.amorlinc(1000, 40000, 40100, -100, 1, 0.15))  # salvage < 0
    assert math.isnan(calc.amorlinc(1000, 40000, 40100, 1200, 1, 0.15))  # salvage > cost
    assert math.isnan(calc.amorlinc(1000, 40000, 40100, 100, 1, -0.15))  # rate <= 0
    assert math.isnan(calc.amorlinc(1000, 40200, 40100, 100, 1, 0.15))  # date_purchased > first_period
    assert math.isnan(calc.amorlinc(1000, 40000, 40100, 100, -1, 0.15))  # period < 0
    assert math.isnan(calc.amorlinc(1000, 40000, 40100, 100, 1, 0.15, 5))  # basis > 4

    # Valid AMORDEGRC
    dep_degrc = calc.amordegrc(1000, 40000, 40100, 100, 1, 0.15, 0)
    assert not math.isnan(dep_degrc) and dep_degrc >= 0

    # AMORDEGRC validation
    assert math.isnan(calc.amordegrc(-1000, 40000, 40100, 100, 1, 0.15))
    assert math.isnan(calc.amordegrc(1000, 40000, 40100, -100, 1, 0.15))
    assert math.isnan(calc.amordegrc(1000, 40000, 40100, 1200, 1, 0.15))
    assert math.isnan(calc.amordegrc(1000, 40000, 40100, 100, 1, -0.15))
    assert math.isnan(calc.amordegrc(1000, 40000, 40100, 100, 1, 0.15, 5))


def test_averageifs():
    # Valid AVERAGEIFS
    res = calc.averageifs([10, 20, 30], [1, 2, 3], ">1", ["a", "b", "c"], "<>a")
    assert res == 25.0

    # Mismatched length between avg_range and criteria_range returns #VALUE!
    assert calc.averageifs([10, 20], [1, 2, 3], ">1") == "#VALUE!"
    # Mismatched length between criteria ranges returns #VALUE!
    assert calc.averageifs([10, 20, 30], [1, 2, 3], ">1", ["a", "b"], "<>a") == "#VALUE!"


def test_betadist_and_betainv():
    # BETADIST legacy signature has no cumulative parameter: (x, alpha, beta, [A], [B])
    val = calc.betadist(0.5, 2, 3)
    assert not math.isnan(val) and 0 <= val <= 1

    # Bounds [A, B]
    val_ab = calc.betadist(2.0, 2, 3, 1.0, 3.0)
    assert abs(val_ab - val) < 1e-6

    # Validation
    assert math.isnan(calc.betadist(0.5, 0, 3))  # alpha <= 0
    assert math.isnan(calc.betadist(0.5, 2, 0))  # beta <= 0
    assert math.isnan(calc.betadist(0.5, 2, 3, 5, 2))  # A >= B
    assert math.isnan(calc.betadist(0.5, 2, 3, 1, 2))  # x < A
    assert math.isnan(calc.betadist(2.5, 2, 3, 1, 2))  # x > B

    # BETAINV validation
    inv = calc.betainv(0.5, 2, 3)
    assert not math.isnan(inv)
    assert math.isnan(calc.betainv(-0.1, 2, 3))  # p < 0
    assert math.isnan(calc.betainv(1.1, 2, 3))  # p > 1
    assert math.isnan(calc.betainv(0.5, -2, 3))  # alpha <= 0
    assert math.isnan(calc.betainv(0.5, 2, -3))  # beta <= 0
    assert math.isnan(calc.betainv(0.5, 2, 3, 5, 2))  # A >= B


def test_binomdist():
    # BINOMDIST(number_s, trials, probability_s, cumulative)
    p_exact = calc.binomdist(6, 10, 0.5, False)
    assert 0 < p_exact < 1
    p_cum = calc.binomdist(6, 10, 0.5, True)
    assert p_exact < p_cum <= 1

    # Validation
    assert math.isnan(calc.binomdist(11, 10, 0.5, False))  # k > n
    assert math.isnan(calc.binomdist(-1, 10, 0.5, False))  # k < 0
    assert math.isnan(calc.binomdist(5, -1, 0.5, False))  # n < 0
    assert math.isnan(calc.binomdist(5, 10, -0.1, False))  # p < 0
    assert math.isnan(calc.binomdist(5, 10, 1.1, False))  # p > 1


def test_choose_bounds():
    assert calc.choose(1, "first", "second", "third") == "first"
    assert calc.choose(3, "first", "second", "third") == "third"

    # Out of bounds returns NaN
    assert math.isnan(calc.choose(0, "a", "b"))
    assert math.isnan(calc.choose(3, "a", "b"))
    assert math.isnan(calc.choose(-1, "a", "b"))
    assert math.isnan(calc.choose(float("inf"), "a", "b"))


def test_complex():
    assert calc.complex(2, 3) == "2+3i"
    assert calc.complex(2, -3) == "2-3i"
    assert calc.complex(2, 3, "j") == "2+3j"
    assert calc.complex(2, -3, "j") == "2-3j"
    assert calc.complex(0, 1) == "i"
    assert calc.complex(0, -1) == "j".replace("j", "-i")
    assert calc.complex(5, 0) == "5"

    # Suffix not "i" or "j" returns #VALUE!
    assert calc.complex(1, 2, "k") == "#VALUE!"
    assert calc.complex(1, 2, "") == "#VALUE!"


def test_confidence():
    # CONFIDENCE(alpha, standard_dev, size)
    val = calc.confidence(0.05, 2.5, 50)
    assert not math.isnan(val) and val > 0

    # Validation
    assert math.isnan(calc.confidence(0.0, 2.5, 50))  # alpha <= 0
    assert math.isnan(calc.confidence(1.0, 2.5, 50))  # alpha >= 1
    assert math.isnan(calc.confidence(0.05, 0.0, 50))  # stddev <= 0
    assert math.isnan(calc.confidence(0.05, -2.5, 50))  # stddev < 0
    assert math.isnan(calc.confidence(0.05, 2.5, 0))  # size < 1


def test_countifs():
    assert calc.countifs([1, 2, 3, 4], ">1", ["a", "b", "c", "d"], "<>a") == 3.0

    # Mismatched lengths return #VALUE!
    assert calc.countifs([1, 2, 3], ">0", [1, 2], ">0") == "#VALUE!"
    assert calc.countifs() == 0.0  # No args
    assert calc.countifs([1, 2, 3]) == "#VALUE!"  # Odd args


def test_coupon_functions():
    # Settlement=43831 (2020-01-01), Maturity=43983 (2020-06-01), freq=2, basis=0
    # Next coupon date
    ncd = calc.coupncd(43831, 43983, 2, 0)
    assert ncd == 43983.0

    # Prev coupon date
    pcd = calc.couppcd(43831, 43983, 2, 0)
    assert pcd == 43800.0

    # Number of coupons
    assert calc.coupnum(43831, 43983, 2, 0) == 1.0

    # Days in period
    assert calc.coupdays(43831, 43983, 2, 0) == 180.0

    # Days from beginning to settlement (30/360 NASD basis=0 gives 30.0 days for 1 month)
    assert calc.coupdaybs(43831, 43983, 2, 0) == 30.0

    # Days from settlement to next coupon (180 - 30 = 150.0)
    assert calc.coupdaysnc(43831, 43983, 2, 0) == 150.0

    # Frequency validation: freq not in (1, 2, 4) returns NaN
    assert math.isnan(calc.coupnum(43831, 43983, 3, 0))
    assert math.isnan(calc.coupnum(43831, 43983, 1e9, 0))
    assert math.isnan(calc.coupdays(43831, 43983, 5, 0))

    # Settlement >= maturity returns NaN
    assert math.isnan(calc.coupnum(43983, 43831, 2, 0))
    assert math.isnan(calc.couppcd(43983, 43983, 2, 0))

    # Basis validation
    assert math.isnan(calc.coupdays(43831, 43983, 2, 5))
    assert math.isnan(calc.coupdays(43831, 43983, 2, -1))


def test_csch():
    # csch(0) is NaN (division by zero)
    assert math.isnan(calc.csch(0))
    # csch of large number overflows sinh, catches OverflowError and returns 0.0
    assert calc.csch(1000) == 0.0
    assert calc.csch(-1000) == 0.0


def test_cumipmt_and_cumprinc():
    # CUMIPMT(rate, nper, pv, start_period, end_period, type)
    ipmt = calc.cumipmt(0.05 / 12, 60, 10000, 1, 12, 0)
    assert not math.isnan(ipmt) and ipmt < 0

    princ = calc.cumprinc(0.05 / 12, 60, 10000, 1, 12, 0)
    assert not math.isnan(princ) and princ < 0

    # Rate <= 0 must return NaN
    assert math.isnan(calc.cumipmt(0.0, 60, 10000, 1, 12, 0))
    assert math.isnan(calc.cumipmt(-0.05, 60, 10000, 1, 12, 0))
    assert math.isnan(calc.cumprinc(0.0, 60, 10000, 1, 12, 0))
    assert math.isnan(calc.cumprinc(-0.05, 60, 10000, 1, 12, 0))

    # Bounds validation
    assert math.isnan(calc.cumipmt(0.05, 0, 10000, 1, 1, 0))  # nper <= 0
    assert math.isnan(calc.cumipmt(0.05, 60, -10000, 1, 1, 0))  # pv <= 0
    assert math.isnan(calc.cumipmt(0.05, 60, 10000, 0, 1, 0))  # start < 1
    assert math.isnan(calc.cumipmt(0.05, 60, 10000, 5, 2, 0))  # end < start
    assert math.isnan(calc.cumipmt(0.05, 60, 10000, 1, 61, 0))  # end > nper
    assert math.isnan(calc.cumipmt(0.05, 60, 10000, 1, 12, 2))  # type not in (0, 1)
