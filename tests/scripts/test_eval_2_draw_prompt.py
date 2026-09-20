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
_WRITER_PROMPT = _EXP / "prompt.writeragent.txt"
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
    "get_draw_tree",
    "shape_upsert",
    "shape_connect",
)


def test_gold_prompt_is_byte_copy_of_hf_tree() -> None:
    gold = _GOLD_PROMPT.read_bytes()
    exp = _GDPVAL_PROMPT.read_bytes()
    assert gold == exp
    row = json.loads((_GOLD / "task.json").read_text(encoding="utf-8"))
    assert row["task_id"] == _TASK_ID
    assert gold.decode("utf-8").rstrip("\n") == row["prompt"]
    assert row["reference_files"] == []


def test_writer_prompt_uses_open_draw_not_pdf() -> None:
    writer = _WRITER_PROMPT.read_text(encoding="utf-8")
    gold = _GDPVAL_PROMPT.read_text(encoding="utf-8")
    assert "already-open Draw document" in writer
    assert "process map in PDF" in gold
    assert "process map in PDF" not in writer
    assert "Microsoft Word" not in writer
    assert "PresentationDocument" not in writer
    assert "Impress" not in writer


def test_writer_prompt_keeps_process_claim_and_avoids_steering() -> None:
    text = _WRITER_PROMPT.read_text(encoding="utf-8")
    for heading in (
        "Clearbend Logistics Hub",
        "automation-compatible",
        "manual processing",
        "decision point",
        "scanning",
        "failure handling",
    ):
        assert heading in text
    lowered = text.lower()
    for banned in _BANNED_STEERING:
        assert banned not in lowered, banned
    for banned in _BANNED_PRODUCT_INTERNALS:
        assert banned not in lowered, banned


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
