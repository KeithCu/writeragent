# WriterAgent - Python Compute Service
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compute-only JSON blob forward helpers.

LibrePy desktop ``=PY()`` keeps Pickle5 + ``split_grid``. The HTTP compute
service is a thinner proxy: peel small control fields from today's single
JSON object (or from an optional multipart ``meta`` part), forward the raw
``data`` JSON bytes to the formula worker, and forward the worker's
``result_json`` bytes back to coolwsd — no host ``json.loads`` of the grid,
no ``host_pack_data``, no second ``json.dumps`` of the result.

Worker stdio still uses the existing length-prefixed Pickle5 envelope so we do
not add a second IPC protocol. Large payloads travel as ``bytes`` fields
(``data_json`` / ``result_json``); pickle copies those buffers, it does not
re-encode the JSON tree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from typing import Any

# HTTP max_body_bytes is 32 MiB; pickle of the envelope needs a little slack.
COMPUTE_MAX_PAYLOAD_BYTES = 33 * 1024 * 1024

WIRE_JSON_FORWARD = "json_forward"
WIRE_PICKLE = "pickle"

# Peel-walker only (single-JSON ingress). Transitional Collabora contract;
# delete with peel_execute_request after kit ships multipart.
_WS = frozenset({0x09, 0x0A, 0x0D, 0x20})
_MAX_JSON_DEPTH = 256
_BOM = b"\xef\xbb\xbf"


class ExecuteRequestError(ValueError):
    """Raised when the HTTP body is not a peelable JSON object or multipart kit body."""


@dataclass(frozen=True)
class ExecuteRequestParts:
    """Small decoded fields plus the untouched ``data`` JSON value bytes."""

    req_id: Any
    code: Any
    mode: Any
    timeout_ms: Any
    init_script: Any
    data_json: bytes | None
    has_session_id: bool


def dumps_response(payload: dict[str, Any]) -> bytes:
    """Encode one kit-safe execute response. Worker is the only dumps site."""
    return json.dumps(payload, allow_nan=False).encode("utf-8")


def decode_worker_result(res: dict[str, Any]) -> dict[str, Any]:
    """Materialize ``result_json`` for pool tests / callers that want a dict.

    The HTTP server must not use this on the success path — it forwards the
    raw bytes instead of re-dumping.
    """
    raw = res.get("result_json")
    if isinstance(raw, (bytes, bytearray)):
        parsed = json.loads(bytes(raw).decode("utf-8"))
        if isinstance(parsed, dict):
            return parsed
    return res


def is_multipart_content_type(content_type: str | None) -> bool:
    """True when the kit opted into the optional multipart ingress."""
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower().startswith("multipart/")


def encode_multipart_execute(
    meta: dict[str, Any],
    data_json: bytes | None = None,
    *,
    boundary: str = "wa-compute",
) -> tuple[str, bytes]:
    """Build optional kit multipart. Returns ``(Content-Type, body bytes)``.

    ``meta`` is small control JSON only — do not nest the grid there.
    """

    def _part(name: str, payload: bytes) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n'
            f"Content-Type: application/json\r\n"
            f"Content-Transfer-Encoding: 8bit\r\n"
            f"\r\n"
        ).encode("ascii") + payload + b"\r\n"

    chunks = [_part("meta", json.dumps(meta, allow_nan=False).encode("utf-8"))]
    if data_json is not None:
        chunks.append(_part("data", data_json))
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def _part_payload_bytes(part: Any) -> bytes:
    payload = part.get_payload(decode=True)
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    text = part.get_payload(decode=False)
    if isinstance(text, (bytes, bytearray)):
        return bytes(text)
    if isinstance(text, str):
        return text.encode("utf-8")
    raise ExecuteRequestError("multipart part has no payload")


def parse_multipart_execute(body: bytes, content_type: str) -> ExecuteRequestParts:
    """Optional kit ingress: peel ``meta`` with the same helper; keep Part B raw.

    Same ``ExecuteRequestParts`` as ``peel_execute_request``. The host never
    ``json.loads`` the ``data`` part. ``meta`` is the #766 peel over a small
    object (no nested grid).
    """
    if not is_multipart_content_type(content_type):
        raise ExecuteRequestError("Content-Type is not multipart")
    raw = b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("utf-8") + b"\r\n\r\n" + body
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    if not msg.is_multipart():
        raise ExecuteRequestError("expected a multipart body")

    named: dict[str, bytes] = {}
    ordered: list[bytes] = []
    for part in msg.iter_parts():
        payload = _part_payload_bytes(part)
        ordered.append(payload)
        disp_name = part.get_param("name", header="content-disposition")
        if isinstance(disp_name, str) and disp_name:
            named[disp_name] = payload

    meta_bytes: bytes | None = None
    for key in ("meta", "metadata"):
        if key in named:
            meta_bytes = named[key]
            break
    data_bytes: bytes | None = named.get("data")

    # multipart/mixed with unnamed parts: first = meta, second = data.
    if meta_bytes is None:
        if not ordered:
            raise ExecuteRequestError("missing meta part")
        meta_bytes = ordered[0]
        if data_bytes is None and len(ordered) >= 2:
            data_bytes = ordered[1]

    # Reuse #766 peel on the small meta object — not a second metadata codec.
    meta_parts = peel_execute_request(meta_bytes)
    if meta_parts.data_json is not None:
        raise ExecuteRequestError("meta part must not include a 'data' field")
    return ExecuteRequestParts(
        req_id=meta_parts.req_id,
        code=meta_parts.code,
        mode=meta_parts.mode,
        timeout_ms=meta_parts.timeout_ms,
        init_script=meta_parts.init_script,
        data_json=data_bytes,
        has_session_id=meta_parts.has_session_id,
    )


def parse_execute_request(body: bytes, content_type: str | None) -> ExecuteRequestParts:
    """MIME dispatch: multipart (long-term) vs JSON-object peel (transitional)."""
    if is_multipart_content_type(content_type):
        return parse_multipart_execute(body, content_type or "")
    # Transitional Collabora contract. Keep until kit ships multipart;
    # then delete this peel branch. Multipart is the long-term ingress.
    return peel_execute_request(body)


def peel_execute_request(body: bytes) -> ExecuteRequestParts:
    """Walk a top-level JSON object; decode small keys; keep ``data`` raw.

    Transitional Collabora contract. Keep until kit ships multipart; then
    delete this peel path. Multipart is the long-term ingress.

    Does not ``json.loads`` the ``data`` value (the large grid). ``code`` /
    ``mode`` / ``timeout_ms`` / ``id`` / ``init_script`` are loaded as isolated
    values so the host can auth, route, and validate without a second codec
    stage for the payload.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise ExecuteRequestError("Request body must be bytes")
    buf = bytes(body)
    if buf.startswith(_BOM):
        buf = buf[len(_BOM) :]
    i = _skip_ws(buf, 0)
    if i >= len(buf) or buf[i] != 0x7B:  # {
        raise ExecuteRequestError("JSON body must be an object")
    i += 1

    fields: dict[str, Any] = {}
    data_json: bytes | None = None
    has_session_id = False
    i = _skip_ws(buf, i)
    if i < len(buf) and buf[i] == 0x7D:  # empty object
        i = _skip_ws(buf, i + 1)
        if i != len(buf):
            raise ExecuteRequestError("Trailing data after JSON object")
        return ExecuteRequestParts(
            req_id=None,
            code=None,
            mode=None,
            timeout_ms=None,
            init_script=None,
            data_json=None,
            has_session_id=False,
        )

    while True:
        i = _skip_ws(buf, i)
        if i >= len(buf) or buf[i] != 0x22:
            raise ExecuteRequestError("Expected object key")
        key_start = i
        i = _skip_string(buf, i)
        try:
            key = json.loads(buf[key_start:i].decode("utf-8"))
        except Exception as exc:
            raise ExecuteRequestError("Invalid object key") from exc
        if not isinstance(key, str):
            raise ExecuteRequestError("Object key must be a string")
        i = _skip_ws(buf, i)
        if i >= len(buf) or buf[i] != 0x3A:  # :
            raise ExecuteRequestError("Expected ':' after object key")
        i = _skip_ws(buf, i + 1)
        val_start = i
        i = _skip_value(buf, i, depth=0)
        value_slice = buf[val_start:i]

        if key == "data":
            data_json = value_slice
        elif key == "data_json":
            # Peel-only alias (single-JSON body). Prefer explicit ``data`` if
            # both appear (last ``data`` still wins). Goes away with peel.
            if data_json is None:
                data_json = _coerce_data_json_field(value_slice)
        elif key == "session_id":
            has_session_id = True
            fields[key] = _loads_small(value_slice)
        else:
            fields[key] = _loads_small(value_slice)

        i = _skip_ws(buf, i)
        if i >= len(buf):
            raise ExecuteRequestError("Unterminated JSON object")
        if buf[i] == 0x7D:  # }
            i = _skip_ws(buf, i + 1)
            if i != len(buf):
                raise ExecuteRequestError("Trailing data after JSON object")
            break
        if buf[i] != 0x2C:  # ,
            raise ExecuteRequestError("Expected ',' or '}' in JSON object")
        i += 1
        # Trailing comma is invalid JSON.
        nxt = _skip_ws(buf, i)
        if nxt < len(buf) and buf[nxt] == 0x7D:
            raise ExecuteRequestError("Trailing comma in JSON object")

    return ExecuteRequestParts(
        req_id=fields.get("id"),
        code=fields.get("code"),
        mode=fields.get("mode"),
        timeout_ms=fields.get("timeout_ms"),
        init_script=fields.get("init_script"),
        data_json=data_json,
        has_session_id=has_session_id,
    )


def _coerce_data_json_field(value_slice: bytes) -> bytes:
    """Peel-only: ``data_json`` string → inner UTF-8 bytes; else raw slice.

    Transitional Collabora contract. Delete with the peel path.
    """
    stripped = value_slice.lstrip()
    if stripped.startswith(b'"'):
        try:
            decoded = json.loads(value_slice.decode("utf-8"))
        except Exception as exc:
            raise ExecuteRequestError("Invalid data_json string") from exc
        if isinstance(decoded, str):
            return decoded.encode("utf-8")
        raise ExecuteRequestError("data_json string must decode to text")
    return value_slice


# --- peel walker (single-JSON only) ---
# Transitional Collabora contract. Keep until kit ships multipart; then
# delete this whole helper block. Multipart is the long-term ingress.


def _loads_small(value_slice: bytes) -> Any:
    try:
        return json.loads(value_slice.decode("utf-8"))
    except Exception as exc:
        raise ExecuteRequestError("Invalid JSON value") from exc


def _skip_ws(buf: bytes, i: int) -> int:
    n = len(buf)
    while i < n and buf[i] in _WS:
        i += 1
    return i


def _skip_string(buf: bytes, i: int) -> int:
    """*i* points at the opening quote. Return index after the closing quote."""
    n = len(buf)
    if i >= n or buf[i] != 0x22:
        raise ExecuteRequestError("Expected JSON string")
    i += 1
    while i < n:
        c = buf[i]
        if c == 0x5C:  # backslash
            i += 1
            if i >= n:
                raise ExecuteRequestError("Unterminated escape")
            if buf[i] == 0x75:  # uXXXX
                i += 1
                if i + 4 > n:
                    raise ExecuteRequestError("Invalid unicode escape")
                i += 4
            else:
                i += 1
            continue
        if c == 0x22:
            return i + 1
        i += 1
    raise ExecuteRequestError("Unterminated JSON string")


def _skip_number(buf: bytes, i: int) -> int:
    n = len(buf)
    if i < n and buf[i] == 0x2D:  # -
        i += 1
    if i >= n or not (0x30 <= buf[i] <= 0x39):
        raise ExecuteRequestError("Invalid JSON number")
    if buf[i] == 0x30:
        i += 1
    else:
        while i < n and 0x30 <= buf[i] <= 0x39:
            i += 1
    if i < n and buf[i] == 0x2E:  # .
        i += 1
        if i >= n or not (0x30 <= buf[i] <= 0x39):
            raise ExecuteRequestError("Invalid JSON number")
        while i < n and 0x30 <= buf[i] <= 0x39:
            i += 1
    if i < n and buf[i] in (0x65, 0x45):  # e/E
        i += 1
        if i < n and buf[i] in (0x2B, 0x2D):
            i += 1
        if i >= n or not (0x30 <= buf[i] <= 0x39):
            raise ExecuteRequestError("Invalid JSON number")
        while i < n and 0x30 <= buf[i] <= 0x39:
            i += 1
    return i


def _skip_literal(buf: bytes, i: int, token: bytes) -> int:
    end = i + len(token)
    if buf[i:end] != token:
        raise ExecuteRequestError("Invalid JSON literal")
    return end


def _skip_value(buf: bytes, i: int, *, depth: int) -> int:
    if depth > _MAX_JSON_DEPTH:
        raise ExecuteRequestError("JSON nesting too deep")
    i = _skip_ws(buf, i)
    if i >= len(buf):
        raise ExecuteRequestError("Unexpected end of JSON")
    c = buf[i]
    if c == 0x22:
        return _skip_string(buf, i)
    if c == 0x7B:  # {
        return _skip_container(buf, i, open_b=0x7B, close_b=0x7D, depth=depth)
    if c == 0x5B:  # [
        return _skip_container(buf, i, open_b=0x5B, close_b=0x5D, depth=depth)
    if c == 0x74:  # true
        return _skip_literal(buf, i, b"true")
    if c == 0x66:  # false
        return _skip_literal(buf, i, b"false")
    if c == 0x6E:  # null
        return _skip_literal(buf, i, b"null")
    if c == 0x2D or 0x30 <= c <= 0x39:
        return _skip_number(buf, i)
    raise ExecuteRequestError("Invalid JSON value")


def _skip_container(buf: bytes, i: int, *, open_b: int, close_b: int, depth: int) -> int:
    if i >= len(buf) or buf[i] != open_b:
        raise ExecuteRequestError("Expected JSON container")
    i += 1
    i = _skip_ws(buf, i)
    if i < len(buf) and buf[i] == close_b:
        return i + 1
    while True:
        if open_b == 0x7B:
            i = _skip_ws(buf, i)
            i = _skip_string(buf, i)
            i = _skip_ws(buf, i)
            if i >= len(buf) or buf[i] != 0x3A:
                raise ExecuteRequestError("Expected ':' in object")
            i = _skip_value(buf, i + 1, depth=depth + 1)
        else:
            i = _skip_value(buf, i, depth=depth + 1)
        i = _skip_ws(buf, i)
        if i >= len(buf):
            raise ExecuteRequestError("Unterminated JSON container")
        if buf[i] == close_b:
            return i + 1
        if buf[i] != 0x2C:
            raise ExecuteRequestError("Expected ',' or container end")
        i += 1
        nxt = _skip_ws(buf, i)
        if nxt < len(buf) and buf[nxt] == close_b:
            raise ExecuteRequestError("Trailing comma in JSON container")
        i = nxt
