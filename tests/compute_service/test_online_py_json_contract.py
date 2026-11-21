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
import sys
from typing import Any

from compute_service.formula_worker import _handle_request
from compute_service.json_egress import to_dumb_json_value
from compute_service.json_forward import parse_execute_request
import pytest


def _execute_code(code: str, *, data: Any = None) -> dict[str, Any]:
    req: dict[str, Any] = {"code": code}
    if data is not None:
        req["data_json"] = json.dumps(data, allow_nan=False).encode("utf-8")
    env = _handle_request(req)
    return json.loads(env["result_json"])


class TestOnlinePyJsonContract:
    """Shapes C++ buildExecuteRequestJson / jsonResultToAny must agree on."""

    def test_scalar_result_roundtrip(self) -> None:
        out = _execute_code("result = 1 + 1")
        assert out["status"] == "ok"
        assert out["result"] == 2
        json.dumps(out, allow_nan=False)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("result = [float('nan'), None]", id="test_null_and_nan"),
            pytest.param("result = [float('inf'), float('-inf')]", id="test_inf_is_null"),
        ],
    )
    def test_null_and_nan(self, value) -> None:
        out = _execute_code(value)
        assert out["status"] == "ok"
        assert out["result"] == [None, None]
        json.dumps(out, allow_nan=False)


    def test_one_by_one_stays_nested(self) -> None:
        out = _execute_code("result = [[7]]")
        assert out["status"] == "ok"
        assert out["result"] == [[7]]

    def test_eval_error_payload(self) -> None:
        out = _execute_code("result = 1 / 0")
        assert out["status"] == "error"
        assert out.get("error")
        assert out.get("traceback") == (
            "Traceback (most recent call last):\n"
            '  File "<sandbox>", line 1, in <module>\n'
            "    result = 1 / 0\n"
            "ZeroDivisionError: division by zero"
        )
        json.dumps(out, allow_nan=False)

    def test_nested_function_traceback_on_wire(self) -> None:
        out = _execute_code("def f(x):\n    return x / 0\n\nresult = f(2)")
        assert out["status"] == "error"
        tb = out.get("traceback", "")
        assert 'File "<sandbox>", line 2, in f' in tb
        assert "    return x / 0" in tb
        assert ".py" not in tb
        assert "/plugin/" not in tb

    @pytest.mark.parametrize(
        "code",
        [
            pytest.param("result = (1,", id="syntax_error"),
            pytest.param("import subprocess\nresult = subprocess", id="blocked_import"),
            pytest.param("import threading\nresult = threading.Lock()", id="unserialisable_result"),
            pytest.param("raise SystemExit(3)", id="system_exit"),
        ],
    )
    def test_no_traceback_without_user_frames(self, code: str) -> None:
        out = _execute_code(code)
        assert out["status"] == "error"
        assert "traceback" not in out

    def test_ragged_grid_passes_through_unpadded(self) -> None:
        # The Python service does not pad ragged grids; the result must stay
        # JSON-serializable (allow_nan=False), and any padding happens on the consumer side.
        out = _execute_code("result = [[1, 2], [3]]")
        json.dumps(out, allow_nan=False)
        assert out["status"] == "ok"
        assert out["result"] == [[1, 2], [3]]

    def test_1d_list_for_row_vector(self) -> None:
        out = _execute_code("import numpy as np\nresult = np.array([10, 20, 30])")
        assert out["status"] == "ok"
        assert out["result"] == [10, 20, 30]

    @pytest.mark.parametrize(
        "value, value_2, value_3, value_4",
        [
            pytest.param("result = [[1, 2], [3, 4]]", 2, 3, 4, id="test_2d_grid"),
            pytest.param('result = [[1, "a"], [2, "b"]]', "a", 2, "b", id="test_mixed_type_grid"),
        ],
    )
    def test_2d_grid(self, value, value_2, value_3, value_4) -> None:
        out = _execute_code(value)
        assert out["status"] == "ok"
        assert out["result"] == [[1, value_2], [value_3, value_4]]

    def test_column_vector_stays_nested(self) -> None:
        # C++ parse keeps [[1],[2],[3]] as 3×1 (emit may flatten ranges the other way).
        out = _execute_code("result = [[1], [2], [3]]")
        assert out["status"] == "ok"
        assert out["result"] == [[1], [2], [3]]

    def test_numpy_column_vector_egress(self) -> None:
        out = _execute_code("import numpy as np\nresult = np.array([[1], [2], [3]])")
        assert out["status"] == "ok"
        assert out["result"] == [[1], [2], [3]]


    def test_images_top_level_on_plot(self) -> None:
        out = _execute_code(
            "import matplotlib.pyplot as plt\n"
            "fig, ax = plt.subplots()\n"
            "ax.plot([0, 1], [0, 1])\n"
            "result = fig"
        )
        assert out["status"] == "ok"
        images = out.get("images") or []
        assert len(images) == 1
        assert images[0].get("format") in ("svg", "png")
        assert images[0].get("data_b64")
        assert out.get("result") is None

    def test_request_body_shape(self) -> None:
        # Document the AddIn request (id + code + optional data + mode).
        # mode is decoded only for validation (routing uses ?session_id=), and
        # the worker dict below is hand-built after parse_execute_request.
        body = {
            "id": "py-test-1",
            "code": "result = float(np.sum(data))",
            "data": [[1, 2, 3]],
            "mode": "isolated",
        }
        raw_body = json.dumps(body, allow_nan=False).encode("utf-8")
        parts = parse_execute_request(raw_body, "application/json")
        assert parts.req_id == "py-test-1"
        assert parts.code is not None
        assert parts.data_json is not None
        env = _handle_request({"id": parts.req_id, "code": parts.code, "data_json": parts.data_json})
        out = json.loads(env["result_json"])
        assert out["id"] == "py-test-1"
        assert out["status"] == "ok"
        assert out["result"] == 6.0

    def test_multi_range_data_as_list_of_grids(self) -> None:
        # C++ sends multiple ranges as a JSON array when aData.getLength() > 1.
        # Injected as ranges (always list); multi-arg data is the same list object.
        data = [[[1, 2]], [[3], [4]]]
        out = _execute_code(
            "result = [len(ranges), data is ranges, ranges[0].values[0][0], ranges[1].values[1][0]]",
            data=data,
        )
        assert out["status"] == "ok"
        assert out["result"] == [2, True, 1, 4]

    @pytest.mark.parametrize(
        "data, code, expected",
        [
            pytest.param(
                [5, [[1, 2]]],
                "result = [len(ranges), ranges[0].values[0][0], list(ranges[1].shape)]",
                [2, 5, [1, 2]],
                id="scalar_then_row_range",
            ),
            pytest.param(
                [5, 6],
                "result = [len(ranges), float(data[0]) + float(data[1])]",
                [2, 11.0],
                id="two_scalars",
            ),
            pytest.param(
                [[[1, 2, 3]], [[4, 5, 6]]],
                "import numpy as np\nresult = [len(ranges), float(np.sum(ranges[1]))]",
                [2, 15.0],
                id="two_row_ranges",
            ),
            pytest.param(
                [[1], [2], [3]],
                "result = [len(ranges), list(data.shape)]",
                [1, [3, 1]],
                id="column_range",
            ),
        ],
    )
    def test_each_formula_argument_is_its_own_input(
        self, data: Any, code: str, expected: Any
    ) -> None:
        out = _execute_code(code, data=data)
        assert out["status"] == "ok"
        assert out["result"] == expected

    def test_ndarray_egress_is_lists(self) -> None:
        import numpy as np

        assert to_dumb_json_value(np.array([[1.0, float("nan")]])) == [[1.0, None]]

    def test_error_field_on_sandbox_failure(self) -> None:
        out = _execute_code("import subprocess\nresult = subprocess")
        assert out["status"] == "error"
        assert "error" in out

    @pytest.mark.parametrize(
        "code, expected",
        [
            pytest.param(
                "import datetime as dt\nimport pandas as pd\npd.DataFrame({'when': [dt.datetime(2026, 1, 2, 3, 4), dt.datetime(2026, 1, 3)], 'n': [1, 2]})",
                [["when", "n"], ["2026-01-02T03:04:00", 1], ["2026-01-03T00:00:00", 2]],
                id="dataframe_datetime",
            ),
            pytest.param(
                "import pandas as pd\npd.Series([1.5, 2.5], name='x')",
                [["x"], [1.5], [2.5]],
                id="named_series",
            ),
            pytest.param(
                "import pandas as pd\npd.DataFrame({'a': []})",
                [["a"]],
                id="empty_frame",
            ),
        ],
    )
    def test_pandas_result_is_kit_grid(self, code: str, expected: list[list[Any]]) -> None:
        pytest.importorskip("pandas")
        out = _execute_code(code)
        assert out["status"] == "ok"
        assert out["result"] == expected
        json.dumps(out, allow_nan=False)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="SIGALRM timeout path is Unix-only; the thread-pool fallback may win the race and carry no frames",
    )
    def test_timeout_traceback_on_wire(self) -> None:
        env = _handle_request(
            {"code": "def spin():\n    while True:\n        pass\n\nspin()", "timeout_sec": 1}
        )
        out = json.loads(env["result_json"])
        assert out["status"] == "error"
        tb = out.get("traceback", "")
        assert "in spin" in tb
        assert ".py" not in tb
        assert "/plugin/" not in tb
