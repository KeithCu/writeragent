# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scripts/generate_open_index.py."""

from __future__ import annotations

from pathlib import Path

from scripts.generate_open_index import main


def _write_plan(docs: Path, extra_bullet: str = "") -> None:
    (docs / "plan.md").write_text(
        f"# P\n\n## Open\n\n- `[feature S many]` Do it.\n{extra_bullet}",
        encoding="utf-8",
    )


def test_check_passes_without_untracked_csv(tmp_path: Path) -> None:
    _write_plan(tmp_path)
    assert main([], docs=tmp_path) == 0
    csv_path = tmp_path / "OPEN.csv"
    csv_path.unlink()

    assert main(["--check"], docs=tmp_path) == 0
    assert not csv_path.exists()


def test_check_fails_when_open_md_is_missing_or_stale(tmp_path: Path) -> None:
    _write_plan(tmp_path)
    assert main(["--check"], docs=tmp_path) == 1

    assert main([], docs=tmp_path) == 0
    assert main(["--check"], docs=tmp_path) == 0

    _write_plan(tmp_path, "- `[eng S few]` Another item.\n")
    assert main(["--check"], docs=tmp_path) == 1
