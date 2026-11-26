# WriterAgent - AI Writing Assistant for LibreOffice
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
"""Unit tests for shared specialized Writer helpers."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.smol_agent import InnerCallRecorder
from plugin.contrib.smolagents.models import get_tool_json_schema
from plugin.contrib.smolagents.tools import validate_tool_arguments
from plugin.doc.specialized_catalog import InnerToolCall, normalize_inner_result
from plugin.framework.tool import ToolContext
from plugin.writer.specialized_base import SpecializedWorkflowFinished, index_error


def test_index_error_valid_index() -> None:
    assert index_error(0, 3, "footnote") is None
    assert index_error(1, 3, "footnote") is None
    assert index_error(2, 3, "footnote") is None


def test_index_error_out_of_bounds() -> None:
    assert index_error(-1, 3, "footnote") == "Invalid index -1. The document has 3 footnote(s)."
    assert index_error(3, 3, "footnote") is not None
    assert index_error(10, 3, "endnote") is not None
    assert index_error(0, 0, "footnote") is not None


@pytest.mark.parametrize(
    "value",
    [
        {"rows": 3, "note": "café"},
        ["a", "b"],
        42,
        True,
    ],
)
def test_workflow_finished_turns_structured_answer_into_json_text(value: Any) -> None:
    out = SpecializedWorkflowFinished().execute(MagicMock(), answer=value)
    assert isinstance(out["answer"], str)
    assert json.loads(out["answer"]) == value
    assert out["finished"] is True
    if isinstance(value, dict):
        assert "café" in out["answer"]


def test_workflow_finished_text_and_null_answers() -> None:
    tool = SpecializedWorkflowFinished()
    assert tool.execute(MagicMock(), answer="done")["answer"] == "done"
    assert tool.execute(MagicMock(), answer=None)["answer"] == ""


def test_workflow_finished_object_answer_finishes_inner_run() -> None:
    ctx = MagicMock(spec=ToolContext)
    ctx.doc_type = "writer"
    ctx.read_only_target = False
    calls: list[InnerToolCall] = []
    recorder = InnerCallRecorder(SpecializedWorkflowFinished(), ctx, calls)

    validate_tool_arguments(recorder, {"answer": {"rows": 3}})

    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        final = recorder(answer={"rows": 3}, sanitize_inputs_outputs=True)

    payload = normalize_inner_result(final, ["tables"], calls)
    assert payload["status"] == "ok"
    assert json.loads(payload["answer"]) == {"rows": 3}
    assert get_tool_json_schema(recorder)["function"]["parameters"]["properties"]["answer"]["type"] == "string"
