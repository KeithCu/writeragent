# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Playback tests for the scripted eval student (no soffice, no API)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from dataset import ALL_EXAMPLES, task_kind  # noqa: E402
from scripted_student import SCRIPTS, ScriptedStudent  # noqa: E402


def test_task_kind_from_task_id() -> None:
    assert task_kind("flowchart_gen") == "draw"
    assert task_kind("data_sorting") == "calc"
    assert task_kind("tax_column") == "calc"
    assert task_kind("table_from_mess") == "writer"


def test_scripts_cover_all_examples() -> None:
    ids = {ex["task_id"] for ex in ALL_EXAMPLES}
    assert ids <= set(SCRIPTS)


def test_playback_order_and_stop_on_content_only() -> None:
    student = ScriptedStudent("table_from_mess")
    first = student.request_with_tools([{"role": "user", "content": "x"}], tools=[])
    assert first["tool_calls"]
    assert first["tool_calls"][0]["function"]["name"] == "apply_document_content"
    second = student.request_with_tools([], tools=[])
    assert not second.get("tool_calls")
    assert second.get("content")
    third = student.request_with_tools([], tools=[])
    assert not third.get("tool_calls")


def test_unknown_task_raises() -> None:
    with pytest.raises(KeyError, match="nope"):
        ScriptedStudent("nope")


def test_tax_and_sort_use_production_names() -> None:
    sort_round = SCRIPTS["data_sorting"][0]
    assert sort_round["tool_calls"][0]["function"]["name"] == (
        "delegate_to_specialized_calc_toolset"
    )
    tax_names = [
        tc["function"]["name"]
        for rnd in SCRIPTS["tax_column"]
        for tc in (rnd.get("tool_calls") or [])
    ]
    assert "write_formula_range" in tax_names
    assert "get_sheet_summary" in tax_names


def _script_tool_names(task_id: str) -> list[str]:
    names: list[str] = []
    for rnd in SCRIPTS[task_id]:
        for tc in rnd.get("tool_calls") or []:
            names.append(tc["function"]["name"])
    return names


def test_scripted_specialized_names_match_schema_stages() -> None:
    from eval_catalog import build_eval_tool_schemas

    def schema_names(kind: str, domain: str | None = None) -> set[str]:
        rows = build_eval_tool_schemas(kind=kind, active_domain=domain)
        out: set[str] = set()
        for row in rows:
            fn = row.get("function") if isinstance(row.get("function"), dict) else None
            name = str((fn or {}).get("name") or row.get("name") or "")
            if name:
                out.add(name)
        return out

    core_draw = schema_names("draw")
    shapes = schema_names("draw", "shapes")
    core_calc = schema_names("calc")
    ranges = schema_names("calc", "ranges")
    flow = _script_tool_names("flowchart_gen")
    assert flow[0] == "delegate_to_specialized_draw_toolset"
    assert flow[0] in core_draw
    for name in flow[1:]:
        if name == "get_draw_tree":
            assert name in core_draw
        else:
            assert name in shapes, name
    sort_names = _script_tool_names("data_sorting")
    assert sort_names[0] == "delegate_to_specialized_calc_toolset"
    assert sort_names[0] in core_calc
    for name in sort_names[1:]:
        assert name in ranges, name


def test_scripted_flowchart_inner_loop_nests_shapes() -> None:
    from dataset import ALL_EXAMPLES
    from llm_chat_eval import run_llm_chat_eval

    ex = next(row for row in ALL_EXAMPLES if row["task_id"] == "flowchart_gen")
    _doc, _usage, err, trace = run_llm_chat_eval(
        system_prompt="eval",
        document_content=ex["document_content"],
        user_question=ex["user_question"],
        endpoint="https://openrouter.ai/api/v1",
        api_key="",
        model="scripted",
        backend="string",
        student="scripted",
        task_id="flowchart_gen",
        verbose=False,
    )
    assert err is None
    names = [item["name"] for item in trace]
    assert "delegate_to_specialized_draw_toolset" in names
    nested = [item for item in trace if item.get("nested")]
    assert any(item["name"] == "shape_upsert" for item in nested)
    assert any(item["name"] == "shape_connect" for item in nested)
    assert any(item["name"] == "specialized_workflow_finished" for item in nested)
    assert all(item.get("domain") == "shapes" for item in nested)


def test_calc_minimal_preset_still_runs_specialized_sort() -> None:
    """Outer allowlist must not strip ranges-domain schemas (don't break specialize)."""
    from dataset import ALL_EXAMPLES
    from llm_chat_eval import run_llm_chat_eval

    ex = next(row for row in ALL_EXAMPLES if row["task_id"] == "data_sorting")
    _doc, _usage, err, trace = run_llm_chat_eval(
        system_prompt="eval",
        document_content=ex["document_content"],
        user_question=ex["user_question"],
        endpoint="https://openrouter.ai/api/v1",
        api_key="",
        model="scripted",
        backend="string",
        student="scripted",
        task_id="data_sorting",
        tools_spec="calc_minimal",
        schema_density="skinny",
        verbose=False,
    )
    assert err is None
    names = [item["name"] for item in trace]
    assert "delegate_to_specialized_calc_toolset" in names
    nested = [item for item in trace if item.get("nested")]
    assert any(item["name"] == "sort_range" for item in nested)
    assert any(item["name"] == "specialized_workflow_finished" for item in nested)


def test_writer_minimal_scripted_apply_task() -> None:
    from dataset import ALL_EXAMPLES
    from llm_chat_eval import run_llm_chat_eval

    ex = next(row for row in ALL_EXAMPLES if row["task_id"] == "table_from_mess")
    _doc, _usage, err, trace = run_llm_chat_eval(
        system_prompt="eval",
        document_content=ex["document_content"],
        user_question=ex["user_question"],
        endpoint="https://openrouter.ai/api/v1",
        api_key="",
        model="scripted",
        backend="string",
        student="scripted",
        task_id="table_from_mess",
        tools_spec="writer_minimal",
        verbose=False,
    )
    assert err is None
    assert any(item["name"] == "apply_document_content" for item in trace)
