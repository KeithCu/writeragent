# WriterAgent tests for scripts/eval_2_gmp_oracle.py
from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from docx import Document
from odf.opendocument import OpenDocumentText
from odf.text import P

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from eval_2_gmp_oracle import (  # noqa: E402
    _FORM_CITE_RE,
    main as oracle_main,
    normalize_hyphen_like,
    read_odg_frames,
    resolve_gmp_artifacts,
    score_artifacts,
    score_pair,
)
from eval_2_headed import (  # noqa: E402
    GMP_FILLABLE_FIELDS,
    GMP_FORM_ODG_NAME,
    write_gmp_change_control_odg,
)
from eval_2_headed import main as headed_main  # noqa: E402

_DRAW_NS = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"

_PASSING = """
Risk Assessment Summary

Subject: Endotoxin mismatch — RMS-3333 / QY-GEL Antifoam (CompCello)

QA Escalation Email
Subject: RMS-3333 Endotoxin — deviation or requalification?
To QA leadership: the COA states Endotoxin Level: Report Result while
RMS-3333 requires Endotoxin Level < 1 EU/ml. A draft change control
request is on the filled Change Control Tracking Form. May we accept the
lot under a deviation, or is full requalification required? Material is
on QA hold / quarantined.

Internal Summary Note (Teams)
Status update: specification mismatch flagged; change control initiated;
material quarantined. Manufacturing timelines are at risk until QA
disposes the lot.

Incident overview
CompCello sent a change notification two months ago (report only
endotoxin). The vendor memo went to an employee who has since left the
company; no centralized process existed. Operational risks include
release delay. Documentation / compliance risks include spec
misalignment. Mitigations: centralized vendor communication tracking
(shared mailbox) and SOP updates for vendor change notices.
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


def _fill_form(path: Path, values: dict[str, str]) -> Path:
    write_gmp_change_control_odg(path)
    with zipfile.ZipFile(path, "r") as zf:
        content = zf.read("content.xml").decode("utf-8")
        extras = {name: zf.read(name) for name in zf.namelist() if name != "content.xml"}
    root = ET.fromstring(content)
    for frame in root.iter(f"{{{_DRAW_NS}}}frame"):
        name = frame.get(f"{{{_DRAW_NS}}}name") or ""
        if name in values:
            for node in list(frame):
                frame.remove(node)
            box = ET.SubElement(frame, "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p")
            # Keep a text-box wrapper like the blank stand-in.
            box.tag = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}text-box"
            para = ET.SubElement(box, "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p")
            para.text = values[name]
    xml = ET.tostring(root, encoding="utf-8")
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in extras.items():
            compress = zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
            zf.writestr(name, data, compress_type=compress)
        zf.writestr("content.xml", xml)
    return path


def _filled_values() -> dict[str, str]:
    return {
        "fld_change_title": "Update RMS-3333 endotoxin criterion for QY-GEL Antifoam",
        "fld_champion": "Project Management",
        "fld_department": "QA / Manufacturing",
        "fld_date_initiated": "09 Sep 2026",
        "fld_product": "QY-GEL Antifoam, CompCello",
        "fld_current_situation": (
            "RMS-3333 requires Endotoxin Level < 1 EU/ml; "
            "COA states Endotoxin Level: Report Result (mismatch)."
        ),
        "fld_proposed_situation": "Update RMS-3333; keep material quarantined pending QA.",
        "fld_justification": "Vendor CompCello changed reporting; hold until disposition.",
    }


def _padded() -> str:
    return _PASSING + (" Follow-through on vendor intake. " * 40)


def test_passing_pair_odt_and_docx(tmp_path: Path) -> None:
    text = _padded()
    form = _fill_form(tmp_path / "Change Control Form.odg", _filled_values())
    for memo in (
        _write_odt(tmp_path / "ok.odt", text),
        _write_docx(tmp_path / "ok.docx", text),
    ):
        result = score_artifacts(memo, form)
        assert result.passed, (memo.name, result.failures)
        assert result.filled_fields >= 5


def test_empty_memo_fails(tmp_path: Path) -> None:
    memo = _write_odt(tmp_path / "empty.odt", "")
    form = _fill_form(tmp_path / GMP_FORM_ODG_NAME, _filled_values())
    result = score_artifacts(memo, form)
    assert not result.passed
    assert any("empty" in item or "word_count" in item for item in result.failures)


def test_blank_form_fails(tmp_path: Path) -> None:
    memo = _write_odt(tmp_path / "memo.odt", _padded())
    form = write_gmp_change_control_odg(tmp_path / GMP_FORM_ODG_NAME)
    result = score_artifacts(memo, form)
    assert not result.passed
    assert any("substantially filled" in item for item in result.failures)


def test_missing_rms_fails() -> None:
    text = _padded().replace("RMS-3333", "SPEC-0000")
    result = score_pair(
        text,
        "filled form with CompCello QY-GEL Report Result < 1 EU quarantined",
        para_count=12,
        filled_fields=6,
        filled_chars=120,
    )
    assert not result.passed
    assert any("RMS-3333" in item for item in result.failures)


def test_normalize_hyphen_like_folds_identity_dashes() -> None:
    assert normalize_hyphen_like("RMS\u20113333") == "RMS-3333"
    assert normalize_hyphen_like("RMS\u20103333") == "RMS-3333"
    assert normalize_hyphen_like("RMS\u22123333") == "RMS-3333"
    assert normalize_hyphen_like("QY\u2010GEL") == "QY-GEL"
    assert normalize_hyphen_like("RMS\u00a03333") == "RMS 3333"


def test_unicode_hyphen_rms_id_matches() -> None:
    """U+2011 in RMS‑3333 is the same identity as ASCII RMS-3333 (encoding only)."""
    text = _padded().replace("RMS-3333", "RMS\u20113333")
    result = score_pair(
        text,
        "Change Title: RMS\u20113333 QY\u2010GEL CompCello Report Result < 1 EU/ml hold",
        para_count=16,
        filled_fields=6,
        filled_chars=120,
    )
    assert result.passed, result.failures


def test_gold_xx_cell_not_required() -> None:
    text = _padded()
    assert "XX-CELL" not in text
    assert "A23-044" not in text
    result = score_pair(
        text,
        "Change Title: RMS-3333 QY-GEL CompCello Report Result < 1 EU/ml hold",
        para_count=16,
        filled_fields=6,
        filled_chars=120,
    )
    assert result.passed, result.failures


def test_husk_body_fails() -> None:
    text = _padded() + "\nError: tool failed\n"
    result = score_pair(
        text,
        "ok",
        para_count=12,
        filled_fields=6,
        filled_chars=120,
    )
    assert not result.passed
    assert any("husk" in item for item in result.failures)


def test_synonym_sections_pass() -> None:
    text = (
        _padded()
        .replace("QA Escalation Email", "Quality Assurance leadership email")
        .replace("Internal Summary Note (Teams)", "Stakeholder status note")
        .replace("left the company", "former employee")
    )
    result = score_pair(
        text,
        "Change Control Tracking Form filled",
        para_count=16,
        filled_fields=6,
        filled_chars=120,
    )
    assert result.passed, result.failures


_CITE_SENTENCE = (
    "A draft change control\n"
    "request is on the filled Change Control Tracking Form. "
)


def _memo_without_form_cite() -> str:
    """Keep change-control theme; drop form / request / Form-920 cites."""
    stripped = _padded().replace(_CITE_SENTENCE, "")
    assert "Change Control Request" not in stripped
    assert "filled Change Control Tracking Form" not in stripped
    assert "draft change control" not in stripped.lower()
    return stripped


def test_form_cite_accepts_change_control_request_alias() -> None:
    """Headed HAPPY (20260909-0225) used prompt wording, not tracking form."""
    happy_phrases = (
        "Change Control Request",
        "Change Control Request completed",
        "drafted a Change Control Request",
        "completed the change control",
        "CCR filed for RMS-3333",
    )
    for phrase in happy_phrases:
        assert _FORM_CITE_RE.search(phrase), phrase
    # Theme-only "change control" is the RMS-update check, not a form cite.
    assert not _FORM_CITE_RE.search("change control initiated")
    assert not _FORM_CITE_RE.search("formal change control")
    assert not _FORM_CITE_RE.search("ACCRINT")
    text = _memo_without_form_cite() + " I drafted a Change Control Request."
    result = score_pair(
        text,
        "Change Title: RMS-3333 QY-GEL CompCello Report Result < 1 EU/ml hold",
        para_count=16,
        filled_fields=9,
        filled_chars=1497,
    )
    assert result.passed, result.failures
    assert not any("cite" in item for item in result.failures)


def test_form_cite_still_fails_without_deliverable_cite() -> None:
    result = score_pair(
        _memo_without_form_cite(),
        "Change Title: RMS-3333 QY-GEL CompCello Report Result < 1 EU/ml hold",
        para_count=16,
        filled_fields=9,
        filled_chars=1497,
    )
    assert not result.passed
    assert any("cite the filled change-control form" in item for item in result.failures)
    # Form fill / identity / husk bands are unchanged — only the cite fails.
    assert result.filled_fields == 9
    assert result.filled_chars == 1497
    assert not any("RMS-3333" in item for item in result.failures)
    assert not any("CompCello" in item for item in result.failures)
    assert not any("husk" in item for item in result.failures)
    assert not any("substantially filled" in item for item in result.failures)


def test_resolve_sibling_form(tmp_path: Path) -> None:
    memo = _write_odt(tmp_path / "final_memo.odt", _padded())
    form = _fill_form(tmp_path / "final_form.odg", _filled_values())
    found_memo, found_form = resolve_gmp_artifacts(memo)
    assert found_memo == memo
    assert found_form == form
    found_memo, found_form = resolve_gmp_artifacts(tmp_path)
    assert found_memo == memo
    assert found_form == form


def test_headed_score_routes_to_gmp_oracle(tmp_path: Path) -> None:
    memo = _write_odt(tmp_path / "final_memo.odt", _padded())
    _fill_form(tmp_path / "final_form.odg", _filled_values())
    assert headed_main(["--task", "gmp-change-control", "--score", str(memo)]) == 0
    empty = _write_odt(tmp_path / "empty.odt", "")
    assert headed_main(["--task", "gmp-change-control", "--score", str(empty)]) == 1


def test_oracle_cli_json(tmp_path: Path, capsys) -> None:
    memo = _write_odt(tmp_path / "final_memo.odt", _padded())
    _fill_form(tmp_path / "final_form.odg", _filled_values())
    assert oracle_main(["--json", str(memo)]) == 0
    out = capsys.readouterr().out
    assert '"passed": true' in out


def test_odg_extracts_named_frames(tmp_path: Path) -> None:
    path = write_gmp_change_control_odg(tmp_path / "blank.odg")
    frames = read_odg_frames(path)
    names = {name for name, _text in frames}
    for field_name, _label in GMP_FILLABLE_FIELDS:
        assert field_name in names
    filled = [text for name, text in frames if name.startswith("fld_") and text.strip()]
    assert filled == []
