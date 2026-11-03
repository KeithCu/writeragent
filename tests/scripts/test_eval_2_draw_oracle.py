# WriterAgent tests for scripts/eval_2_draw_oracle.py
from __future__ import annotations

import sys
from pathlib import Path

from odf.opendocument import OpenDocumentText
from odf.text import P

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from eval_2_draw_oracle import (  # noqa: E402
    main as oracle_main,
    normalize_hyphen_like,
    read_odg_drawing,
    resolve_draw_artifact,
    score_artifact,
    score_drawing,
)
from eval_2_headed import (  # noqa: E402
    DRAW_PRIMARY_ODG_NAME,
    _write_odg_zip,
    write_draw_primary_odg,
)
from eval_2_headed import main as headed_main  # noqa: E402

_PASSING_LABELS = (
    "Clearbend Logistics Hub — inbound piece flow",
    "Start / inbound receiving",
    "Unload / induction",
    "Decision: automation-compatible?",
    "Scan barcode",
    "Automation lane: auto sort / divert",
    "Automation outbound dispatch",
    "Jam / no-read failure → manual",
    "Manual lane: exception triage",
    "Relabel / rework",
    "Manual sort",
    "Manual outbound dispatch",
    "End",
)


def _write_odt(path: Path, text: str) -> Path:
    doc = OpenDocumentText()
    for line in text.strip().splitlines():
        doc.text.addElement(P(text=line))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def _write_labeled_odg(
    path: Path,
    labels: tuple[str, ...],
    *,
    connectors: int = 3,
) -> Path:
    """Build a process-map-shaped ODG from labels + dummy connectors."""
    write_draw_primary_odg(path)
    frames: list[str] = []
    y = 2.0
    for idx, label in enumerate(labels):
        frames.append(
            f'<draw:frame draw:name="node_{idx}" svg:x="2cm" svg:y="{y:.1f}cm" '
            f'svg:width="8cm" svg:height="1.2cm"><draw:text-box>'
            f"<text:p>{label}</text:p>"
            "</draw:text-box></draw:frame>"
        )
        y += 1.4
    for idx in range(connectors):
        frames.append(
            f'<draw:connector draw:name="link_{idx}" svg:x1="10cm" svg:y1="{2 + idx:.1f}cm" '
            f'svg:x2="11cm" svg:y2="{3 + idx:.1f}cm"/>'
        )
    _write_odg_zip(path, page_name="ProcessFlow", body_inner="".join(frames))
    return path


def test_passing_drawing(tmp_path: Path) -> None:
    path = _write_labeled_odg(tmp_path / "ok.odg", _PASSING_LABELS)
    result = score_artifact(path)
    assert result.passed, result.failures
    assert result.labeled_shapes >= 6
    assert result.connector_count >= 2


def test_blank_canvas_fails(tmp_path: Path) -> None:
    path = write_draw_primary_odg(tmp_path / DRAW_PRIMARY_ODG_NAME)
    result = score_artifact(path)
    assert not result.passed
    assert any("title-only" in item or "labeled shapes" in item for item in result.failures)


def test_missing_clearbend_fails() -> None:
    text = "\n".join(
        label.replace("Clearbend Logistics Hub — inbound piece flow", "Some other hub")
        for label in _PASSING_LABELS
    )
    result = score_drawing(
        text,
        labeled_shapes=12,
        connector_count=4,
        labeled_chars=200,
    )
    assert not result.passed
    assert any("Clearbend" in item for item in result.failures)


def test_missing_connectors_fails() -> None:
    result = score_drawing(
        "\n".join(_PASSING_LABELS),
        labeled_shapes=12,
        connector_count=0,
        labeled_chars=200,
    )
    assert not result.passed
    assert any("connectors" in item for item in result.failures)


def test_husk_body_fails() -> None:
    result = score_drawing(
        "\n".join(_PASSING_LABELS) + "\nError: tool failed\n",
        labeled_shapes=12,
        connector_count=4,
        labeled_chars=200,
    )
    assert not result.passed
    assert any("husk" in item for item in result.failures)


def test_writer_odt_is_not_a_substitute(tmp_path: Path) -> None:
    memo = _write_odt(tmp_path / "final_memo.odt", "\n".join(_PASSING_LABELS))
    result = score_artifact(memo)
    assert not result.passed
    assert any(".odg" in item for item in result.failures)


def test_normalize_hyphen_like_folds_identity_dashes() -> None:
    assert normalize_hyphen_like("non\u2011conveyable") == "non-conveyable"
    assert normalize_hyphen_like("no\u2010read") == "no-read"


def test_unicode_hyphen_anchors_match() -> None:
    text = "\n".join(_PASSING_LABELS).replace("no-read", "no\u2011read")
    result = score_drawing(
        text,
        labeled_shapes=12,
        connector_count=4,
        labeled_chars=200,
    )
    assert result.passed, result.failures


def test_synonym_lanes_pass() -> None:
    labels = (
        "Clearbend Logistics Hub",
        "Start inbound receiving",
        "Unload induction",
        "Classification: conveyable vs non-conveyable",
        "Scan / OCR",
        "Automated divert to chutes",
        "Outbound dispatch",
        "Reject / out-of-spec exception",
        "Manual incompatible triage",
        "Rescan rework",
        "End",
    )
    result = score_drawing(
        "\n".join(labels),
        labeled_shapes=11,
        connector_count=3,
        labeled_chars=220,
    )
    assert result.passed, result.failures


def test_resolve_trial_dir(tmp_path: Path) -> None:
    path = _write_labeled_odg(tmp_path / "final_drawing.odg", _PASSING_LABELS)
    assert resolve_draw_artifact(tmp_path) == path
    assert resolve_draw_artifact(path) == path


def test_headed_score_routes_to_draw_oracle(tmp_path: Path) -> None:
    path = _write_labeled_odg(tmp_path / "final_drawing.odg", _PASSING_LABELS)
    assert headed_main(["--task", "draw-primary", "--score", str(path)]) == 0
    empty = write_draw_primary_odg(tmp_path / "empty.odg")
    assert headed_main(["--task", "draw-primary", "--score", str(empty)]) == 1


def test_oracle_cli_json(tmp_path: Path, capsys) -> None:
    path = _write_labeled_odg(tmp_path / "final_drawing.odg", _PASSING_LABELS)
    assert oracle_main(["--json", str(path)]) == 0
    out = capsys.readouterr().out
    assert '"passed": true' in out


def test_odg_extracts_title_only_blank(tmp_path: Path) -> None:
    path = write_draw_primary_odg(tmp_path / "blank.odg")
    extract = read_odg_drawing(path)
    assert extract.labeled_shapes == 0
    assert extract.connector_count == 0
    assert "Process Flow Map" in extract.text
