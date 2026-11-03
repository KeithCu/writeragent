# WriterAgent tests for eval-2 Floorstand Writer→Calc writer prompt
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
_TASK_ID = "c3525d4d-2012-45df-853e-2d2a0e902991"
_GOLD = _REPO / "docs" / "eval" / "gdpval" / _TASK_ID
_EXP = _REPO / "docs" / "eval" / "eval-2" / "writer-calc-peer-write"
_WRITER_PROMPT = _EXP / "prompt.writeragent.txt"
_GDPVAL_PROMPT = _EXP / "prompt.gdpval.txt"
_GOLD_PROMPT = _GOLD / "prompt.txt"
_GOLD_BUDGET = (
    _GOLD
    / "deliverable_files"
    / "242d24d95a28c9597ff7ddff19d31e6a"
    / "Deliverable Holiday Floorstand Budget.xlsx"
)
_GOLD_EMAIL = (
    _GOLD
    / "deliverable_files"
    / "3dcb8a9db76ff416cf0c731cd312d910"
    / "Final Email Deliverable.docx"
)
_TRAIL_DOCX = _EXP / "fixtures" / "Email Trail Floorstands.docx"
_TRAIL_ODT = _EXP / "fixtures" / "Email Trail Floorstands.odt"
_ORIG_XLSX = _EXP / "fixtures" / "Holiday Floorstand Store List Original.xlsx"
_ORIG_ODS = _EXP / "fixtures" / "Holiday Floorstand Store List Original.ods"
_MATRIX_XLSX = _EXP / "fixtures" / "Holiday Matrix final count.xlsx"
_MATRIX_ODS = _EXP / "fixtures" / "Holiday Matrix final count.ods"
_BUDGET_ODS = _EXP / "fixtures" / "Holiday Floorstand Budget.ods"
_TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"

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
    "specialize",
)


def test_gold_prompt_is_byte_copy_of_hf_tree() -> None:
    gold = _GOLD_PROMPT.read_bytes()
    exp = _GDPVAL_PROMPT.read_bytes()
    assert gold == exp
    row = json.loads((_GOLD / "task.json").read_text(encoding="utf-8"))
    assert row["task_id"] == _TASK_ID
    assert gold.decode("utf-8").rstrip("\n") == row["prompt"]


def test_writer_prompt_uses_open_writer_and_calc_not_excel_or_word() -> None:
    writer = _WRITER_PROMPT.read_text(encoding="utf-8")
    gold = _GDPVAL_PROMPT.read_text(encoding="utf-8")
    assert "this open Writer document" in writer
    assert "already open as a Calc document" in writer
    assert "Microsoft Word" not in writer
    assert "Word document format" not in writer
    assert "Please deliver an Excel file." in gold
    assert "Please deliver an Excel file." not in writer
    assert "source materials in this folder" in writer
    assert "Holiday Floorstand Store List Original in this folder" in writer
    assert "Holiday Matrix final count" in writer
    assert "Email Trail Floorstands" in writer


def test_writer_prompt_keeps_floorstand_claim_and_avoids_steering() -> None:
    text = _WRITER_PROMPT.read_text(encoding="utf-8")
    for heading in (
        "floor stands",
        "$0.25",
        "shelf strip",
        "Store 4099",
        "3737",
        "overage percentage",
        "original program cost",
        "revised program cost",
        "leave that cell blank rather than guessing",
    ):
        assert heading in text
    lowered = text.lower()
    for banned in _BANNED_STEERING:
        assert banned not in lowered, banned
    for banned in _BANNED_PRODUCT_INTERNALS:
        assert banned not in lowered, banned
    # Exact gold totals stay out of the product prompt (oracle cheat).
    assert "24,670.80" not in text
    assert "1,320" not in text
    assert "22,802.41" not in text


def test_gold_deliverable_bytes_match_experiment_copy() -> None:
    assert _GOLD_BUDGET.is_file(), _GOLD_BUDGET
    assert _GOLD_EMAIL.is_file(), _GOLD_EMAIL
    budget_copy = _EXP / "gold" / "Deliverable Holiday Floorstand Budget.xlsx"
    email_copy = _EXP / "gold" / "Final Email Deliverable.docx"
    assert budget_copy.read_bytes() == _GOLD_BUDGET.read_bytes()
    assert email_copy.read_bytes() == _GOLD_EMAIL.read_bytes()


def test_fixtures_exist_and_scaffold_is_not_gold_xlsx() -> None:
    assert _TRAIL_DOCX.is_file()
    assert _TRAIL_ODT.is_file()
    assert _ORIG_XLSX.is_file()
    assert _ORIG_ODS.is_file()
    assert _MATRIX_XLSX.is_file()
    assert _MATRIX_ODS.is_file()
    assert _BUDGET_ODS.is_file()
    gold_trail = (
        _GOLD
        / "reference_files"
        / "0d79e0cdd2e811609e73dcadc34d682f"
        / "Email Trail Floorstands.docx"
    )
    gold_orig = (
        _GOLD
        / "reference_files"
        / "123c0b1cf9e9b6ecfccc06501a205384"
        / "Holiday Floorstand Store List Original.xlsx"
    )
    gold_matrix = (
        _GOLD
        / "reference_files"
        / "8548106161db37c37ff079eb0eec6ff9"
        / "Holiday Matrix final count.xlsx"
    )
    assert _TRAIL_DOCX.read_bytes() == gold_trail.read_bytes()
    assert _ORIG_XLSX.read_bytes() == gold_orig.read_bytes()
    assert _MATRIX_XLSX.read_bytes() == gold_matrix.read_bytes()
    assert _BUDGET_ODS.read_bytes() != _GOLD_BUDGET.read_bytes()
    with zipfile.ZipFile(_BUDGET_ODS) as zf:
        root = ET.fromstring(zf.read("content.xml"))
    names = [
        table.get(f"{{{_TABLE_NS}}}name") or ""
        for table in root.iter(f"{{{_TABLE_NS}}}table")
    ]
    assert "Cost Comparison" in names
    assert "Final Store List" in names


def test_email_trail_odt_keeps_gold_anchors() -> None:
    with zipfile.ZipFile(_TRAIL_ODT) as zf:
        text = "".join(ET.fromstring(zf.read("content.xml")).itertext())
    assert "1228" in text
    assert "$17.69" in text
    assert "$5.65" in text
    assert "5%" in text
    assert "$22,802.41" in text
    assert "Shelf strips" in text
