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

from plugin.framework.deal_shim import DEAL_MAX_CMD_ARGS, DEAL_MAX_SOURCE, DEAL_MAX_TOKEN, UNDER_CROSSHAIR, _profile, ascii_bounded, str_bounded, deal

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
    raw: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class JsonRpcParseError:
    """Failed to parse an incoming JSON-RPC message."""

    message: str
    code: int = INVALID_REQUEST


def _deal_jsonrpc_msg_ok_crosshair(msg: object) -> bool:
    return not isinstance(msg, dict) or (
        len(msg) <= DEAL_MAX_CMD_ARGS and all(type(k) is str and ascii_bounded(k, DEAL_MAX_TOKEN) for k in msg)
    )


_deal_jsonrpc_msg_ok = _profile(_deal_jsonrpc_msg_ok_crosshair)


def _jsonrpc_parts(msg: object) -> tuple[str, dict[str, Any], Any] | None:
    # Validate common JSON-RPC 2.0 structure for requests and notifications.
    # The old notification check accepted methodless/non-string-method messages as 202.
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


@deal.pre(lambda msg: _deal_jsonrpc_msg_ok(msg))
@deal.post(lambda result: isinstance(result, (ParsedJsonRpcRequest, JsonRpcParseError)))
def parse_jsonrpc_request(msg: object) -> ParsedJsonRpcRequest | JsonRpcParseError:
    """Parse and validate a single JSON-RPC request object.

    Notifications (missing ``id`` or ``id`` is null) are not requests — callers should
    treat ``req_id is None`` before calling this; a notification is a valid message with
    a missing or null id, and is_jsonrpc_notification uses the same validation.
    """
    # crosshair: off
    # Do not sniff sys.modules["crosshair"] (deal_shim): if that module is
    # loaded, the old branch returned JsonRpcParseError("mock") for every
    # valid request.
    parts = _jsonrpc_parts(msg)
    if parts is None:
        return JsonRpcParseError("Invalid JSON-RPC 2.0 request")
    method, params, req_id = parts
    if req_id is None or isinstance(req_id, bool) or not isinstance(req_id, (str, int)):
        return JsonRpcParseError("Invalid JSON-RPC 2.0 request")
    return ParsedJsonRpcRequest(method=method, params=params, req_id=req_id, raw=cast("dict[str, Any]", msg))


@deal.post(lambda result: isinstance(result, bool))
def is_jsonrpc_notification(msg: object) -> bool:
    """True when *msg* is a JSON-RPC notification (no response expected)."""
    # CrossHair TypeError on typing.Literal['a','b','rc'] (FV §8.1 D).
    # crosshair: off
    parts = _jsonrpc_parts(msg)
    return parts is not None and parts[2] is None


@deal.pre(lambda req_id, result: isinstance(result, dict) and len(result) <= DEAL_MAX_CMD_ARGS)
@deal.post(lambda result: isinstance(result, dict) and result.get("jsonrpc") == "2.0" and "result" in result)
def jsonrpc_success(req_id: RequestId, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _deal_jsonrpc_failure_ok_pytest(message: object, code: object) -> bool:
    # Tool errors are longer than DEAL_MAX_SOURCE. The body copies the
    # string into the error object. CrossHair keeps the short message.
    return isinstance(message, str) and isinstance(code, int)


def _deal_jsonrpc_failure_ok_crosshair(message: object, code: object) -> bool:
    return str_bounded(message, DEAL_MAX_SOURCE) and isinstance(code, int)


_deal_jsonrpc_failure_ok = _profile(_deal_jsonrpc_failure_ok_crosshair, _deal_jsonrpc_failure_ok_pytest)


@deal.pre(lambda req_id, code, message, data=None: _deal_jsonrpc_failure_ok(message, code))
@deal.post(lambda result: isinstance(result, dict) and result.get("jsonrpc") == "2.0" and "error" in result)
def jsonrpc_failure(req_id: RequestId, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


@dataclasses.dataclass
class InitializeResult:
    protocol_version: str
    capabilities: dict[str, Any]
    server_info: dict[str, Any]
    instructions: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"protocolVersion": self.protocol_version, "capabilities": self.capabilities, "serverInfo": self.server_info}
        if self.instructions is not None:
            out["instructions"] = self.instructions
        return out


def _deal_initialize_client_version_ok_crosshair(client_protocol_version: object) -> bool:
    return client_protocol_version is None or not isinstance(client_protocol_version, str) or str_bounded(client_protocol_version, DEAL_MAX_TOKEN)


_deal_initialize_client_version_ok = _profile(_deal_initialize_client_version_ok_crosshair)


@deal.pre(
    lambda protocol_version, server_version, instructions, client_protocol_version=None: (
        (not isinstance(protocol_version, str) or str_bounded(protocol_version, DEAL_MAX_TOKEN))
        and str_bounded(server_version, DEAL_MAX_TOKEN)
        and str_bounded(instructions, DEAL_MAX_SOURCE)
        and _deal_initialize_client_version_ok(client_protocol_version)
    )
)
@deal.post(lambda result: isinstance(result, dict) and "protocolVersion" in result and "capabilities" in result)
def initialize_result(*, protocol_version: str, server_version: str, instructions: str, client_protocol_version: str | None = None) -> dict[str, Any]:
    negotiated = client_protocol_version if client_protocol_version is not None else protocol_version
    return InitializeResult(protocol_version=negotiated, capabilities={"tools": {"listChanged": False}, "resources": {"listChanged": False}, "prompts": {"listChanged": False}}, server_info={"name": "WriterAgent MCP", "version": server_version}, instructions=instructions).to_dict()


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
        # crosshair: off
        return _call_tool_request_params_from_dict(params)


_DEAL_CALL_DICT_LEN = 2 if UNDER_CROSSHAIR else DEAL_MAX_CMD_ARGS
_DEAL_CALL_KEY_LEN = 4 if UNDER_CROSSHAIR else DEAL_MAX_TOKEN
_DEAL_CALL_VAL_LEN = 4 if UNDER_CROSSHAIR else DEAL_MAX_SOURCE


def _deal_call_tool_params_ok_crosshair(params: object) -> bool:
    if type(params) is not dict:
        return False
    # type() is dict narrows to Top[dict] (keys Never); ty then rejects Literal lookups.
    raw = cast("dict[str, Any]", params)
    if len(raw) > _DEAL_CALL_DICT_LEN:
        return False
    name = raw.get("name")
    if not isinstance(name, str) or not ascii_bounded(name, _DEAL_CALL_VAL_LEN, min_len=1):
        return False
    arguments = raw.get("arguments")
    if arguments is not None and type(arguments) is not dict:
        return False
    if not all(type(k) is str and ascii_bounded(k, _DEAL_CALL_KEY_LEN) for k in raw):
        return False
    for v in raw.values():
        if v is None:
            continue
        if isinstance(v, str) and ascii_bounded(v, _DEAL_CALL_VAL_LEN):
            continue
        if type(v) is dict and len(v) <= _DEAL_CALL_DICT_LEN and all(type(nk) is str and ascii_bounded(nk, _DEAL_CALL_KEY_LEN) for nk in v):
            continue
        return False
    return True


_deal_call_tool_params_ok = _profile(_deal_call_tool_params_ok_crosshair)


@deal.pre(lambda params: _deal_call_tool_params_ok(params))
def _call_tool_request_params_from_dict(params: dict[str, Any]) -> CallToolRequestParams:
    # crosshair: off
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


def _deal_image_b64_ok_pytest(data_b64: object, mime_type: object = "image/png") -> bool:
    # A real image is far longer than DEAL_MAX_SOURCE. The body wraps the
    # string. CrossHair keeps the short base64 domain.
    return isinstance(data_b64, str) and isinstance(mime_type, str) and len(mime_type) >= 1


def _deal_image_b64_ok_crosshair(data_b64: object, mime_type: object = "image/png") -> bool:
    return str_bounded(data_b64, DEAL_MAX_SOURCE) and ascii_bounded(mime_type, DEAL_MAX_TOKEN, min_len=1)


_deal_image_b64_ok = _profile(_deal_image_b64_ok_crosshair, _deal_image_b64_ok_pytest)


@deal.pre(lambda data_b64, mime_type="image/png", is_error=False: _deal_image_b64_ok(data_b64, mime_type))
@deal.post(lambda result: isinstance(result, dict) and "content" in result)
def call_tool_result_image(data_b64: str, mime_type: str = "image/png", *, is_error: bool = False) -> dict[str, Any]:
    """Tool result carrying an IMAGE content block (MCP image type) instead of text.

    Used by get_image so a vision-capable client receives the picture itself rather than base64
    pasted as text. data_b64 is the raw base64 (no data: URI prefix), per the MCP image content shape."""
    out: dict[str, Any] = {"content": [{"type": "image", "data": data_b64, "mimeType": mime_type}]}
    if is_error:
        out["isError"] = True
    return out

