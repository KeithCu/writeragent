# WriterAgent - Python Compute Service
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""JSON blob-forward helpers for the compute HTTP host.

LibrePy desktop ``=PY()`` stays Pickle5 + ``split_grid`` both ways. The compute
service is a thinner proxy:

- **Ingress (kit):** ``multipart/form-data`` with a small JSON ``meta`` part and
  a raw ``application/json`` ``data`` part. The host parses only ``meta`` and
  forwards the data-part bytes to the formula worker. The worker is the one
  ``json.loads`` of the grid.
- **Ingress (small/dev fallback):** a single ``application/json`` object. The
  host may ``loads`` that whole body (including a nested ``data`` array) and
  re-encode ``data`` to bytes for the worker.
- **Egress:** the worker dumps the HTTP result JSON once; the host forwards
  those bytes into the WSGI response (no ``json.dumps`` of a large result).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from typing import Any

HTTP_JSON_KEY = "http_json"
DATA_JSON_KEY = "data_json"

_META_PART_NAMES = frozenset({"meta", "metadata"})
_DATA_PART_NAMES = frozenset({"data"})


@dataclass(frozen=True)
class ExecuteRequestParts:
    """Peeled execute request: small metadata plus optional raw data JSON bytes."""

    meta: dict[str, Any]
    data_json: bytes | None
    multipart: bool


class ExecuteRequestParseError(ValueError):
    """Raised when the HTTP body is not a usable execute request."""


def is_multipart_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower().startswith("multipart/")


def encode_data_json(data: Any) -> bytes:
    """Encode a Python ``data`` value to UTF-8 JSON bytes (JSON fallback / pool API)."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        return data.encode("utf-8")
    return json.dumps(data, allow_nan=False).encode("utf-8")


def encode_multipart_execute(
    meta: dict[str, Any],
    data_json: bytes | None = None,
    *,
    boundary: str = "wa-compute",
) -> tuple[str, bytes]:
    """Build the kit multipart body. Returns ``(Content-Type, body bytes)``."""

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
    raise ExecuteRequestParseError("multipart part has no payload")


def parse_multipart_execute(body: bytes, content_type: str) -> ExecuteRequestParts:
    """Parse kit multipart: load ``meta`` only; keep the ``data`` part as raw bytes."""
    if not is_multipart_content_type(content_type):
        raise ExecuteRequestParseError("Content-Type is not multipart")
    # email.parser wants a full MIME message; prepend the request Content-Type.
    raw = b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("utf-8") + b"\r\n\r\n" + body
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    if not msg.is_multipart():
        raise ExecuteRequestParseError("expected a multipart body")

    named: dict[str, bytes] = {}
    ordered: list[bytes] = []
    for part in msg.iter_parts():
        payload = _part_payload_bytes(part)
        ordered.append(payload)
        disp_name = part.get_param("name", header="content-disposition")
        if isinstance(disp_name, str) and disp_name:
            named[disp_name] = payload

    meta_bytes: bytes | None = None
    for key in _META_PART_NAMES:
        if key in named:
            meta_bytes = named[key]
            break
    data_bytes: bytes | None = None
    for key in _DATA_PART_NAMES:
        if key in named:
            data_bytes = named[key]
            break

    # multipart/mixed with unnamed parts: first = meta, second = data.
    if meta_bytes is None:
        if not ordered:
            raise ExecuteRequestParseError("missing meta part")
        meta_bytes = ordered[0]
        if data_bytes is None and len(ordered) >= 2:
            data_bytes = ordered[1]

    try:
        meta = json.loads(meta_bytes.decode("utf-8"))
    except Exception as exc:
        raise ExecuteRequestParseError("meta part is not valid JSON") from exc
    if not isinstance(meta, dict):
        raise ExecuteRequestParseError("meta part must be a JSON object")
    if "data" in meta:
        # Kit contract: the grid lives in Part B, never nested inside meta.
        raise ExecuteRequestParseError("meta part must not include a 'data' field")
    return ExecuteRequestParts(meta=meta, data_json=data_bytes, multipart=True)


def parse_json_execute_fallback(body: bytes) -> ExecuteRequestParts:
    """Small/dev single-object JSON. Nested ``data`` is re-encoded for the worker."""
    try:
        req = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise ExecuteRequestParseError("Invalid JSON") from exc
    if not isinstance(req, dict):
        raise ExecuteRequestParseError("JSON body must be an object")
    data_json: bytes | None = None
    if "data" in req:
        data_json = encode_data_json(req["data"])
    meta = {key: value for key, value in req.items() if key != "data"}
    return ExecuteRequestParts(meta=meta, data_json=data_json, multipart=False)


def parse_execute_request(body: bytes, content_type: str | None) -> ExecuteRequestParts:
    """Dispatch multipart (kit) vs JSON-object (small/dev) execute bodies."""
    if is_multipart_content_type(content_type):
        return parse_multipart_execute(body, content_type or "")
    return parse_json_execute_fallback(body)


def dump_worker_http_json(payload: dict[str, Any]) -> dict[str, Any]:
    """Worker-side: serialize the HTTP body once; pickle only tiny fields + bytes."""
    http_json = json.dumps(payload, allow_nan=False).encode("utf-8")
    return {
        "id": payload.get("id"),
        "status": payload.get("status"),
        HTTP_JSON_KEY: http_json,
    }


def extract_http_json(payload: dict[str, Any] | None) -> bytes | None:
    """Return worker-emitted HTTP body bytes, or None when the host must dump."""
    if not isinstance(payload, dict):
        return None
    raw = payload.get(HTTP_JSON_KEY)
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if isinstance(raw, str):
        return raw.encode("utf-8")
    return None


def merge_worker_response(res: Any, req_id: str | None) -> dict[str, Any]:
    """Expose parsed fields for the pool Python API; keep exact ``http_json`` bytes.

    One ``json.loads`` of the worker blob is OK for ``pool.execute`` callers.
    The HTTP host must forward ``http_json`` and must not ``json.dumps`` it.
    """
    if not isinstance(res, dict):
        out = {"status": "error", "error": "Invalid worker response"}
        if req_id is not None:
            out["id"] = req_id
        return out

    merged = dict(res)
    if req_id is not None:
        merged["id"] = req_id

    raw = extract_http_json(merged)
    if raw is None:
        return merged
    try:
        parsed = json.loads(raw)
    except Exception:
        return merged
    if not isinstance(parsed, dict):
        return merged
    parsed[HTTP_JSON_KEY] = raw
    if req_id is not None:
        parsed["id"] = req_id
    return parsed
