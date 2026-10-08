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
    ExecuteRequestError,
    decode_worker_result,
    dumps_response,
    MAX_META_BYTES,
    encode_multipart_execute,
    is_multipart_content_type,
    parse_execute_request,
    parse_multipart_execute,
    peel_execute_request,
    validate_session_id,
)
from compute_service.server import create_wsgi_app


@pytest.fixture(autouse=True)
def _cleanup_formula_pool():
    yield
    shutdown_formula_pool()


def _wsgi_post(
    app,
    body: bytes,
    *,
    path: str = "/v1/execute",
    query: str = "",
    content_type: str | None = None,
) -> tuple[str, bytes]:
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
    if content_type is not None:
        environ["CONTENT_TYPE"] = content_type
    out = b"".join(app(environ, start_response))
    return status_holder[0], out


def _manual_multipart(
    parts: list[tuple[str, bytes]],
    *,
    boundary: str = "wa-compute",
    content_type: str | None = None,
    line: str = "\r\n",
    cte: str | None = None,
    extra_header: str = "",
) -> tuple[str, bytes]:
    """Hand-built body for shapes the encoder refuses to emit."""
    nl = line.encode("ascii")
    chunks: list[bytes] = []
    for name, payload in parts:
        cte_line = f"Content-Transfer-Encoding: {cte}{line}" if cte else ""
        head = (
            f"--{boundary}{line}"
            f'Content-Disposition: form-data; name="{name}"{line}'
            f"{extra_header}"
            f"{cte_line}"
            f"{line}"
        )
        chunks.append(head.encode("ascii") + payload + nl)
    chunks.append(f"--{boundary}--{line}".encode("ascii"))
    ctype = content_type or f"multipart/form-data; boundary={boundary}"
    return ctype, b"".join(chunks)


class TestPeelExecuteRequest:

    def test_peel_skips_unknown_keys(self) -> None:
        payload = b'{"id": "1", "junk": [1,2,3], "code": "1"}'
        # Assert behavior: should parse without crashing and return the specified fields
        res = parse_execute_request(payload, None)
        assert res.req_id == "1"
        assert res.code == "1"

    def test_skip_string_invalid_unicode_escape_malformed(self) -> None:
        with pytest.raises(ExecuteRequestError, match="Invalid unicode escape|Unterminated escape"):
            payload = b'{"id": "\\u12",", "code": "1"}'
            parse_execute_request(payload, None)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(b'{"id": [1e9999], "code": "1"}', id="test_nested_nonfinite_id_rejected"),
            pytest.param(b'{"timeout_ms": [1000], "code": "1"}', id="test_non_scalar_timeout_ms_rejected"),
        ],
    )
    def test_nested_nonfinite_id_rejected(self, value) -> None:
        with pytest.raises(ExecuteRequestError):
            payload = value
            parse_execute_request(payload, None)

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

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(b"[1,2,3]", id="test_rejects_non_object"),
            pytest.param(b'{"code":"result=1",}', id="test_rejects_trailing_comma"),
            pytest.param(b'{"code":"result=1"}{"x":1}', id="test_rejects_trailing_junk"),
        ],
    )
    def test_rejects_non_object(self, value) -> None:
        with pytest.raises(ExecuteRequestError):
            peel_execute_request(value)

    def test_overflow_id_is_rejected(self) -> None:
        with pytest.raises(ExecuteRequestError, match="non-finite"):
            peel_execute_request(b'{"id":1e9999,"code":"result = 1"}')


GRID_BYTES = b"[[1,2,3],[4,5,6]]"
# Compact JSON a default json.dumps (spaces after separators) would not emit.
CUSTOM_RESULT_JSON = b'{"status":"ok","result":[[1,2,3],[4,5,6]],"stdout":""}'


class TestMultipartExecute:
    def test_data_part_bytes_are_not_loaded(self) -> None:
        content_type, body = encode_multipart_execute(
            {"id": "p-1", "mode": "isolated"},
            GRID_BYTES,
            code="result = 1",
        )
        with patch("email.parser.BytesParser") as parser:
            parts = parse_multipart_execute(body, content_type)
        assert parser.call_count == 0
        assert parts.req_id == "p-1"
        assert parts.code == b"result = 1"
        assert parts.init_script is None
        assert parts.mode == "isolated"
        assert parts.data_json == GRID_BYTES
        assert parts.has_session_id is False

    def test_meta_must_not_embed_payload_fields(self) -> None:
        for key, value in (
            ("code", "result = 1"),
            ("data", [1, 2]),
            ("data_json", "[1]"),
            ("init_script", "HELPER = 1"),
        ):
            content_type, body = encode_multipart_execute({key: value}, GRID_BYTES, code="result = 1")
            with pytest.raises(ExecuteRequestError, match=key):
                parse_multipart_execute(body, content_type)

    def test_session_id_in_meta_is_flagged(self) -> None:
        content_type, body = encode_multipart_execute(
            {"session_id": "nope"},
            GRID_BYTES,
            code="result = 1",
        )
        parts = parse_multipart_execute(body, content_type)
        assert parts.has_session_id is True
        assert parts.code == b"result = 1"
        assert parts.data_json == GRID_BYTES

    def test_source_parts_round_trip_without_json_unescape(self) -> None:
        code = 'x = "data"\nresult = "café\\n"'
        init = "HELPER = 'data'\n"
        content_type, body = encode_multipart_execute(
            {"id": "src", "timeout_ms": 1500},
            GRID_BYTES,
            code=code,
            init_script=init,
        )
        parts = parse_multipart_execute(body, content_type)
        assert parts.code == code.encode("utf-8")
        assert parts.init_script == init.encode("utf-8")
        assert parts.timeout_ms == 1500
        assert parts.data_json == GRID_BYTES

    def test_parts_are_accepted_in_any_order(self) -> None:
        code = b'result = "data"'
        init = b"HELPER = 1\n"
        content_type, body = _manual_multipart(
            [
                ("data", GRID_BYTES),
                ("init_script", init),
                ("code", code),
                ("meta", b'{"id":"ord","mode":"shared"}'),
            ]
        )
        parts = parse_multipart_execute(body, content_type)
        assert parts.req_id == "ord"
        assert parts.mode == "shared"
        assert parts.code == code
        assert parts.init_script == init
        assert parts.data_json == GRID_BYTES

    def test_encoder_avoids_boundary_that_appears_in_source(self) -> None:
        source = "result = 1\r\n--wa-compute\r\nresult = 2"
        content_type, body = encode_multipart_execute({}, code=source)
        assert content_type.split("boundary=", 1)[1] != "wa-compute"
        parts = parse_multipart_execute(body, content_type)
        assert parts.code == source.encode("utf-8")

    def test_encoder_avoids_boundary_that_appears_in_data(self) -> None:
        data = b"[1]\r\n--wa-compute\r\n[2]"
        content_type, body = encode_multipart_execute({}, data, code="result = 1")
        assert content_type.split("boundary=", 1)[1] != "wa-compute"
        parts = parse_multipart_execute(body, content_type)
        assert parts.data_json == data

    def test_rfc2046_empty_part_is_empty_bytes(self) -> None:
        """headers\\r\\n\\r\\n--boundary is an empty part, not a malformed body.

        The CRLF before --boundary belongs to the delimiter. On an empty
        part that CRLF is also the blank line that ended the headers.
        Treating it only as the delimiter used to reject the part.
        """
        nl = b"\r\n"
        body = (
            b"--wa-compute"
            + nl
            + b'Content-Disposition: form-data; name="meta"'
            + nl
            + nl
            + b"{}"
            + nl
            + b"--wa-compute"
            + nl
            + b'Content-Disposition: form-data; name="code"'
            + nl
            + nl
            + b"--wa-compute"
            + nl
            + b'Content-Disposition: form-data; name="init_script"'
            + nl
            + nl
            + b"--wa-compute--"
            + nl
        )
        parts = parse_multipart_execute(body, "multipart/form-data; boundary=wa-compute")
        assert parts.code == b""
        assert parts.init_script == b""
        assert parts.req_id is None

        lf = body.replace(b"\r\n", b"\n")
        lf_parts = parse_multipart_execute(lf, "multipart/form-data; boundary=wa-compute")
        assert lf_parts.code == b""
        assert lf_parts.init_script == b""

    def test_part_body_keeps_its_own_crlf(self) -> None:
        """Two CRLFs before the boundary: the first belongs to the body."""
        nl = b"\r\n"
        body = (
            b"--wa-compute"
            + nl
            + b'Content-Disposition: form-data; name="meta"'
            + nl
            + nl
            + b"{}"
            + nl
            + b"--wa-compute"
            + nl
            + b'Content-Disposition: form-data; name="code"'
            + nl
            + nl
            + b"\r\n\r\n"
            + b"--wa-compute--"
            + nl
        )
        parts = parse_multipart_execute(body, "multipart/form-data; boundary=wa-compute")
        assert parts.code == b"\r\n"

    def test_unterminated_multipart_is_rejected(self) -> None:
        _content_type, body = _manual_multipart([("meta", b"{}"), ("code", b"result = 1")])
        close = b"--wa-compute--\r\n"
        assert body.endswith(close)
        with pytest.raises(ExecuteRequestError, match="unterminated"):
            parse_multipart_execute(body[: -len(close)], "multipart/form-data; boundary=wa-compute")

    def test_lf_body_and_quoted_boundary(self) -> None:
        code = b"result = 1"
        content_type, body = _manual_multipart(
            [
                ("meta", b'{"id":"lf"}'),
                ("code", code),
                ("data", b"[1]"),
            ],
            boundary="wa compute",
            content_type='multipart/form-data; boundary="wa compute"',
            line="\n",
        )
        body = b"preamble\n" + body + b"epilogue"
        parts = parse_multipart_execute(body, content_type)
        assert parts.req_id == "lf"
        assert parts.code == code
        assert parts.data_json == b"[1]"

    def test_folded_disposition_name(self) -> None:
        nl = b"\r\n"
        body = (
            b"--wa-compute" + nl
            + b'Content-Disposition: form-data;' + nl
            + b' name="code"' + nl
            + nl
            + b"result = 1" + nl
            + b"--wa-compute" + nl
            + b'Content-Disposition: form-data; name="meta"' + nl
            + nl
            + b"{}" + nl
            + b"--wa-compute--" + nl
        )
        parts = parse_multipart_execute(body, "multipart/form-data; boundary=wa-compute")
        assert parts.code == b"result = 1"
        assert parts.req_id is None

    def test_oversize_meta_is_not_json_loaded(self) -> None:
        prefix = b'{"id":"'
        suffix = b'"}'
        pad = MAX_META_BYTES - len(prefix) - len(suffix)
        exact = prefix + (b"a" * pad) + suffix
        assert len(exact) == MAX_META_BYTES
        content_type, body = _manual_multipart([("meta", exact), ("code", b"result = 1")])
        parts = parse_multipart_execute(body, content_type)
        assert parts.req_id == "a" * pad

        over = prefix + (b"a" * (pad + 1)) + suffix
        content_type, body = _manual_multipart([("meta", over), ("code", b"result = 1")])

        def spy(value: object, *args: object, **kwargs: object) -> object:
            raise AssertionError(value)

        with patch("compute_service.json_forward.json.loads", side_effect=spy):
            with pytest.raises(ExecuteRequestError, match="meta part exceeds"):
                parse_multipart_execute(body, content_type)

    def test_missing_meta_duplicate_unknown_and_base64_are_rejected(self) -> None:
        missing_meta = _manual_multipart([("code", b"result = 1")])[1]
        with pytest.raises(ExecuteRequestError, match="meta"):
            parse_multipart_execute(missing_meta, "multipart/form-data; boundary=wa-compute")

        duplicate = _manual_multipart(
            [("meta", b"{}"), ("code", b"result = 1"), ("code", b"result = 2")]
        )[1]
        with pytest.raises(ExecuteRequestError, match="duplicate"):
            parse_multipart_execute(duplicate, "multipart/form-data; boundary=wa-compute")

        unknown = _manual_multipart([("meta", b"{}"), ("notes", b"x"), ("code", b"result = 1")])[1]
        with pytest.raises(ExecuteRequestError, match="unknown"):
            parse_multipart_execute(unknown, "multipart/form-data; boundary=wa-compute")

        content_type, encoded = _manual_multipart(
            [("meta", b"{}"), ("code", b"result = 1")],
            cte="base64",
        )
        with pytest.raises(ExecuteRequestError, match="content-transfer-encoding"):
            parse_multipart_execute(encoded, content_type)

    def test_mime_dispatch(self) -> None:
        content_type, body = encode_multipart_execute({}, GRID_BYTES, code="result = 1")
        assert is_multipart_content_type(content_type)
        assert not is_multipart_content_type("application/json")
        assert not is_multipart_content_type(None)
        assert not is_multipart_content_type("")
        mp = parse_execute_request(body, content_type)
        assert mp.data_json == GRID_BYTES
        peeled = parse_execute_request(b'{"code":"result = 1","data":[9]}', "application/json")
        assert peeled.data_json == b"[9]"
        peeled_default = parse_execute_request(b'{"code":"result = 1"}', None)
        assert peeled_default.data_json is None
        # Same bytes under application/json must not take the multipart parser.
        with pytest.raises(ExecuteRequestError):
            parse_execute_request(body, "application/json")


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
        with patch("compute_service.server._start_json") as mock_dumps:
            mock_dumps.side_effect = AssertionError("host must not re-dumps a result_json success")
            status, out = _wsgi_post(app, b'{"code":"result = data","data":[[1,2],[3,4]]}')
        assert status.startswith("200")
        assert out == result_json

    def test_multipart_forwards_data_bytes_and_result_json(self) -> None:
        seen: dict = {}

        def execute_fn(**kwargs):
            seen.update(kwargs)
            return {"status": "ok", "result_json": CUSTOM_RESULT_JSON}

        app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)
        content_type, body = encode_multipart_execute(
            {"id": "m-1"},
            GRID_BYTES,
            code="result = float(np.sum(data))",
        )
        status, out = _wsgi_post(app, body, content_type=content_type)
        assert status.startswith("200")
        assert seen["code"] == "result = float(np.sum(data))"
        assert seen["init_script"] is None
        assert seen["data_json"] == GRID_BYTES
        assert "data" not in seen
        assert seen.get("decode_result") is False
        assert seen.get("wire") == WIRE_JSON_FORWARD
        assert out == CUSTOM_RESULT_JSON

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

        def execute_fn(**_kwargs):
            return {"status": "ok", "result_json": CUSTOM_RESULT_JSON}

        app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)
        code = 'CODE_SENTINEL = "data"'
        init = "INIT_SENTINEL = 'data'"
        content_type, body = encode_multipart_execute(
            {},
            GRID_BYTES,
            code=code,
            init_script=init,
        )
        with patch("compute_service.json_forward.json.loads", side_effect=spy):
            status, out = _wsgi_post(app, body, content_type=content_type)
        assert status.startswith("200")
        assert out == CUSTOM_RESULT_JSON
        assert GRID_BYTES.decode("utf-8") not in loaded
        assert not any(item.lstrip().startswith("[[1,2,3]") for item in loaded)
        assert not any(code in item or init in item for item in loaded)

    def test_multipart_host_does_not_dumps_result(self) -> None:
        def execute_fn(**_kwargs):
            return {"status": "ok", "result_json": CUSTOM_RESULT_JSON}

        app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)
        content_type, body = encode_multipart_execute({}, GRID_BYTES, code="result = 1")
        with patch("compute_service.server._start_json") as mock_dumps:
            mock_dumps.side_effect = AssertionError("host must not re-dumps a result_json success")
            status, out = _wsgi_post(app, body, content_type=content_type)
        assert status.startswith("200")
        assert out == CUSTOM_RESULT_JSON

    def test_bad_multipart_is_400(self) -> None:
        app = create_wsgi_app(
            ComputeSettings(),
            execute_fn=lambda **_kw: {"status": "ok", "result": 1, "stdout": ""},
        )
        status, out = _wsgi_post(
            app,
            b"not-actually-multipart",
            content_type="multipart/form-data; boundary=wa-compute",
        )
        assert status.startswith("400")
        assert json.loads(out)["error"] == "Invalid multipart execute body"

    def test_multipart_source_reaches_execute_as_text(self) -> None:
        seen: dict = {}
        code = 'x = "data"\nresult = "café\\n"'
        init = "HELPER = 'data'\n"

        def execute_fn(**kwargs):
            seen.update(kwargs)
            return {"status": "ok", "result_json": CUSTOM_RESULT_JSON}

        app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)
        content_type, body = encode_multipart_execute(
            {"id": "src"},
            GRID_BYTES,
            code=code,
            init_script=init,
        )
        status, out = _wsgi_post(app, body, content_type=content_type)
        assert status.startswith("200")
        assert out == CUSTOM_RESULT_JSON
        assert seen["code"] == code
        assert seen["init_script"] == init
        assert seen["data_json"] == GRID_BYTES

    def test_empty_init_part_becomes_none(self) -> None:
        seen: dict = {}

        def execute_fn(**kwargs):
            seen.update(kwargs)
            return {"status": "ok", "result_json": CUSTOM_RESULT_JSON}

        app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)
        content_type, body = _manual_multipart(
            [("meta", b'{"id":"e"}'), ("code", b"result = 1"), ("init_script", b"")]
        )
        status, _out = _wsgi_post(app, body, content_type=content_type)
        assert status.startswith("200")
        assert seen["init_script"] is None
        assert seen["code"] == "result = 1"

    def test_missing_code_part_is_400(self) -> None:
        def execute_fn(**_kwargs):
            raise AssertionError("missing code must not lease a worker")

        app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)
        content_type, body = _manual_multipart([("meta", b'{"id":"c1"}')])
        status, out = _wsgi_post(app, body, content_type=content_type)
        assert status.startswith("400")
        payload = json.loads(out)
        assert payload["error"] == "Missing 'code' string parameter."
        assert payload["id"] == "c1"

    def test_missing_meta_part_is_400(self) -> None:
        def execute_fn(**_kwargs):
            raise AssertionError("missing meta must not lease a worker")

        app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)
        content_type, body = _manual_multipart([("code", b"result = 1")])
        status, out = _wsgi_post(app, body, content_type=content_type)
        assert status.startswith("400")
        assert json.loads(out)["error"] == "Invalid multipart execute body"

    def test_source_byte_cap(self) -> None:
        def execute_fn(**_kwargs):
            raise AssertionError("oversize source must not lease a worker")

        app = create_wsgi_app(ComputeSettings(max_code_chars=64), execute_fn=execute_fn)
        content_type, body = encode_multipart_execute({"id": "big"}, code="a" * 65)
        status, out = _wsgi_post(app, body, content_type=content_type)
        assert status.startswith("400")
        payload = json.loads(out)
        assert payload["code"] == "CODE_TOO_LARGE"
        assert payload["error"].startswith("code exceeds")
        assert payload["id"] == "big"

        content_type, body = encode_multipart_execute(
            {"id": "big-init"},
            code="result = 1",
            init_script="b" * 65,
        )
        status, out = _wsgi_post(app, body, content_type=content_type)
        assert status.startswith("400")
        payload = json.loads(out)
        assert payload["code"] == "CODE_TOO_LARGE"
        assert payload["error"].startswith("init_script exceeds")

        seen: dict = {}

        def ok_execute(**kwargs):
            seen.update(kwargs)
            return {"status": "ok", "result_json": CUSTOM_RESULT_JSON}

        app_ok = create_wsgi_app(ComputeSettings(max_code_chars=64), execute_fn=ok_execute)
        content_type, body = encode_multipart_execute({}, code="c" * 64)
        status, _out = _wsgi_post(app_ok, body, content_type=content_type)
        assert status.startswith("200")
        assert seen["code"] == "c" * 64

    def test_meta_nonfinite_is_400(self) -> None:
        def execute_fn(**_kwargs):
            raise AssertionError("NaN/Infinity meta must not run")

        app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)
        for meta in (b'{"timeout_ms": Infinity}', b'{"id": NaN}', b'{"id":1e9999}', b'{"timeout_ms":1e9999}'):
            content_type, body = _manual_multipart([("meta", meta), ("code", b"result = 1")])
            status, out = _wsgi_post(app, body, content_type=content_type)
            assert status.startswith("400"), out
            assert json.loads(out)["error"] == "Invalid multipart execute body"

    def test_invalid_utf8_code_is_400(self) -> None:
        def execute_fn(**_kwargs):
            raise AssertionError("invalid utf-8 must not lease a worker")

        app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)
        content_type, body = _manual_multipart([("meta", b"{}"), ("code", b"\xff")])
        status, out = _wsgi_post(app, body, content_type=content_type)
        assert status.startswith("400")
        assert json.loads(out)["error"] == "Invalid UTF-8 in code part."

    def test_session_id_in_meta_uses_request_body_wording(self) -> None:
        def execute_fn(**_kwargs):
            raise AssertionError("session_id in meta must not lease a worker")

        app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)
        content_type, body = encode_multipart_execute(
            {"id": "sid", "session_id": "nope"},
            code="result = 1",
        )
        status, out = _wsgi_post(app, body, content_type=content_type)
        assert status.startswith("400")
        payload = json.loads(out)
        assert "not in the request body" in payload["error"]
        assert payload["id"] == "sid"

    def test_json_content_type_still_peels(self) -> None:
        seen: dict = {}
        result_json = dumps_response({"status": "ok", "result": 6, "stdout": ""})

        def execute_fn(**kwargs):
            seen.update(kwargs)
            return {"status": "ok", "result_json": result_json}

        app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)
        data_literal = b"[1, 2, 3]"
        body = b'{"code":"result = sum(data)","data":' + data_literal + b"}"
        status, out = _wsgi_post(app, body, content_type="application/json")
        assert status.startswith("200")
        assert seen["data_json"] == data_literal
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
        # 40×30 = 1200 cells (above compute pickle min_cells=1000) but under
        # DEAL_MAX_SHAPE_DIM so materialize_inputs' list-of-grids pre still holds.
        grid = [[float(r * 30 + c) for c in range(30)] for r in range(40)]
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            with patch("plugin.scripting.payload_codec.host_pack_data") as mock_pack:
                res = pool.execute(
                    code="result = [len(data.values), data.values[-1][-1]]",
                    data=grid,
                    req_id="jf-1",
                    wire=WIRE_JSON_FORWARD,
                )
            assert mock_pack.call_count == 0
            assert res.get("status") == "ok", res
            assert res.get("result") == [40, 1199.0]
        finally:
            pool.shutdown()

    def test_pickle_wire_is_rejected_not_rewritten(self) -> None:
        """``wire=pickle`` used to host_pack the grid and run the cell.

        Rewriting it to json_forward, or packing ``data`` as a second payload,
        accepts a request the JSON wire does not.
        """
        grid = [[float(r * 30 + c) for c in range(30)] for r in range(40)]
        with patch("plugin.scripting.payload_codec.host_pack_data") as mock_pack:
            with pytest.raises(ExecuteRequestError, match="wire"):
                FormulaProcessPool._build_execute_payload(
                    code="result = len(data)",
                    data=grid,
                    wire="pickle",
                )
        assert mock_pack.call_count == 0

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


class TestPeelAndMultipartAgree:
    """One logical execute must not be accepted on one ingress and rejected or rewritten on the other."""

    def _run(self, body: bytes, content_type: str | None, *, limit: int) -> tuple[str, dict, bytes]:
        seen: dict = {}

        def execute_fn(**kwargs):
            seen.update(kwargs)
            return {"status": "ok", "result_json": dumps_response({"status": "ok", "result": 1, "stdout": ""})}

        app = create_wsgi_app(ComputeSettings(max_code_chars=limit), execute_fn=execute_fn)
        status, out = _wsgi_post(app, body, content_type=content_type)
        return status, seen, out

    def test_multibyte_source_at_cap_matches(self) -> None:
        # 64 code points, 128 UTF-8 bytes. Counting bytes against max_code_chars
        # accepts the peel string and rejects the multipart part.
        limit = 64
        code = "α" * limit
        init = "β" * limit
        data = b"[[1,2],[3,4]]"
        peel = b'{"id":"agree","code":' + json.dumps(code).encode() + b',"init_script":' + json.dumps(init).encode() + b',"mode":"isolated","data":' + data + b"}"
        content_type, multipart = encode_multipart_execute({"id": "agree", "mode": "isolated"}, data, code=code, init_script=init)
        status_p, seen_p, _out_p = self._run(peel, "application/json", limit=limit)
        status_m, seen_m, _out_m = self._run(multipart, content_type, limit=limit)
        assert status_p.startswith("200"), _out_p
        assert status_m.startswith("200"), _out_m
        for key in ("code", "init_script", "mode", "data_json", "wire", "session_id"):
            assert seen_p[key] == seen_m[key]
        assert seen_p["code"] == code
        assert seen_p["init_script"] == init
        assert seen_p["data_json"] == data
        assert seen_p["wire"] == WIRE_JSON_FORWARD
        assert seen_p["mode"] == "isolated"

    def test_over_cap_and_bad_mode_rejected_on_both(self) -> None:
        limit = 64
        code = "α" * (limit + 1)

        def _assert_rejected(body: bytes, content_type: str | None) -> None:
            status, seen, out = self._run(body, content_type, limit=limit)
            assert status.startswith("400"), out
            assert seen == {}
            payload = json.loads(out)
            assert payload.get("code") == "CODE_TOO_LARGE"

        peel = json.dumps({"id": "big", "code": code}).encode()
        content_type, multipart = encode_multipart_execute({"id": "big"}, code=code)
        _assert_rejected(peel, "application/json")
        _assert_rejected(multipart, content_type)

        for mode in (False, 0, "Shared"):
            peel_mode = json.dumps({"id": "mode", "code": "result = 1", "mode": mode}).encode()
            mp_type, mp_body = encode_multipart_execute({"id": "mode", "mode": mode}, code="result = 1")
            for body, ctype in ((peel_mode, "application/json"), (mp_body, mp_type)):
                status, seen, out = self._run(body, ctype, limit=64)
                assert status.startswith("400"), (mode, ctype, out)
                assert seen == {}
                assert "mode" in json.loads(out).get("error", "")

    def test_empty_init_script_is_absent_on_both(self) -> None:
        peel = json.dumps({"id": "e", "code": "result = 1", "init_script": ""}).encode()
        content_type, multipart = encode_multipart_execute({"id": "e"}, code="result = 1", init_script="")
        status_p, seen_p, out_p = self._run(peel, "application/json", limit=64)
        status_m, seen_m, out_m = self._run(multipart, content_type, limit=64)
        assert status_p.startswith("200"), out_p
        assert status_m.startswith("200"), out_m
        assert seen_p["init_script"] is None
        assert seen_m["init_script"] is None

    def test_non_text_init_script_is_not_dropped(self) -> None:
        """A peel number used to be ignored and the cell ran with no init script."""

        def execute_fn(**_kwargs):
            raise AssertionError("non-text init_script must not run")

        app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)
        body = json.dumps({"id": "n", "code": "result = 1", "init_script": 123}).encode()
        status, out = _wsgi_post(app, body, content_type="application/json")
        assert status.startswith("400")
        payload = json.loads(out)
        assert payload["id"] == "n"
        assert "init_script" in payload["error"]


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
                patch("compute_service.server._start_json") as mock_host_dumps,
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

    def test_multipart_large_data_in_out_no_host_pack_or_result_dumps(self) -> None:
        rows, cols = 25, 40
        grid = [[float(r * cols + c) for c in range(cols)] for r in range(rows)]
        data_json = json.dumps(grid, allow_nan=False).encode("utf-8")
        content_type, body = encode_multipart_execute(
            {"id": "http-mp-big"},
            data_json,
            code="result = data",
        )
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        app = create_wsgi_app(
            ComputeSettings(host="127.0.0.1", workers=1),
            execute_fn=lambda **kw: pool.execute(**kw),
        )
        try:
            with (
                patch("plugin.scripting.payload_codec.host_pack_data") as mock_pack,
                patch("compute_service.server._start_json") as mock_host_dumps,
            ):
                mock_host_dumps.side_effect = AssertionError("host must not dumps the large result")
                status, out = _wsgi_post(app, body, content_type=content_type)
        finally:
            pool.shutdown()
        assert status.startswith("200"), out
        assert mock_pack.call_count == 0
        parsed = json.loads(out)
        assert parsed["status"] == "ok"
        assert parsed["id"] == "http-mp-big"
        assert parsed["result"][0][0] == 0.0
        assert parsed["result"][-1][-1] == float(rows * cols - 1)


def test_validate_session_id() -> None:
    """validate_session_id accepts valid session IDs and rejects non-strings, blanks, and reserved namespaces."""
    assert validate_session_id("my_session") == "my_session"
    assert validate_session_id("  session_123  ") == "session_123"

    for invalid_val in (None, "", "   ", 123, [], {}):
        with pytest.raises(ExecuteRequestError) as exc_info:
            validate_session_id(invalid_val)
        assert exc_info.value.code == "INVALID_SESSION_ID"

    for reserved in ("sess:init", "isolated:abc", "foo:init", "isolated:"):
        with pytest.raises(ExecuteRequestError) as exc_info:
            validate_session_id(reserved)
        assert exc_info.value.code == "INVALID_SESSION_ID"

