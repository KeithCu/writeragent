# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.



from unittest.mock import MagicMock

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

