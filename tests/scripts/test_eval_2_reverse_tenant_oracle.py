# WriterAgent tests for scripts/eval_2_reverse_tenant_oracle.py
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from odf.opendocument import OpenDocumentSpreadsheet
from odf.table import Table, TableCell, TableRow
from odf.text import P

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from eval_2_headed import (  # noqa: E402
    CBA_EXCERPT_ODT_NAME,
    THEATRE_CBA_ODS_NAME,
    write_blank_calc_ods,
)
from eval_2_headed import main as headed_main  # noqa: E402
from eval_2_reverse_tenant_oracle import (  # noqa: E402
    main as oracle_main,
    resolve_reverse_tenant_artifacts,
    score_workbook,
)

_REPO = Path(__file__).resolve().parents[2]
_GOLD_XLSX = (
    _REPO
    / "docs"
    / "eval"
    / "eval-2"
    / "reverse-tenant"
    / "gold"
    / "Theatre CBA.xlsx"
)


def _cell(val: Any) -> TableCell:
    if val is None or val == "":
        return TableCell()
    cell = TableCell(valuetype="string")
    cell.addElement(P(text=str(val)))
    return cell


def write_ods(path: Path, sheets: dict[str, list[tuple[Any, ...]]]) -> Path:
    doc = OpenDocumentSpreadsheet()
    for name, rows in sheets.items():
        table = Table(name=name)
        for row in rows:
            trow = TableRow()
            for value in row:
                trow.addElement(_cell(value))
            table.addElement(trow)
        doc.spreadsheet.addElement(table)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


_PASSING_RATES = (
    ("Rates", "contractor weekly payroll"),
    ("Base wage", "251.06"),
    ("Weekly guarantee", "2008.50"),
    ("Rehearsal", "55.67"),
    ("Sound check 1hr", "76.59"),
    ("Vacation", "5.5%"),
    ("Premium", "15%"),
    ("Doubling", "25%"),
    ("Audit", "yes"),
    ("Performance", "yes"),
)
_PASSING_ROSTER = (
    ("Name", "Instrument"),
    ("A", "Synthesizer"),
    ("B", "Violin"),
    ("C", "Viola"),
    ("D", "Trumpet"),
    ("E", "French Horn"),
)
_PASSING_SCHEDULE = (
    ("Day", "Service"),
    ("Tue", "Rehearsal"),
    ("Tue", "Sound Check"),
    ("Tue", "Performance"),
)


def _write_passing(path: Path) -> Path:
    return write_ods(
        path,
        {
            "Rates": _PASSING_RATES,
            "Roster": _PASSING_ROSTER,
            "Schedule": _PASSING_SCHEDULE,
        },
    )


def test_gold_deliverable_passes() -> None:
    result = score_workbook(_GOLD_XLSX)
    assert result.passed, result.failures
    assert result.scored_cells >= 12
    assert len(result.instruments) >= 3
    assert len(result.pay_categories) >= 3


def test_passing_ods(tmp_path: Path) -> None:
    path = _write_passing(tmp_path / "ok.ods")
    result = score_workbook(path)
    assert result.passed, result.failures
    assert "synthesizer" in result.instruments
    assert "violin" in result.instruments
    assert result.brief_present is False


def test_blank_workbook_fails(tmp_path: Path) -> None:
    path = write_blank_calc_ods(tmp_path / THEATRE_CBA_ODS_NAME)
    result = score_workbook(path)
    assert not result.passed
    assert any("populated cells" in item for item in result.failures)


def test_missing_instruments_fails(tmp_path: Path) -> None:
    path = write_ods(
        tmp_path / "no-instruments.ods",
        {
            "Rates": _PASSING_RATES,
            "Roster": (("Name", "Role"), ("A", "Player")),
            "Schedule": _PASSING_SCHEDULE,
        },
    )
    result = score_workbook(path)
    assert not result.passed
    assert any("instruments" in item for item in result.failures)


def test_husk_dominated_fails(tmp_path: Path) -> None:
    husks = tuple(("Error: tool failed", "#DIV/0!") for _unused in range(40))
    path = write_ods(
        tmp_path / "husk.ods",
        {
            "Rates": _PASSING_RATES + husks,
            "Roster": _PASSING_ROSTER,
            "Schedule": _PASSING_SCHEDULE,
        },
    )
    result = score_workbook(path)
    assert not result.passed
    assert any("husk" in item for item in result.failures)


def test_headed_score_routes_to_reverse_oracle(tmp_path: Path) -> None:
    ok = _write_passing(tmp_path / "final_workbook.ods")
    assert headed_main(["--task", "reverse-tenant", "--score", str(ok)]) == 0
    empty = write_blank_calc_ods(tmp_path / "empty.ods")
    assert headed_main(["--task", "reverse-tenant", "--score", str(empty)]) == 1


def test_resolve_trial_dir_records_brief(tmp_path: Path) -> None:
    workbook = _write_passing(tmp_path / THEATRE_CBA_ODS_NAME)
    brief = tmp_path / CBA_EXCERPT_ODT_NAME
    brief.write_bytes(b"ODT")
    found_wb, found_brief = resolve_reverse_tenant_artifacts(tmp_path)
    assert found_wb == workbook
    assert found_brief == brief
    result = score_workbook(found_wb, brief_present=found_brief is not None)
    assert result.passed, result.failures
    assert result.brief_present is True


def test_oracle_cli_json(tmp_path: Path, capsys) -> None:
    path = _write_passing(tmp_path / "ok.ods")
    assert oracle_main(["--json", str(path)]) == 0
    out = capsys.readouterr().out
    assert '"passed": true' in out
