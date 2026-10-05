# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Native enhance writes the NotesShape and does not report a failed apply as ok."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugin.ppt_master.adapter.uno_enhance import apply_enhancement_project

_NOTES = "com.sun.star.presentation.NotesShape"
_FOOTER = "com.sun.star.presentation.FooterShape"


class DisposedException(Exception):
    pass


class _Shape:
    def __init__(self, shape_type: str, text: str = "") -> None:
        self.shape_type = shape_type
        self.text = text
        self.fail: BaseException | None = None

    def getShapeType(self) -> str:
        return self.shape_type

    def setString(self, value: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.text = value


class _NotesPage:
    def __init__(self, shapes: list[_Shape]) -> None:
        self.shapes = shapes

    def getCount(self) -> int:
        return len(self.shapes)

    def getByIndex(self, index: int) -> _Shape:
        return self.shapes[index]


class _Slide:
    def __init__(self, shapes: list[_Shape]) -> None:
        self.notes = _NotesPage(shapes)
        self.props: dict[str, object] = {}
        self.transition_fail: BaseException | None = None

    def getNotesPage(self) -> _NotesPage:
        return self.notes

    def setPropertyValue(self, name: str, value: object) -> None:
        if self.transition_fail is not None:
            raise self.transition_fail
        self.props[name] = value


class _Pages:
    def __init__(self, slides: list[_Slide]) -> None:
        self.slides = slides

    def getCount(self) -> int:
        return len(self.slides)

    def getByIndex(self, index: int) -> _Slide:
        return self.slides[index]


class _Doc:
    def __init__(self, slides: list[_Slide], *, impress: bool = True) -> None:
        self.pages = _Pages(slides)
        self.impress = impress

    def getDrawPages(self) -> _Pages:
        return self.pages

    def supportsService(self, name: str) -> bool:
        return self.impress and name == "com.sun.star.presentation.PresentationDocument"


def _write_plan(tmp_path: Path, slides: list[dict[str, object]]) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    (path / "enhancement_plan.json").write_text(json.dumps({"slides": slides}), encoding="utf-8")
    return path


def test_apply_writes_notes_shape_not_footer(tmp_path: Path):
    footer = _Shape(_FOOTER, "footer")
    notes = _Shape(_NOTES, "")
    doc = _Doc([_Slide([footer, notes])])
    project = _write_plan(tmp_path, [{"slide_index": 0, "notes": "Say this"}])

    result = apply_enhancement_project(doc, project)

    assert result["status"] == "ok"
    assert result["applied"] == 1
    assert notes.text == "Say this"
    assert footer.text == "footer"


def test_apply_does_not_count_missing_notes_shape(tmp_path: Path):
    footer = _Shape(_FOOTER, "footer")
    doc = _Doc([_Slide([footer])])
    project = _write_plan(tmp_path, [{"index": 0, "notes": "Say this"}])

    result = apply_enhancement_project(doc, project)

    assert result["status"] == "error"
    assert result["applied"] == 0
    assert result["failed"] == 1
    assert footer.text == "footer"
    assert "no NotesShape" in result["message"]


def test_apply_does_not_claim_ok_when_setstring_fails(tmp_path: Path):
    failed = _Shape(_NOTES, "keep")
    failed.fail = ValueError("read only")
    ok_notes = _Shape(_NOTES, "")
    doc = _Doc([_Slide([failed]), _Slide([_Shape(_FOOTER, "chrome"), ok_notes])])
    project = _write_plan(
        tmp_path,
        [
            {"slide_index": 0, "notes": "first"},
            {"slide_index": 1, "notes": "second"},
        ],
    )

    result = apply_enhancement_project(doc, project)

    assert result["status"] == "error"
    assert result["applied"] == 1
    assert result["failed"] == 1
    assert failed.text == "keep"
    assert ok_notes.text == "second"
    assert "read only" in result["message"]


def test_apply_reraises_dispose_from_notes(tmp_path: Path):
    notes = _Shape(_NOTES, "keep")
    notes.fail = DisposedException("gone")
    doc = _Doc([_Slide([notes])])
    project = _write_plan(tmp_path, [{"slide_index": 0, "notes": "next"}])

    with pytest.raises(DisposedException):
        apply_enhancement_project(doc, project)
    assert notes.text == "keep"


def test_apply_reraises_dispose_from_transition_and_counts_notes(tmp_path: Path):
    notes = _Shape(_NOTES, "")
    slide = _Slide([notes])
    slide.transition_fail = DisposedException("page gone")
    doc = _Doc([slide])
    project = _write_plan(tmp_path, [{"slide_index": 0, "notes": "Cue", "transition": {"type": 2}}])

    with pytest.raises(DisposedException):
        apply_enhancement_project(doc, project)
    assert notes.text == "Cue"


def test_apply_transition_failure_is_not_ok(tmp_path: Path):
    notes = _Shape(_NOTES, "")
    slide = _Slide([notes])
    slide.transition_fail = ValueError("bad type")
    doc = _Doc([slide])
    project = _write_plan(tmp_path, [{"slide_index": 0, "notes": "Cue", "transition": {"type": 2}}])

    result = apply_enhancement_project(doc, project)

    assert result["status"] == "error"
    assert result["applied"] == 1
    assert notes.text == "Cue"
    assert "Effect" not in slide.props
    assert "bad type" in result["message"]


def test_apply_notes_and_transition_both_count(tmp_path: Path):
    notes = _Shape(_NOTES, "")
    slide = _Slide([_Shape(_FOOTER, "f"), notes])
    doc = _Doc([slide])
    project = _write_plan(tmp_path, [{"slide_index": 0, "notes": "Cue", "transition": {"type": 4}}])

    result = apply_enhancement_project(doc, project)

    assert result == {"status": "ok", "applied": 2}
    assert notes.text == "Cue"
    assert slide.props["Effect"] == 4


def test_apply_skips_notes_on_non_impress(tmp_path: Path):
    notes = _Shape(_NOTES, "unchanged")
    doc = _Doc([_Slide([notes])], impress=False)
    project = _write_plan(tmp_path, [{"slide_index": 0, "notes": "Nope"}])

    result = apply_enhancement_project(doc, project)

    assert result == {"status": "ok", "applied": 0}
    assert notes.text == "unchanged"


def test_apply_missing_plan_is_ok(tmp_path: Path):
    result = apply_enhancement_project(_Doc([]), tmp_path)
    assert result["status"] == "ok"
    assert result["applied"] == 0
