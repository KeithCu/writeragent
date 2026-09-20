# WriterAgent tests for eval-2 GMP Change Control writer prompt
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
_TASK_ID = "58ac1cc5-5754-4580-8c9c-8c67e1a9d619"
_GOLD = _REPO / "docs" / "eval" / "gdpval" / _TASK_ID
_EXP = _REPO / "docs" / "eval" / "eval-2" / "gmp-change-control-58ac1cc5"
_WRITER_PROMPT = _EXP / "prompt.writeragent.txt"
_GDPVAL_PROMPT = _EXP / "prompt.gdpval.txt"
_GOLD_PROMPT = _GOLD / "prompt.txt"
_GOLD_REPORT = (
    _GOLD
    / "deliverable_files"
    / "4c01fb7cc03307958ff4682e0efa09ab"
    / "MR_Risk Assessment Summary Report.docx"
)
_GOLD_FORM = (
    _GOLD
    / "deliverable_files"
    / "ba7264aa2c1cc72ecbdad75922076346"
    / "Change Control Form Filled.pdf"
)
_COA_PDF = _EXP / "fixtures" / "Anti foam COA_MR.pdf"
_SPEC_DOCX = _EXP / "fixtures" / "Material Spec_MR.docx"
_SPEC_ODT = _EXP / "fixtures" / "Material Spec_MR.odt"
_FORM_ODG = _EXP / "fixtures" / "Change Control Form.odg"
_BLANK_PDF = _EXP / "fixtures" / "Change Control Form.pdf"
_DRAW_NS = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"

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


def test_writer_prompt_uses_open_writer_and_draw_not_pdf_or_word() -> None:
    writer = _WRITER_PROMPT.read_text(encoding="utf-8")
    gold = _GDPVAL_PROMPT.read_text(encoding="utf-8")
    assert "this open Writer document" in writer
    assert "already open as a Draw document" in writer
    assert "Microsoft Word" not in writer
    assert "separate PDF" not in writer
    assert "separate Word document" in gold
    assert "Attach the completed form as a separate PDF document." in gold
    assert "source materials in this folder" in writer


def test_writer_prompt_keeps_gmp_claim_and_avoids_steering() -> None:
    text = _WRITER_PROMPT.read_text(encoding="utf-8")
    for heading in (
        "QY-GEL Antifoam",
        "CompCello",
        "RMS-3333",
        "Endotoxin Level: Report Result",
        "< 1 EU/ml",
        "QA Escalation Email",
        "Internal Summary Note",
        "left the company",
        "centralized vendor communication tracking",
    ):
        assert heading in text
    lowered = text.lower()
    for banned in _BANNED_STEERING:
        assert banned not in lowered, banned
    for banned in _BANNED_PRODUCT_INTERNALS:
        assert banned not in lowered, banned


def test_gold_deliverable_bytes_match_experiment_copy() -> None:
    assert _GOLD_REPORT.is_file(), _GOLD_REPORT
    assert _GOLD_FORM.is_file(), _GOLD_FORM
    report_copy = _EXP / "gold" / "MR_Risk Assessment Summary Report.docx"
    form_copy = _EXP / "gold" / "Change Control Form Filled.pdf"
    assert report_copy.read_bytes() == _GOLD_REPORT.read_bytes()
    assert form_copy.read_bytes() == _GOLD_FORM.read_bytes()


def test_fixtures_exist_and_stand_in_is_not_gold_pdf() -> None:
    assert _COA_PDF.is_file()
    assert _SPEC_DOCX.is_file()
    assert _SPEC_ODT.is_file()
    assert _FORM_ODG.is_file()
    assert _BLANK_PDF.is_file()
    gold_coa = (
        _GOLD
        / "reference_files"
        / "d4b383f877a2619cbc570dc276377ff1"
        / "Anti foam COA_MR.pdf"
    )
    gold_spec = (
        _GOLD
        / "reference_files"
        / "81df0e569f4dd130e12e49d5e13e15a3"
        / "Material Spec_MR.docx"
    )
    gold_blank = (
        _GOLD
        / "reference_files"
        / "cdd2a8a216946f04ecb01de7f1650f02"
        / "Change Control Form.pdf"
    )
    assert _COA_PDF.read_bytes() == gold_coa.read_bytes()
    assert _SPEC_DOCX.read_bytes() == gold_spec.read_bytes()
    assert _BLANK_PDF.read_bytes() == gold_blank.read_bytes()
    assert _FORM_ODG.read_bytes() != _BLANK_PDF.read_bytes()
    with zipfile.ZipFile(_FORM_ODG) as zf:
        root = ET.fromstring(zf.read("content.xml"))
    names = [
        frame.get(f"{{{_DRAW_NS}}}name") or ""
        for frame in root.iter(f"{{{_DRAW_NS}}}frame")
    ]
    assert "fld_change_title" in names
    assert "fld_current_situation" in names


def test_spec_odt_keeps_gold_anchors() -> None:
    with zipfile.ZipFile(_SPEC_ODT) as zf:
        text = ET.fromstring(zf.read("content.xml")).itertext()
    joined = "".join(text)
    assert "RMS-3333" in joined
    assert "CompCello" in joined
    assert "QY-GEL" in joined
    assert "< 1 EU/ml" in joined
