# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
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
"""SSE snapshot merge for streaming tool calls.

``accumulate_delta`` and ``coalesce_split_tool_calls`` run inside the LLM
client while it builds an assistant message. The UI drain never calls them.
Re-exported from ``async_stream``.
"""

from __future__ import annotations

from typing import Any, cast

from plugin.framework.deal_shim import deal

# ── Streaming Delta Accumulation (OpenAI-Compatible) ───────────────


# Portions below copied from openai-python (https://github.com/openai/openai-python)
# src/openai/lib/streaming/_deltas.py
# License: Apache 2.0 (https://github.com/openai/openai-python/blob/main/LICENSE)


@deal.pre(lambda acc, delta: type(acc) is dict and type(delta) is dict)
@deal.post(lambda result: isinstance(result, dict))
@deal.raises(TypeError, RuntimeError)
def accumulate_delta(acc: dict[object, object], delta: dict[object, object]) -> dict[object, object]:
    """Merge a streaming chunk delta into an accumulated message/snapshot.

    Required for tool-calling: used in stream_request_with_tools to build the full
    assistant message from SSE chunks. Content and tool_calls (with partial
    function.arguments) are merged by index; strings are concatenated.
    """
    # Recursive merge of unbounded nested dicts/strings hangs deep check even with
    # a top-level len cap. Pytest still runs @deal; check-all skips this entry.
    # crosshair: off
    if type(acc) is not dict or type(delta) is not dict:
        raise TypeError("accumulate_delta requires plain dict acc and delta")
    for key, delta_value in delta.items():
        if key not in acc:
            acc[key] = delta_value
            continue

        acc_value = acc[key]
        if acc_value is None:
            acc[key] = delta_value
            continue

        # the `index` property is used in arrays of objects so it should
        # not be accumulated like other values e.g.
        # [{'foo': 'bar', 'index': 0}]
        #
        # the same applies to `type` properties as they're used for
        # discriminated unions
        if key == "index" or key == "type":
            acc[key] = delta_value
            continue

        if isinstance(acc_value, str) and isinstance(delta_value, str):
            acc_value += delta_value
        elif isinstance(acc_value, (int, float)) and isinstance(delta_value, (int, float)):
            acc_value += delta_value
        elif isinstance(acc_value, dict) and isinstance(delta_value, dict):
            acc_value = accumulate_delta(cast("dict[object, object]", acc_value), cast("dict[object, object]", delta_value))
        elif isinstance(acc_value, list) and isinstance(delta_value, list):
            # for lists of non-dictionary items we'll only ever get new entries
            # in the array, existing entries will never be changed
            if all(isinstance(x, (str, int, float)) for x in acc_value):
                cast("list[Any]", acc_value).extend(delta_value)
                continue

            for delta_entry in delta_value:
                if not isinstance(delta_entry, dict):
                    raise TypeError(f"Unexpected list delta entry is not a dictionary: {delta_entry}")

                try:
                    index = cast("dict[str, Any]", delta_entry)["index"]
                except KeyError as exc:
                    raise RuntimeError(f"Expected list delta entry to have an `index` key; {delta_entry}") from exc

                if not isinstance(index, int):
                    raise TypeError(f"Unexpected, list delta entry `index` value is not an integer; {index}")

                try:
                    acc_entry = cast("list[Any]", acc_value)[index]
                except IndexError:
                    cast("list[Any]", acc_value).insert(index, delta_entry)
                else:
                    if not isinstance(acc_entry, dict):
                        raise TypeError("not handled yet")

                    cast("list[Any]", acc_value)[index] = accumulate_delta(cast("dict[object, object]", acc_entry), cast("dict[object, object]", delta_entry))

        acc[key] = acc_value

    return acc


def coalesce_split_tool_calls(tool_calls: object) -> list[Any]:
    """Merge provider stream-split phantom tool_calls into the prior real call.

    OpenRouter/gpt-oss-120b:nitro streaming sometimes emits a second tool_call
    delta with a new ``index``, empty ``id``, empty ``function.name``, and the
    remainder of the previous call's JSON ``arguments``. Without coalescing,
    the empty-name call is executed (UNKNOWN_TOOL / ``tool_call_id: ""``) and
    the next API round 400s with ``tool_calls[n].function.name must be a
    non-empty string``.

    Walks ``tool_calls`` in order. Empty/missing ``function.name`` entries
    append their ``function.arguments`` string onto the previous kept call and
    are dropped. With no previous kept call, the empty-name entry is dropped.
    Kept calls are re-indexed from 0.
    """
    if not isinstance(tool_calls, list):
        return []
    kept: list[dict[str, Any]] = []
    # ty: isinstance(list) on object, then isinstance(dict) on items, infers
    # dict[Never, Never] so .get("function") is invalid-argument-type.
    for raw in cast("list[Any]", tool_calls):
        if not isinstance(raw, dict):
            continue
        tc = cast("dict[str, Any]", raw)
        fn_raw = tc.get("function")
        fn = cast("dict[str, Any]", fn_raw if isinstance(fn_raw, dict) else {})
        name = fn.get("name")
        if name:
            kept_tc = dict(tc)
            kept_fn = dict(fn)
            kept_tc["function"] = kept_fn
            kept.append(kept_tc)
            continue
        if not kept:
            continue
        args = fn.get("arguments")
        if args is None:
            args = ""
        elif not isinstance(args, str):
            args = str(args)
        prev_fn = kept[-1]["function"]
        prev_args = prev_fn.get("arguments")
        if prev_args is None:
            prev_args = ""
        elif not isinstance(prev_args, str):
            prev_args = str(prev_args)
        prev_fn["arguments"] = prev_args + args
    for i, tc in enumerate(kept):
        tc["index"] = i
    return kept
