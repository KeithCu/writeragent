# WriterAgent - Python Compute Service JSON Peel Walker (Transitional)
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Transitional single-JSON peel walker for Collabora Online.

TODO(kit-multipart): Delete this file completely when Collabora Online kit
ships multipart/form-data support. Moving the walker into this isolated module
allows one-file removal when transitional JSON-peel support is retired.
"""

from __future__ import annotations

import json
from typing import Any

from compute_service.json_forward import (
    ExecuteRequestError,
    ExecuteRequestParts,
    _reject_json_constant,
    _reject_nonfinite_number,
)

_WS = frozenset({0x09, 0x0A, 0x0D, 0x20})
_MAX_JSON_DEPTH = 256
_BOM = b"\xef\xbb\xbf"


def peel_execute_request(body: bytes) -> ExecuteRequestParts:
    """Walk a top-level JSON object; decode small keys; keep ``data`` raw.

    Transitional Collabora contract. Keep until kit ships multipart; then
    delete this file. Multipart is the long-term ingress.

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
        return ExecuteRequestParts(req_id=None, code=None, mode=None, timeout_ms=None, init_script=None, data_json=None, has_session_id=False)

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


def _loads_small(value_slice: bytes) -> Any:
    try:
        value = json.loads(value_slice.decode("utf-8"), parse_constant=_reject_json_constant)
    except Exception as exc:
        raise ExecuteRequestError("Invalid JSON value") from exc
    _reject_nonfinite_number(value)
    return value


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
