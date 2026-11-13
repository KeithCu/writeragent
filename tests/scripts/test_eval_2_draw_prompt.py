# WriterAgent tests for eval-2 Draw-primary process-map prompt
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Draw prompt deltas stay tiny; gold tree stays an untouched HF copy."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_REPO = Path(__file__).resolve().parents[2]
_TASK_ID = "8a7b6fca-60cc-4ae3-b649-971753cbf8b9"
_GOLD = _REPO / "docs" / "eval" / "gdpval" / _TASK_ID
_EXP = _REPO / "docs" / "eval" / "eval-2" / "draw-primary-deliverable"
_GDPVAL_PROMPT = _EXP / "prompt.gdpval.txt"
_GOLD_PROMPT = _GOLD / "prompt.txt"
_GOLD_DELIVERABLE = (
    _GOLD
    / "deliverable_files"
    / "70fc7c97c4f41c368c2aeb0227a194e0"
    / "Process Flow Map.pdf"
)
_CANVAS_ODG = _EXP / "fixtures" / "Process Flow Map.odg"
_GOLD_PDF = _EXP / "gold" / "Process Flow Map.pdf"
_DRAW_NS = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"


def test_gold_prompt_is_byte_copy_of_hf_tree() -> None:
    gold = _GOLD_PROMPT.read_bytes()
    exp = _GDPVAL_PROMPT.read_bytes()
    assert gold == exp
    row = json.loads((_GOLD / "task.json").read_text(encoding="utf-8"))
    assert row["task_id"] == _TASK_ID
    assert gold.decode("utf-8").rstrip("\n") == row["prompt"]
    assert row["reference_files"] == []


def test_gold_deliverable_bytes_match_experiment_copy() -> None:
    assert _GOLD_DELIVERABLE.is_file(), _GOLD_DELIVERABLE
    assert _GOLD_PDF.is_file(), _GOLD_PDF
    assert _GOLD_PDF.read_bytes() == _GOLD_DELIVERABLE.read_bytes()


def test_fixtures_exist_and_stand_in_is_not_gold_pdf() -> None:
    assert _CANVAS_ODG.is_file()
    assert _GOLD_PDF.is_file()
    assert _CANVAS_ODG.read_bytes() != _GOLD_PDF.read_bytes()
    with zipfile.ZipFile(_CANVAS_ODG) as zf:
        root = ET.fromstring(zf.read("content.xml"))
        media = zf.read("mimetype").decode("ascii")
    assert media == "application/vnd.oasis.opendocument.graphics"
    names = [
        frame.get(f"{{{_DRAW_NS}}}name") or ""
        for frame in root.iter(f"{{{_DRAW_NS}}}frame")
    ]
    assert "title" in names
    joined = "".join(root.itertext())
    assert "Process Flow Map" in joined
    # Blank canvas must not leak the gold process claim.
    assert "Clearbend" not in joined
    assert "automation" not in joined.lower()
