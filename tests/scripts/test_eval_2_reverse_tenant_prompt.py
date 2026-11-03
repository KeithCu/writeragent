# WriterAgent tests for eval-2 Reverse Tenant (Theatre CBA) prompt
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
_TASK_ID = "4520f882-715a-482d-8e87-1cb3cbdfe975"
_GOLD = _REPO / "docs" / "eval" / "gdpval" / _TASK_ID
_EXP = _REPO / "docs" / "eval" / "eval-2" / "reverse-tenant"
_WRITER_PROMPT = _EXP / "prompt.writeragent.txt"
_GDPVAL_PROMPT = _EXP / "prompt.gdpval.txt"
_GOLD_PROMPT = _GOLD / "prompt.txt"
_GOLD_DELIVERABLE = (
    _GOLD
    / "deliverable_files"
    / "9b5f97b8e386f6d87dcd42fe683d77b2"
    / "Theatre CBA.xlsx"
)
_CBA_DOCX = _EXP / "fixtures" / "CBA excerpt.docx"
_CBA_ODT = _EXP / "fixtures" / "CBA excerpt.odt"
_ROSTER_XLSX = _EXP / "fixtures" / "Sample roster and schedule.xlsx"
_ROSTER_ODS = _EXP / "fixtures" / "Sample roster and schedule.ods"
_SSML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

_BANNED_STEERING = (
    "don't invent",
    "do not invent",
    "don't make up",
    "from knowledge",
    "web not required",
    "already named",
)
_BANNED_PRODUCT_INTERNALS = (
    "peer_inner",
    "send_peer_work",
    "send_peer_result",
    "fill_draw_fields",
    "document_research",
    "specialized_workflow_finished",
)


def test_gold_prompt_is_byte_copy_of_hf_tree() -> None:
    gold = _GOLD_PROMPT.read_bytes()
    exp = _GDPVAL_PROMPT.read_bytes()
    assert gold == exp
    row = json.loads((_GOLD / "task.json").read_text(encoding="utf-8"))
    assert row["task_id"] == _TASK_ID
    assert gold.decode("utf-8").rstrip("\n") == row["prompt"]


def test_writer_prompt_uses_open_workbook_not_new_excel() -> None:
    writer = _WRITER_PROMPT.read_text(encoding="utf-8")
    gold = _GDPVAL_PROMPT.read_text(encoding="utf-8")
    assert "this open workbook" in writer
    assert "already open and is the deliverable" in writer
    assert "CBA excerpt" in writer
    assert "in this folder" in writer
    assert "Do not rewrite that Writer document" in writer
    assert "build a spreadsheet in Excel" in gold
    assert "build a spreadsheet in Excel" not in writer
    assert "have been attached as reference materials" in gold
    assert "are in this folder as reference materials" in writer


def test_writer_prompt_keeps_theatre_cba_claim_and_avoids_steering() -> None:
    text = _WRITER_PROMPT.read_text(encoding="utf-8")
    for heading in (
        "theatre",
        "local musicians",
        "Broadway",
        "collective bargaining agreement",
        "music contractor",
        "weekly payroll",
        "sample roster and schedule",
        "orchestra configuration",
        "payroll categories",
    ):
        assert heading in text
    lowered = text.lower()
    for banned in _BANNED_STEERING:
        assert banned not in lowered, banned
    for banned in _BANNED_PRODUCT_INTERNALS:
        assert banned not in lowered, banned


def test_gold_deliverable_bytes_match_experiment_copy() -> None:
    assert _GOLD_DELIVERABLE.is_file(), _GOLD_DELIVERABLE
    gold_copy = _EXP / "gold" / "Theatre CBA.xlsx"
    assert gold_copy.is_file()
    assert gold_copy.read_bytes() == _GOLD_DELIVERABLE.read_bytes()


def test_fixtures_exist_and_roster_keeps_gold_sheet() -> None:
    assert _CBA_DOCX.is_file()
    assert _CBA_ODT.is_file()
    assert _ROSTER_XLSX.is_file()
    assert _ROSTER_ODS.is_file()
    gold_roster = (
        _GOLD
        / "reference_files"
        / "4d6d96f2061fc75357419dba98993b90"
        / "Sample roster and schedule.xlsx"
    )
    gold_cba = (
        _GOLD
        / "reference_files"
        / "4e2deede441818560dc6da2a5a98bd1d"
        / "CBA excerpt.docx"
    )
    assert _ROSTER_XLSX.read_bytes() == gold_roster.read_bytes()
    assert _CBA_DOCX.read_bytes() == gold_cba.read_bytes()
    with zipfile.ZipFile(_ROSTER_XLSX) as zf:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        names = [s.get("name") or "" for s in wb.findall(f"{{{_SSML}}}sheets/{{{_SSML}}}sheet")]
    assert names == ["Sheet1"]


def test_cba_odt_keeps_gold_anchors() -> None:
    with zipfile.ZipFile(_CBA_ODT) as zf:
        joined = "".join(ET.fromstring(zf.read("content.xml")).itertext())
    for needle in (
        "ARTICLE 4 - WAGES",
        "$251.06",
        "$2008.50",
        "$55.67",
        "$76.59",
        "$137.87",
        "Trumpet/Horn",
        "Doubling",
        "Vacation Pay",
        "6:30 p.m.",
        "9:00 a.m.",
        "Sound Check",
    ):
        assert needle in joined, needle
