# WriterAgent tests for eval-2 Tenant Retention writer prompt
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
_TASK_ID = "ed2bc14c-99ac-4a2a-8467-482a1a5d67f3"
_GOLD = _REPO / "docs" / "eval" / "gdpval" / _TASK_ID
_EXP = _REPO / "docs" / "eval" / "eval-2" / "tenant-retention-ed2bc14c"
_WRITER_PROMPT = _EXP / "prompt.writeragent.txt"
_GDPVAL_PROMPT = _EXP / "prompt.gdpval.txt"
_GOLD_PROMPT = _GOLD / "prompt.txt"
_GOLD_DELIVERABLE = (
    _GOLD
    / "deliverable_files"
    / "7d180b0d497f670b2066800e24b57665"
    / "Tenant Rentention Strategy.docx"
)
_LETTER_DOCX = _EXP / "fixtures" / "Current Renewal Letter.docx"
_LETTER_ODT = _EXP / "fixtures" / "Current Renewal Letter.odt"
_SURVEY_XLSX = _EXP / "fixtures" / "Exit Survey Feedback.xlsx"
_SSML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

_BANNED_STEERING = (
    "don't invent",
    "do not invent",
    "don't make up",
    "from knowledge",
    "web not required",
    "already named",
)


def test_gold_prompt_is_byte_copy_of_hf_tree() -> None:
    gold = _GOLD_PROMPT.read_bytes()
    exp = _GDPVAL_PROMPT.read_bytes()
    assert gold == exp
    row = json.loads((_GOLD / "task.json").read_text(encoding="utf-8"))
    assert row["task_id"] == _TASK_ID
    assert gold.decode("utf-8").rstrip("\n") == row["prompt"]


def test_writer_prompt_uses_open_writer_doc_not_microsoft_word() -> None:
    writer = _WRITER_PROMPT.read_text(encoding="utf-8")
    gold = _GDPVAL_PROMPT.read_text(encoding="utf-8")
    assert "this open Writer document" in writer
    assert "Microsoft Word" not in writer
    assert "Microsoft Word" in gold
    assert 'The Excel file attached ("Exit Survey Feedback.xlsx")' in gold
    assert "The spreadsheet titled ‘Exit Survey Feedback’" in writer
    assert 'the attached ("Current Renewal Letter.docx")' in gold
    assert "the Current Renewal Letter in this folder" in writer


def test_writer_prompt_keeps_four_sections_and_avoids_steering() -> None:
    text = _WRITER_PROMPT.read_text(encoding="utf-8")
    for heading in (
        "Analysis of Departure Reasons",
        "Tiered Renewal Offer Structure",
        "Communication Plan",
        "Community Engagement Initiatives",
    ):
        assert heading in text
    lowered = text.lower()
    for banned in _BANNED_STEERING:
        assert banned not in lowered, banned


def test_gold_deliverable_keeps_rentention_typo() -> None:
    assert _GOLD_DELIVERABLE.is_file(), _GOLD_DELIVERABLE
    assert _GOLD_DELIVERABLE.name == "Tenant Rentention Strategy.docx"
    gold_copy = _EXP / "gold" / "Tenant Rentention Strategy.docx"
    assert gold_copy.is_file()
    assert gold_copy.read_bytes() == _GOLD_DELIVERABLE.read_bytes()
    # Eval-2 notes/oracle must not treat the typo as a required string.
    oracle = (_EXP / "rubric.eval2.md").read_text(encoding="utf-8")
    assert "Do **not** fail on the gold basename typo" in oracle or "Do **not** fail" in oracle


def test_fixtures_exist_and_survey_keeps_gold_sheet_typo() -> None:
    assert _LETTER_DOCX.is_file()
    assert _LETTER_ODT.is_file()
    assert _SURVEY_XLSX.is_file()
    with zipfile.ZipFile(_SURVEY_XLSX) as zf:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        names = [s.get("name") or "" for s in wb.findall(f"{{{_SSML}}}sheets/{{{_SSML}}}sheet")]
    assert names == ["Suvery Comments"]
