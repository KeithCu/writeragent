"""Contracts sidecar for plugin.chatbot.memory."""

from __future__ import annotations

import deal

from plugin.chatbot import memory as _prod
from tests.contracts.bounds import (
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_SOURCE,
    DEAL_MAX_TOKEN,
    _profile,
    str_bounded,
)


def _deal_memory_args_ok_crosshair(arguments: object) -> bool:
    return (isinstance(arguments, str) and str_bounded(arguments, DEAL_MAX_SOURCE)) or (
        isinstance(arguments, dict)
        and len(arguments) <= DEAL_MAX_CMD_ARGS
        and (not isinstance(arguments.get("key"), str) or str_bounded(arguments.get("key"), DEAL_MAX_TOKEN))
        and (not isinstance(arguments.get("content"), str) or str_bounded(arguments.get("content"), DEAL_MAX_SOURCE))
    )


_deal_memory_args_ok = _profile(_deal_memory_args_ok_crosshair)


def _deal_memory_chat_line_ok_crosshair(arguments: object) -> bool:
    if isinstance(arguments, str):
        return str_bounded(arguments, DEAL_MAX_SOURCE)
    get = getattr(arguments, "get", None)
    if not callable(get):
        return False
    key = get("key")
    content = get("content")
    return (not isinstance(key, str) or str_bounded(key, DEAL_MAX_TOKEN)) and (
        not isinstance(content, str) or str_bounded(content, DEAL_MAX_SOURCE)
    )


_deal_memory_chat_line_ok = _profile(_deal_memory_chat_line_ok_crosshair)

upsert_memory_arguments_dict = deal.pre(
    lambda arguments, *_unused, **__: _deal_memory_args_ok(arguments)
)(
    deal.post(lambda result: result is None or isinstance(result, dict))(
        _prod.upsert_memory_arguments_dict
    )
)

format_upsert_memory_chat_line = deal.pre(
    lambda arguments: _deal_memory_chat_line_ok(arguments)
)(
    deal.post(lambda result: isinstance(result, str) and result.endswith("\n"))(
        _prod.format_upsert_memory_chat_line
    )
)

PYTEST = [
    "upsert_memory_arguments_dict",
    "format_upsert_memory_chat_line",
]

TARGETS: list[str] = []
