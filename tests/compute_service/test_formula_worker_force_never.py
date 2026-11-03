# WriterAgent - Formula worker force="never" serialization tests
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests verifying formula_worker.execute_code uses force="never" to bypass split_grid."""

from __future__ import annotations

import json
from typing import Any

from compute_service.formula_worker import _handle_request
from plugin.scripting.payload_codec import is_split_grid
import pytest


def _execute_code(code: str) -> dict[str, Any]:
    env = _handle_request({"code": code})
    return json.loads(env["result_json"])


def test_numpy_arange_returns_plain_int_lists() -> None:
    pytest.importorskip("numpy")
    code = "import numpy as np\nresult = np.arange(300).reshape(30, 10)"
    out = _execute_code(code)
    assert out["status"] == "ok"
    res = out["result"]
    assert isinstance(res, list)
    assert len(res) == 30
    for r_idx, row in enumerate(res):
        assert isinstance(row, list)
        assert len(row) == 10
        for c_idx, val in enumerate(row):
            assert type(val) is int
            assert val == r_idx * 10 + c_idx
    assert not is_split_grid(res)


def test_int64_large_integers_exact() -> None:
    pytest.importorskip("numpy")
    code = """
import numpy as np
result = np.array([2**53 + 1, 2**62] * 60, dtype=np.int64)
"""
    out = _execute_code(code)
    assert out["status"] == "ok"
    res = out["result"]
    assert isinstance(res, list)
    assert len(res) == 120
    for idx, val in enumerate(res):
        assert type(val) is int
        expected = (2**53 + 1) if (idx % 2 == 0) else (2**62)
        assert val == expected
    assert not is_split_grid(res)


def test_float_array_stays_float() -> None:
    pytest.importorskip("numpy")
    code = """
import numpy as np
result = np.linspace(0.0, 1.0, 150)
"""
    out = _execute_code(code)
    assert out["status"] == "ok"
    res = out["result"]
    assert isinstance(res, list)
    assert len(res) == 150
    for val in res:
        assert type(val) is float
    assert not is_split_grid(res)


def test_pandas_dataframe_returns_plain_rows() -> None:
    pd = pytest.importorskip("pandas")
    code = """
import pandas as pd
df = pd.DataFrame({
    "ints": list(range(400)),
    "floats": [i * 0.5 for i in range(400)],
    "strings": [f"row_{i}" for i in range(400)],
})
result = df
"""
    out = _execute_code(code)
    assert out["status"] == "ok"
    res = out["result"]
    assert isinstance(res, dict)
    assert res.get("__wa_payload__") == "dataframe"
    assert res.get("columns") == ["ints", "floats", "strings"]
    data = res.get("data")
    assert isinstance(data, list)
    assert len(data) == 400
    assert not is_split_grid(data)

    expected_df = pd.DataFrame({
        "ints": list(range(400)),
        "floats": [i * 0.5 for i in range(400)],
        "strings": [f"row_{i}" for i in range(400)],
    })
    expected_rows = expected_df.values.tolist()
    assert data == expected_rows
    for row in data:
        assert isinstance(row, list)
        assert len(row) == 3
        assert type(row[0]) is int
        assert type(row[1]) is float
        assert type(row[2]) is str


def test_pandas_series_returns_plain_list() -> None:
    pytest.importorskip("pandas")
    # Named Series
    code_named = """
import pandas as pd
result = pd.Series(list(range(200)), name="nums")
"""
    out = _execute_code(code_named)
    assert out["status"] == "ok"
    res = out["result"]
    assert isinstance(res, dict)
    assert res.get("__wa_payload__") == "dataframe"
    assert res.get("columns") == ["nums"]
    data = res.get("data")
    assert isinstance(data, list)
    assert len(data) == 200
    assert not is_split_grid(data)
    for i, val in enumerate(data):
        assert type(val) is int
        assert val == i

    # Unnamed Series
    code_unnamed = """
import pandas as pd
result = pd.Series([i * 1.5 for i in range(200)])
"""
    out_unnamed = _execute_code(code_unnamed)
    assert out_unnamed["status"] == "ok"
    res_unnamed = out_unnamed["result"]
    assert isinstance(res_unnamed, list)
    assert len(res_unnamed) == 200
    assert not is_split_grid(res_unnamed)
    for i, val in enumerate(res_unnamed):
        assert type(val) is float
        assert val == i * 1.5
