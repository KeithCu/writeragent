# WriterAgent tests for scripts/eval_2_tenant_oracle.py
from __future__ import annotations

import sys
from pathlib import Path

from docx import Document
from odf.opendocument import OpenDocumentText
from odf.table import Table, TableCell, TableRow
from odf.text import H, P
import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from eval_2_headed import main as headed_main  # noqa: E402
from eval_2_tenant_oracle import (  # noqa: E402
    main as oracle_main,
    read_memo_paragraphs,
    score_memo,
    score_text,
)

_PASSING = """
Business Memo
Subject: Tenant Retention Strategy for Harborview Flats, Stamford, Connecticut

Objective: increase resident retention by 10% in the next 6 months.

1. Analysis of Departure Reasons
Exit Survey Feedback (20 comments):
- rent increase 9/20 (45%)
- lack of community / feeling disconnected 5/20 (25%)

2. Tiered Renewal Offer Structure
- early-bird offer at 90 days
- standard offer at 60 days
- month-to-month premium

3. Communication Plan
Touchpoints at 90-day, 60-day, and 30-day marks before lease end.

4. Community Engagement Initiatives
We will host two resident events next quarter on the front lawn and in the lounge.
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
    doc.save(str(path))
    return path


def _write_heading_table_odt(path: Path) -> Path:
    """Tiny headed-memo fixture: section titles in ``text:h``, counts in cells.

    Matches the Gemini false-FAIL shape (headings + ``9`` / ``45.0%`` split
    across table cells, no literal ``9/20``). Would fail the old extract and
    the old count/percent regexes.
    """
    doc = OpenDocumentText()
    doc.text.addElement(P(text="Business Memo"))
    doc.text.addElement(
        P(text="Subject: Tenant Retention Strategy for Harborview Flats, Stamford, Connecticut")
    )
    doc.text.addElement(
        P(text="Objective: increase resident retention by 10% in the next 6 months.")
    )
    doc.text.addElement(P(text="Exit Survey Feedback (N=20)."))
    doc.text.addElement(H(outlinelevel=1, text="1. Analysis of Resident Departure Reasons"))
    table = Table()
    for rec in (
        ("Reason", "Count", "Share"),
        ("rent increase", "9", "45.0%"),
        ("lack of community / feeling disconnected", "5", "25.0%"),
    ):
        row = TableRow()
        for val in rec:
            cell = TableCell(valuetype="string")
            cell.addElement(P(text=val))
            row.addElement(cell)
        table.addElement(row)
    doc.text.addElement(table)
    doc.text.addElement(H(outlinelevel=1, text="2. Tiered Renewal Offer Structure"))
    doc.text.addElement(
        P(text="early-bird offer at 90 days, standard offer at 60 days, month-to-month premium.")
    )
    doc.text.addElement(
        H(outlinelevel=1, text="3. Resident Communication Plan & Outreach Sequence")
    )
    doc.text.addElement(P(text="Touchpoints at 90-day, 60-day, and 30-day marks before lease end."))
    doc.text.addElement(
        H(outlinelevel=1, text="4. Low-Cost, High-Impact Community Engagement Initiatives")
    )
    doc.text.addElement(
        P(text="We will host two resident events next quarter on the front lawn and in the lounge.")
    )
    doc.text.addElement(P(text=("Retention follow-through. " * 80).strip()))
    doc.save(str(path))
    return path


def _padded() -> str:
    return _PASSING + (" Retention follow-through. " * 80)


def test_passing_memo_docx_and_odt(tmp_path: Path) -> None:
    text = _padded()
    docx = _write_docx(tmp_path / "ok.docx", text)
    odt = _write_odt(tmp_path / "ok.odt", text)
    for path in (docx, odt):
        result = score_memo(path)
        assert result.passed, (path.name, result.failures)
        assert 180 <= result.word_count <= 1800


def test_empty_memo_fails(tmp_path: Path) -> None:
    path = _write_odt(tmp_path / "empty.odt", "")
    result = score_memo(path)
    assert not result.passed
    assert any("empty" in item or "word_count" in item for item in result.failures)


@pytest.mark.parametrize(
    "value, value_2, value_3, value_4, para_count",
    [
        pytest.param("9/20 (45%)", "4/20 (20%)", "5/20 (25%)", "2/20 (10%)", 12, id="test_wrong_survey_counts_fail"),
        pytest.param("rent increase 9/20 (45%)", "rent increase\n4\n45.0%", "lack of community / feeling disconnected 5/20 (25%)", "lack of community / feeling disconnected\n2\n25.0%", 16, id="test_wrong_split_counts_still_fail"),
    ],
)
def test_wrong_survey_counts_fail(value, value_2, value_3, value_4, para_count) -> None:
    text = _padded().replace(value, value_2).replace(value_3, value_4)
    result = score_text(text, para_count=para_count)
    assert not result.passed
    assert any("9/20" in item for item in result.failures)
    assert any("5/20" in item for item in result.failures)


def test_missing_stamford_fails() -> None:
    text = _padded().replace("Stamford, Connecticut", "elsewhere")
    result = score_text(text, para_count=12)
    assert not result.passed
    assert any("Stamford" in item for item in result.failures)


def test_husk_body_fails() -> None:
    text = _padded() + "\nError: tool failed\n"
    result = score_text(text, para_count=12)
    assert not result.passed
    assert any("husk" in item for item in result.failures)


def test_oracle_does_not_require_gold_typo() -> None:
    text = _padded()
    assert "Rentention" not in text
    result = score_text(text, para_count=12)
    assert result.passed, result.failures


def test_decimal_and_word_percents_pass() -> None:
    for pct45, pct25 in (("45.0%", "25.0%"), ("45 percent", "25 percent"), ("45%", "25%")):
        text = _padded().replace("45%", pct45).replace("25%", pct25)
        result = score_text(text, para_count=12)
        assert result.passed, (pct45, pct25, result.failures)


def test_split_table_counts_pass() -> None:
    text = (
        _padded()
        .replace("rent increase 9/20 (45%)", "rent increase\n9\n45.0%")
        .replace(
            "lack of community / feeling disconnected 5/20 (25%)",
            "lack of community / feeling disconnected\n5\n25.0%",
        )
    )
    assert "9/20" not in text
    assert "5/20" not in text
    result = score_text(text, para_count=16)
    assert result.passed, result.failures

def test_word_count_between_1400_and_1800_passes() -> None:
    text = _PASSING
    filler = " Retention follow-through."
    result = score_text(text, para_count=12)
    while result.word_count < 1450:
        text += filler
        result = score_text(text, para_count=12)
    assert 1400 < result.word_count <= 1800
    assert result.passed, result.failures


def test_synonym_section_titles_pass() -> None:
    text = (
        _padded()
        .replace("1. Analysis of Departure Reasons", "1. Exit Survey Analysis")
        .replace("3. Communication Plan", "3. Email Timeline")
        .replace("4. Community Engagement Initiatives", "4. Engagement Initiative")
    )
    result = score_text(text, para_count=12)
    assert result.passed, result.failures


def test_odt_extracts_text_h_headings(tmp_path: Path) -> None:
    doc = OpenDocumentText()
    doc.text.addElement(H(outlinelevel=1, text="1. Analysis of Resident Departure Reasons"))
    doc.text.addElement(P(text="body paragraph only"))
    path = tmp_path / "heading-only.odt"
    doc.save(str(path))
    paras = read_memo_paragraphs(path)
    assert any("Analysis of Resident Departure Reasons" in para for para in paras)


def test_odt_heading_and_split_table_fixture(tmp_path: Path) -> None:
    path = _write_heading_table_odt(tmp_path / "headed-table.odt")
    paras = read_memo_paragraphs(path)
    joined = "\n".join(paras)
    assert "Analysis of Resident Departure Reasons" in joined
    assert "9/20" not in joined
    assert "45.0%" in joined
    result = score_memo(path)
    assert result.passed, result.failures


def test_headed_score_routes_odt_to_tenant_oracle(tmp_path: Path) -> None:
    path = _write_odt(tmp_path / "final_memo.odt", _padded())
    assert headed_main(["--score", str(path)]) == 0
    empty = _write_odt(tmp_path / "empty.odt", "")
    assert headed_main(["--task", "tenant-retention", "--score", str(empty)]) == 1


def test_oracle_cli_json(tmp_path: Path, capsys) -> None:
    path = _write_odt(tmp_path / "final_memo.odt", _padded())
    assert oracle_main(["--json", str(path)]) == 0
    out = capsys.readouterr().out
    assert '"passed": true' in out
