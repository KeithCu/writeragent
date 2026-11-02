# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.client.stream_normalizer."""

from __future__ import annotations

from typing import Any

import deal

from plugin.framework.client import stream_normalizer as _prod
from tests.contracts.bounds import DEAL_MAX_SHAPE_DIM


def _normalize_delta_tool_calls_ok(delta: dict[str, Any]) -> bool:
    tool_calls = delta.get("tool_calls")
    if type(tool_calls) is not list:
        return True
    for tc in tool_calls:
        if type(tc) is not dict:
            continue
        if tc.get("type") is None:
            return False
        fn = tc.get("function")
        if type(fn) is dict and fn.get("arguments") is None:
            return False
    return True


accumulate_streaming_thinking = deal.ensure(
    lambda text_parts, meta, delta, result: meta.get("source") is None
    or meta.get("source") in _prod._STREAMING_SOURCE_VALUES
)(
    deal.pre(
        lambda text_parts, meta, delta: isinstance(text_parts, list)
        and type(meta) is dict
        and (meta.get("source") is None or meta.get("source") in _prod._STREAMING_SOURCE_VALUES)
    )(_prod.accumulate_streaming_thinking)
)

_merge_reasoning_details = deal.ensure(
    lambda entries, result: len(result) <= sum(1 for e in entries if type(e) is dict)
)(
    deal.post(lambda result: isinstance(result, list))(
        deal.pre(lambda entries: isinstance(entries, list))(_prod._merge_reasoning_details)
    )
)

_streaming_replay = deal.post(
    lambda result: isinstance(result, dict)
    and set(result.keys()) <= {"reasoning", "reasoning_content", "reasoning_details"}
)(_prod._streaming_replay)

_normalize_stream_delta = deal.post(lambda result: isinstance(result, dict))(_prod._normalize_stream_delta)

_thinking_text_from_delta = deal.post(lambda result: isinstance(result, str))(
    deal.pre(lambda delta: isinstance(delta, dict))(_prod._thinking_text_from_delta)
)

normalize_delta = deal.ensure(
    lambda delta, result: type(delta) is not dict or _normalize_delta_tool_calls_ok(delta)
)(
    deal.ensure(
        lambda delta, result: type(delta) is not dict or "role" not in delta or delta.get("role") is not None
    )(
        deal.pre(
            lambda delta: type(delta) is not dict
            or (
                len(delta) <= DEAL_MAX_SHAPE_DIM
                and (not isinstance(delta.get("tool_calls"), list) or len(delta["tool_calls"]) <= DEAL_MAX_SHAPE_DIM)
            )
        )(_prod.normalize_delta)
    )
)

strip_think_tags = deal.ensure(
    lambda text, result: _prod._THINK_TAG_BLOCK_RE.search(result[0]) is None
)(
    deal.post(
        lambda result: isinstance(result, tuple)
        and len(result) == 2
        and isinstance(result[0], str)
        and (result[1] is None or isinstance(result[1], str))
    )(_prod.strip_think_tags)
)

PYTEST = [
    "accumulate_streaming_thinking",
    "_merge_reasoning_details",
    "_streaming_replay",
    "_normalize_stream_delta",
    "_thinking_text_from_delta",
    "normalize_delta",
    "strip_think_tags",
]
TARGETS = [f"{__name__}.normalize_delta"]
