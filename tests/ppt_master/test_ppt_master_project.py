# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

from plugin.contrib.ppt_master.upstream import collect_svg_files
from plugin.ppt_master.project_notes import notes_for_slides

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ppt_master_minimal"


def test_collect_svg_files_minimal_fixture():
    files = collect_svg_files(FIXTURE)
    assert len(files) == 3
    assert files[0].name == "01_intro.svg"


def test_notes_for_slides_index_fallback():
    files = collect_svg_files(FIXTURE)
    notes = notes_for_slides(FIXTURE, files, None)
    assert notes.get(0) is not None
    assert "Opening slide" in notes[0]
    assert notes.get(1) is None
