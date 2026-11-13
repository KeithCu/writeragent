# WriterAgent tests for eval-2 Cadaver Proposal writer prompt
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Writer prompt deltas stay tiny; gold tree stays an untouched HF copy."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_REPO = Path(__file__).resolve().parents[2]
_TASK_ID = "61b0946a-5c1c-4bf6-8607-84d7c7e0dfe0"
_GOLD = _REPO / "docs" / "eval" / "gdpval" / _TASK_ID
_EXP = _REPO / "docs" / "eval" / "eval-2" / "cadaver-proposal-61b0946a"
_GDPVAL_PROMPT = _EXP / "prompt.gdpval.txt"
_GOLD_PROMPT = _GOLD / "prompt.txt"
_GOLD_DELIVERABLE = (
    _GOLD
    / "deliverable_files"
    / "958655d3fbec4aa795d8f0d4c7ad4725"
    / "Collaborative Cadaver Program Proposal.docx"
)
_BUDGET_XLSX = _EXP / "fixtures" / "Cadaver Budget.xlsx"
_BUDGET_ODS = _EXP / "fixtures" / "Cadaver Budget.ods"
_SSML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def test_gold_prompt_is_byte_copy_of_hf_tree() -> None:
    gold = _GOLD_PROMPT.read_bytes()
    exp = _GDPVAL_PROMPT.read_bytes()
    assert gold == exp
    row = json.loads((_GOLD / "task.json").read_text(encoding="utf-8"))
    assert row["task_id"] == _TASK_ID
    assert gold.decode("utf-8").rstrip("\n") == row["prompt"]


def test_gold_deliverable_bytes_match_experiment_copy() -> None:
    assert _GOLD_DELIVERABLE.is_file(), _GOLD_DELIVERABLE
    gold_copy = _EXP / "gold" / "Collaborative Cadaver Program Proposal.docx"
    assert gold_copy.is_file()
    assert gold_copy.read_bytes() == _GOLD_DELIVERABLE.read_bytes()


def test_fixtures_exist_and_budget_keeps_gold_sheet_and_cells() -> None:
    assert _BUDGET_XLSX.is_file()
    assert _BUDGET_ODS.is_file()
    gold_xlsx = (
        _GOLD
        / "reference_files"
        / "9be06106acf0ff3002fa17addb379048"
        / "Cadaver Budget.xlsx"
    )
    assert _BUDGET_XLSX.read_bytes() == gold_xlsx.read_bytes()
    with zipfile.ZipFile(_BUDGET_XLSX) as zf:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        names = [s.get("name") or "" for s in wb.findall(f"{{{_SSML}}}sheets/{{{_SSML}}}sheet")]
    assert names == ["Sheet1"]
    from openpyxl import load_workbook

    sheet = load_workbook(_BUDGET_XLSX, data_only=True)["Sheet1"]
    assert sheet["D2"].value == 2000
    assert sheet["C2"].value == 4
    assert sheet["B17"].value == "Annual Cadaver Lab Fee"
    assert sheet["D17"].value == 1000
