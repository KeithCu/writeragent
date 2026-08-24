# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Good fixtures pass result oracles; mutated fixtures fail. No API, no soffice."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from oracles import (  # noqa: E402
    check_oracle,
    uses_llm_judge,
)
from scripted_student import (  # noqa: E402
    _BULK_CLEANUP,
    _BULLET_CONSISTENCY,
    _COMMENT_MANAGEMENT,
    _FORMAT_PRESERVATION,
    _LOGICAL_REWRITING,
    _REFORMAT_RESUME,
    _SECTION_REFACTOR,
    _SMART_SUMMARIZATION,
    _STYLE_APPLICATION,
    _STYLE_CONSISTENCY,
    _TABLE_ENGINEERING,
    _TABLE_FROM_MESS,
)

_SORT_GOOD = json.dumps(
    {
        "status": "ok",
        "snapshot": True,
        "headers": ["Product", "Revenue"],
        "grid": [
            ["Product", "Revenue"],
            ["Tool", 2100],
            ["Widget", 1200],
            ["Device", 950],
            ["Gadget", 850],
        ],
    }
)
_SORT_BAD = json.dumps(
    {
        "status": "ok",
        "snapshot": True,
        "headers": ["Product", "Revenue"],
        "grid": [
            ["Product", "Revenue"],
            ["Widget", 1200],
            ["Tool", 2100],
            ["Device", 950],
            ["Gadget", 850],
        ],
    }
)
_TAX_GOOD = json.dumps(
    {
        "status": "ok",
        "snapshot": True,
        "headers": ["Item", "Price", "Tax"],
        "grid": [
            ["Item", "Price", "Tax"],
            ["Apple", 10, 0.8],
            ["Banana", 5, 0.4],
            ["Orange", 8, 0.64],
            ["Pear", 12.5, 1.0],
        ],
    }
)
_TAX_BAD = json.dumps(
    {
        "status": "ok",
        "snapshot": True,
        "headers": ["Item", "Price", "Tax"],
        "grid": [
            ["Item", "Price", "Tax"],
            ["Apple", 10, 1.0],
            ["Banana", 5, 0.5],
            ["Orange", 8, 0.8],
            ["Pear", 12.5, 1.25],
        ],
    }
)
_FLOW_GOOD = json.dumps(
    {
        "status": "ok",
        "tree": [
            {"text": "Start"},
            {"text": "Process: user login"},
            {"text": "Decision: credentials valid?"},
            {"text": "End"},
        ],
    }
)


@pytest.mark.parametrize(
    ("task_id", "doc"),
    [
        ("table_from_mess", _TABLE_FROM_MESS),
        ("table_engineering", _TABLE_ENGINEERING),
        ("bulk_cleanup", _BULK_CLEANUP),
        ("format_preservation", _FORMAT_PRESERVATION),
        ("style_application", _STYLE_APPLICATION),
        ("bullet_consistency", _BULLET_CONSISTENCY),
        ("style_consistency", _STYLE_CONSISTENCY),
        ("section_refactor", _SECTION_REFACTOR),
        ("comment_management", _COMMENT_MANAGEMENT),
        ("reformat_resume", _REFORMAT_RESUME),
        ("logical_rewriting", _LOGICAL_REWRITING),
        ("smart_summarization", _SMART_SUMMARIZATION),
        ("data_sorting", _SORT_GOOD),
        ("tax_column", _TAX_GOOD),
        ("flowchart_gen", _FLOW_GOOD),
    ],
)
def test_good_fixtures_pass(task_id: str, doc: str) -> None:
    assert check_oracle(task_id, doc) == []


@pytest.mark.parametrize(
    ("task_id", "doc", "needle"),
    [
        ("table_from_mess", _TABLE_FROM_MESS.replace("Total", "Subtotal").replace("$1458.46", "$1.00"), "Total"),
        ("table_engineering", _TABLE_ENGINEERING.replace("7.75", "0.00"), "7.75"),
        ("bulk_cleanup", _BULK_CLEANUP.replace("extra spaces", "extra  spaces"), "double space"),
        (
            "format_preservation",
            _FORMAT_PRESERVATION.replace("John Doe (legacy", "Jane Smith (legacy"),
            "legal",
        ),
        (
            "style_application",
            _STYLE_APPLICATION.replace("<p>Background</p>", "<h1>Background</h1>"),
            "Background",
        ),
        (
            "bullet_consistency",
            _BULLET_CONSISTENCY.replace("- First thing.", "* First thing"),
            "hyphen+period",
        ),
        (
            "section_refactor",
            _SECTION_REFACTOR.replace("<h1>Goal</h1>", "<h1>Conclusion</h1>"),
            "Conclusion",
        ),
        ("data_sorting", _SORT_BAD, "descending"),
        ("tax_column", _TAX_BAD, "8%"),
        ("logical_rewriting", _LOGICAL_REWRITING.replace("WriterAgent", "LocalWriter"), "LocalWriter"),
        ("flowchart_gen", json.dumps({"status": "ok", "tree": [{"text": "Start"}]}), "Process"),
    ],
)
def test_mutated_fixtures_fail(task_id: str, doc: str, needle: str) -> None:
    fails = check_oracle(task_id, doc)
    assert fails, f"{task_id} should fail on mutated fixture"
    assert any(needle.lower() in f.lower() for f in fails), (needle, fails)


def test_unsorted_input_fails_data_sorting() -> None:
    raw = "Product\tRevenue\nWidget\t1200\nGadget\t850\nTool\t2100\nDevice\t950"
    fails = check_oracle("data_sorting", raw)
    assert fails


def test_empty_doc_fails_structural() -> None:
    assert check_oracle("table_from_mess", "")
    assert check_oracle("bulk_cleanup", "hello")


def test_golds_pass_oracles() -> None:
    golds = json.loads((_PO / "gold_standards.json").read_text(encoding="utf-8"))
    for task_id, doc in golds.items():
        assert check_oracle(task_id, doc) == [], task_id


def test_whitespace_needles_are_ignored() -> None:
    from eval_core import _correctness_breakdown
    from types import SimpleNamespace

    ex = SimpleNamespace(
        task_id="format_preservation",
        expected_contains=[" ", "Jane Smith - Project Lead"],
        reject_contains=[" "],
    )
    score, missing, found_reject, oracle_failures = _correctness_breakdown(
        ex, _FORMAT_PRESERVATION
    )
    assert " " not in missing
    assert " " not in found_reject
    assert oracle_failures == []
    assert score == 1.0


def test_judge_only_for_creative() -> None:
    assert uses_llm_judge("reformat_resume", "creative")
    assert uses_llm_judge("logical_rewriting")
    assert uses_llm_judge("smart_summarization")
    assert not uses_llm_judge("table_from_mess", "structural")
    assert not uses_llm_judge("tax_column", "structural")
