"""Contracts sidecar for plugin.mcp.cors."""

from __future__ import annotations

import deal

from plugin.mcp import cors as _prod
from tests.contracts.bounds import (
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_ORIGIN,
    UNDER_CROSSHAIR,
    _profile,
    ascii_bounded,
    inverse_ensure,
)

_ORIGIN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:/.-[]")
_HEADER_LIST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-, _")


def _deal_origin_ok_pytest(origin: object) -> bool:
    return isinstance(origin, str)


def _deal_origin_ok_crosshair(origin: object) -> bool:
    return isinstance(origin, str) and len(origin) <= DEAL_MAX_ORIGIN and all(c in _ORIGIN_CHARS for c in origin)


_deal_origin_ok = _deal_origin_ok_crosshair if UNDER_CROSSHAIR else _deal_origin_ok_pytest


def _deal_allow_headers_ok_pytest(value: object) -> bool:
    return isinstance(value, str)


def _deal_allow_headers_ok_crosshair(value: object) -> bool:
    if not isinstance(value, str) or not ascii_bounded(value, DEAL_MAX_ORIGIN):
        return False
    if value.count(",") > DEAL_MAX_CMD_ARGS:
        return False
    return all(c in _HEADER_LIST_CHARS for c in value)


_deal_allow_headers_ok = (
    _deal_allow_headers_ok_crosshair if UNDER_CROSSHAIR else _deal_allow_headers_ok_pytest
)


def _deal_origins_config_ok_crosshair(value: object) -> bool:
    return (
        value is None
        or (isinstance(value, str) and _deal_origin_ok(value))
        or (
            isinstance(value, list)
            and len(value) <= DEAL_MAX_CMD_ARGS
            and all(_deal_origin_ok(item) for item in value)
        )
    )


_deal_origins_config_ok = _profile(_deal_origins_config_ok_crosshair)

normalize_cors_origin = deal.pre(lambda value: value is None or _deal_origin_ok(value))(
    deal.post(
        lambda result: result is None
        or (
            isinstance(result, str)
            and (result.lower().startswith("http://") or result.lower().startswith("https://"))
            and not result.endswith("/")
        )
    )(_prod.normalize_cors_origin)
)

normalize_origins_list = deal.pre(lambda value: _deal_origins_config_ok(value))(
    deal.post(lambda result: isinstance(result, list) and all(isinstance(x, str) for x in result))(
        inverse_ensure(lambda value, result: len(result) == len(set(result)))(
            _prod.normalize_origins_list
        )
    )
)

is_private_browser_origin = deal.pre(lambda origin: _deal_origin_ok(origin))(
    deal.post(lambda result: isinstance(result, bool))(_prod.is_private_browser_origin)
)

set_extra_allowed_origins = deal.pre(lambda origins: _deal_origins_config_ok(origins))(
    _prod.set_extra_allowed_origins
)

is_extra_allowed_origin = deal.pre(lambda origin: _deal_origin_ok(origin))(
    deal.post(lambda result: isinstance(result, bool))(_prod.is_extra_allowed_origin)
)

is_safe_origin = deal.pre(lambda origin: _deal_origin_ok(origin))(
    deal.post(lambda result: isinstance(result, bool))(_prod.is_safe_origin)
)

merge_allow_headers = deal.pre(
    lambda access_control_request_headers: access_control_request_headers is None
    or _deal_allow_headers_ok(access_control_request_headers)
)(
    deal.post(lambda result: isinstance(result, str))(_prod.merge_allow_headers)
)

PYTEST = [
    "normalize_cors_origin",
    "normalize_origins_list",
    "is_private_browser_origin",
    "set_extra_allowed_origins",
    "is_extra_allowed_origin",
    "is_safe_origin",
    "merge_allow_headers",
]

TARGETS: list[str] = []
