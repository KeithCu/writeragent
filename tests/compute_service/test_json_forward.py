# WriterAgent - Python Compute Service JSON-forward tests
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Peel + blob-forward path: raw data bytes reach the worker; host does not re-dumps results."""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from compute_service.config import ComputeSettings
from compute_service.formula_pool import FormulaProcessPool, shutdown_formula_pool
from compute_service.json_forward import (
    WIRE_JSON_FORWARD,
    WIRE_PICKLE,
    ExecuteRequestError,
    decode_worker_result,
    dumps_response,
    peel_execute_request,
)
from compute_service.server import create_wsgi_app
from plugin.scripting.payload_codec import host_pack_data


@pytest.fixture(autouse=True)
def _cleanup_formula_pool():
    yield
    shutdown_formula_pool()


def _wsgi_post(app, body: bytes, *, path: str = "/v1/execute", query: str = "") -> tuple[str, bytes]:
    status_holder: list[str] = []

    def start_response(status: str, _headers: list) -> None:
        status_holder.append(status)

    environ = {
        "PATH_INFO": path,
        "REQUEST_METHOD": "POST",
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    out = b"".join(app(environ, start_response))
    return status_holder[0], out


class TestPeelExecuteRequest:
    def test_keeps_exact_data_bytes(self) -> None:
        data_literal = b'[1, 2, {"k": "caf\\u00e9"}]'
        body = b'{"id":"r1","code":"result = data","data":' + data_literal + b',"mode":"isolated"}'
        parts = peel_execute_request(body)
        assert parts.req_id == "r1"
        assert parts.code == "result = data"
        assert parts.mode == "isolated"
        assert parts.data_json == data_literal
        assert parts.has_session_id is False

    def test_data_first_key_and_escaped_code(self) -> None:
        body = b'{"data":[[1,2],[3,4]],"code":"result = \\"data\\""}'
        parts = peel_execute_request(body)
        assert parts.data_json == b"[[1,2],[3,4]]"
        assert parts.code == 'result = "data"'

    def test_ignores_data_word_inside_code_string(self) -> None:
        body = b'{"code":"x = \\"data\\"\\nresult = 1","mode":"shared"}'
        parts = peel_execute_request(body)
        assert parts.data_json is None
        assert parts.mode == "shared"

    def test_session_id_in_body_is_flagged(self) -> None:
        parts = peel_execute_request(b'{"code":"result=1","session_id":"nope"}')
        assert parts.has_session_id is True

    def test_data_json_string_field(self) -> None:
        inner = "[10, 20, 30]"
        body = json.dumps({"code": "result = data", "data_json": inner}).encode("utf-8")
        parts = peel_execute_request(body)
        assert parts.data_json == inner.encode("utf-8")

    def test_explicit_data_wins_over_data_json(self) -> None:
        body = b'{"data_json":"[0]","data":[9],"code":"result=1"}'
        parts = peel_execute_request(body)
        assert parts.data_json == b"[9]"

    def test_null_data_is_raw_null(self) -> None:
        parts = peel_execute_request(b'{"code":"result=1","data":null}')
        assert parts.data_json == b"null"

    def test_bom_and_whitespace(self) -> None:
        body = b'\xef\xbb\xbf { "code" : "result = 2" , "timeout_ms" : 1500 } '
        parts = peel_execute_request(body)
        assert parts.code == "result = 2"
        assert parts.timeout_ms == 1500

    def test_rejects_non_object(self) -> None:
        with pytest.raises(ExecuteRequestError):
            peel_execute_request(b"[1,2,3]")

    def test_rejects_trailing_comma(self) -> None:
        with pytest.raises(ExecuteRequestError):
            peel_execute_request(b'{"code":"result=1",}')

    def test_rejects_trailing_junk(self) -> None:
        with pytest.raises(ExecuteRequestError):
            peel_execute_request(b'{"code":"result=1"}{"x":1}')


class TestHttpBlobForward:
    def test_raw_data_bytes_reach_execute_fn(self) -> None:
        seen: dict = {}
        result_json = dumps_response({"status": "ok", "result": 6, "stdout": "", "id": "sum-1"})

        def execute_fn(**kwargs):
            seen.update(kwargs)
            return {"status": "ok", "result_json": result_json}

        app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)
        data_literal = b"[1, 2, 3]"
        body = b'{"id":"sum-1","code":"result = sum(data)","data":' + data_literal + b"}"
        status, out = _wsgi_post(app, body)
        assert status.startswith("200")
        assert seen["data_json"] == data_literal
        assert "data" not in seen
        assert seen.get("decode_result") is False
        assert seen.get("wire") == WIRE_JSON_FORWARD
        # Host forwarded the worker bytes — no second dumps of the result.
        assert out == result_json

    def test_host_json_dumps_not_used_on_result_json(self) -> None:
        result_json = b'{"status":"ok","result":[[1,2],[3,4]],"stdout":""}'

        def execute_fn(**_kwargs):
            return {"status": "ok", "result_json": result_json}

        app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)
        with patch("compute_service.server._json_bytes") as mock_dumps:
            mock_dumps.side_effect = AssertionError("host must not re-dumps a result_json success")
            status, out = _wsgi_post(app, b'{"code":"result = data","data":[[1,2],[3,4]]}')
        assert status.startswith("200")
        assert out == result_json

    def test_mock_dict_result_still_dumps_for_compat(self) -> None:
        app = create_wsgi_app(
            ComputeSettings(),
            execute_fn=lambda **_kw: {"status": "ok", "result": 1, "stdout": ""},
        )
        status, out = _wsgi_post(app, b'{"id":"compat","code":"result = 1"}')
        assert status.startswith("200")
        assert json.loads(out) == {"status": "ok", "result": 1, "stdout": "", "id": "compat"}


class TestFormulaPoolWire:
    def test_json_forward_does_not_host_pack(self) -> None:
        grid = [[float(i), float(i + 1)] for i in range(600)]  # 1200 cells
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            with patch("plugin.scripting.payload_codec.host_pack_data") as mock_pack:
                res = pool.execute(
                    code="result = [len(data), data[-1][-1]]",
                    data=grid,
                    req_id="jf-1",
                    wire=WIRE_JSON_FORWARD,
                )
            assert mock_pack.call_count == 0
            assert res.get("status") == "ok"
            assert res.get("result") == [600, 600.0]
        finally:
            pool.shutdown()

    def test_pickle_wire_still_host_packs_large_grid(self) -> None:
        grid = [[float(i)] for i in range(1000)]
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            with patch("plugin.scripting.payload_codec.host_pack_data", wraps=host_pack_data) as mock_pack:
                res = pool.execute(
                    code="result = len(data)",
                    data=grid,
                    req_id="pk-1",
                    wire=WIRE_PICKLE,
                )
            assert mock_pack.call_count == 1
            assert res.get("status") == "ok"
            assert res.get("result") == 1000
        finally:
            pool.shutdown()

    def test_data_json_bytes_reach_worker_unchanged(self) -> None:
        blob = b'[[1, 2], [3, "caf\xc3\xa9"]]'
        captured: dict = {}
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            real_write = pool.workers[0].execute

            def spy(payload, timeout_sec):
                if isinstance(payload, dict) and "data_json" in payload:
                    captured["data_json"] = payload["data_json"]
                    captured["has_data"] = "data" in payload
                return real_write(payload, timeout_sec)

            with patch.object(pool.workers[0], "execute", side_effect=spy):
                res = pool.execute(
                    code="result = data[1][1]",
                    data_json=blob,
                    req_id="blob-1",
                    wire=WIRE_JSON_FORWARD,
                )
            assert captured["data_json"] == blob
            assert captured["has_data"] is False
            assert res.get("status") == "ok"
            assert res.get("result") == "café"
        finally:
            pool.shutdown()

    def test_decode_result_false_returns_result_json_bytes(self) -> None:
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            raw = pool.execute(
                code="result = [[1, 2], [3, 4]]",
                req_id="raw-1",
                decode_result=False,
            )
            assert isinstance(raw.get("result_json"), (bytes, bytearray))
            parsed = decode_worker_result(raw)
            assert parsed.get("status") == "ok"
            assert parsed.get("result") == [[1, 2], [3, 4]]
            assert parsed.get("id") == "raw-1"
        finally:
            pool.shutdown()

    def test_large_grid_in_and_out(self) -> None:
        rows, cols = 40, 30  # 1200 cells
        grid = [[r * cols + c for c in range(cols)] for r in range(rows)]
        data_json = json.dumps(grid, allow_nan=False).encode("utf-8")
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            with patch("plugin.scripting.payload_codec.host_pack_data") as mock_pack:
                res = pool.execute(
                    code="result = data",
                    data_json=data_json,
                    req_id="big-1",
                    wire=WIRE_JSON_FORWARD,
                    decode_result=False,
                )
            assert mock_pack.call_count == 0
            body = res["result_json"]
            assert isinstance(body, (bytes, bytearray))
            parsed = json.loads(body)
            assert parsed["status"] == "ok"
            assert parsed["result"][0][0] == 0
            assert parsed["result"][-1][-1] == rows * cols - 1
        finally:
            pool.shutdown()


class TestHttpLargeRoundTrip:
    def test_large_data_in_out_no_host_pack_or_result_dumps(self) -> None:
        rows, cols = 25, 40
        grid = [[float(r * cols + c) for c in range(cols)] for r in range(rows)]
        payload = {
            "id": "http-big",
            "code": "result = data",
            "data": grid,
        }
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        app = create_wsgi_app(
            ComputeSettings(host="127.0.0.1", workers=1),
            execute_fn=lambda **kw: pool.execute(**kw),
        )
        try:
            with (
                patch("plugin.scripting.payload_codec.host_pack_data") as mock_pack,
                patch("compute_service.server._json_bytes") as mock_host_dumps,
            ):
                mock_host_dumps.side_effect = AssertionError("host must not dumps the large result")
                status, out = _wsgi_post(app, body)
        finally:
            pool.shutdown()
        assert status.startswith("200"), out
        assert mock_pack.call_count == 0
        parsed = json.loads(out)
        assert parsed["status"] == "ok"
        assert parsed["id"] == "http-big"
        assert parsed["result"][0][0] == 0.0
        assert parsed["result"][-1][-1] == float(rows * cols - 1)
