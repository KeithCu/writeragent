# WriterAgent tests for scripts/eval_2_cadaver_oracle.py
from __future__ import annotations

import re
import sys
from pathlib import Path

from docx import Document
from odf.draw import Frame, TextBox
from odf.opendocument import OpenDocumentText
from odf.table import Table, TableCell, TableRow
from odf.text import H, P
import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_REPO = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from eval_2_cadaver_oracle import (  # noqa: E402
    main as oracle_main,
    read_proposal,
    score_proposal,
    score_text,
)
from eval_2_headed import main as headed_main  # noqa: E402

_GEMINI_FIXTURE = (
    _REPO
    / "docs"
    / "eval"
    / "eval-2"
    / "cadaver-proposal-61b0946a"
    / "runs"
    / "20260908-2246-gemini-3.8-flash-r200"
    / "final_proposal.odt"
)

_PASSING = """
Introduction
Collaborative Cadaver Program Proposal for General Surgery, Thoracic
Surgery, Otolaryngology, and Orthopedic Surgery.

Cost savings come first. Baseline is 4 cadavers/year for General Surgery.
Formula: (4 × per-cadaver) + Annual Cadaver Lab Fee. The analysis excludes
Supplies and Education. Graph and savings table for 1-4 departments below.

Ethical use: we maximize cadaver use to honor donors and respect final wishes.
Anatomy: abdomen for General Surgery; thorax for Thoracic Surgery; head and
neck for Otolaryngology; limbs for Orthopedic Surgery.

Freeze/thaw cycles are 10-12. Once thawed there is a 3-hour window.
Simple 30-45 minutes (up to 4 per window; totals 40-48). Standard 1-1.5
hours (2-3 per window; totals 20-36). Complex 2-3 hours (1 per window;
totals 10-12). This proposal does not account for mixing complexity.
"""


def _write_docx(path: Path, text: str) -> Path:
    doc = Document()
    for line in text.strip().splitlines():
        doc.add_paragraph(line)
    doc.save(str(path))
    return path


def _write_odt(path: Path, text: str, *, title_as_heading: bool = False) -> Path:
    doc = OpenDocumentText()
    lines = text.strip().splitlines()
    if title_as_heading and lines:
        heading = H(outlinelevel=1)
        heading.addText(lines[0])
        doc.text.addElement(heading)
        lines = lines[1:]
    for line in lines:
        doc.text.addElement(P(text=line))
    doc.save(str(path))
    return path


def _add_savings_frame(doc: OpenDocumentText, caption: str) -> None:
    # Name must not contain "saving" — that would leak into the visual window.
    frame = Frame(width="10cm", height="6cm", name="EmbeddedObject")
    box = TextBox()
    box.addElement(P(text="embedded object"))
    frame.addElement(box)
    doc.text.addElement(frame)
    doc.text.addElement(P(text=caption))


def _has_chart_or_graph_word(text: str) -> bool:
    return bool(re.search(r"\bcharts?\b|\bgraphs?\b", text, re.I))


def _padded() -> str:
    return _PASSING + (" Shared cadaver training capacity. " * 80)


def _gemini_shaped() -> str:
    """Headed Gemini wording: Overview, lab facility fee, 60–90 min, 3 to 4."""
    return """
Program Overview
Collaborative Cadaver Program Proposal for General Surgery, Thoracic
Surgery, Otolaryngology, and Orthopedic Surgery.

This overview honors donors and their final wishes. The hospital charter
is the governing document for shared specimens.

Cost Savings
Baseline is 4 cadavers/year for General Surgery. Annual cost is
$3,000 × 4 + $1,000 = $13,000 using the per-cadaver bundle plus the
Annual Anatomy Lab Facility Fee. The analysis excludes specialty-unique
Supplies and Education. Graphical representation of annual savings for
1-4 departments is embedded.

Ethical Stewardship
We maximize cadaver use to honor donors and respect final wishes.
Anatomy: abdomen for General Surgery; thorax for Thoracic Surgery; head and
neck for Otolaryngology; limbs for Orthopedic Surgery.

Freeze/thaw cycles are 10-12. Once thawed there is a 3-hour window.
Simple 30-45 minutes (3 to 4 per window; totals 40-48). Standard 60-90
minutes (2-3 per window; totals 20-36). Complex 2-3 hours (1 per window;
totals 10-12). This proposal does not account for mixing complexity.
"""


def test_passing_proposal_docx_and_odt(tmp_path: Path) -> None:
    text = _padded()
    docx = _write_docx(tmp_path / "ok.docx", text)
    odt = _write_odt(tmp_path / "ok.odt", text)
    for path in (docx, odt):
        result = score_proposal(path)
        assert result.passed, (path.name, result.failures)
        assert 250 <= result.word_count <= 3200


def test_odt_heading_title_is_scored(tmp_path: Path) -> None:
    """Headed Writer puts the title in text:h; body stays text:p."""
    body = _padded().replace("Collaborative Cadaver Program Proposal", "Working draft", 1)
    path = _write_odt(tmp_path / "headed.odt", "Collaborative Cadaver Program Proposal\n" + body, title_as_heading=True)
    result = score_proposal(path)
    assert result.passed, result.failures


def test_empty_proposal_fails(tmp_path: Path) -> None:
    path = _write_odt(tmp_path / "empty.odt", "")
    result = score_proposal(path)
    assert not result.passed
    assert any("empty" in item or "word_count" in item for item in result.failures)


def test_missing_exclude_wording_fails() -> None:
    text = _padded().replace("excludes\nSupplies and Education", "mentions Supplies and Education")
    result = score_text(text, para_count=12)
    assert not result.passed
    assert any("exclude" in item for item in result.failures)


def test_missing_formula_fails() -> None:
    text = _padded().replace("(4 × per-cadaver) + Annual Cadaver Lab Fee", "some shared costs")
    result = score_text(text, para_count=12)
    assert not result.passed
    assert any("formula" in item for item in result.failures)


def test_cost_savings_must_come_before_ethics() -> None:
    text = _padded()
    ethics = "Ethical use: we maximize cadaver use to honor donors and respect final wishes."
    cost = "Cost savings come first."
    swapped = text.replace(ethics, "PLACEHOLDER_ETHICS").replace(cost, ethics).replace("PLACEHOLDER_ETHICS", cost)
    result = score_text(swapped, para_count=12)
    assert not result.passed
    assert any("not first" in item for item in result.failures)


def test_missing_standard_36_fails() -> None:
    text = _padded().replace("20-36", "20-24")
    result = score_text(text, para_count=12)
    assert not result.passed
    assert any("20-36" in item for item in result.failures)


@pytest.mark.parametrize(
    "value, value_2",
    [
        pytest.param("$", "2,000", id="test_does_not_require_exact_dollars"),
        pytest.param("Hope Hospital", "Silverview", id="test_does_not_require_hope_or_silverview"),
    ],
)
def test_does_not_require_exact_dollars(value, value_2) -> None:
    text = _padded()
    assert value not in text
    assert value_2 not in text
    result = score_text(text, para_count=12)
    assert result.passed, result.failures

def test_husk_body_fails() -> None:
    text = _padded() + "\nError: tool failed\n"
    result = score_text(text, para_count=12)
    assert not result.passed
    assert any("husk" in item for item in result.failures)


def test_headed_score_routes_to_cadaver_oracle(tmp_path: Path) -> None:
    path = _write_odt(tmp_path / "final_proposal.odt", _padded())
    assert headed_main(["--task", "cadaver-proposal", "--score", str(path)]) == 0
    empty = _write_odt(tmp_path / "empty.odt", "")
    assert headed_main(["--task", "cadaver-proposal", "--score", str(empty)]) == 1


def test_oracle_cli_json(tmp_path: Path, capsys) -> None:
    path = _write_odt(tmp_path / "final_proposal.odt", _padded())
    assert oracle_main(["--json", str(path)]) == 0
    out = capsys.readouterr().out
    assert '"passed": true' in out


def test_overview_and_executive_summary_count_as_introduction() -> None:
    for title in ("Overview", "Executive Summary", "Program Overview"):
        text = _padded().replace("Introduction", title, 1)
        assert "Introduction" not in text
        result = score_text(text, para_count=12)
        assert result.passed, (title, result.failures)


def test_lab_facility_fee_alias_passes() -> None:
    text = _padded().replace("Annual Cadaver Lab Fee", "Annual Anatomy Lab Facility Fee")
    assert "lab fee" not in text.lower()
    result = score_text(text, para_count=12)
    assert result.passed, result.failures


def test_exclude_either_order_still_requires_both_names() -> None:
    text = _padded().replace(
        "The analysis excludes\nSupplies and Education.",
        "Education and Supplies are excluded as specialty-unique costs.",
    )
    result = score_text(text, para_count=12)
    assert result.passed, result.failures
    missing_education = _padded().replace("Supplies and Education", "Supplies")
    result = score_text(missing_education, para_count=12)
    assert not result.passed
    assert any("exclude" in item for item in result.failures)


def test_arithmetic_baseline_formula_passes() -> None:
    text = _padded().replace(
        "(4 × per-cadaver) + Annual Cadaver Lab Fee",
        "$3k + $1k = $13k plus the Annual Cadaver Lab Fee per-cadaver baseline",
    )
    assert "(4 × per-cadaver)" not in text
    result = score_text(text, para_count=12)
    assert result.passed, result.failures


def test_graphical_and_figure_pass_without_chart_word() -> None:
    for token in ("Graphical representation", "Figure 1"):
        text = _padded().replace(
            "Graph and savings table for 1-4 departments below.",
            f"{token} of annual savings.",
        )
        assert not _has_chart_or_graph_word(text)
        result = score_text(text, para_count=12)
        assert result.passed, (token, result.failures)


def test_standard_60_90_minutes_and_simple_3_to_4() -> None:
    text = (
        _padded()
        .replace("Standard 1-1.5\nhours", "Standard 60-90 minutes")
        .replace("(up to 4 per window", "(3 to 4 per window")
    )
    assert "1-1.5" not in text
    assert "up to 4" not in text
    result = score_text(text, para_count=12)
    assert result.passed, result.failures


def test_word_band_allows_2501_to_3200() -> None:
    text = _PASSING
    filler = " Shared cadaver training capacity."
    result = score_text(text, para_count=12)
    while result.word_count < 2600:
        text += filler
        result = score_text(text, para_count=12)
    assert 2501 <= result.word_count <= 3200
    assert result.passed, result.failures
    while result.word_count <= 3200:
        text += filler
        result = score_text(text, para_count=12)
    assert result.word_count > 3200
    assert not result.passed
    assert any("word_count" in item for item in result.failures)


def test_early_honor_donor_in_overview_does_not_fail_cost_first() -> None:
    """Overview may mention donors before the Cost Savings heading."""
    text = _gemini_shaped() + (" Shared cadaver training capacity. " * 80)
    # First honor/donor hit is in the overview, before "Cost Savings".
    honor_at = text.lower().index("honors donors")
    cost_heading_at = text.index("Cost Savings")
    assert honor_at < cost_heading_at
    result = score_text(
        text,
        para_count=16,
        headings=["Program Overview", "Cost Savings", "Ethical Stewardship"],
    )
    assert result.passed, result.failures


def test_ethics_heading_before_cost_heading_still_fails() -> None:
    text = _padded()
    ethics = "Ethical use: we maximize cadaver use to honor donors and respect final wishes."
    cost = "Cost savings come first."
    swapped = text.replace(ethics, "PLACEHOLDER_ETHICS").replace(cost, ethics).replace(
        "PLACEHOLDER_ETHICS", cost
    )
    result = score_text(
        swapped,
        para_count=12,
        headings=["Ethical Stewardship", "Cost Savings"],
    )
    assert not result.passed
    assert any("not first" in item for item in result.failures)


def test_odt_frame_with_savings_caption_counts_as_graph(tmp_path: Path) -> None:
    """Bare 'charter' is not a chart; a draw:frame + savings caption is."""
    body = (
        _padded()
        .replace("Graph and savings table for 1-4 departments below.", "See the embedded object.")
        .replace("Introduction", "Program Overview", 1)
    )
    assert not _has_chart_or_graph_word(body)
    doc = OpenDocumentText()
    for line in body.strip().splitlines():
        if line in {"Program Overview"}:
            doc.text.addElement(H(outlinelevel=1, text=line))
        else:
            doc.text.addElement(P(text=line))
    _add_savings_frame(doc, "Annual savings by participating departments.")
    path = tmp_path / "framed.odt"
    doc.save(str(path))
    extract = read_proposal(path)
    assert extract.has_savings_visual
    result = score_proposal(path)
    assert result.passed, result.failures


def test_bare_frame_without_savings_caption_is_not_graph(tmp_path: Path) -> None:
    body = _padded().replace(
        "Graph and savings table for 1-4 departments below.",
        "See the embedded object in the hospital charter.",
    )
    doc = OpenDocumentText()
    for line in body.strip().splitlines():
        doc.text.addElement(P(text=line))
    _add_savings_frame(doc, "Decorative logo only.")
    path = tmp_path / "bare-frame.odt"
    doc.save(str(path))
    extract = read_proposal(path)
    assert not extract.has_savings_visual
    result = score_proposal(path)
    assert not result.passed
    assert any("graph" in item or "chart" in item for item in result.failures)


def test_savings_table_element_counts_as_graph(tmp_path: Path) -> None:
    body = _padded().replace(
        "Graph and savings table for 1-4 departments below.",
        "See the embedded object in the hospital charter.",
    )
    doc = OpenDocumentText()
    for line in body.strip().splitlines():
        doc.text.addElement(P(text=line))
    table = Table()
    for rec in (("Departments", "Annual Savings"), ("1", "0"), ("4", "39000")):
        row = TableRow()
        for val in rec:
            cell = TableCell(valuetype="string")
            cell.addElement(P(text=val))
            row.addElement(cell)
        table.addElement(row)
    doc.text.addElement(table)
    path = tmp_path / "savings-table.odt"
    doc.save(str(path))
    extract = read_proposal(path)
    assert extract.has_savings_visual
    result = score_proposal(path)
    assert result.passed, result.failures


def test_gemini_shaped_softened_proposal_passes() -> None:
    text = _gemini_shaped() + (" Shared cadaver training capacity. " * 80)
    assert "Introduction" not in text
    assert "1-1.5" not in text
    assert "up to 4" not in text
    assert not _has_chart_or_graph_word(text)
    result = score_text(
        text,
        para_count=18,
        headings=["Program Overview", "Cost Savings", "Ethical Stewardship"],
    )
    assert result.passed, result.failures


def test_gemini_headed_fixture_passes_when_present() -> None:
    """Re-score the headed Gemini artifact when Keith's run is on disk."""
    if not _GEMINI_FIXTURE.is_file():
        return
    result = score_proposal(_GEMINI_FIXTURE)
    assert result.passed, result.failures
