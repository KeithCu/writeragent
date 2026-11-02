# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""MCP / JSON-RPC wire shapes (stdlib only — mirrors official mcp.types subset).

Used by plugin/mcp/mcp_protocol.py and plugin/mcp/mcp_session.py for parse/build without Pydantic or the mcp package.
"""

from __future__ import annotations

import dataclasses
from typing import Any, cast

# Aligned with upstream python-sdk LATEST_PROTOCOL_VERSION.
MCP_PROTOCOL_VERSION = "2025-11-25"

# Standard JSON-RPC error codes (JSON-RPC 2.0 + MCP extensions).
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SERVER_BUSY = -32000
EXECUTION_TIMEOUT = -32001

RequestId = int | str | None


@dataclasses.dataclass(frozen=True)
class ParsedJsonRpcRequest:
    """Validated JSON-RPC 2.0 request (not a notification)."""

    method: str
    params: dict[str, Any]
    req_id: int | str


def _jsonrpc_parts(msg: object) -> tuple[str, dict[str, Any], Any] | None:
    # Validate common JSON-RPC 2.0 structure for requests and notifications.
    if not isinstance(msg, dict):
        return None
    raw = cast("dict[str, Any]", msg)
    if raw.get("jsonrpc") != "2.0":
        return None
    method = raw.get("method")
    if not isinstance(method, str) or not method:
        return None
    params = raw.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return None
    return (method, dict(params), raw.get("id"))


def parse_jsonrpc_request(msg: object) -> ParsedJsonRpcRequest | None:
    """Parse and validate a single JSON-RPC request object.

    Notifications (missing ``id`` or ``id`` is null) are not requests — callers should
    treat ``req_id is None`` before calling this; a notification is a valid message with
    a missing or null id, and is_jsonrpc_notification uses the same validation.
    """
    # Do not sniff sys.modules["crosshair"]: that returned a parse error for every valid request.
    parts = _jsonrpc_parts(msg)
    if parts is None:
        return None
    method, params, req_id = parts
    if req_id is None or isinstance(req_id, bool) or not isinstance(req_id, (str, int)):
        return None
    return ParsedJsonRpcRequest(method=method, params=params, req_id=req_id)


def is_jsonrpc_notification(msg: object) -> bool:
    """True when *msg* is a JSON-RPC notification (no response expected)."""
    # CrossHair TypeError on typing.Literal['a','b','rc'] (FV §8.1 D).
    parts = _jsonrpc_parts(msg)
    return parts is not None and parts[2] is None


def jsonrpc_success(req_id: RequestId, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def jsonrpc_failure(req_id: RequestId, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def initialize_result(*, protocol_version: str, server_version: str, instructions: str) -> dict[str, Any]:
    return {
        "protocolVersion": protocol_version,
        "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}, "prompts": {"listChanged": False}},
        "serverInfo": {"name": "WriterAgent MCP", "version": server_version},
        "instructions": instructions,
    }


@dataclasses.dataclass(frozen=True)
class CallToolRequestParams:
    name: str
    arguments: dict[str, Any]

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> CallToolRequestParams:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("tools/call requires params.name")
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("tools/call params.arguments must be an object")
        return CallToolRequestParams(name=name, arguments=dict(arguments))


def call_tool_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["isError"] = True
    return out


def call_tool_result_image(data_b64: str, mime_type: str = "image/png", *, is_error: bool = False) -> dict[str, Any]:
    """Tool result carrying an IMAGE content block (MCP image type) instead of text.

    Used by get_image so a vision-capable client receives the picture itself rather than base64
    pasted as text. data_b64 is the raw base64 (no data: URI prefix), per the MCP image content shape."""
    out: dict[str, Any] = {"content": [{"type": "image", "data": data_b64, "mimeType": mime_type}]}
    if is_error:
        out["isError"] = True
    return out

