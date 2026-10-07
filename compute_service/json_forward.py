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
_ALLOWED_CTE = frozenset({"7bit", "8bit", "binary"})
_BOUNDARY_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'()+_,-./:=?")

WIRE_JSON_FORWARD = "json_forward"
VALID_EXECUTE_MODES = frozenset({"isolated", "shared"})
# One payload wire. ``pickle`` used to mean host_pack_data / split_grid on
# the same stdio envelope, and a value the host rejected was rewritten and run.
VALID_WIRES = frozenset({WIRE_JSON_FORWARD})
VALID_RESPONSE_STATUSES = frozenset({"ok", "error"})


class ExecuteRequestError(ValueError):
    """Raised when an execute request or response does not match the one schema."""


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


def require_execute_mode(mode: Any) -> str:
    """Return ``isolated`` or ``shared``.

    Any other value used to be rewritten to isolated and executed, so a
    typo looked like a successful cell that kept no workbook state. Callers
    share this check; there is no second, looser interpretation on the worker.
    """
    if isinstance(mode, str) and mode in VALID_EXECUTE_MODES:
        return mode
    raise ExecuteRequestError("mode must be 'isolated' or 'shared'.")


def canonical_execute_mode(mode: Any) -> str:
    """Missing mode is isolated. Every other value must be a real mode.

    ``mode or "isolated"`` treated ``false`` and ``0`` as missing, so the
    HTTP handler ran an isolated cell that the worker would have rejected.
    Only ``None`` and ``""`` are the omitted mode. Peel and multipart both
    call this; the pool and the worker call it too.
    """
    if mode is None or mode == "":
        return require_execute_mode("isolated")
    return require_execute_mode(mode)


def require_execute_wire(wire: Any) -> str:
    """Return the one stdio payload wire. Unknown values are not rewritten."""
    if isinstance(wire, str) and wire in VALID_WIRES:
        return wire
    raise ExecuteRequestError(f"wire must be {WIRE_JSON_FORWARD!r}.")


def validate_execute_response(payload: dict[str, Any]) -> dict[str, Any]:
    """The one execute response shape, checked once before the worker dumps it.

    ``status`` is ``ok`` (with ``result``) or ``error`` (with ``error`` text).
    The HTTP host forwards the dumped bytes and does not interpret them again.
    """
    if not isinstance(payload, dict):
        raise ExecuteRequestError("execute response must be an object")
    status = payload.get("status")
    if status not in VALID_RESPONSE_STATUSES:
        raise ExecuteRequestError("execute response status must be 'ok' or 'error'")
    if status == "error" and not payload.get("error") and not payload.get("message"):
        raise ExecuteRequestError("execute error response requires error")
    if status == "ok" and "result" not in payload:
        raise ExecuteRequestError("execute ok response requires result")
    return payload


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


def parse_multipart_execute(body: bytes, content_type: str) -> ExecuteRequestParts:
    """Long-term kit ingress. Boundary-scan the body and ``json.loads`` meta only.

    ``code`` and ``init_script`` are returned as raw part bytes. They used to
    live inside ``meta`` and were JSON-unescaped on the HTTP host, which is
    payload work: the worker needs a ``str``, not a parsed JSON string. The
    server UTF-8-decodes the slice once. ``data`` is returned untouched.
    """
    if not is_multipart_content_type(content_type):
        raise ExecuteRequestError("Content-Type is not multipart")
    if not isinstance(body, (bytes, bytearray)):
        raise ExecuteRequestError("Request body must be bytes")
    boundary = _boundary_from_content_type(content_type)
    named = _scan_multipart_parts(memoryview(body), boundary)
    req_id, mode, timeout_ms, has_session_id = _parse_meta_object(bytes(named["meta"]))
    return ExecuteRequestParts(req_id=req_id, code=bytes(named["code"]) if "code" in named else None, mode=mode, timeout_ms=timeout_ms, init_script=bytes(named["init_script"]) if "init_script" in named else None, data_json=bytes(named["data"]) if "data" in named else None, has_session_id=has_session_id)


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


def _scan_multipart_parts(body: bytes | memoryview, boundary: bytes) -> dict[str, bytes | memoryview]:
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
    parts: dict[str, bytes | memoryview] = {}
    while kind == "open":
        if len(parts) >= _MAX_MULTIPART_PARTS:
            raise ExecuteRequestError("too many multipart parts")
        header_at = _skip_delimiter_line(body, pos, delim, kind)
        header_end, header_bytes = _read_header_block(body, header_at)
        name, cte = _part_name_and_cte(header_bytes)
        if cte is not None and cte not in _ALLOWED_CTE:
            raise ExecuteRequestError("unsupported content-transfer-encoding")
        if name not in _PART_NAMES:
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


def _next_delimiter(body: bytes | memoryview, start: int, delim: bytes) -> tuple[int, str] | None:
    i = start
    n = len(body)
    if isinstance(body, memoryview):
        b_obj = body.obj if hasattr(body, "obj") and isinstance(body.obj, bytes) else bytes(body)
        while i < n:
            j = b_obj.find(delim, i)
            if j < 0:
                return None
            kind = _classify_delimiter(body, j, delim)
            if kind is not None:
                return j, kind
            i = j + 1
        return None
    while i < n:
        j = body.find(delim, i)
        if j < 0:
            return None
        kind = _classify_delimiter(body, j, delim)
        if kind is not None:
            return j, kind
        i = j + 1
    return None


def _classify_delimiter(body: bytes | memoryview, i: int, delim: bytes) -> str | None:
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


def _skip_delimiter_line(body: bytes | memoryview, dash_at: int, delim: bytes, kind: str) -> int:
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


def _payload_end_before_delimiter(body: bytes | memoryview, dash_at: int) -> int:
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


def _read_header_block(body: bytes | memoryview, start: int) -> tuple[int, bytes]:
    limit = min(len(body), start + _MAX_PART_HEADERS)
    i = start
    if isinstance(body, memoryview):
        b_obj = body.obj if hasattr(body, "obj") and isinstance(body.obj, bytes) else bytes(body)
        while i < limit:
            nl = b_obj.find(b"\n", i, limit)
            if nl < 0:
                raise ExecuteRequestError("unterminated multipart headers")
            content_end = nl - 1 if nl > i and b_obj[nl - 1] == 0x0D else nl
            if content_end == i:
                return nl + 1, bytes(body[start:i])
            i = nl + 1
        raise ExecuteRequestError("multipart headers exceed cap")
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
    lower = value.lower()
    i = 0
    found: str | None = None
    while True:
        j = lower.find("name=", i)
        if j < 0:
            break
        if j > 0 and lower[j - 1] not in " \t;":
            i = j + 5
            continue
        k = j + 5
        if k < len(value) and value[k] == '"':
            k += 1
            chars: list[str] = []
            closed = False
            while k < len(value):
                if value[k] == "\\" and k + 1 < len(value):
                    chars.append(value[k + 1])
                    k += 2
                    continue
                if value[k] == '"':
                    closed = True
                    k += 1
                    break
                chars.append(value[k])
                k += 1
            if not closed:
                raise ExecuteRequestError("unterminated part name")
            found = "".join(chars)
            i = k
            continue
        end = k
        while end < len(value) and value[end] not in " \t;":
            end += 1
        if end == k:
            raise ExecuteRequestError("missing part name")
        found = value[k:end]
        i = end
    if not found:
        raise ExecuteRequestError("missing part name")
    return found


def _parse_meta_object(meta_bytes: bytes) -> tuple[Any, Any, Any, bool]:
    if len(meta_bytes) > MAX_META_BYTES:
        raise ExecuteRequestError("meta part exceeds size cap")
    try:
        obj = json.loads(meta_bytes.decode("utf-8"), parse_constant=_reject_json_constant)
    except Exception as exc:
        raise ExecuteRequestError("invalid meta JSON") from exc
    if not isinstance(obj, dict):
        raise ExecuteRequestError("meta part must be a JSON object")
    for key in _FORBIDDEN_META_KEYS:
        if key in obj:
            raise ExecuteRequestError(f"meta part must not include a {key!r} field")
    req_id = obj.get("id")
    if req_id is not None and not isinstance(req_id, (str, int)):
        raise ExecuteRequestError("id must be a scalar")
    timeout_ms = obj.get("timeout_ms")
    if timeout_ms is not None and not isinstance(timeout_ms, (int, float)):
        raise ExecuteRequestError("timeout_ms must be a scalar")
    # 1e9999 survives parse_constant (that hook only sees NaN / Infinity
    # tokens) and becomes inf. Echoing it as id crashed allow_nan=False.
    _reject_nonfinite_number(req_id)
    _reject_nonfinite_number(timeout_ms)
    return req_id, obj.get("mode"), timeout_ms, "session_id" in obj


def parse_execute_request(body: bytes, content_type: str | None) -> ExecuteRequestParts:
    """MIME dispatch: multipart (long-term) vs JSON-object peel (transitional)."""
    if is_multipart_content_type(content_type):
        return parse_multipart_execute(body, content_type or "")
    # Transitional Collabora contract. Keep until kit ships multipart;
    # then delete this peel branch. Multipart is the long-term ingress.
    from compute_service.json_peel import peel_execute_request

    return peel_execute_request(body)


def __getattr__(name: str) -> Any:
    if name == "peel_execute_request":
        from compute_service.json_peel import peel_execute_request

        return peel_execute_request
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

