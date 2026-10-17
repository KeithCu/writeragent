# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.



from unittest.mock import MagicMock, patch

from plugin.calc.error_detector import (
    ERROR_PATTERNS,
    ERROR_TYPES,
    ErrorDetector,
    get_calc_error_name,
)


def test_error_detector_data():
    assert 502 in ERROR_TYPES
    assert len(ERROR_PATTERNS) > 0
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


def test_explain_error_precedent_probe_fallback():
    bridge = MagicMock()
    inspector = MagicMock()
    cell = MagicMock()
    cell.getError.return_value = 532  # #DIV/0!
    cell.getFormula.return_value = "=A1/B1"
    sheet = MagicMock()
    sheet.getCellByPosition.return_value = cell
    bridge.get_active_sheet.return_value = sheet
    inspector.get_cell_details.return_value = {"formula": "=A1/B1"}

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


def test_explain_error_dep_chain_best_effort():
    bridge = MagicMock()
    inspector = MagicMock()
    cell = MagicMock()
    cell.getError.return_value = 532
    cell.getFormula.return_value = "=A1"
    sheet = MagicMock()
    sheet.getCellByPosition.return_value = cell
    bridge.get_active_sheet.return_value = sheet
    inspector.get_cell_details.return_value = {"formula": "=A1"}
    inspector.read_cell.return_value = {"address": "A1", "value": "0", "type": "number"}

    detector = ErrorDetector(bridge, inspector)
    with patch("plugin.calc.error_detector.fetch_formula_dep_chain", side_effect=RuntimeError("no dep chain")):
        res = detector.explain_error("B1")
        assert "dependency_chain" not in res
        assert res["error"]["code"] == "#DIV/0!"


