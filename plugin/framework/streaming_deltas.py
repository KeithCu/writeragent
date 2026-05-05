from __future__ import annotations

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
# Copied from openai-python (https://github.com/openai/openai-python)
# src/openai/lib/streaming/_deltas.py
# License: Apache 2.0 (https://github.com/openai/openai-python/blob/main/LICENSE)
# Minimal local helpers (is_dict, is_list) added so we have no dependency on the SDK.

from typing import cast, Any


def _is_dict(x: object) -> bool:
    return isinstance(x, dict)


def _is_list(x: object) -> bool:
    return isinstance(x, list)


def accumulate_delta(acc: dict[object, object], delta: dict[object, object]) -> dict[object, object]:
    """Merge a streaming chunk delta into an accumulated message/snapshot.

    Required for tool-calling: used in stream_request_with_tools to build the full
    assistant message from SSE chunks. Content and tool_calls (with partial
    function.arguments) are merged by index; strings are concatenated.
    """
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
