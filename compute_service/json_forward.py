# WriterAgent - Python Compute Service
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compute-only JSON blob forward helpers.

LibrePy desktop ``=PY()`` keeps Pickle5 + ``split_grid``. The HTTP compute
service is a thinner proxy: peel small control fields from today's single
JSON object, or boundary-scan a multipart body and ``json.loads`` only its
``meta`` part. ``code``, ``init_script``, and ``data`` are raw part bytes.
Forward the raw ``data`` JSON to the formula worker, and forward the worker's
``result_json`` bytes back to coolwsd — no host ``json.loads`` of the grid,
no ``host_pack_data``, no second ``json.dumps`` of the result.

Worker stdio still uses the length-prefixed Pickle5 envelope as framing.
There is one payload on that envelope: raw ``data_json`` / ``result_json``
bytes. Pickle copies those buffers; it does not re-encode the JSON tree and
there is no second ``split_grid`` payload field.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

# HTTP max_body_bytes is 32 MiB; pickle of the envelope needs a little slack.
COMPUTE_MAX_PAYLOAD_BYTES = 33 * 1024 * 1024

# ``meta`` is id / mode / timeout_ms only. Cap it so a stuffed part cannot
# force ``json.loads`` of a payload-sized buffer.
MAX_META_BYTES = 64 * 1024
_MAX_PART_HEADERS = 8 * 1024
_MAX_MULTIPART_PARTS = 8
_PART_NAMES = frozenset({"meta", "code", "init_script", "data"})
_FORBIDDEN_META_KEYS = ("code", "data", "data_json", "init_script")
_VISION_PART_NAMES = frozenset({"meta", "image"})
_VISION_FORBIDDEN_META_KEYS = ("image", "image_b64")
_ALLOWED_CTE = frozenset({"7bit", "8bit", "binary"})

VALID_EXECUTE_MODES = frozenset({"isolated", "shared"})


class ExecuteRequestError(ValueError):
    """Raised when an execute request or response does not match the one schema."""

    code: str | None

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def _reject_json_constant(token: str) -> None:
    """``json.loads`` accepts NaN/Infinity. The peel walker does not.

    ``timeout_ms: Infinity`` then raised OverflowError outside the execute
    try, and ``id: NaN`` failed the kit dump. Reject the tokens here.
    """
    raise ValueError(token)


def _reject_nonfinite_number(value: Any) -> None:
    """Reject ``inf`` from numeric overflow, not only NaN/Infinity tokens.

    ``json.loads`` turns ``1e9999`` into ``inf`` without calling
    ``parse_constant``. The HTTP 400 path then echoed that ``id`` through
    ``allow_nan=False`` and the WSGI callable crashed.
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExecuteRequestError("non-finite JSON number")
    elif isinstance(value, list):
        for item in value:
            _reject_nonfinite_number(item)
    elif isinstance(value, dict):
        for _k, v in value.items():
            _reject_nonfinite_number(v)


def canonical_execute_mode(mode: Any) -> str:
    """Missing mode is isolated. Every other value must be a real mode.

    Only ``None`` and ``""`` are omitted. ``false`` and ``0`` are not
    missing, or the HTTP handler would run an isolated cell. Peel,
    multipart, and the pool call this before the worker runs.
    """
    if mode is None or mode == "":
        return "isolated"
    if isinstance(mode, str) and mode in VALID_EXECUTE_MODES:
        return mode
    raise ExecuteRequestError("mode must be 'isolated' or 'shared'.")


def validate_session_id(session_id: Any) -> str:
    """Validate that *session_id* is a valid identifier and not in a reserved namespace.

    Names ending in ':init' or starting with 'isolated:' are reserved for
    internal init-script snapshotting and cannot be claimed by callers.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise ExecuteRequestError("session_id must be a non-empty string.", code="INVALID_SESSION_ID")
    sid = session_id.strip()
    if sid.endswith(":init") or sid.startswith("isolated:"):
        raise ExecuteRequestError(f"Invalid session_id {sid!r}: names ending in ':init' or starting with 'isolated:' are reserved.", code="INVALID_SESSION_ID")
    return sid


@dataclass(frozen=True)
class ExecuteRequestParts:
    """Control fields plus raw payload bytes.

    On the peel path ``code`` and ``init_script`` are JSON-decoded values.
    On multipart they are the raw part bytes (``init_script`` is ``None`` when
    that part is absent). The HTTP server UTF-8-decodes those bytes.
    ``data_json`` is always the untouched grid bytes.
    """

    req_id: Any
    code: Any
    mode: Any
    timeout_ms: Any
    init_script: Any
    data_json: bytes | None
    has_session_id: bool


@dataclass(frozen=True)
class VisionRequestParts:
    """Control fields plus raw image bytes for /v1/vision."""

    req_id: Any
    helper: Any
    timeout_ms: Any
    params: Any
    file_path: Any
    image: bytes | None


def dumps_response(payload: dict[str, Any]) -> bytes:
    """Encode one kit-safe execute response. Worker is the only dumps site."""
    return json.dumps(payload, allow_nan=False).encode("utf-8")


def json_forward_envelope(res: dict[str, Any], *, req_id: Any, session_reset: bool = False, namespace_created: bool = False) -> dict[str, Any]:
    """Pickle envelope: small status for host logs + result_json bytes to forward."""
    body = res
    if session_reset:
        body["session_reset"] = True
    try:
        result_json = dumps_response(body)
        status = res.get("status")
    except (TypeError, ValueError) as exc:
        fallback: dict[str, Any] = {"status": "error", "error": f"JSON encode failed: {exc}"}
        if req_id is not None:
            fallback["id"] = req_id
        if session_reset:
            fallback["session_reset"] = True
        result_json = dumps_response(fallback)
        status = "error"
    out: dict[str, Any] = {"id": req_id, "status": status, "result_json": result_json, "namespace_created": namespace_created}
    if session_reset:
        out["session_reset"] = True
    return out


def is_multipart_content_type(content_type: str | None) -> bool:
    """True when the kit opted into the optional multipart ingress."""
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower().startswith("multipart/")


def _scan_multipart_body(body: bytes, content_type: str, allowed: frozenset[str]) -> dict[str, bytes]:
    if not is_multipart_content_type(content_type):
        raise ExecuteRequestError("Content-Type is not multipart")
    if not isinstance(body, (bytes, bytearray)):
        raise ExecuteRequestError("Request body must be bytes")
    boundary = _boundary_from_content_type(content_type)
    return _scan_multipart_parts(bytes(body), boundary, allowed)


def parse_multipart_execute(body: bytes, content_type: str) -> ExecuteRequestParts:
    """Long-term kit ingress. Boundary-scan the body and ``json.loads`` meta only.

    ``code`` and ``init_script`` are returned as raw part bytes. They stay
    out of ``meta`` so the HTTP host does not JSON-unescape them: the worker
    needs a ``str``, not a parsed JSON string. The server UTF-8-decodes the
    slice once. ``data`` is returned untouched.

    Performance:
    Benchmarks demonstrate multipart ingress is substantially faster than
    JSON peel:
    - On tabular / grid data (`tabular_stats`), multipart achieves 1.74x - 1.85x
      higher throughput (~517 RPS vs ~280 RPS on 4 workers) because it avoids
      walking the large JSON payload character-by-character on the HTTP host.
    - On formula / vector code (`numpy_vector`), multipart achieves ~25% - 27%
      higher throughput (~551 - 571 RPS vs ~435 - 450 RPS on 4 workers) by
      streaming raw UTF-8 code parts without JSON string unescaping.
    """
    named = _scan_multipart_body(body, content_type, _PART_NAMES)
    meta = _parse_meta_object(named["meta"], _FORBIDDEN_META_KEYS)
    return ExecuteRequestParts(
        req_id=meta.get("id"),
        code=named.get("code"),
        mode=meta.get("mode"),
        timeout_ms=meta.get("timeout_ms"),
        init_script=named.get("init_script"),
        data_json=named.get("data"),
        has_session_id="session_id" in meta,
    )


def _validate_boundary(boundary: str) -> None:
    if not boundary or len(boundary) > 70 or any(ch in boundary for ch in "\r\n\x00"):
        raise ExecuteRequestError("invalid multipart boundary")


def _boundary_from_content_type(content_type: str) -> bytes:
    params = _content_type_params(content_type)
    raw = params.get("boundary")
    if raw is None:
        raise ExecuteRequestError("missing multipart boundary")
    _validate_boundary(raw)
    try:
        return raw.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ExecuteRequestError("multipart boundary must be ASCII") from exc


def _content_type_params(value: str) -> dict[str, str]:
    items = _split_header_list(value)
    params: dict[str, str] = {}
    for item in items[1:]:
        if not item or "=" not in item:
            continue
        key, raw_value = item.split("=", 1)
        params[key.strip().lower()] = _unquote_param(raw_value)
    return params


def _split_header_list(value: str) -> list[str]:
    items: list[str] = []
    buf: list[str] = []
    in_quotes = False
    escaped = False
    for ch in value:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if in_quotes and ch == "\\":
            buf.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
            continue
        if ch == ";" and not in_quotes:
            items.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if escaped or in_quotes:
        raise ExecuteRequestError("unterminated quoted parameter")
    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    return items


def _unquote_param(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        inner = value[1:-1]
        out: list[str] = []
        i = 0
        while i < len(inner):
            if inner[i] == "\\" and i + 1 < len(inner):
                out.append(inner[i + 1])
                i += 2
                continue
            out.append(inner[i])
            i += 1
        return "".join(out)
    return value


def _scan_multipart_parts(body: bytes, boundary: bytes, allowed: frozenset[str]) -> dict[str, bytes]:
    """Slice named parts by boundary.

    ``email.parser.BytesParser`` built a message tree and walked every part,
    including the grid and the formula source. A delimiter scan only finds
    ``--boundary`` lines and slices the bytes between them. Part bodies are
    not decoded: ``base64`` and ``quoted-printable`` are rejected so the host
    never transforms payload bytes.
    """
    delim = b"--" + boundary
    found = _next_delimiter(body, 0, delim)
    if found is None:
        raise ExecuteRequestError("missing multipart boundary")
    pos, kind = found
    parts: dict[str, bytes] = {}
    while kind == "open":
        if len(parts) >= _MAX_MULTIPART_PARTS:
            raise ExecuteRequestError("too many multipart parts")
        header_at = _skip_delimiter_line(body, pos, delim, kind)
        header_end, header_bytes = _read_header_block(body, header_at)
        name, cte = _part_name_and_cte(header_bytes)
        if cte is not None and cte not in _ALLOWED_CTE:
            raise ExecuteRequestError("unsupported content-transfer-encoding")
        if name not in allowed:
            raise ExecuteRequestError(f"unknown multipart part {name!r}")
        if name in parts:
            raise ExecuteRequestError(f"duplicate multipart part {name!r}")
        nxt = _next_delimiter(body, header_end, delim)
        if nxt is None:
            raise ExecuteRequestError("unterminated multipart body")
        nxt_at, nxt_kind = nxt
        payload_end = _payload_end_before_delimiter(body, nxt_at)
        if payload_end < header_end:
            # RFC 2046 attaches the CRLF before --boundary to the delimiter.
            # An empty part is headers\r\n\r\n--boundary: that CRLF is also
            # the blank line that ended the headers, so dropping it landed
            # before the body and the part was rejected as malformed.
            # The body is empty. A longer gap is still a broken framing.
            if nxt_at == header_end and body[payload_end:header_end] in (b"\r\n", b"\n"):
                payload_end = header_end
            else:
                raise ExecuteRequestError("malformed multipart body")
        parts[name] = body[header_end:payload_end]
        pos, kind = nxt_at, nxt_kind
    if kind != "close":
        raise ExecuteRequestError("unterminated multipart body")
    if "meta" not in parts:
        raise ExecuteRequestError("missing meta part")
    return parts


def _next_delimiter(body: bytes, start: int, delim: bytes) -> tuple[int, str] | None:
    i = start
    n = len(body)
    while i < n:
        j = body.find(delim, i)
        if j < 0:
            return None
        kind = _classify_delimiter(body, j, delim)
        if kind is not None:
            return j, kind
        i = j + 1
    return None


def _classify_delimiter(body: bytes, i: int, delim: bytes) -> str | None:
    """Return ``open`` or ``close`` when ``body[i:]`` is a real delimiter line.

    A hit has to sit at the start of the body or immediately after LF, and the
    bytes after the boundary have to be a line ending (open) or ``--`` plus a
    line ending (close). A longer token such as ``--boundary-1`` is not a
    match for ``--boundary``.
    """
    if body[i : i + len(delim)] != delim:
        return None
    if i > 0 and body[i - 1] != 0x0A:
        return None
    j = i + len(delim)
    if body[j : j + 2] == b"--":
        j += 2
        kind = "close"
    else:
        kind = "open"
    while j < len(body) and body[j] in (0x20, 0x09):
        j += 1
    if j >= len(body) or body[j] in (0x0D, 0x0A):
        return kind
    return None


def _skip_delimiter_line(body: bytes, dash_at: int, delim: bytes, kind: str) -> int:
    j = dash_at + len(delim)
    if kind == "close":
        j += 2
    while j < len(body) and body[j] in (0x20, 0x09):
        j += 1
    if j >= len(body):
        if kind == "close":
            return j
        raise ExecuteRequestError("unterminated multipart delimiter")
    if body[j : j + 2] == b"\r\n":
        return j + 2
    if body[j] == 0x0A:
        return j + 1
    raise ExecuteRequestError("malformed multipart delimiter")


def _payload_end_before_delimiter(body: bytes, dash_at: int) -> int:
    """Index where the part body ends: the introducing linebreak is the delimiter's.

    ``\\r\\n--boundary`` drops both bytes. A lone ``\\n`` drops one, which is
    how LF-only messages parse. A part in an LF-only message cannot end in a
    bare CR, because that CR would look like the first half of CRLF.
    """
    if dash_at >= 2 and body[dash_at - 2 : dash_at] == b"\r\n":
        return dash_at - 2
    if dash_at >= 1 and body[dash_at - 1] == 0x0A:
        return dash_at - 1
    if dash_at == 0:
        return 0
    raise ExecuteRequestError("malformed multipart delimiter")


def _read_header_block(body: bytes, start: int) -> tuple[int, bytes]:
    limit = min(len(body), start + _MAX_PART_HEADERS)
    i = start
    while i < limit:
        nl = body.find(b"\n", i, limit)
        if nl < 0:
            raise ExecuteRequestError("unterminated multipart headers")
        content_end = nl - 1 if nl > i and body[nl - 1] == 0x0D else nl
        if content_end == i:
            return nl + 1, body[start:i]
        i = nl + 1
    raise ExecuteRequestError("multipart headers exceed cap")


def _part_name_and_cte(header_bytes: bytes) -> tuple[str, str | None]:
    disposition: str | None = None
    cte: str | None = None
    for key, value in _unfold_header_fields(header_bytes):
        if key == "content-disposition":
            disposition = value
        elif key == "content-transfer-encoding":
            cte = value
    if disposition is None:
        raise ExecuteRequestError("missing part name")
    name = _disposition_name(disposition)
    if cte is None:
        return name, None
    token = cte.split(";", 1)[0].strip().lower()
    if not token:
        return name, None
    return name, token.split()[0]


def _unfold_header_fields(header_bytes: bytes) -> list[tuple[str, str]]:
    lines: list[str] = []
    for raw_line in header_bytes.split(b"\n"):
        if raw_line.endswith(b"\r"):
            raw_line = raw_line[:-1]
        try:
            text = raw_line.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ExecuteRequestError("multipart headers must be ASCII") from exc
        if text.startswith((" ", "\t")):
            if not lines:
                raise ExecuteRequestError("malformed multipart header")
            lines[-1] = lines[-1] + " " + text.strip()
            continue
        if text:
            lines.append(text)
    fields: list[tuple[str, str]] = []
    for line in lines:
        if ":" not in line:
            raise ExecuteRequestError("malformed multipart header")
        name, value = line.split(":", 1)
        fields.append((name.strip().lower(), value.strip()))
    return fields


def _disposition_name(value: str) -> str:
    """``Content-Disposition`` ``name=`` (quoted or token). ``name*`` is not accepted."""
    found: str | None = None
    for item in _split_header_list(value):
        if "=" not in item:
            continue
        key, raw_value = item.split("=", 1)
        if key.lower() != "name":
            continue
        # Unquoted empty, or a token that starts with space or tab, is the old
        # scanner's immediate "missing part name" (name=; name="code").
        # A quoted empty name is stored and a later name= can replace it.
        if raw_value == "" or (raw_value[0] != '"' and raw_value[0] in " \t"):
            raise ExecuteRequestError("missing part name")
        found = _unquote_param(raw_value)
    if not found:
        raise ExecuteRequestError("missing part name")
    return found


def _parse_meta_object(meta_bytes: bytes, forbidden_keys: tuple[str, ...] | frozenset[str]) -> dict[str, Any]:
    if len(meta_bytes) > MAX_META_BYTES:
        raise ExecuteRequestError("meta part exceeds size cap")
    try:
        obj = json.loads(meta_bytes.decode("utf-8"), parse_constant=_reject_json_constant)
    except Exception as exc:
        raise ExecuteRequestError("invalid meta JSON") from exc
    if not isinstance(obj, dict):
        raise ExecuteRequestError("meta part must be a JSON object")
    for key in forbidden_keys:
        if key in obj:
            raise ExecuteRequestError(f"meta part must not include a {key!r} field")
    req_id = obj.get("id")
    if req_id is not None and (not isinstance(req_id, (str, int)) or isinstance(req_id, bool)):
        raise ExecuteRequestError("id must be a scalar")
    timeout_ms = obj.get("timeout_ms")
    if timeout_ms is not None and (not isinstance(timeout_ms, (int, float)) or isinstance(timeout_ms, bool)):
        raise ExecuteRequestError("timeout_ms must be a scalar")
    # 1e9999 survives parse_constant (that hook only sees NaN / Infinity
    # tokens) and becomes inf. Echoing it as id crashed allow_nan=False.
    _reject_nonfinite_number(req_id)
    _reject_nonfinite_number(timeout_ms)
    return obj


def parse_multipart_vision(body: bytes, content_type: str) -> VisionRequestParts:
    """Boundary-scan vision multipart body and json.loads meta only."""
    named = _scan_multipart_body(body, content_type, _VISION_PART_NAMES)
    meta = _parse_meta_object(named["meta"], _VISION_FORBIDDEN_META_KEYS)
    return VisionRequestParts(
        req_id=meta.get("id"),
        helper=meta.get("helper"),
        timeout_ms=meta.get("timeout_ms"),
        params=meta.get("params"),
        file_path=meta.get("file_path"),
        image=named.get("image"),
    )


def parse_execute_request(body: bytes, content_type: str | None) -> ExecuteRequestParts:
    """MIME dispatch: multipart (long-term) vs JSON-object peel (transitional)."""
    if is_multipart_content_type(content_type):
        return parse_multipart_execute(body, content_type or "")
    # Transitional Collabora contract. Keep until kit ships multipart;
    # then delete this peel branch. Multipart is the long-term ingress.
    from compute_service.json_peel import peel_execute_request

    return peel_execute_request(body)
