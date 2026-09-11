# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Named-slice injection for the live MIPROv2 student (no OpenRouter)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from dataset import DATA_SORTING, TAX_COLUMN, filter_examples, parse_task_id_filter
from eval_catalog import apply_schema_patches, build_eval_tool_schemas
from eval_prompts import get_eval_system_prompt
from llm_chat_eval import run_llm_chat_eval
from optimize_slices import (
    DEFAULT_LLM_SLICE,
    apply_named_slice,
    get_slice_baseline,
    get_slice_spec,
    list_slice_names,
    schema_patches_for_slice,
)


def test_named_slices_cover_calc_and_writer_hooks() -> None:
    names = set(list_slice_names())
    assert {
        "calc_core",
        "writer_core",
        "write_formula_range",
        "write_formula_range.values",
        "sort_range",
        "full_prompt",
    } <= names
    assert DEFAULT_LLM_SLICE == "calc_core"
    assert get_slice_spec("sort_range").kind == "tool_description"
    assert get_slice_spec("write_formula_range.values").param_name == "values"


def test_unknown_slice_raises() -> None:
    with pytest.raises(ValueError, match="Unknown slice"):
        get_slice_spec("not_a_slice")


def test_calc_core_injection_replaces_fragment_on_calc_tasks() -> None:
    baseline = get_slice_baseline("calc_core")
    assert "SORT:" in baseline
    prompt, patches = apply_named_slice("tax_column", "calc_core", "SLICE_MARKER_XYZ")
    assert patches is None
    assert "SLICE_MARKER_XYZ" in prompt
    assert baseline not in prompt
    assert get_eval_system_prompt("tax_column") != prompt


def test_calc_core_injection_is_noop_on_writer_tasks() -> None:
    prompt, patches = apply_named_slice("table_from_mess", "calc_core", "SLICE_MARKER_XYZ")
    assert patches is None
    assert "SLICE_MARKER_XYZ" not in prompt
    assert prompt == get_eval_system_prompt("table_from_mess")


def test_sort_range_slice_patches_specialized_schema() -> None:
    marker = "SORT_SLICE_MARKER"
    patches = schema_patches_for_slice("sort_range", marker)
    assert patches == {"sort_range": {"description": marker}}
    prompt, applied = apply_named_slice("data_sorting", "sort_range", marker)
    assert prompt == get_eval_system_prompt("data_sorting")
    assert applied == patches
    schemas = apply_schema_patches(
        build_eval_tool_schemas(kind="calc", active_domain="ranges"), applied
    )
    fn = next(
        (row.get("function") or row)
        for row in schemas
        if (row.get("function") or row).get("name") == "sort_range"
    )
    assert fn["description"] == marker


def test_write_formula_range_values_slice() -> None:
    marker = "VALUES_SLICE_MARKER"
    prompt, patches = apply_named_slice("tax_column", "write_formula_range.values", marker)
    assert prompt == get_eval_system_prompt("tax_column")
    fn = next(
        (row.get("function") or row)
        for row in apply_schema_patches(build_eval_tool_schemas(kind="calc"), patches)
        if (row.get("function") or row).get("name") == "write_formula_range"
    )
    assert fn["parameters"]["properties"]["values"]["description"] == marker


def test_full_prompt_slice_replaces_entire_system_prompt() -> None:
    prompt, patches = apply_named_slice("tax_column", "full_prompt", "ONLY_THIS")
    assert prompt == "ONLY_THIS"
    assert patches is None


def test_live_eval_path_scripted_with_injected_slice() -> None:
    """Same student as run_eval --student scripted; slice hook must not break dispatch."""
    baseline = get_slice_baseline("calc_core")
    prompt, patches = apply_named_slice("tax_column", "calc_core", baseline)
    final, usage, error, trace = run_llm_chat_eval(
        system_prompt=prompt,
        document_content=TAX_COLUMN["document_content"],
        user_question=TAX_COLUMN["user_question"],
        endpoint="https://openrouter.ai/api/v1",
        api_key="",
        model="scripted",
        backend="string",
        student="scripted",
        task_id="tax_column",
        schema_patches=patches,
    )
    assert error is None
    assert final
    assert usage["total_tokens"] == 0
    assert any(entry.get("name") == "write_formula_range" for entry in trace)


def test_filter_examples_comma_list() -> None:
    assert parse_task_id_filter("data_sorting, tax_column") == ["data_sorting", "tax_column"]
    rows = filter_examples(task_ids=["data_sorting", "tax_column"])
    assert [r["task_id"] for r in rows] == ["data_sorting", "tax_column"]


def test_live_eval_student_constructs_and_runs_scripted() -> None:
    pytest.importorskip("dspy")
    from program_llm import LiveEvalStudent

    mod = LiveEvalStudent(
        slice_name="calc_core",
        student="scripted",
        model="scripted",
        endpoint="https://openrouter.ai/api/v1",
        api_key="",
    )
    assert "SORT:" in mod.get_slice_text()
    pred = mod(
        document_content=DATA_SORTING["document_content"],
        user_question=DATA_SORTING["user_question"],
        task_id="data_sorting",
    )
    assert pred.final_document
    assert pred.error is None
    assert pred.baseline_slice_len == len(get_slice_baseline("calc_core"))
    assert callable(pred.get_lm_usage)
    usage = pred.get_lm_usage()
    assert isinstance(usage, dict)


def test_make_judge_metric_applies_length_penalty_without_judge() -> None:
    from metric import make_judge_metric, slice_length_penalty

    metric = make_judge_metric(None, token_penalty_lambda=0.0)
    example = SimpleNamespace(
        document_content="x",
        user_question="y",
        task_id="tax_column",
        category="structural",
        expected_contains=[],
        reject_contains=[],
        gold_document="",
        rubric="",
        use_quality_judge=False,
    )
    pred = SimpleNamespace(
        final_document='{"error": "no"}',
        total_tokens=0,
        slice_text="a" * 40,
        baseline_slice_len=10,
    )
    # Hard gate fails (no oracle pass) so score is 0 before penalty.
    assert metric(example, pred) == 0.0
    assert slice_length_penalty("a" * 40, 10) == pytest.approx(1.0)
    assert slice_length_penalty("a" * 20, 10) == 0.0
