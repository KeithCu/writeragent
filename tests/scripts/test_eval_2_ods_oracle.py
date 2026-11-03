# WriterAgent tests for scripts/eval_2_ods_oracle.py
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from odf.opendocument import OpenDocumentSpreadsheet
from odf.table import Table, TableCell, TableRow
from odf.text import P

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from eval_2_headed import main as headed_main  # noqa: E402
from eval_2_ods_oracle import (  # noqa: E402
    FLAG_COLUMN_INDEX,
    Cell,
    is_husk_text,
    main as oracle_main,
    parse_required_sample_size,
    score_sheets,
    score_workbook,
)

# Population A–H headers from the eval-2 fixture (not gold I/J).
_SAMPLE_HEADERS = (
    "No",
    "Division",
    "Sub-Division",
    "Country",
    "Legal Entity",
    "KRIs",
    "Q3 2024 KRI",
    "Q2 2024 KRI",
    "Variance",
    "",
    "Sample flag",
)


def _cell(val: Any) -> TableCell:
    if val is None or val == "":
        return TableCell()
    if isinstance(val, dict):
        formula = val.get("formula")
        text = val.get("text", "")
        cell = TableCell(valuetype="string", formula=formula) if formula else TableCell(valuetype="string")
        if text != "":
            cell.addElement(P(text=str(text)))
        return cell
    if isinstance(val, bool):
        cell = TableCell(valuetype="boolean", boolean=val)
        cell.addElement(P(text="true" if val else "false"))
        return cell
    if isinstance(val, int):
        cell = TableCell(valuetype="float", value=float(val))
        cell.addElement(P(text=str(val)))
        return cell
    if isinstance(val, float):
        cell = TableCell(valuetype="float", value=val)
        cell.addElement(P(text=str(val)))
        return cell
    cell = TableCell(valuetype="string")
    cell.addElement(P(text=str(val)))
    return cell


def _pad_to_k(values: tuple[Any, ...]) -> tuple[Any, ...]:
    """Pad a short row so index 10 is column K (eval-2 flag column)."""
    padded = list(values)
    while len(padded) < FLAG_COLUMN_INDEX + 1:
        padded.append("")
    return tuple(padded)


def write_ods(path: Path, sheets: dict[str, list[tuple[Any, ...]]]) -> Path:
    doc = OpenDocumentSpreadsheet()
    for name, rows in sheets.items():
        table = Table(name=name)
        for rec in rows:
            row = TableRow()
            for val in rec:
                row.addElement(_cell(val))
            table.addElement(row)
        doc.spreadsheet.addElement(table)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def _ssc(*, r: int | str = 1, extra: tuple[tuple[Any, ...], ...] = ()) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = [
        ("Sample size", r),
        ("confidence", "90%"),
        ("tolerable error", "10%"),
    ]
    rows.extend(extra)
    return rows


def _good_sample_row(*, flag: int | str | None = 1) -> tuple[Any, ...]:
    return _pad_to_k((1, "AM", "Asset Management", "Italy", "Entity", "A1", 20, 10, 1.0, "", flag))


def write_pass_ods(path: Path) -> Path:
    return write_ods(
        path,
        {
            "Sample": [_SAMPLE_HEADERS, _good_sample_row()],
            "Sample Size Calculation": _ssc(r=1),
        },
    )


def write_missing_sample_ods(path: Path) -> Path:
    return write_ods(path, {"Sample Size Calculation": _ssc(r=1), "Sheet1": [("No",), (1,)]})


def write_empty_sample_ods(path: Path) -> Path:
    return write_ods(
        path,
        {
            "Sample": [_SAMPLE_HEADERS],
            "Sample Size Calculation": _ssc(r=1),
        },
    )


def write_husk_dominated_ods(path: Path) -> Path:
    # Flag=1 would satisfy S≥R if husks were ignored. Residue must still fail.
    husk_row = _pad_to_k(
        (
            "#DIV/0!",
            "Err:507",
            "Error: PreContractError",
            "_deal_grid_ok",
            "DEAL_MAX_SHAPE_DIM",
            {"text": "COM.SUN.STAR.SHEET.ADDIN.PYTHONFUNCTION", "formula": "of:=PYTHONFUNCTION()"},
            "",
            "",
            "",
            "",
            1,
        )
    )
    return write_ods(
        path,
        {
            "Sample": [_SAMPLE_HEADERS, husk_row],
            "Sample Size Calculation": _ssc(r=1),
        },
    )


def write_ready_looking_no_flags_ods(path: Path) -> Path:
    # Both deliverable sheets + data + a Ready/STREAM_DONE note — no K flags.
    row = _pad_to_k((1, "AM", "Asset Management", "Italy", "Entity", "A1", 20, 10, 1.0, "STREAM_DONE", ""))
    return write_ods(
        path,
        {
            "Sample": [_SAMPLE_HEADERS, row, _pad_to_k(("Ready", "chat idle", "", "", "", "", "", "", "", "", ""))],
            "Sample Size Calculation": _ssc(r=1, extra=(("status", "Ready"),)),
        },
    )


def test_pass_minimal_good_sample(tmp_path: Path) -> None:
    path = write_pass_ods(tmp_path / "pass.ods")
    result = score_workbook(path)
    assert result.passed, result.failures
    assert result.sample_data_rows == 1
    assert result.s_flags == 1
    assert result.r_required == 1
    assert result.husk_cells == 0


def test_fail_missing_sample(tmp_path: Path) -> None:
    path = write_missing_sample_ods(tmp_path / "missing-sample.ods")
    result = score_workbook(path)
    assert not result.passed
    assert any("missing sheet 'Sample'" in item for item in result.failures)


def test_fail_empty_sample(tmp_path: Path) -> None:
    path = write_empty_sample_ods(tmp_path / "empty-sample.ods")
    result = score_workbook(path)
    assert not result.passed
    assert result.sample_data_rows == 0
    assert any("no data rows" in item for item in result.failures)


def test_fail_husk_dominated(tmp_path: Path) -> None:
    path = write_husk_dominated_ods(tmp_path / "husks.ods")
    result = score_workbook(path)
    assert not result.passed
    assert any("husk-dominated" in item for item in result.failures)


def test_fail_ready_looking_but_no_flags(tmp_path: Path) -> None:
    path = write_ready_looking_no_flags_ods(tmp_path / "ready-looking.ods")
    result = score_workbook(path)
    assert not result.passed
    assert result.sample_data_rows == 2
    assert result.s_flags == 0
    assert any(item.startswith("S=0") for item in result.failures)
    # Chat chrome must not greenwash.
    assert "Ready" not in result.failures
    assert "STREAM_DONE" not in result.failures


def test_sheet_names_case_and_spaces(tmp_path: Path) -> None:
    path = write_ods(
        tmp_path / "case.ods",
        {
            " sample ": [_SAMPLE_HEADERS, _good_sample_row()],
            "Sample Size Calculation ": _ssc(r=1),
        },
    )
    assert score_workbook(path).passed


def test_r_zero_fails_even_with_flags(tmp_path: Path) -> None:
    path = write_ods(
        tmp_path / "r0.ods",
        {
            "Sample": [_SAMPLE_HEADERS, _good_sample_row()],
            "Sample Size Calculation": _ssc(r=0),
        },
    )
    result = score_workbook(path)
    assert not result.passed
    assert any("R from Sample Size Calculation" in item for item in result.failures)


def test_s_less_than_r_fails(tmp_path: Path) -> None:
    path = write_ods(
        tmp_path / "short.ods",
        {
            "Sample": [_SAMPLE_HEADERS, _good_sample_row()],
            "Sample Size Calculation": _ssc(r=2),
        },
    )
    result = score_workbook(path)
    assert not result.passed
    assert any("S=1" in item and "R=2" in item for item in result.failures)


def test_anon_db_sheet_is_not_sample(tmp_path: Path) -> None:
    path = write_ods(
        tmp_path / "anon.ods",
        {
            "__Anonymous_Sheet_DB__0": [_SAMPLE_HEADERS, _good_sample_row()],
            "Sample Size Calculation": _ssc(r=1),
        },
    )
    result = score_workbook(path)
    assert not result.passed
    assert any("missing sheet 'Sample'" in item for item in result.failures)
    assert "__Anonymous_Sheet_DB__0" not in result.sheet_names


def test_missing_file_fails_closed(tmp_path: Path) -> None:
    result = score_workbook(tmp_path / "nope.ods")
    assert not result.passed
    assert result.failures[0].startswith("workbook not found")


def test_parse_r_ignores_population_n() -> None:
    rows = [
        [Cell("Dataset"), Cell(1516)],
        [Cell("Sample size"), Cell(65)],
    ]
    assert parse_required_sample_size(rows) == 65
    assert parse_required_sample_size([[Cell("R = 1")]]) == 1
    assert parse_required_sample_size([[Cell("Dataset"), Cell(1516)]]) is None


def test_husk_tokens() -> None:
    assert is_husk_text("#DIV/0!")
    assert is_husk_text("Err:507")
    assert is_husk_text("Error: boom")
    assert is_husk_text("_deal_grid_ok")
    assert is_husk_text("DEAL_MAX_SHAPE_DIM")
    assert is_husk_text("=COM.SUN.STAR.SHEET.ADDIN.PYTHONFUNCTION()")
    assert not is_husk_text("1")
    assert not is_husk_text("Ready")


def test_score_sheets_has_no_ready_parameter() -> None:
    # Structural only — a Ready-looking workbook without flags still fails.
    sheets = {
        "Sample": [
            [Cell("No")],
            [Cell("Ready")],
        ],
        "Sample Size Calculation": [[Cell("Sample size"), Cell(1)]],
    }
    result = score_sheets(sheets)
    assert not result.passed
    assert result.s_flags == 0


def test_cli_pass_and_fail_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = write_pass_ods(tmp_path / "pass.ods")
    bad = write_empty_sample_ods(tmp_path / "empty.ods")
    assert oracle_main([str(good)]) == 0
    assert "PASS" in capsys.readouterr().out
    assert oracle_main([str(bad), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False


def test_gold_sample_workbook_is_not_eval2_pass() -> None:
    """Gold is a separate file (tab ``Sample Size``, flags in J). Do not letter-shift it."""
    gold = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "eval"
        / "eval-2"
        / "afc-sample-83d10b06"
        / "gold"
        / "Sample v2.xlsx"
    )
    if not gold.is_file():
        pytest.skip("gold Sample v2.xlsx not checked out")
    result = score_workbook(gold)
    assert not result.passed
    assert any("Sample Size Calculation" in item for item in result.failures)


def test_headed_score_does_not_write_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workbook = write_pass_ods(tmp_path / "pass.ods")
    config = tmp_path / "writeragent.json"
    assert headed_main(["--score", str(workbook), "--config", str(config)]) == 0
    assert not config.exists()
    assert "PASS" in capsys.readouterr().out
    assert headed_main(["--score", str(write_empty_sample_ods(tmp_path / "empty.ods"))]) == 1
