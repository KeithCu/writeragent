# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / CrossHair (FQN) for async_stream.accumulate_delta."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.framework.async_stream import accumulate_delta, _format_agent_tool_stream_line


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

_CROSSHAIR_ERROR_RE = re.compile(r": error:")
_CROSSHAIR_TARGET = "plugin.framework.async_stream.accumulate_delta"


def _find_crosshair() -> str | None:
    crosshair_path = shutil.which("crosshair")
    if crosshair_path:
        return crosshair_path
    venv_bin_ch = Path(".venv/bin/crosshair")
    if venv_bin_ch.exists():
        return str(venv_bin_ch)
    return None


@given(parts=st.lists(st.text(max_size=20), min_size=1, max_size=6))
@settings(max_examples=60)
def test_hypothesis_content_concat(parts: list[str]) -> None:
    acc: dict = {}
    for part in parts:
        accumulate_delta(acc, {"content": part})
    assert acc["content"] == "".join(parts)


def test_tool_call_args_concat_by_index() -> None:
    acc: dict = {}
    accumulate_delta(acc, {"tool_calls": [{"index": 0, "function": {"name": "f", "arguments": "hel"}}]})
    accumulate_delta(acc, {"tool_calls": [{"index": 0, "function": {"arguments": "lo"}}]})
    assert acc["tool_calls"][0]["function"]["arguments"] == "hello"
    assert acc["tool_calls"][0]["function"]["name"] == "f"


def test_chunked_equals_full_merge_content() -> None:
    chunks = [{"content": "Hel"}, {"content": "lo"}, {"content": "!"}]
    acc: dict = {}
    for c in chunks:
        accumulate_delta(acc, c)
    full = accumulate_delta({}, {"content": "Hello!"})
    assert acc == full


def test_raises_on_bad_list_delta() -> None:
    with pytest.raises(TypeError):
        accumulate_delta({"items": [{"index": 0, "x": 1}]}, {"items": ["bad"]})
    with pytest.raises(RuntimeError):
        accumulate_delta({"items": [{"index": 0, "x": 1}]}, {"items": [{"x": 2}]})


@pytest.mark.slow
def test_crosshair_accumulate_delta_fqn_if_available() -> None:
    crosshair_path = _find_crosshair()
    if not crosshair_path:
        pytest.skip("CrossHair concolic execution engine is not installed.")
    result = subprocess.run(
        [crosshair_path, "check", "-v", "--report_all", _CROSSHAIR_TARGET],
        capture_output=True,
        text=True,
        timeout=300,
    )
    combined = f"{result.stdout}\n{result.stderr}".strip()
    print(f"CrossHair output:\n{combined}")
    errors = [line for line in combined.splitlines() if _CROSSHAIR_ERROR_RE.search(line)]
    assert not errors, "CrossHair counterexamples found:\n" + "\n".join(errors)
    if result.returncode == 2:
        pytest.fail(f"CrossHair internal error (exit 2):\n{combined}")
