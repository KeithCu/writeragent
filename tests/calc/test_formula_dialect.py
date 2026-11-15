# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Excel-to-Calc formula dialect translation (no soffice)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugin.calc.formula_dialect import (
    API_LANGUAGE,
    CONV_OOO,
    CONV_XL_A1,
    FormulaDialects,
    GROUP_ALL,
    GROUP_ARRAY_SEPARATORS,
    GROUP_SPECIAL,
    SPECIAL_BAD,
    XL_ENGLISH,
    accepted,
    needs_dialect_check,
    strip_xlfn,
)

_BAD_OP = 14
_OPEN_OP = 25
_CLOSE_OP = 26
_ROW_SEP_OP = 27
_COL_SEP_OP = 28
_GOOD_OP = 200


def _tok(opcode: int) -> SimpleNamespace:
    return SimpleNamespace(OpCode=opcode)


class _ScriptedParser:
    def __init__(self, parse_fn: Any, print_fn: Any = None) -> None:
        self.CompileEnglish = False
        self.FormulaConvention = -1
        self.OpCodeMap = None
        self._parse_fn = parse_fn
        self._print_fn = print_fn
        self.parse_calls: list[str] = []
        self.print_calls: list[Any] = []

    def parseFormula(self, text: str, _pos: Any) -> list[SimpleNamespace]:
        self.parse_calls.append(text)
        return [_tok(op) for op in self._parse_fn(text)]

    def printFormula(self, tokens: Any, _pos: Any) -> str:
        self.print_calls.append(tokens)
        if self._print_fn is None:
            raise AssertionError("printFormula should not be called")
        return self._print_fn(tokens)


class _FakeMapper:
    def getAvailableMappings(self, lang: int, group: int) -> list[SimpleNamespace]:
        if lang == XL_ENGLISH and group == GROUP_SPECIAL:
            entries = [SimpleNamespace(Token=_tok(i)) for i in range(SPECIAL_BAD + 1)]
            entries[SPECIAL_BAD] = SimpleNamespace(Token=_tok(_BAD_OP))
            return entries
        if lang == XL_ENGLISH and group == GROUP_ARRAY_SEPARATORS:
            return [
                SimpleNamespace(Name="{", Token=_tok(_OPEN_OP)),
                SimpleNamespace(Name="}", Token=_tok(_CLOSE_OP)),
                SimpleNamespace(Name=";", Token=_tok(_ROW_SEP_OP)),
                SimpleNamespace(Name=",", Token=_tok(_COL_SEP_OP)),
            ]
        if lang == API_LANGUAGE and group == GROUP_ALL:
            return [SimpleNamespace(Name="API", Token=_tok(1))]
        if lang == XL_ENGLISH and group == GROUP_ALL:
            return [SimpleNamespace(Name="XL", Token=_tok(2))]
        raise AssertionError(f"Unexpected mapping request: lang={lang}, group={group}")


class _FakeDoc:
    def __init__(self, parsers: list[_ScriptedParser]) -> None:
        self._mapper = _FakeMapper()
        self._parsers = iter(parsers)
        self.create_calls: list[str] = []

    def createInstance(self, service: str) -> Any:
        self.create_calls.append(service)
        if service == "com.sun.star.sheet.FormulaOpCodeMapper":
            return self._mapper
        if service == "com.sun.star.sheet.FormulaParser":
            return next(self._parsers)
        raise AssertionError(f"Unexpected service: {service}")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("=A1*2", False),
        ('="a,b"', True),
        ("=_xlfn.UNIQUE(A1:A5)", True),
        ("=_XLFN.XLOOKUP(1,A1:A5,B1:B5)", True),
        ("=Orders!A1", True),
        ("", False),
        ("0.0825", False),
    ],
)
def test_needs_dialect_check(text: str, expected: bool) -> None:
    assert needs_dialect_check(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("=_xlfn.XLOOKUP(1,A1:A5,B1:B5)", "=XLOOKUP(1,A1:A5,B1:B5)"),
        ("=_xlfn._xlws.FILTER(A1:A5,A1:A5>0)", "=FILTER(A1:A5,A1:A5>0)"),
        ("=_xlws.SORT(A1:A5)", "=SORT(A1:A5)"),
        ("=_XLFN._XLWS.UNIQUE(A1:A5)", "=UNIQUE(A1:A5)"),
    ],
)
def test_strip_xlfn(text: str, expected: str) -> None:
    assert strip_xlfn(text) == expected


def test_accepted_rejects_bad_and_stray_array_separators() -> None:
    seps = frozenset({_ROW_SEP_OP, _COL_SEP_OP})
    assert accepted([_GOOD_OP], _BAD_OP, _OPEN_OP, seps) is True
    assert accepted([_GOOD_OP, _BAD_OP], _BAD_OP, _OPEN_OP, seps) is False
    assert accepted([_GOOD_OP, _ROW_SEP_OP], _BAD_OP, _OPEN_OP, seps) is False
    assert accepted([_OPEN_OP, _ROW_SEP_OP, _CLOSE_OP], _BAD_OP, _OPEN_OP, seps) is True


def test_to_api_clean_returns_input_without_printing() -> None:
    api = _ScriptedParser(lambda _t: [_GOOD_OP])
    xl = _ScriptedParser(lambda _t: [_BAD_OP])
    xl_refs = _ScriptedParser(lambda _t: [_BAD_OP])
    dialects = FormulaDialects(_FakeDoc([api, xl, xl_refs]))

    assert dialects.to_api('="a,b"') == '="a,b"'
    assert api.FormulaConvention == CONV_OOO
    assert xl.FormulaConvention == CONV_XL_A1
    assert xl_refs.FormulaConvention == CONV_XL_A1
    assert xl_refs.OpCodeMap is None
    assert api.print_calls == []
    assert xl.parse_calls == []


def test_to_api_skips_parsers_for_plain_and_empty() -> None:
    api = _ScriptedParser(lambda _t: [_GOOD_OP])
    xl = _ScriptedParser(lambda _t: [_GOOD_OP])
    xl_refs = _ScriptedParser(lambda _t: [_GOOD_OP])
    dialects = FormulaDialects(_FakeDoc([api, xl, xl_refs]))

    assert dialects.to_api("") == ""
    assert dialects.to_api("0.0825") == "0.0825"
    assert api.parse_calls == []


def test_to_api_readds_leading_equals_only_when_input_had_one() -> None:
    api = _ScriptedParser(
        lambda t: [_GOOD_OP] if ";" in t or "$Orders." in t else [_BAD_OP],
        print_fn=lambda tokens: "SUM(A1;B1)" if len(tokens) == 2 else "$Orders.$A$1:$A$5",
    )
    xl = _ScriptedParser(lambda t: [_GOOD_OP, _GOOD_OP] if "," in t else [_GOOD_OP])
    xl_refs = _ScriptedParser(lambda _t: [_BAD_OP])
    dialects = FormulaDialects(_FakeDoc([api, xl, xl_refs]))

    assert dialects.to_api("=SUM(A1,B1)") == "=SUM(A1;B1)"
    assert dialects.to_api("Orders!$A$1:$A$5") == "$Orders.$A$1:$A$5"


def test_to_api_fallthrough_and_xlfn_only_on_excel_attempt() -> None:
    # API rejects first print from Excel ("BAD_PRINT"), accepts second print from xl_refs ("SUM($Orders.A1:A5;B1)").
    api = _ScriptedParser(
        lambda t: [_GOOD_OP] if t == "=SUM($Orders.A1:A5;B1)" else [_BAD_OP],
        print_fn=lambda tokens: "BAD_PRINT" if tokens[0].OpCode == 101 else "SUM($Orders.A1:A5;B1)",
    )
    xl = _ScriptedParser(lambda _t: [101])
    xl_refs = _ScriptedParser(lambda _t: [102])
    dialects = FormulaDialects(_FakeDoc([api, xl, xl_refs]))

    assert dialects.to_api("=_xlfn.FILTER(Orders!A1:A5;B1)") == "=SUM($Orders.A1:A5;B1)"
    assert xl.parse_calls == ["=FILTER(Orders!A1:A5;B1)"]
    assert xl_refs.parse_calls == ["=_xlfn.FILTER(Orders!A1:A5;B1)"]


def test_to_api_returns_input_when_all_grammars_fail() -> None:
    api = _ScriptedParser(lambda _t: [_BAD_OP], print_fn=lambda _tokens: "BAD")
    xl = _ScriptedParser(lambda _t: [_BAD_OP])
    xl_refs = _ScriptedParser(lambda _t: [_BAD_OP])
    dialects = FormulaDialects(_FakeDoc([api, xl, xl_refs]))

    assert dialects.to_api("=SUM(Orders.A1,B1)") == "=SUM(Orders.A1,B1)"


def test_to_api_skips_create_instance_for_plain_formula() -> None:
    fake_doc = _FakeDoc([])
    dialects = FormulaDialects(fake_doc)

    assert dialects.to_api("=A1*2") == "=A1*2"
    assert fake_doc.create_calls == []
