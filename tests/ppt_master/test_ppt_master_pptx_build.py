# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plugin.ppt_master.pptx_build import build_project_pptx, ensure_project_pptx, find_project_pptx


def test_find_project_pptx_prefers_native_over_svg_snapshot(tmp_path: Path):
    exports = tmp_path / "exports"
    exports.mkdir()
    svg_snap = exports / "deck_20260101_120000_svg.pptx"
    native = exports / "deck_20260101_120000.pptx"
    svg_snap.write_bytes(b"svg")
    native.write_bytes(b"native")
    # Make native newer
    import os
    import time

    os.utime(svg_snap, (time.time() + 10, time.time() + 10))
    assert find_project_pptx(tmp_path) == native


def test_find_project_pptx_falls_back_to_svg_snapshot(tmp_path: Path):
    exports = tmp_path / "exports"
    exports.mkdir()
    svg_snap = exports / "deck_svg.pptx"
    svg_snap.write_bytes(b"svg")
    assert find_project_pptx(tmp_path) == svg_snap


def test_find_project_pptx_missing_exports(tmp_path: Path):
    assert find_project_pptx(tmp_path) is None


@patch("plugin.ppt_master.pptx_build.subprocess.run")
@patch("plugin.ppt_master.pptx_build.resolve_venv_python")
@patch("plugin.ppt_master.pptx_build.get_config_str")
def test_build_project_pptx_success(mock_cfg, mock_resolve, mock_run, tmp_path: Path):
    mock_cfg.return_value = "/venv"
    mock_resolve.return_value = "/venv/bin/python"
    exports = tmp_path / "exports"
    exports.mkdir()
    pptx = exports / "proj_20260101.pptx"
    pptx.write_bytes(b"pptx")
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    data_root = tmp_path / "skill"
    (data_root / "scripts").mkdir(parents=True)
    (data_root / "scripts" / "svg_to_pptx.py").write_text("# stub", encoding="utf-8")

    path, err = build_project_pptx(None, tmp_path, data_root)
    assert err is None
    assert path == pptx
    mock_run.assert_called_once()
    assert mock_run.call_args.args[0][0] == "/venv/bin/python"


@patch("plugin.ppt_master.pptx_build.get_config_str")
def test_build_project_pptx_requires_venv(mock_cfg, tmp_path: Path):
    mock_cfg.return_value = ""
    path, err = build_project_pptx(None, tmp_path, tmp_path)
    assert path is None
    assert err is not None
    assert "venv" in err.lower()


@patch("plugin.ppt_master.pptx_build.build_project_pptx")
def test_ensure_project_pptx_uses_existing(mock_build, tmp_path: Path):
    exports = tmp_path / "exports"
    exports.mkdir()
    existing = exports / "deck.pptx"
    existing.write_bytes(b"x")
    path, err = ensure_project_pptx(None, tmp_path, tmp_path)
    assert path == existing
    assert err is None
    mock_build.assert_not_called()


@patch("plugin.ppt_master.pptx_build.build_project_pptx")
def test_ensure_project_pptx_builds_when_missing(mock_build, tmp_path: Path):
    built = tmp_path / "exports" / "built.pptx"
    mock_build.return_value = (built, None)
    path, err = ensure_project_pptx(None, tmp_path, tmp_path)
    assert path == built
    assert err is None
    mock_build.assert_called_once()
