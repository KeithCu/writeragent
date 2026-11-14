# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for Excel ref resolution (resolve_refs)."""

from __future__ import annotations

import pytest

from plugin.calc.excel_py_convert.resolve_refs import (
    ResolvedDep,
    resolve_dep,
)


class DummyModel:
    def __init__(self) -> None:
        self.anchor_snapshots = {"A6": "A6:C10"}
        self.tables = {"Table1": "A1:D50"}


def test_resolve_dep_range_and_table() -> None:
    model = DummyModel()  # type: ignore[assignment]
    dep1 = resolve_dep("A1:B10", model)
    assert isinstance(dep1, ResolvedDep)
    assert dep1.kind == "range"
    assert dep1.a1 == "A1:B10"

    dep2 = resolve_dep("Table1[#All]", model)
    assert dep2.kind == "table_snapshot"
    assert dep2.a1 == "A1:D50"

    dep3 = resolve_dep("_xlfn.ANCHORARRAY(A6)", model)
    assert dep3.kind == "anchor_snapshot"
    assert dep3.a1 == "A6:C10"

    # Suffix case folding still reaches the regex after the surface-shape gate.
    # Table *name* lookup is case-insensitive (an exact match wins).
    dep4 = resolve_dep("Table1[#all]", model)
    assert dep4.kind == "table_snapshot"
    assert dep4.a1 == "A1:D50"

    dep5 = resolve_dep("ANCHORARRAY(A6)", model)
    assert dep5.kind == "anchor_snapshot"
    assert dep5.a1 == "A6:C10"

    dep6 = resolve_dep("A6#", model)
    assert dep6.kind == "anchor_snapshot"
    assert dep6.a1 == "A6:C10"

    # Out-of-bounds A1 coords reject, within-bounds accept
    for oob in ("XFE1", "ZZZZ1", "A0", "A1048577", "Sales2024", "Table1"):
        res = resolve_dep(oob, model)
        assert res.kind == "unresolved", f"{oob} should be unresolved"
        assert "out-of-bounds A1 ref" in res.note, f"{oob} note should state out-of-bounds"

    max_dep = resolve_dep("XFD1048576", model)
    assert max_dep.kind == "range"
    assert max_dep.a1 == "XFD1048576"

    for valid_span in ("A1:B10", "A:A", "1:10", "Data!A:A", "$A$1:$B$2"):
        res = resolve_dep(valid_span, model)
        assert res.kind == "range", f"{valid_span} should be range"

    # Dollar sign in sheet name preserved, not mangled
    res_dollar = resolve_dep("'A$B'!A6", model)
    assert res_dollar.kind == "range"
    assert res_dollar.a1 == "'A$B'!A6"

    # Spill with dollar in sheet name resolves against snapshot
    model.anchor_snapshots["'A$B'!A6"] = "'A$B'!A6:D20"
    res_spill_dollar = resolve_dep("'A$B'!A6#", model)
    assert res_spill_dollar.kind == "anchor_snapshot"
    assert res_spill_dollar.a1 == "'A$B'!A6:D20"


def test_resolve_dep_table_name_case_insensitive() -> None:
    model = DummyModel()  # type: ignore[assignment]
    model.tables = {"tradeData": "Data!A1:AA10"}
    for dep in ("tradeData[#All]", "TRADEDATA[#All]", "TradeData[#ALL]"):
        resolved = resolve_dep(dep, model)
        assert resolved.kind == "table_snapshot"
        assert resolved.a1 == "Data!A1:AA10"
    assert resolve_dep("nosuch[#All]", model).kind == "unresolved"

    model.tables = {"Données": "Data!A1:B2"}
    unicode_dep = resolve_dep("DONNÉES[#All]", model)
    assert unicode_dep.kind == "table_snapshot"
    assert unicode_dep.a1 == "Data!A1:B2"


def test_resolve_dep_table_exact_name_wins_over_case_variant() -> None:
    model = DummyModel()  # type: ignore[assignment]
    model.tables = {"tradeData": "A1:A2", "TRADEDATA": "B1:B2"}
    assert resolve_dep("TRADEDATA[#All]", model).a1 == "B1:B2"
    assert resolve_dep("TradeData[#All]", model).kind == "table_snapshot"


@pytest.mark.parametrize("dep", ["_", "[", "]", "\x00", "!", ":", "#", ""])
def test_resolve_dep_junk_no_pattern_error(dep: str) -> None:
    """Junk 1-char deps must not hit character-class regexes.

    CPython returns unresolved without triggering regex parser errors on
    special characters.
    """
    model = DummyModel()  # type: ignore[assignment]
    resolved = resolve_dep(dep, model)
    assert resolved.kind == "unresolved"

