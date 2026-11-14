# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / CrossHair (FQN) for stream_delta.accumulate_delta."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.framework.stream_delta import accumulate_delta
from plugin.framework.async_stream import _format_agent_tool_stream_line
from tests.harness.crosshair import assert_crosshair_clean


def test_format_agent_tool_stream_line_basic() -> None:
    res = _format_agent_tool_stream_line("TOOL:", {"arg": 1})
    assert res.startswith("\nTOOL:")
    assert res.endswith("\n")
    assert '"arg": 1' in res


@given(
    prefix=st.text(alphabet=st.characters(blacklist_categories=("Cs",), max_codepoint=127), min_size=1, max_size=10),
    data=st.one_of(st.text(max_size=20), st.integers(), st.dictionaries(st.text(max_size=5), st.integers())),
)
@settings(max_examples=50)
def test_hypothesis_format_agent_tool_stream_line_invariants(prefix: str, data: Any) -> None:
    res = _format_agent_tool_stream_line(prefix, data)
    assert isinstance(res, str)
    assert res.startswith("\n") and res.endswith("\n")
    assert prefix in res

_CROSSHAIR_TARGET = "plugin.framework.stream_delta.accumulate_delta"


@given(parts=st.lists(st.text(max_size=20), min_size=1, max_size=6))
@settings(max_examples=60)
def test_hypothesis_content_concat(parts: list[str]) -> None:
    acc: dict = {}
    for part in parts:
        accumulate_delta(acc, {"content": part})
    assert acc["content"] == "".join(parts)


@pytest.mark.slow
def test_crosshair_accumulate_delta_fqn_if_available() -> None:
    assert_crosshair_clean(_CROSSHAIR_TARGET)
