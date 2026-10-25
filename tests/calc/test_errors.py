# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.errors import DetectErrors


@pytest.mark.parametrize("invalid_range", [[""], [123], {"a": 1}])
def test_detect_errors_invalid_range(invalid_range):
    ctx = MagicMock()
    tool = DetectErrors()

    with patch("plugin.calc.errors.ErrorDetector") as mock_detector:
        res = tool.execute(ctx, range=invalid_range)

    assert res["status"] == "error"
    assert res.get("code") == "INVALID_RANGE"
    mock_detector.assert_not_called()


def test_detect_errors_single_range():
    ctx = MagicMock()
    tool = DetectErrors()

    with patch("plugin.calc.errors.CalcBridge"), \
         patch("plugin.calc.errors.CellInspector"), \
         patch("plugin.calc.errors.ErrorDetector") as mock_detector_cls:
        instance = mock_detector_cls.return_value
        instance.detect_and_explain.return_value = {
            "range": "A1:A2",
            "total_error_count": 1,
            "error_count": 1,
            "truncated": False,
            "errors": [{"address": "Sheet1.A1"}],
        }

        res = tool.execute(ctx, range="A1:A2")

    assert res["status"] == "ok"
    instance.detect_and_explain.assert_called_once_with(range_str="A1:A2")
    assert res["result"]["range"] == "A1:A2"


def test_detect_errors_multi_range_aggregates():
    ctx = MagicMock()
    tool = DetectErrors()

    result1 = {
        "range": "A1:A2",
        "total_error_count": 5,
        "error_count": 2,
        "truncated": True,
        "errors": [{"address": "Sheet1.A1"}, {"address": "Sheet1.A2"}],
    }
    result2 = {
        "range": "B1:B2",
        "total_error_count": 0,
        "error_count": 0,
        "truncated": False,
        "errors": [],
    }

    with patch("plugin.calc.errors.CalcBridge"), \
         patch("plugin.calc.errors.CellInspector"), \
         patch("plugin.calc.errors.ErrorDetector") as mock_detector_cls:
        instance = mock_detector_cls.return_value
        instance.detect_and_explain.side_effect = [result1, result2]

        res = tool.execute(ctx, range=["A1:A2", "B1:B2"])

    assert res["status"] == "ok"
    data = res["result"]
    assert data["ranges"] == ["A1:A2", "B1:B2"]
    assert data["total_error_count"] == 5
    assert data["error_count"] == 2
    assert data["truncated"] is True
    assert data["errors"] == result1["errors"]
