# WriterAgent - Online =PY() dumb-JSON contract tests
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Mirror Core any↔JSON packing expectations for Online =PY() (no C++ in pytest).

The C++ add-in (collabofficefull scaddins/pythoncompute) emits the same shapes
over the compute_service wire. Keep this file aligned when changing anyjson.cxx.
"""

from __future__ import annotations

import json

import pytest

from compute_service.executor import execute_code
from compute_service.json_egress import to_dumb_json_value


class TestOnlinePyJsonContract:
    """Shapes C++ buildExecuteRequestJson / jsonResultToAny must agree on."""

    def test_scalar_result_roundtrip(self) -> None:
        out = execute_code("result = 1 + 1")
        assert out["status"] == "ok"
        assert out["result"] == 2
        json.dumps(out, allow_nan=False)

    def test_null_and_nan(self) -> None:
        out = execute_code("result = [float('nan'), None]")
        assert out["status"] == "ok"
        assert out["result"] == [None, None]

    def test_1d_list_for_row_vector(self) -> None:
        out = execute_code("import numpy as np\nresult = np.array([10, 20, 30])")
        assert out["status"] == "ok"
        assert out["result"] == [10, 20, 30]

    def test_2d_grid(self) -> None:
        out = execute_code("result = [[1, 2], [3, 4]]")
        assert out["status"] == "ok"
        assert out["result"] == [[1, 2], [3, 4]]

    def test_request_body_shape(self) -> None:
        # Document the AddIn request (id + code + optional data + mode).
        body = {
            "id": "py-test-1",
            "code": "result = sum(data)",
            "data": [1, 2, 3],
            "mode": "isolated",
        }
        json.dumps(body, allow_nan=False)
        out = execute_code(body["code"], data=body["data"], mode=body["mode"])
        assert out["status"] == "ok"
        assert out["result"] == 6

    def test_multi_range_data_as_list_of_grids(self) -> None:
        # C++ sends multiple ranges as a JSON array when aData.getLength() > 1.
        data = [[[1, 2]], [[3], [4]]]
        out = execute_code(
            "result = [len(data), data[0][0][0], data[1][1][0]]",
            data=data,
        )
        assert out["status"] == "ok"
        assert out["result"] == [2, 1, 4]

    def test_ndarray_egress_is_lists(self) -> None:
        import numpy as np

        assert to_dumb_json_value(np.array([[1.0, float("nan")]])) == [[1.0, None]]


class TestBusyPlaceholderSemantics:
    """Document interim / error strings (Core uses XVolatileResult, not FormulaError)."""

    def test_busy_literal(self) -> None:
        assert "#BUSY!" == "#BUSY!"

    def test_error_field_on_sandbox_failure(self) -> None:
        out = execute_code("import os\nresult = os.name")
        assert out["status"] == "error"
        assert "error" in out
