# WriterAgent tests for eval-2 fixture and gold-tree parity
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify eval-2 experiment trees stay aligned with checked-in GDPval gold trees."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_GDPVAL = _REPO / "docs" / "eval" / "gdpval"
_EVAL2 = _REPO / "docs" / "eval" / "eval-2"

_GOLD_PROMPT_CASES = [
    ("61b0946a-5c1c-4bf6-8607-84d7c7e0dfe0", "cadaver-proposal-61b0946a"),
    ("5f6c57dd-feb6-4e70-b152-4969d92d1608", "calc-primary-model"),
    ("8a7b6fca-60cc-4ae3-b649-971753cbf8b9", "draw-primary-deliverable"),
    ("c3525d4d-2012-45df-853e-2d2a0e902991", "writer-calc-peer-write"),
    ("58ac1cc5-5754-4580-8c9c-8c67e1a9d619", "gmp-change-control-58ac1cc5"),
    ("4520f882-715a-482d-8e87-1cb3cbdfe975", "reverse-tenant"),
    ("ed2bc14c-99ac-4a2a-8467-482a1a5d67f3", "tenant-retention-ed2bc14c"),
]

_GOLD_DELIVERABLE_CASES = [
    (
        "61b0946a-5c1c-4bf6-8607-84d7c7e0dfe0",
        "cadaver-proposal-61b0946a",
        "958655d3fbec4aa795d8f0d4c7ad4725/Collaborative Cadaver Program Proposal.docx",
        "Collaborative Cadaver Program Proposal.docx",
    ),
    (
        "8a7b6fca-60cc-4ae3-b649-971753cbf8b9",
        "draw-primary-deliverable",
        "70fc7c97c4f41c368c2aeb0227a194e0/Process Flow Map.pdf",
        "Process Flow Map.pdf",
    ),
    (
        "c3525d4d-2012-45df-853e-2d2a0e902991",
        "writer-calc-peer-write",
        "242d24d95a28c9597ff7ddff19d31e6a/Deliverable Holiday Floorstand Budget.xlsx",
        "Deliverable Holiday Floorstand Budget.xlsx",
    ),
    (
        "c3525d4d-2012-45df-853e-2d2a0e902991",
        "writer-calc-peer-write",
        "3dcb8a9db76ff416cf0c731cd312d910/Final Email Deliverable.docx",
        "Final Email Deliverable.docx",
    ),
    (
        "58ac1cc5-5754-4580-8c9c-8c67e1a9d619",
        "gmp-change-control-58ac1cc5",
        "4c01fb7cc03307958ff4682e0efa09ab/MR_Risk Assessment Summary Report.docx",
        "MR_Risk Assessment Summary Report.docx",
    ),
    (
        "58ac1cc5-5754-4580-8c9c-8c67e1a9d619",
        "gmp-change-control-58ac1cc5",
        "ba7264aa2c1cc72ecbdad75922076346/Change Control Form Filled.pdf",
        "Change Control Form Filled.pdf",
    ),
    (
        "4520f882-715a-482d-8e87-1cb3cbdfe975",
        "reverse-tenant",
        "9b5f97b8e386f6d87dcd42fe683d77b2/Theatre CBA.xlsx",
        "Theatre CBA.xlsx",
    ),
    (
        "ed2bc14c-99ac-4a2a-8467-482a1a5d67f3",
        "tenant-retention-ed2bc14c",
        "7d180b0d497f670b2066800e24b57665/Tenant Rentention Strategy.docx",
        "Tenant Rentention Strategy.docx",
    ),
]

_REFERENCE_FIXTURE_CASES = [
    (
        "61b0946a-5c1c-4bf6-8607-84d7c7e0dfe0",
        "cadaver-proposal-61b0946a",
        "9be06106acf0ff3002fa17addb379048/Cadaver Budget.xlsx",
        "Cadaver Budget.xlsx",
    ),
    (
        "5f6c57dd-feb6-4e70-b152-4969d92d1608",
        "calc-primary-model",
        "52dfa090145b2077b0434571f616f4b1/Raw Data for Branch Profitability Final.xlsx",
        "Raw Data for Branch Profitability Final.xlsx",
    ),
    (
        "c3525d4d-2012-45df-853e-2d2a0e902991",
        "writer-calc-peer-write",
        "0d79e0cdd2e811609e73dcadc34d682f/Email Trail Floorstands.docx",
        "Email Trail Floorstands.docx",
    ),
    (
        "c3525d4d-2012-45df-853e-2d2a0e902991",
        "writer-calc-peer-write",
        "123c0b1cf9e9b6ecfccc06501a205384/Holiday Floorstand Store List Original.xlsx",
        "Holiday Floorstand Store List Original.xlsx",
    ),
    (
        "c3525d4d-2012-45df-853e-2d2a0e902991",
        "writer-calc-peer-write",
        "8548106161db37c37ff079eb0eec6ff9/Holiday Matrix final count.xlsx",
        "Holiday Matrix final count.xlsx",
    ),
    (
        "58ac1cc5-5754-4580-8c9c-8c67e1a9d619",
        "gmp-change-control-58ac1cc5",
        "d4b383f877a2619cbc570dc276377ff1/Anti foam COA_MR.pdf",
        "Anti foam COA_MR.pdf",
    ),
    (
        "58ac1cc5-5754-4580-8c9c-8c67e1a9d619",
        "gmp-change-control-58ac1cc5",
        "81df0e569f4dd130e12e49d5e13e15a3/Material Spec_MR.docx",
        "Material Spec_MR.docx",
    ),
    (
        "58ac1cc5-5754-4580-8c9c-8c67e1a9d619",
        "gmp-change-control-58ac1cc5",
        "cdd2a8a216946f04ecb01de7f1650f02/Change Control Form.pdf",
        "Change Control Form.pdf",
    ),
    (
        "4520f882-715a-482d-8e87-1cb3cbdfe975",
        "reverse-tenant",
        "4d6d96f2061fc75357419dba98993b90/Sample roster and schedule.xlsx",
        "Sample roster and schedule.xlsx",
    ),
    (
        "4520f882-715a-482d-8e87-1cb3cbdfe975",
        "reverse-tenant",
        "4e2deede441818560dc6da2a5a98bd1d/CBA excerpt.docx",
        "CBA excerpt.docx",
    ),
]

_KNOWN_GDPVAL_IDS = {
    "ed2bc14c-99ac-4a2a-8467-482a1a5d67f3",
    "61b0946a-5c1c-4bf6-8607-84d7c7e0dfe0",
    "83d10b06-26d1-4636-a32c-23f92c57f30b",
    "58ac1cc5-5754-4580-8c9c-8c67e1a9d619",
    "c3525d4d-2012-45df-853e-2d2a0e902991",
    "5f6c57dd-feb6-4e70-b152-4969d92d1608",
    "a46d5cd2-55fe-48fa-a4c6-6aaf6b9991b5",
    "8a7b6fca-60cc-4ae3-b649-971753cbf8b9",
    "4520f882-715a-482d-8e87-1cb3cbdfe975",
}


@pytest.mark.parametrize(("task_id", "exp_dir"), _GOLD_PROMPT_CASES)
def test_gold_prompt_is_byte_copy_of_hf_tree(task_id: str, exp_dir: str) -> None:
    gold_dir = _GDPVAL / task_id
    gold = (gold_dir / "prompt.txt").read_bytes()
    exp = (_EVAL2 / exp_dir / "prompt.gdpval.txt").read_bytes()
    assert gold == exp
    row = json.loads((gold_dir / "task.json").read_text(encoding="utf-8"))
    assert row["task_id"] == task_id
    assert gold.decode("utf-8").rstrip("\n") == row["prompt"]


@pytest.mark.parametrize(
    ("task_id", "exp_dir", "gold_relpath", "exp_name"),
    _GOLD_DELIVERABLE_CASES,
)
def test_gold_deliverable_bytes_match_experiment_copy(
    task_id: str,
    exp_dir: str,
    gold_relpath: str,
    exp_name: str,
) -> None:
    gold_file = _GDPVAL / task_id / "deliverable_files" / gold_relpath
    exp_copy = _EVAL2 / exp_dir / "gold" / exp_name
    assert gold_file.is_file(), gold_file
    assert exp_copy.is_file(), exp_copy
    assert exp_copy.read_bytes() == gold_file.read_bytes()


@pytest.mark.parametrize(
    ("task_id", "exp_dir", "ref_relpath", "fixture_name"),
    _REFERENCE_FIXTURE_CASES,
)
def test_reference_fixture_bytes_match_gold(
    task_id: str,
    exp_dir: str,
    ref_relpath: str,
    fixture_name: str,
) -> None:
    gold_file = _GDPVAL / task_id / "reference_files" / ref_relpath
    exp_fixture = _EVAL2 / exp_dir / "fixtures" / fixture_name
    assert gold_file.is_file(), gold_file
    assert exp_fixture.is_file(), exp_fixture
    assert exp_fixture.read_bytes() == gold_file.read_bytes()


def test_native_slot_has_no_gdpval_gold_tree() -> None:
    exp_dir = _EVAL2 / "long-writer-pack"
    source = (exp_dir / "SOURCE.md").read_text(encoding="utf-8")
    for gold_id in _KNOWN_GDPVAL_IDS:
        assert f"docs/eval/gdpval/{gold_id}/" not in source, gold_id
    assert not (exp_dir / "prompt.gdpval.txt").exists()
    extra_trees = [
        path.name
        for path in _GDPVAL.iterdir()
        if path.is_dir() and path.name not in _KNOWN_GDPVAL_IDS
    ]
    assert extra_trees == []
