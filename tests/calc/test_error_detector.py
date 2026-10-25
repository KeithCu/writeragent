# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.



from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugin.calc.error_detector import (
    ERROR_PATTERNS,
    ERROR_TYPES,
    ErrorDetector,
    get_calc_error_name,
    is_calc_error_display,
)


def test_error_detector_data():
    assert 502 in ERROR_TYPES
    expected_tokens = frozenset({
        "#REF!", "#NAME?", "#VALUE!", "#DIV/0!", "#NULL!", "#N/A", "#NUM!", "#ERROR!",
        "Err:502", "Err:504", "Err:519", "Err:522", "Err:524", "Err:525", "Err:532",
    })
    assert ERROR_PATTERNS == expected_tokens
    assert len({t.upper() for t in ERROR_PATTERNS}) == len(ERROR_PATTERNS)
    assert is_calc_error_display("#div/0!") == "#DIV/0!"
    assert is_calc_error_display(" err:502 ") == "Err:502"
    assert is_calc_error_display("#ERROR!") == "#ERROR!"
    assert is_calc_error_display("#hashtag") is None
    assert is_calc_error_display(1) is None
    for code, info in ERROR_TYPES.items():
        assert "name" in info
        assert "description" in info


def test_get_calc_error_name():
    assert get_calc_error_name(501) == "Invalid character"
    assert get_calc_error_name(532) == "#DIV/0!"
    assert get_calc_error_name(9999) == "Unknown error (9999)"


def test_get_error_type():
    cell = MagicMock()
    cell.getError.return_value = 0
    assert ErrorDetector.get_error_type(cell) == {}

    cell.getError.return_value = 502
    res = ErrorDetector.get_error_type(cell)
    assert res["name"] == "Invalid argument"

    cell.getError.return_value = 8888
    res = ErrorDetector.get_error_type(cell)
    assert res["code"] == "Err:8888"

    cell.getError.return_value = 32767
    res = ErrorDetector.get_error_type(cell)
    assert res["code"] == "#N/A"
    assert res["error_code"] == 32767

    cell.getError.return_value = 522
    res = ErrorDetector.get_error_type(cell)
    assert res["error_code"] == 522


def test_generate_suggestion():
    assert "circular" in ErrorDetector._generate_suggestion({"error_code": 522, "code": "#REF!", "description": ""}, []).lower()
    assert "spelled" not in ErrorDetector._generate_suggestion({"error_code": 504, "code": "#NAME?", "description": "The function argument is invalid."}, [])
    assert "#REF!" in ErrorDetector._generate_suggestion({"error_code": 524, "code": "#REF!", "description": ""}, [])

    sugg_532 = ErrorDetector._generate_suggestion({"error_code": 532, "code": "#DIV/0!", "description": ""}, [{"address": "B1", "value": 0}])
    assert "B1" in sugg_532

    sugg_519 = ErrorDetector._generate_suggestion({"error_code": 519, "code": "#VALUE!", "description": ""}, [{"address": "C1", "type": "text"}])
    assert "C1" in sugg_519


def test_explain_error_precedent_probe_fallback():
    bridge = MagicMock()
    inspector = MagicMock()
    cell = MagicMock()
    cell.getError.return_value = 532  # #DIV/0!
    cell.getFormula.return_value = "=A1/B1"
    sheet = MagicMock()
    sheet.getCellByPosition.return_value = cell
    sheet.getName.return_value = "Sheet1"
    bridge.resolve.return_value = (sheet, "C1")

    # A1 succeeds, B1 raises non-disposal Exception (e.g. read error)
    def fake_read_cell(addr: str) -> dict[str, str]:
        if addr == "A1":
            return {"address": "A1", "value": "10", "type": "number"}
        raise RuntimeError("cannot read cell")

    inspector.read_cell.side_effect = fake_read_cell

    detector = ErrorDetector(bridge, inspector)
    res = detector.explain_error("C1")
    assert res["error"]["code"] == "#DIV/0!"
    precs = {p["address"]: p for p in res["precedents"]}
    assert precs["A1"]["value"] == "10"
    assert precs["B1"]["value"] == "UNREADABLE"


def test_explain_error_dep_chain_failure_is_swallowed():
    bridge = MagicMock()
    inspector = MagicMock()
    cell = MagicMock()
    cell.getError.return_value = 532
    cell.getFormula.return_value = "=A1"
    sheet = MagicMock()
    sheet.getCellByPosition.return_value = cell
    sheet.getName.return_value = "Sheet1"
    bridge.resolve.return_value = (sheet, "B1")
    inspector.read_cell.return_value = {"address": "A1", "value": "0", "type": "number"}

    detector = ErrorDetector(bridge, inspector)
    with patch("plugin.calc.formula_dep_chain.fetch_formula_dep_chain", side_effect=RuntimeError("no dep chain")):
        res = detector.explain_error("B1")
        assert "dependency_chain" not in res
        assert res["error"]["code"] == "#DIV/0!"


def test_detect_errors_qualifies_sheet_address():
    bridge = MagicMock()
    sheet = MagicMock()
    bridge.resolve.return_value = (sheet, "A1")
    sheet.getName.return_value = "Sheet2"

    cell = MagicMock()
    cell.getError.return_value = 532
    cell.getFormula.return_value = "=1/0"
    cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0)

    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = cell

    cells_collection = MagicMock()
    cells_collection.createEnumeration.return_value = enum

    formula_cells = MagicMock()
    formula_cells.getCells.return_value = cells_collection

    cell_range = MagicMock()
    cell_range.queryFormulaCells.return_value = formula_cells
    sheet.getCellRangeByPosition.return_value = cell_range

    detector = ErrorDetector(bridge, MagicMock())
    res = detector.detect_errors("Sheet2.A1")

    assert len(res) == 1
    assert res[0]["address"] == "Sheet2.A1"
    assert res[0]["formula"] == "=1/0"
    assert res[0]["error"]["error_code"] == 532


def test_explain_error_reads_the_named_sheet():
    bridge = MagicMock()
    sheet2 = MagicMock()
    bridge.resolve.return_value = (sheet2, "A1")
    sheet2.getName.return_value = "Sheet2"

    error_cell = MagicMock()
    error_cell.getError.return_value = 532
    error_cell.getFormula.return_value = "=B1"
    sheet2.getCellByPosition.return_value = error_cell

    inspector = MagicMock()
    inspector.read_cell.return_value = {"address": "B1", "value": 0, "type": "number"}

    active_sheet = MagicMock()
    bridge.get_active_sheet.return_value = active_sheet

    detector = ErrorDetector(bridge, inspector)
    with patch("plugin.calc.formula_dep_chain.fetch_formula_dep_chain", return_value=None):
        res = detector.explain_error("Sheet2.A1")

    assert res["address"] == "Sheet2.A1"
    active_sheet.getCellByPosition.assert_not_called()


def test_detect_and_explain_caps_explanations():
    detector = ErrorDetector(MagicMock(), MagicMock())
    detector.detect_errors = MagicMock(return_value=[{"address": f"A{i + 1}", "formula": "", "error": {}} for i in range(51)])
    detector.explain_error = MagicMock(side_effect=lambda address: {"address": address})

    res = detector.detect_and_explain("A1:A51")
    assert res["total_error_count"] == 51
    assert res["error_count"] == 50
    assert res["truncated"] is True
    assert len(res["errors"]) == 50


def test_detect_and_explain_uncapped():
    detector = ErrorDetector(MagicMock(), MagicMock())
    detector.detect_errors = MagicMock(return_value=[{"address": f"A{i + 1}", "formula": "", "error": {}} for i in range(2)])
    detector.explain_error = MagicMock(side_effect=lambda address: {"address": address})

    res = detector.detect_and_explain("A1:A2")
    assert res["total_error_count"] == 2
    assert res["error_count"] == 2
    assert res["truncated"] is False
    assert len(res["errors"]) == 2


