"""Contracts sidecar for plugin.mcp.wire_types."""

from __future__ import annotations

import deal

from plugin.mcp import wire_types as _prod
from plugin.mcp.wire_types import ParsedJsonRpcRequest
from tests.contracts.bounds import (
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_SOURCE,
    DEAL_MAX_TOKEN,
    _profile,
    ascii_bounded,
    str_bounded,
)


def _deal_jsonrpc_msg_ok_crosshair(msg: object) -> bool:
    return not isinstance(msg, dict) or (
        len(msg) <= DEAL_MAX_CMD_ARGS and all(type(k) is str and ascii_bounded(k, DEAL_MAX_TOKEN) for k in msg)
    )


_deal_jsonrpc_msg_ok = _profile(_deal_jsonrpc_msg_ok_crosshair)


def _deal_jsonrpc_failure_ok_pytest(message: object, code: object) -> bool:
    return isinstance(message, str) and isinstance(code, int)


def _deal_jsonrpc_failure_ok_crosshair(message: object, code: object) -> bool:
    return str_bounded(message, DEAL_MAX_SOURCE) and isinstance(code, int)


_deal_jsonrpc_failure_ok = _profile(_deal_jsonrpc_failure_ok_crosshair, _deal_jsonrpc_failure_ok_pytest)


def _deal_image_b64_ok_pytest(data_b64: object, mime_type: object = "image/png") -> bool:
    return isinstance(data_b64, str) and isinstance(mime_type, str) and len(mime_type) >= 1


def _deal_image_b64_ok_crosshair(data_b64: object, mime_type: object = "image/png") -> bool:
    return str_bounded(data_b64, DEAL_MAX_SOURCE) and ascii_bounded(mime_type, DEAL_MAX_TOKEN, min_len=1)


_deal_image_b64_ok = _profile(_deal_image_b64_ok_crosshair, _deal_image_b64_ok_pytest)

parse_jsonrpc_request = deal.pre(lambda msg: _deal_jsonrpc_msg_ok(msg))(
    deal.post(lambda result: result is None or isinstance(result, ParsedJsonRpcRequest))(
        _prod.parse_jsonrpc_request
    )
)

is_jsonrpc_notification = deal.post(lambda result: isinstance(result, bool))(
    _prod.is_jsonrpc_notification
)

jsonrpc_success = deal.pre(
    lambda req_id, result: isinstance(result, dict) and len(result) <= DEAL_MAX_CMD_ARGS
)(
    deal.post(
        lambda result: isinstance(result, dict)
        and result.get("jsonrpc") == "2.0"
        and "result" in result
    )(_prod.jsonrpc_success)
)

jsonrpc_failure = deal.pre(
    lambda req_id, code, message, data=None: _deal_jsonrpc_failure_ok(message, code)
)(
    deal.post(
        lambda result: isinstance(result, dict)
        and result.get("jsonrpc") == "2.0"
        and "error" in result
    )(_prod.jsonrpc_failure)
)

initialize_result = deal.pre(
    lambda *, protocol_version, server_version, instructions: (
        (not isinstance(protocol_version, str) or str_bounded(protocol_version, DEAL_MAX_TOKEN))
        and str_bounded(server_version, DEAL_MAX_TOKEN)
        and str_bounded(instructions, DEAL_MAX_SOURCE)
    )
)(
    deal.post(
        lambda result: isinstance(result, dict)
        and "protocolVersion" in result
        and "capabilities" in result
    )(_prod.initialize_result)
)

call_tool_result_image = deal.pre(
    lambda data_b64, mime_type="image/png", is_error=False: _deal_image_b64_ok(data_b64, mime_type)
)(
    deal.post(lambda result: isinstance(result, dict) and "content" in result)(
        _prod.call_tool_result_image
    )
)

PYTEST = [
    "parse_jsonrpc_request",
    "is_jsonrpc_notification",
    "jsonrpc_success",
    "jsonrpc_failure",
    "initialize_result",
    "call_tool_result_image",
]

TARGETS = [
    f"{__name__}.jsonrpc_success",
    f"{__name__}.jsonrpc_failure",
    f"{__name__}.initialize_result",
    f"{__name__}.call_tool_result_image",
]
