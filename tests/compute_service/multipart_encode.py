# WriterAgent - Multipart test & benchmark encoding helpers
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""Multipart form-data encoding helpers for tests and benchmarks."""

from __future__ import annotations

import json
from typing import Any

from compute_service.json_forward import ExecuteRequestError, _validate_boundary

_BOUNDARY_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'()+_,-./:=?")


def _as_source_bytes(value: str | bytes, *, label: str) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    raise ExecuteRequestError(f"{label} must be text")


def _boundary_param(boundary: str) -> str:
    if boundary and all(ch in _BOUNDARY_TOKEN_CHARS for ch in boundary):
        return boundary
    escaped = boundary.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def _pick_boundary(payloads: list[bytes], preferred: str) -> str:
    """Choose a delimiter token that does not occur in any part body."""
    candidate = preferred
    n = 0
    while True:
        _validate_boundary(candidate)
        try:
            needle = f"--{candidate}".encode("ascii")
        except UnicodeEncodeError as exc:
            raise ExecuteRequestError("multipart boundary must be ASCII") from exc
        if not any(needle in part for part in payloads):
            return candidate
        n += 1
        if n > 10000:
            raise ExecuteRequestError("could not choose a multipart boundary")
        candidate = f"{preferred}-{n}"


def encode_multipart_execute(meta: dict[str, Any], data_json: bytes | None = None, *, code: str | bytes, init_script: str | bytes | None = None, boundary: str | None = None) -> tuple[str, bytes]:
    """Build kit multipart. Returns ``(Content-Type, body bytes)``.

    ``meta`` is ``id`` / ``mode`` / ``timeout_ms`` only. ``code`` and
    ``init_script`` are raw source bytes, not JSON strings. ``data_json`` is
    the raw grid. If ``--boundary`` occurs in any part body, a suffixed
    boundary is chosen so the delimiter cannot split the payload.
    """
    meta_bytes = json.dumps(meta, allow_nan=False).encode("utf-8")
    code_bytes = _as_source_bytes(code, label="code")
    init_bytes = None if init_script is None else _as_source_bytes(init_script, label="init_script")
    payloads = [meta_bytes, code_bytes]
    if init_bytes is not None:
        payloads.append(init_bytes)
    if data_json is not None:
        payloads.append(bytes(data_json))
    chosen = _pick_boundary(payloads, boundary or "wa-compute")

    def _part(name: str, content_type: str, payload: bytes) -> bytes:
        return (f'--{chosen}\r\nContent-Disposition: form-data; name="{name}"\r\nContent-Type: {content_type}\r\nContent-Transfer-Encoding: 8bit\r\n\r\n').encode("ascii") + payload + b"\r\n"

    chunks = [_part("meta", "application/json", meta_bytes), _part("code", "text/plain; charset=utf-8", code_bytes)]
    if init_bytes is not None:
        chunks.append(_part("init_script", "text/plain; charset=utf-8", init_bytes))
    if data_json is not None:
        chunks.append(_part("data", "application/json", bytes(data_json)))
    chunks.append(f"--{chosen}--\r\n".encode("ascii"))
    return f"multipart/form-data; boundary={_boundary_param(chosen)}", b"".join(chunks)


def encode_multipart_vision(meta: dict[str, Any], image: bytes | bytearray | None = None, *, boundary: str | None = None) -> tuple[str, bytes]:
    """Build vision multipart. Returns ``(Content-Type, body bytes)``."""
    meta_bytes = json.dumps(meta, allow_nan=False).encode("utf-8")
    image_bytes = None if image is None else bytes(image)
    payloads = [meta_bytes]
    if image_bytes is not None:
        payloads.append(image_bytes)
    chosen = _pick_boundary(payloads, boundary or "wa-vision")

    def _part(name: str, content_type: str, cte: str, payload: bytes) -> bytes:
        return (f'--{chosen}\r\nContent-Disposition: form-data; name="{name}"\r\nContent-Type: {content_type}\r\nContent-Transfer-Encoding: {cte}\r\n\r\n').encode("ascii") + payload + b"\r\n"

    chunks = [_part("meta", "application/json", "8bit", meta_bytes)]
    if image_bytes is not None:
        chunks.append(_part("image", "application/octet-stream", "binary", image_bytes))
    chunks.append(f"--{chosen}--\r\n".encode("ascii"))
    return f"multipart/form-data; boundary={_boundary_param(chosen)}", b"".join(chunks)
