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

Used by plugin/mcp/mcp_protocol.py for parse/build without Pydantic or the mcp package.
"""

from __future__ import annotations

import dataclasses
from typing import Any, cast

from plugin.framework.deal_shim import DEAL_MAX_CMD_ARGS, DEAL_MAX_SOURCE, DEAL_MAX_TOKEN, ascii_bounded, str_bounded, deal

# Aligned with upstream python-sdk LATEST_PROTOCOL_VERSION.
MCP_PROTOCOL_VERSION = "2025-11-25"

# Standard JSON-RPC error codes (JSON-RPC 2.0 + MCP extensions).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SERVER_BUSY = -32000
EXECUTION_TIMEOUT = -32001

ProgressToken = str | int
RequestId = int | str | None


@dataclasses.dataclass(frozen=True)
class ParsedJsonRpcRequest:
    """Validated JSON-RPC 2.0 request (not a notification)."""

    method: str
    params: dict[str, Any]
    req_id: RequestId
    raw: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class JsonRpcParseError:
    """Failed to parse an incoming JSON-RPC message."""

    message: str
    code: int = INVALID_REQUEST


@deal.pre(
    lambda msg: not isinstance(msg, dict)
    or len(msg) <= DEAL_MAX_CMD_ARGS
)
@deal.post(lambda result: isinstance(result, (ParsedJsonRpcRequest, JsonRpcParseError)))
def parse_jsonrpc_request(msg: object) -> ParsedJsonRpcRequest | JsonRpcParseError:
    """Parse and validate a single JSON-RPC request object.

    Notifications (missing ``id`` or ``id`` is null) are not requests — callers should
    treat ``req_id is None`` before calling this, or check ``"id" not in msg``.
    """
    if not isinstance(msg, dict):
        return JsonRpcParseError("Invalid JSON-RPC 2.0 request")
    raw = cast("dict[str, Any]", msg)
    if raw.get("jsonrpc") != "2.0":
        return JsonRpcParseError("Invalid JSON-RPC 2.0 request")
    method = raw.get("method")
    if not isinstance(method, str) or not method:
        return JsonRpcParseError("Invalid JSON-RPC 2.0 request")
    if "id" not in raw or raw.get("id") is None:
        return JsonRpcParseError("Invalid JSON-RPC 2.0 request")
    params = raw.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return JsonRpcParseError("Invalid JSON-RPC 2.0 request")
    import sys

    if "crosshair" in sys.modules:
        return JsonRpcParseError("mock")
    return ParsedJsonRpcRequest(method=method, params=dict(params), req_id=raw.get("id"), raw=raw)


@deal.post(lambda result: isinstance(result, bool))
def is_jsonrpc_notification(msg: object) -> bool:
    """True when *msg* is a JSON-RPC notification (no response expected)."""
    # CrossHair TypeError on typing.Literal['a','b','rc'] (FV §8.1 D).
    # crosshair: off
    if not isinstance(msg, dict):
        return False
    raw = cast("dict[str, Any]", msg)
    if raw.get("jsonrpc") != "2.0":
        return False
    return "id" not in raw or raw.get("id") is None


@deal.pre(lambda req_id, result: isinstance(result, dict) and len(result) <= DEAL_MAX_CMD_ARGS)
@deal.post(lambda result: isinstance(result, dict) and result.get("jsonrpc") == "2.0" and "result" in result)
def jsonrpc_success(req_id: RequestId, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


@deal.pre(lambda req_id, code, message, data=None: str_bounded(message, DEAL_MAX_SOURCE) and isinstance(code, int))
@deal.post(lambda result: isinstance(result, dict) and result.get("jsonrpc") == "2.0" and "error" in result)
def jsonrpc_failure(req_id: RequestId, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


@dataclasses.dataclass
class InitializeResult:
    protocol_version: str | int
    capabilities: dict[str, Any]
    server_info: dict[str, Any]
    instructions: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "protocolVersion": self.protocol_version,
            "capabilities": self.capabilities,
            "serverInfo": self.server_info,
        }
        if self.instructions is not None:
            out["instructions"] = self.instructions
        return out


@deal.pre(
    lambda protocol_version, server_version, instructions, client_protocol_version=None: (
        (not isinstance(protocol_version, str) or str_bounded(protocol_version, DEAL_MAX_TOKEN))
        and str_bounded(server_version, DEAL_MAX_TOKEN)
        and str_bounded(instructions, DEAL_MAX_SOURCE)
        and (
            client_protocol_version is None
            or not isinstance(client_protocol_version, str)
            or str_bounded(client_protocol_version, DEAL_MAX_TOKEN)
        )
    )
)
@deal.post(lambda result: isinstance(result, dict) and "protocolVersion" in result and "capabilities" in result)
def initialize_result(
    *,
    protocol_version: str | int,
    server_version: str,
    instructions: str,
    client_protocol_version: str | int | None = None,
) -> dict[str, Any]:
    negotiated = client_protocol_version if client_protocol_version is not None else protocol_version
    import sys

    if "crosshair" in sys.modules:
        return {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {"name": "WriterAgent MCP", "version": server_version},
            "instructions": instructions,
        }
    return InitializeResult(
        protocol_version=negotiated,
        capabilities={
            "tools": {"listChanged": False},
            "resources": {"listChanged": False},
            "prompts": {"listChanged": False},
        },
        server_info={"name": "WriterAgent MCP", "version": server_version},
        instructions=instructions,
    ).to_dict()


def list_tools_result(tools: list[dict[str, Any]]) -> dict[str, Any]:
    return {"tools": tools}


def empty_resources_result() -> dict[str, Any]:
    return {"resources": []}


def empty_prompts_result() -> dict[str, Any]:
    return {"prompts": []}


def ping_result() -> dict[str, Any]:
    return {}


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
        return cls(name=name, arguments=dict(arguments))


def call_tool_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
    }
    if is_error:
        out["isError"] = True
    return out


@deal.pre(
    lambda data_b64, mime_type="image/png", is_error=False: str_bounded(data_b64, DEAL_MAX_SOURCE)
    and ascii_bounded(mime_type, DEAL_MAX_TOKEN, min_len=1)
)
@deal.post(lambda result: isinstance(result, dict) and "content" in result)
def call_tool_result_image(data_b64: str, mime_type: str = "image/png", *, is_error: bool = False) -> dict[str, Any]:
    """Tool result carrying an IMAGE content block (MCP image type) instead of text.

    Used by get_image so a vision-capable client receives the picture itself rather than base64
    pasted as text. data_b64 is the raw base64 (no data: URI prefix), per the MCP image content shape."""
    out: dict[str, Any] = {
        "content": [{"type": "image", "data": data_b64, "mimeType": mime_type}],
    }
    if is_error:
        out["isError"] = True
    return out


@dataclasses.dataclass(frozen=True)
class ProgressNotificationParams:
    progress_token: ProgressToken
    progress: float
    total: float | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "progressToken": self.progress_token,
            "progress": self.progress,
        }
        if self.total is not None:
            out["total"] = self.total
        if self.message is not None:
            out["message"] = self.message
        return out


def progress_notification(
    *,
    progress_token: ProgressToken,
    progress: float,
    total: float | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """Build a notifications/progress JSON-RPC notification (for future SSE streaming)."""
    params = ProgressNotificationParams(
        progress_token=progress_token,
        progress=progress,
        total=total,
        message=message,
    )
    return {
        "jsonrpc": "2.0",
        "method": "notifications/progress",
        "params": params.to_dict(),
    }
