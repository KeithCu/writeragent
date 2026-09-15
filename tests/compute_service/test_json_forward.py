# WriterAgent - Python Compute Service JSON blob-forward tests
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from compute_service.config import ComputeSettings
from compute_service.json_forward import (
    HTTP_JSON_KEY,
    ExecuteRequestParseError,
    dump_worker_http_json,
    encode_multipart_execute,
    extract_http_json,
    merge_worker_response,
    parse_execute_request,
    parse_json_execute_fallback,
    parse_multipart_execute,
)
from compute_service.server import create_wsgi_app


GRID_BYTES = b"[[1,2,3],[4,5,6]]"
# Compact JSON a default json.dumps (spaces after separators) would not emit.
CUSTOM_HTTP_JSON = b'{"status":"ok","result":[[1,2,3],[4,5,6]],"stdout":""}'


def _wsgi_call(
    app,
    body: bytes,
    content_type: str,
    *,
    path: str = "/v1/execute",
) -> tuple[str, list[tuple[str, str]], bytes]:
    status_holder: list[str] = []
    headers_holder: list[list[tuple[str, str]]] = []

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        status_holder.append(status)
        headers_holder.append(headers)

    environ = {
        "PATH_INFO": path,
        "REQUEST_METHOD": "POST",
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    raw = b"".join(app(environ, start_response))
    return status_holder[0], headers_holder[0], raw


class TestMultipartPeel:
    def test_data_part_bytes_are_not_loaded(self) -> None:
        content_type, body = encode_multipart_execute(
            {"id": "p-1", "code": "result = 1", "mode": "isolated"},
            GRID_BYTES,
        )
        parts = parse_multipart_execute(body, content_type)
        assert parts.multipart is True
        assert parts.meta["code"] == "result = 1"
        assert parts.meta["id"] == "p-1"
        assert "data" not in parts.meta
        assert parts.data_json == GRID_BYTES

    def test_multipart_mixed_unnamed_parts(self) -> None:
        boundary = "wa-mixed"
        meta = json.dumps({"code": "result = 2"}).encode("utf-8")
        body = (
            f"--{boundary}\r\nContent-Type: application/json\r\n\r\n".encode("ascii")
            + meta
            + f"\r\n--{boundary}\r\nContent-Type: application/json\r\n\r\n".encode("ascii")
            + GRID_BYTES
            + f"\r\n--{boundary}--\r\n".encode("ascii")
        )
        parts = parse_multipart_execute(body, f"multipart/mixed; boundary={boundary}")
        assert parts.meta["code"] == "result = 2"
        assert parts.data_json == GRID_BYTES

    def test_meta_must_not_embed_data(self) -> None:
        content_type, body = encode_multipart_execute(
            {"code": "result = 1", "data": [1, 2]},
            GRID_BYTES,
        )
        with pytest.raises(ExecuteRequestParseError, match="data"):
            parse_multipart_execute(body, content_type)

    def test_json_fallback_reencodes_nested_data(self) -> None:
        body = json.dumps({"code": "result = 1", "data": [[1, 2], [3, 4]]}).encode("utf-8")
        parts = parse_json_execute_fallback(body)
        assert parts.multipart is False
        assert parts.meta == {"code": "result = 1"}
        assert parts.data_json is not None
        assert json.loads(parts.data_json) == [[1, 2], [3, 4]]

    def test_dispatch_uses_content_type(self) -> None:
        content_type, body = encode_multipart_execute({"code": "result = 1"}, GRID_BYTES)
        parts = parse_execute_request(body, content_type)
        assert parts.multipart and parts.data_json == GRID_BYTES
        fallback = parse_execute_request(b'{"code":"result = 1"}', "application/json")
        assert fallback.multipart is False
        assert fallback.data_json is None


class TestHttpJsonEnvelope:
    def test_dump_then_extract_roundtrip(self) -> None:
        dumped = dump_worker_http_json({"id": "r1", "status": "ok", "result": [[1]], "stdout": ""})
        assert dumped["status"] == "ok"
        assert "result" not in dumped
        raw = extract_http_json(dumped)
        assert raw is not None
        assert json.loads(raw)["result"] == [[1]]

    def test_merge_keeps_exact_bytes(self) -> None:
        worker = {"status": "ok", HTTP_JSON_KEY: CUSTOM_HTTP_JSON}
        merged = merge_worker_response(worker, "req-9")
        assert merged["id"] == "req-9"
        assert merged["result"] == [[1, 2, 3], [4, 5, 6]]
        assert merged[HTTP_JSON_KEY] == CUSTOM_HTTP_JSON


class TestWsgiBlobForward:
    def test_multipart_forwards_data_bytes_and_http_json(self) -> None:
        seen: dict[str, object] = {}

        def fake_execute(**kwargs: object) -> dict[str, object]:
            seen.update(kwargs)
            return {"status": "ok", HTTP_JSON_KEY: CUSTOM_HTTP_JSON}

        app = create_wsgi_app(ComputeSettings(), execute_fn=fake_execute)
        content_type, body = encode_multipart_execute(
            {"id": "m-1", "code": "result = float(np.sum(data))"},
            GRID_BYTES,
        )
        status, _headers, raw = _wsgi_call(app, body, content_type)
        assert status.startswith("200")
        assert seen.get("code") == "result = float(np.sum(data))"
        assert seen.get("data_json") == GRID_BYTES
        assert "data" not in seen or seen.get("data") is None
        # Identity: host must not json.dumps the result (default dumps inserts spaces).
        assert raw == CUSTOM_HTTP_JSON

    def test_multipart_host_does_not_json_loads_grid(self) -> None:
        real_loads = json.loads
        loaded: list[str] = []

        def spy(value: object, *args: object, **kwargs: object) -> object:
            if isinstance(value, (bytes, bytearray)):
                text = bytes(value).decode("utf-8")
            elif isinstance(value, str):
                text = value
            else:
                text = ""
            loaded.append(text)
            return real_loads(value, *args, **kwargs)

        def fake_execute(**kwargs: object) -> dict[str, object]:
            return {"status": "ok", "result": 1, "stdout": ""}

        app = create_wsgi_app(ComputeSettings(), execute_fn=fake_execute)
        content_type, body = encode_multipart_execute(
            {"code": "result = 1"},
            GRID_BYTES,
        )
        with patch("compute_service.json_forward.json.loads", side_effect=spy):
            status, _headers, raw = _wsgi_call(app, body, content_type)
        assert status.startswith("200")
        assert json.loads(raw)["result"] == 1
        assert GRID_BYTES.decode("utf-8") not in loaded
        assert not any(item.lstrip().startswith("[[1,2,3]") for item in loaded)

    def test_json_fallback_still_executes(self) -> None:
        seen: dict[str, object] = {}

        def fake_execute(**kwargs: object) -> dict[str, object]:
            seen.update(kwargs)
            return {"status": "ok", "result": 25.0, "stdout": ""}

        app = create_wsgi_app(ComputeSettings(), execute_fn=fake_execute)
        body = json.dumps(
            {"code": "result = float(np.mean(data))", "data": [10, 20, 30, 40]}
        ).encode("utf-8")
        status, _headers, raw = _wsgi_call(app, body, "application/json")
        assert status.startswith("200")
        assert json.loads(raw)["result"] == 25.0
        assert seen.get("data_json") == json.dumps([10, 20, 30, 40]).encode("utf-8")

    def test_host_pack_not_used_on_multipart(self) -> None:
        def fake_execute(**kwargs: object) -> dict[str, object]:
            return {"status": "ok", HTTP_JSON_KEY: CUSTOM_HTTP_JSON}

        app = create_wsgi_app(ComputeSettings(), execute_fn=fake_execute)
        content_type, body = encode_multipart_execute({"code": "result = 1"}, GRID_BYTES)
        with patch(
            "plugin.scripting.payload_codec.host_pack_data",
            side_effect=AssertionError("host_pack_data must not run on compute HTTP"),
        ):
            status, _headers, raw = _wsgi_call(app, body, content_type)
        assert status.startswith("200")
        assert raw == CUSTOM_HTTP_JSON


class TestFormulaPoolDataJson:
    def test_raw_data_json_reaches_worker(self) -> None:
        from compute_service.formula_pool import FormulaProcessPool

        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            with patch(
                "plugin.scripting.payload_codec.host_pack_data",
                side_effect=AssertionError("split_grid pack is LibrePy-only"),
            ):
                res = pool.execute(
                    code="import numpy as np\nresult = float(np.sum(data))",
                    data_json=GRID_BYTES,
                    req_id="dj-1",
                )
            assert res.get("id") == "dj-1"
            assert res.get("status") == "ok"
            assert res.get("result") == 21.0
            assert extract_http_json(res) is not None
        finally:
            pool.shutdown()
