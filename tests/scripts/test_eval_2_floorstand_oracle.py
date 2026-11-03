# WriterAgent tests for scripts/eval_2_floorstand_oracle.py
from __future__ import annotations

import sys
from pathlib import Path

from docx import Document
from odf.opendocument import OpenDocumentText
from odf.text import P

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_REPO = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from eval_2_floorstand_oracle import (  # noqa: E402
    main as oracle_main,
    normalize_hyphen_like,
    resolve_floorstand_artifacts,
    score_artifacts,
    score_pair,
)
from eval_2_headed import (  # noqa: E402
    FLOORSTAND_BUDGET_ODS_NAME,
    FLOORSTAND_EMAIL_NAME,
    write_floorstand_budget_ods,
)
from eval_2_headed import main as headed_main  # noqa: E402

_GOLD_EMAIL = (
    _REPO
    / "docs"
    / "eval"
    / "eval-2"
    / "writer-calc-peer-write"
    / "gold"
    / "Final Email Deliverable.docx"
)
_GOLD_BUDGET = (
    _REPO
    / "docs"
    / "eval"
    / "eval-2"
    / "writer-calc-peer-write"
    / "gold"
    / "Deliverable Holiday Floorstand Budget.xlsx"
)
_ORIG_XLSX = (
    _REPO
    / "docs"
    / "eval"
    / "eval-2"
    / "writer-calc-peer-write"
    / "fixtures"
    / "Holiday Floorstand Store List Original.xlsx"
)

_PASSING = """
Draft email — holiday floorstand display budget

Hello,

Please see the updated budget workbook. The floorstand program budget
changed after the retailer added stores and the shelf-strip cost rose.
Updated units to produce are in the workbook, along with the program
cost change and the new total program budget.

Thank you,
Order Analyst
"""


def _write_docx(path: Path, text: str) -> Path:
    doc = Document()
    for line in text.strip().splitlines():
        doc.add_paragraph(line)
    doc.save(str(path))
    return path


def _write_odt(path: Path, text: str) -> Path:
    doc = OpenDocumentText()
    for line in text.strip().splitlines():
        doc.text.addElement(P(text=line))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def _padded() -> str:
    return _PASSING + (" Follow-through on the holiday program. " * 8)


def test_gold_pair_passes() -> None:
    result = score_artifacts(_GOLD_EMAIL, _GOLD_BUDGET)
    assert result.passed, result.failures
    assert result.nonempty_cells >= 8


def test_passing_pair_odt_and_docx(tmp_path: Path) -> None:
    text = _padded()
    for memo in (
        _write_odt(tmp_path / "ok.odt", text),
        _write_docx(tmp_path / "ok.docx", text),
    ):
        result = score_artifacts(memo, _GOLD_BUDGET)
        assert result.passed, (memo.name, result.failures)


def test_empty_memo_fails(tmp_path: Path) -> None:
    memo = _write_odt(tmp_path / "empty.odt", "")
    result = score_artifacts(memo, _GOLD_BUDGET)
    assert not result.passed
    assert any("empty" in item or "word_count" in item for item in result.failures)


def test_blank_scaffold_fails(tmp_path: Path) -> None:
    memo = _write_odt(tmp_path / "memo.odt", _padded())
    scaffold = write_floorstand_budget_ods(tmp_path / FLOORSTAND_BUDGET_ODS_NAME)
    result = score_artifacts(memo, scaffold)
    assert not result.passed
    assert any("scaffold" in item or "cost comparison" in item for item in result.failures)


def test_research_only_store_list_fails() -> None:
    result = score_artifacts(_GOLD_EMAIL, _ORIG_XLSX)
    assert not result.passed
    assert any("research-only" in item for item in result.failures)


def test_missing_floorstand_fails() -> None:
    text = _padded().replace("floorstand", "display").replace("floor-stand", "display")
    result = score_pair(
        text,
        "original cost per unit vs revised program cost shelf strip stores",
        para_count=8,
        nonempty_cells=12,
        sheet_count=2,
    )
    assert not result.passed
    assert any("floorstand" in item for item in result.failures)


def test_normalize_hyphen_like_folds_identity_dashes() -> None:
    assert normalize_hyphen_like("floor\u2011stand") == "floor-stand"
    assert normalize_hyphen_like("shelf\u2010strip") == "shelf-strip"
    assert normalize_hyphen_like("floor\u00a0stand") == "floor stand"


def test_unicode_hyphen_floorstand_matches() -> None:
    text = _padded().replace("floorstand", "floor\u2011stand")
    result = score_pair(
        text,
        "original vs revised cost per unit floor\u2011stand shelf\u2010strip stores",
        para_count=8,
        nonempty_cells=12,
        sheet_count=2,
    )
    assert result.passed, result.failures


def test_gold_microcopy_not_required() -> None:
    text = _padded()
    assert "24,670.80" not in text
    assert "1,320" not in text
    result = score_pair(
        text,
        "Cost Comparison original vs revised program cost shelf strip stores floorstand",
        para_count=8,
        nonempty_cells=12,
        sheet_count=2,
    )
    assert result.passed, result.failures


def test_husk_body_fails() -> None:
    text = _padded() + "\nError: tool failed\n"
    result = score_pair(
        text,
        "original vs revised cost per unit",
        para_count=8,
        nonempty_cells=12,
        sheet_count=2,
    )
    assert not result.passed
    assert any("husk" in item for item in result.failures)


def test_resolve_sibling_workbook(tmp_path: Path) -> None:
    memo = _write_odt(tmp_path / "final_memo.odt", _padded())
    book = write_floorstand_budget_ods(tmp_path / "final_workbook.ods")
    found_memo, found_book = resolve_floorstand_artifacts(memo)
    assert found_memo == memo
    assert found_book == book
    found_memo, found_book = resolve_floorstand_artifacts(tmp_path)
    assert found_memo == memo
    assert found_book == book


def test_resolve_skips_research_store_list(tmp_path: Path) -> None:
    memo = _write_odt(tmp_path / FLOORSTAND_EMAIL_NAME, _padded())
    (tmp_path / "Holiday Floorstand Store List Original.ods").write_bytes(b"PK")
    budget = write_floorstand_budget_ods(tmp_path / FLOORSTAND_BUDGET_ODS_NAME)
    found_memo, found_book = resolve_floorstand_artifacts(tmp_path)
    assert found_memo == memo
    assert found_book == budget


def test_headed_score_routes_to_floorstand_oracle(tmp_path: Path) -> None:
    memo = _write_odt(tmp_path / "final_memo.odt", _padded())
    dest = tmp_path / "final_workbook.xlsx"
    dest.write_bytes(_GOLD_BUDGET.read_bytes())
    assert headed_main(["--task", "writer-calc-peer-write", "--score", str(memo)]) == 0
    other = tmp_path / "failcase"
    other.mkdir()
    empty = _write_odt(other / "empty.odt", "")
    write_floorstand_budget_ods(other / FLOORSTAND_BUDGET_ODS_NAME)
    assert headed_main(["--task", "writer-calc-peer-write", "--score", str(empty)]) == 1


def test_oracle_cli_json(tmp_path: Path, capsys) -> None:
    memo = _write_odt(tmp_path / "final_memo.odt", _padded())
    dest = tmp_path / "final_workbook.xlsx"
    dest.write_bytes(_GOLD_BUDGET.read_bytes())
    assert oracle_main(["--json", str(memo)]) == 0
    out = capsys.readouterr().out
    assert '"passed": true' in out
