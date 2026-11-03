# WriterAgent tests for scripts/eval_2_calc_primary_oracle.py
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

from eval_2_calc_primary_oracle import (  # noqa: E402
    MIN_FORMULAS,
    MIN_SCHEDULE_SHEETS,
    main as oracle_main,
    score_workbook,
)
from eval_2_headed import main as headed_main  # noqa: E402

_LABELS = (
    "Income statement comparison",
    "Monthly trended income 2024",
    "Branch ranking",
    "Regional comparison",
    "Efficiency metrics ARPU",
    "Revenue",
    "COGS",
    "SG&A",
    "EBITDA",
    "Gross Margin",
    "Revenue (Units)",
    "Project Backlog (Units)",
    "Implementation Hours",
    "Shared Service Allocations",
    "M1-M12 2023",
    "M13-M24 2024",
    "M23",
    "M24",
    "Regions A through G",
    *(f"Branch {n}" for n in range(1, 11)),
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
    if isinstance(val, int):
        cell = TableCell(valuetype="float", value=float(val))
        cell.addElement(P(text=str(val)))
        return cell
    cell = TableCell(valuetype="string")
    cell.addElement(P(text=str(val)))
    return cell


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


def _formulas(n: int = MIN_FORMULAS) -> list[tuple[Any, ...]]:
    """Distinct relative formulas so the pin check stays quiet."""
    return [
        ({"formula": f"of:=[.B{idx + 2}]", "text": str(idx)},)
        for idx in range(n)
    ]


def _label_rows() -> list[tuple[Any, ...]]:
    return [(label,) for label in _LABELS]


def _bare(*extra: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    return [("line", "value"), *extra, ("row", 1)]


def write_pass_ods(path: Path) -> Path:
    return write_ods(
        path,
        {
            "Raw Data": [("Branch", "Region", "Account", "M1"), ("Branch 1", "G", "Revenue", 1)],
            "IS Comparison": [("line", "value"), *_label_rows(), *_formulas()],
            "Monthly Trend": _bare(("2024 month", 1)),
            "Branch Ranking": _bare(("rank", 1)),
            "Regional View": _bare(("Region A", 1)),
            "Metrics": _bare(("ARPU", {"formula": "of:=[.C2]/[.D2]", "text": "10"})),
        },
    )


def test_pass_minimal_good_model(tmp_path: Path) -> None:
    path = write_pass_ods(tmp_path / "pass.ods")
    result = score_workbook(path)
    assert result.passed, result.failures
    assert result.schedule_sheets >= MIN_SCHEDULE_SHEETS
    assert result.formula_cells >= MIN_FORMULAS
    assert result.pinned_columns == 0
    assert result.husk_cells == 0


def test_fail_empty_create_sheet_tabs(tmp_path: Path) -> None:
    path = write_ods(
        tmp_path / "empty-tabs.ods",
        {
            "Raw Data": [("Branch",), ("Branch 1",)],
            "IS Comparison": [("header",)],
            "Monthly Trend": [("header",)],
            "Branch Ranking": [("header",)],
            "Regional View": [("header",)],
            "Metrics": [("header",)],
        },
    )
    result = score_workbook(path)
    assert not result.passed
    assert result.schedule_sheets == 0
    assert any("empty create_sheet" in item for item in result.failures)


def test_fail_too_few_schedule_sheets(tmp_path: Path) -> None:
    path = write_ods(
        tmp_path / "two-tabs.ods",
        {
            "Raw Data": [("Account",), ("Revenue",)],
            "IS Comparison": [("line", "value"), *_label_rows(), *_formulas()],
        },
    )
    result = score_workbook(path)
    assert not result.passed
    assert any("populated schedule sheets" in item for item in result.failures)


def test_fail_pinned_relative_formula(tmp_path: Path) -> None:
    # Column B only: identical relative formula, so fill-down did not adjust.
    pinned = [("", {"formula": "of:=[.B2]", "text": "1"}) for _unused in range(4)]
    path = write_ods(
        tmp_path / "pinned.ods",
        {
            "Raw Data": [("Account",), ("Revenue",)],
            "IS Comparison": [("line", "value"), *_label_rows(), *pinned, *_formulas()],
            "Monthly Trend": _bare(("row", 1)),
            "Branch Ranking": _bare(("row", 1)),
            "Regional View": _bare(("row", 1)),
            "Metrics": _bare(("row", 1)),
        },
    )
    result = score_workbook(path)
    assert not result.passed
    assert any("pinned relative formula" in item for item in result.failures)


def test_absolute_formula_repeat_is_not_pin(tmp_path: Path) -> None:
    absolute = [("", {"formula": "of:=[.$B$2]", "text": "1"}) for _unused in range(4)]
    path = write_ods(
        tmp_path / "absolute.ods",
        {
            "Raw Data": [("Account",), ("Revenue",)],
            "IS Comparison": [("line", "value"), *_label_rows(), *absolute, *_formulas()],
            "Monthly Trend": _bare(("row", 1)),
            "Branch Ranking": _bare(("row", 1)),
            "Regional View": _bare(("row", 1)),
            "Metrics": _bare(("row", 1)),
        },
    )
    result = score_workbook(path)
    assert result.passed, result.failures
    assert result.pinned_columns == 0


def test_fail_arpu_orders_without_units(tmp_path: Path) -> None:
    path = write_ods(
        tmp_path / "wrong-factor.ods",
        {
            "Raw Data": [("Account",), ("Revenue",)],
            "IS Comparison": [("line", "value"), *_label_rows(), *_formulas()],
            "Monthly Trend": _bare(("row", 1)),
            "Branch Ranking": _bare(("row", 1)),
            "Regional View": _bare(("row", 1)),
            "Metrics": _bare(
                ("ARPU", {"formula": "of:=[.A2]/Orders", "text": "9"}),
            ),
        },
    )
    result = score_workbook(path)
    assert not result.passed
    assert any("ARPU formula uses Orders" in item for item in result.failures)


def test_fail_husk_dominated(tmp_path: Path) -> None:
    husk = ("#DIV/0!", "Err:507", "Error: boom")
    path = write_ods(
        tmp_path / "husks.ods",
        {
            "Raw Data": [("Account",), ("Revenue",)],
            "IS Comparison": [("h",), husk, husk],
            "Monthly Trend": [("h",), husk],
            "Branch Ranking": [("h",), husk],
            "Regional View": [("h",), husk],
            "Metrics": [("h",), husk],
        },
    )
    result = score_workbook(path)
    assert not result.passed
    assert any("husk-dominated" in item for item in result.failures)


def test_fail_missing_factor_token(tmp_path: Path) -> None:
    labels = [label for label in _LABELS if "Backlog" not in label]
    rows = [("line", "value"), *[(label,) for label in labels], *_formulas()]
    path = write_ods(
        tmp_path / "no-backlog.ods",
        {
            "Raw Data": [("Account",), ("Revenue",)],
            "IS Comparison": rows,
            "Monthly Trend": _bare(("row", 1)),
            "Branch Ranking": _bare(("row", 1)),
            "Regional View": _bare(("row", 1)),
            "Metrics": _bare(("row", 1)),
        },
    )
    result = score_workbook(path)
    assert not result.passed
    assert any("Project Backlog" in item for item in result.failures)


def test_missing_file_fails_closed(tmp_path: Path) -> None:
    result = score_workbook(tmp_path / "nope.ods")
    assert not result.passed
    assert result.failures[0].startswith("workbook not found")


def test_score_sheets_has_no_ready_parameter() -> None:
    assert "ready" not in score_workbook.__doc__.lower() if score_workbook.__doc__ else True


def test_cli_pass_and_fail_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = write_pass_ods(tmp_path / "pass.ods")
    bad = write_ods(tmp_path / "empty.ods", {"Raw Data": [("h",)]})
    assert oracle_main([str(good)]) == 0
    assert "PASS" in capsys.readouterr().out
    assert oracle_main([str(bad), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False


def test_headed_score_routes_to_calc_primary_oracle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = write_pass_ods(tmp_path / "final_workbook.ods")
    assert headed_main(["--task", "calc-primary-model", "--score", str(good)]) == 0
    assert "PASS" in capsys.readouterr().out
    empty = write_ods(tmp_path / "empty.ods", {"Raw Data": [("h",)]})
    assert headed_main(["--task", "calc-primary-model", "--score", str(empty)]) == 1


def test_raw_fixture_alone_is_not_a_pass() -> None:
    """The staged Raw Data ODS has no schedule sheets — fail closed."""
    fixture = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "eval"
        / "eval-2"
        / "calc-primary-model"
        / "fixtures"
        / "Raw Data for Branch Profitability Final.ods"
    )
    if not fixture.is_file():
        pytest.skip("calc-primary fixture not checked out")
    result = score_workbook(fixture)
    assert not result.passed
    assert any("populated schedule sheets" in item for item in result.failures)
