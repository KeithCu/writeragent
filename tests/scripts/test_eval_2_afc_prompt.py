# WriterAgent tests for eval-2 AFC writer prompt column axes
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Writer prompt axes and step-3 keys come from the Population fixture, not gold."""
from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_AFC = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "eval"
    / "eval-2"
    / "afc-sample-83d10b06"
)
_GOLD_POPULATION = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "eval"
    / "gdpval"
    / "83d10b06-26d1-4636-a32c-23f92c57f30b"
    / "reference_files"
    / "cc781e4dc0985c8eb327a53ec03b5900"
    / "Population v2.xlsx"
)
_WRITER_PROMPT = _AFC / "prompt.writeragent.txt"
_GDPVAL_PROMPT = _AFC / "prompt.gdpval.txt"
_POPULATION_XLSX = _AFC / "fixtures" / "Population v2.xlsx"
_POPULATION_ODS = _AFC / "fixtures" / "Population v2.ods"
_SSML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"

# Locked eval-2 one-liner (fixture G=Q3, H=Q2; in-workbook J/K).
_AXES = "Q2 is in H, Q3 is in G; variance = (G−H)/H into J; flags in K"

# Step-3 exact keys after the 2026-09-07 smoother-path cut. Each must be
# cell text in the fixture so Flash/Gemini string search can hit.
_STEP3_FIXTURE_KEYS = (
    "Willett Bank Rome",
    "Willett Bank Athens",
    "Willett Bank Lux",
    "Willett Bank Brazil",
    "Willett Bank UAE",
    "Terrorist Financing breaches",
    "Proliferation Financing breaches",
    "Marine Finance",
    "Correspondent Banking",
    "Cayman Islands",
    "Pakistan",
    "UAE",
)

# GDPVal composites that are *not* fixture cell text. Writer prompt must
# drop them; gold prompt keeps them.
_GDPVAL_ONLY_KEYS = (
    "CB Cash Italy",
    "CB Correspondent Banking Greece",
    "IB Debt Markets Luxembourg",
    "CB Trade Finance Brazil",
    "PB EMEA UAE",
    "A1 and C1",
    "Trade Finance",
)


def _xlsx_shared_strings(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        shared_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        shared: list[str] = []
        for si in shared_root.findall(f"{{{_SSML}}}si"):
            shared.append("".join(t.text or "" for t in si.findall(f".//{{{_SSML}}}t")))
        return shared


def _xlsx_sheet_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        return [s.get("name") or "" for s in wb.findall(f"{{{_SSML}}}sheets/{{{_SSML}}}sheet")]


def _xlsx_header_row(path: Path) -> list[str]:
    """First-row labels from an xlsx shared-string sheet. No LibreOffice."""
    shared = _xlsx_shared_strings(path)
    with zipfile.ZipFile(path) as zf:
        sheet = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
        row = sheet.find(f"{{{_SSML}}}sheetData/{{{_SSML}}}row")
        assert row is not None, f"no rows in {path}"
        headers: list[str] = []
        for cell in row.findall(f"{{{_SSML}}}c"):
            value = cell.findtext(f"{{{_SSML}}}v") or ""
            if cell.get("t") == "s" and value.isdigit():
                headers.append(shared[int(value)])
            else:
                headers.append(value)
        return headers


def _ods_table_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("content.xml"))
    return [
        table.get(f"{{{_TABLE_NS}}}name") or ""
        for table in root.findall(f".//{{{_TABLE_NS}}}table")
    ]


def _ods_database_range_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("content.xml"))
    return [
        rng.get(f"{{{_TABLE_NS}}}name") or ""
        for rng in root.findall(f".//{{{_TABLE_NS}}}database-range")
    ]


def _ods_cell_strings(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("content.xml"))
    return {"".join(p.itertext()) for p in root.findall(f".//{{{_TEXT_NS}}}p")}


def test_writer_prompt_uses_fixture_qh_axes() -> None:
    text = _WRITER_PROMPT.read_text(encoding="utf-8")
    assert _AXES in text
    assert "columns H and I" not in text
    # Gold copy stays the GDPval wording; do not “fix” it by letter-shift.
    gold = _GDPVAL_PROMPT.read_text(encoding="utf-8")
    assert "columns H and I" in gold


def test_population_fixture_headers_are_g_q3_h_q2() -> None:
    headers = _xlsx_header_row(_POPULATION_XLSX)
    assert headers[:8] == [
        "No",
        "Division",
        "Sub-Division",
        "Country",
        "Legal Entity",
        "KRIs",
        "Q3 2024 KRI",
        "Q2 2024 KRI",
    ]
    # A–H only: I is not a Population field (gold H/I wording is wrong here).
    assert len([h for h in headers if h]) == 8


def test_writer_prompt_step3_uses_fixture_keys_not_gdpval_composites() -> None:
    text = _WRITER_PROMPT.read_text(encoding="utf-8")
    gold = _GDPVAL_PROMPT.read_text(encoding="utf-8")
    for key in _STEP3_FIXTURE_KEYS:
        assert key in text, f"writer prompt missing fixture key {key!r}"
    for key in _GDPVAL_ONLY_KEYS:
        assert key not in text, f"writer prompt still has GDPVal-only key {key!r}"
        assert key in gold, f"gold prompt lost GDPVal key {key!r}"
    # No optional steering one-liners (rename + step-3 remaps only).
    lowered = text.lower()
    for banned in (
        "already named",
        "don't rename",
        "do not rename",
        "from knowledge",
        "web not required",
    ):
        assert banned not in lowered, banned


def test_eval2_population_sheet_is_named_population() -> None:
    xlsx_names = _xlsx_sheet_names(_POPULATION_XLSX)
    assert xlsx_names == ["Population"], xlsx_names
    ods_names = _ods_table_names(_POPULATION_ODS)
    assert ods_names == ["Population"], ods_names
    # Conversion artifact stays as a database-range, not a visible sheet.
    db_names = _ods_database_range_names(_POPULATION_ODS)
    assert any(name.startswith("__Anonymous_Sheet_DB__") for name in db_names), db_names


def test_step3_keys_exist_as_fixture_cell_text() -> None:
    xlsx_text = set(_xlsx_shared_strings(_POPULATION_XLSX))
    ods_text = _ods_cell_strings(_POPULATION_ODS)
    for key in _STEP3_FIXTURE_KEYS:
        assert key in xlsx_text, f"xlsx fixture missing {key!r}"
        assert key in ods_text, f"ods fixture missing {key!r}"
    for key in _GDPVAL_ONLY_KEYS:
        assert key not in xlsx_text, f"xlsx unexpectedly has {key!r}"
        assert key not in ods_text, f"ods unexpectedly has {key!r}"


def test_gold_gdpval_population_sheet_stays_sheet1() -> None:
    """Smoother-path rename is eval-2 only; do not retitle gold GDPVal."""
    assert _GOLD_POPULATION.is_file(), f"missing gold Population {_GOLD_POPULATION}"
    assert _xlsx_sheet_names(_GOLD_POPULATION) == ["Sheet1"]
