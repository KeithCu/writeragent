# WriterAgent tests for eval-2 Calc-primary model writer prompt
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
_TASK_ID = "5f6c57dd-feb6-4e70-b152-4969d92d1608"
_GOLD = _REPO / "docs" / "eval" / "gdpval" / _TASK_ID
_EXP = _REPO / "docs" / "eval" / "eval-2" / "calc-primary-model"
_WRITER_PROMPT = _EXP / "prompt.writeragent.txt"
_GDPVAL_PROMPT = _EXP / "prompt.gdpval.txt"
_GOLD_PROMPT = _GOLD / "prompt.txt"
_GOLD_XLSX = (
    _GOLD
    / "reference_files"
    / "52dfa090145b2077b0434571f616f4b1"
    / "Raw Data for Branch Profitability Final.xlsx"
)
_FIXTURE_XLSX = _EXP / "fixtures" / "Raw Data for Branch Profitability Final.xlsx"
_FIXTURE_ODS = _EXP / "fixtures" / "Raw Data for Branch Profitability Final.ods"
_SSML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"

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
    "create_sheet",
    "write_formula_range",
)
_FIXTURE_KEYS = (
    "Branch 1",
    "Branch 10",
    "Corporate",
    "Revenue (Units)",
    "Shared Service Allocations",
    "Implementation Hours",
    "Project Backlog (Units)",
    "Labor COGs",
    "M23",
    "M24",
)


def _xlsx_sheet_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        return [s.get("name") or "" for s in wb.findall(f"{{{_SSML}}}sheets/{{{_SSML}}}sheet")]


def _ods_table_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("content.xml"))
    return [
        table.get(f"{{{_TABLE_NS}}}name") or ""
        for table in root.findall(f".//{{{_TABLE_NS}}}table")
    ]


def _ods_cell_strings(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("content.xml"))
    return {"".join(para.itertext()) for para in root.findall(f".//{{{_TEXT_NS}}}p")}


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
    assert "spreadsheet titled ‘Raw Data’" in writer
    assert "Raw Data sheet" in writer
    assert "attached Excel spreadsheet" in gold
    assert "built in Excel" in gold
    assert "built in Excel" not in writer
    assert "Microsoft Excel" not in writer
    assert "create a new" not in writer.lower()


def test_writer_prompt_keeps_gold_claim_and_avoids_steering() -> None:
    text = _WRITER_PROMPT.read_text(encoding="utf-8")
    for heading in (
        "Income statement comparing",
        "Monthly trended income statement",
        "Branch ranking analysis",
        "Regional comparison",
        "Efficiency Metrics",
        "M23 and M24",
        "Regions A through G",
        "average revenue per unit (ARPU)",
        "positive variance",
        "negative variance",
    ):
        assert heading in text
    lowered = text.lower()
    for banned in _BANNED_STEERING:
        assert banned not in lowered, banned
    for banned in _BANNED_PRODUCT_INTERNALS:
        assert banned not in lowered, banned


def test_fixtures_exist_and_match_gold_xlsx() -> None:
    assert _GOLD_XLSX.is_file(), _GOLD_XLSX
    assert _FIXTURE_XLSX.is_file()
    assert _FIXTURE_ODS.is_file()
    assert _FIXTURE_XLSX.read_bytes() == _GOLD_XLSX.read_bytes()
    assert _xlsx_sheet_names(_FIXTURE_XLSX) == ["Raw Data"]
    assert _ods_table_names(_FIXTURE_ODS) == ["Raw Data"]


def test_fixture_keys_are_cell_text() -> None:
    ods_text = _ods_cell_strings(_FIXTURE_ODS)
    for key in _FIXTURE_KEYS:
        assert key in ods_text, f"ods fixture missing {key!r}"


def test_gold_tree_stays_unedited_reference() -> None:
    """Eval-2 copies the xlsx; do not rewrite the gdpval tree."""
    assert _xlsx_sheet_names(_GOLD_XLSX) == ["Raw Data"]
    assert _GOLD_XLSX.is_file()
