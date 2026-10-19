# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scripts/extract_strings.py."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.extract_strings import (
    _find_py_files,
    auto_translate,
    generate_pot,
    merge_translations,
)


def test_find_py_files_depth_first_unsorted_includes_stub(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    z_dir = plugin_dir / "z_writer"
    z_dir.mkdir(parents=True)
    (z_dir / "b.py").write_text("", encoding="utf-8")

    a_dir = plugin_dir / "a_calc"
    a_dir.mkdir(parents=True)
    (a_dir / "c.py").write_text("", encoding="utf-8")

    stub = plugin_dir / "xdl_strings.py"
    stub.write_text("", encoding="utf-8")

    found = _find_py_files(plugin_dir, tmp_path)
    assert "plugin/xdl_strings.py" in found
    # Depth-first scandir order should not match sorted order if entries vary
    assert len(found) == 3


@patch("scripts.extract_strings.merge_yaml_into_pot", return_value=0)
@patch("scripts.extract_strings.extract_xdl_strings.main")
@patch("shutil.which", return_value="/usr/bin/xgettext")
@patch("subprocess.run")
def test_generate_pot_default_mode_argv(
    mock_run: MagicMock,
    mock_which: MagicMock,
    mock_extract_xdl: MagicMock,
    mock_yaml: MagicMock,
    tmp_path: Path,
) -> None:
    mock_run.return_value = MagicMock(returncode=0)
    (tmp_path / "plugin").mkdir(parents=True)
    (tmp_path / "locales").mkdir(parents=True)

    ret = generate_pot(pot_only=False, root_path=tmp_path)
    assert ret == 0
    mock_extract_xdl.assert_called_once()
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "xgettext"
    assert "--keyword=N_" in cmd
    assert "--add-location=file" in cmd
    assert "-f" in cmd
    assert "-d" in cmd
    assert "writeragent" in cmd


@patch("scripts.extract_strings.merge_yaml_into_pot", return_value=0)
@patch("scripts.extract_strings.extract_xdl_strings.main")
@patch("shutil.which", return_value="/usr/bin/xgettext")
@patch("subprocess.run")
def test_generate_pot_pot_only_omits_keyword(
    mock_run: MagicMock,
    mock_which: MagicMock,
    mock_extract_xdl: MagicMock,
    mock_yaml: MagicMock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_run.return_value = MagicMock(returncode=0)
    (tmp_path / "plugin").mkdir(parents=True)
    (tmp_path / "locales").mkdir(parents=True)

    ret = generate_pot(pot_only=True, root_path=tmp_path)
    assert ret == 0
    out = capsys.readouterr().out
    assert "Regenerating translation templates (.pot) without updating .po..." in out
    cmd = mock_run.call_args[0][0]
    assert "--keyword=N_" not in cmd
    assert "--add-location=file" in cmd
    assert "-f" in cmd


@patch("shutil.which", return_value=None)
@patch("subprocess.run")
def test_missing_xgettext_skips_in_both_modes(
    mock_run: MagicMock,
    mock_which: MagicMock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ret_default = generate_pot(pot_only=False, root_path=tmp_path)
    assert ret_default == 0
    out_default = capsys.readouterr().out
    assert "Skipping string extraction (xgettext not found; install gettext: choco install gettext.install)" in out_default

    ret_pot = generate_pot(pot_only=True, root_path=tmp_path)
    assert ret_pot == 0
    out_pot = capsys.readouterr().out
    assert "Skipping .pot regeneration (xgettext not found; install gettext: choco install gettext.install)" in out_pot

    mock_run.assert_not_called()


@patch("scripts.extract_strings.merge_yaml_into_pot", return_value=0)
@patch("shutil.which", return_value="/usr/bin/xgettext")
@patch("subprocess.run")
def test_stub_unlinked_on_xgettext_failure(
    mock_run: MagicMock,
    mock_which: MagicMock,
    mock_yaml: MagicMock,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir(parents=True)
    mock_run.return_value = MagicMock(returncode=1)

    def write_stub() -> None:
        (plugin_dir / "xdl_strings.py").write_text("# stub", encoding="utf-8")

    with patch("scripts.extract_strings.extract_xdl_strings.main", side_effect=write_stub):
        ret = generate_pot(pot_only=False, root_path=tmp_path)
        assert ret == 1
        assert not (plugin_dir / "xdl_strings.py").exists()


@patch("scripts.extract_strings.merge_yaml_into_pot", return_value=1)
@patch("shutil.which", return_value="/usr/bin/xgettext")
@patch("subprocess.run")
def test_stub_unlinked_on_yaml_merge_failure(
    mock_run: MagicMock,
    mock_which: MagicMock,
    mock_yaml: MagicMock,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir(parents=True)
    mock_run.return_value = MagicMock(returncode=0)

    def write_stub() -> None:
        (plugin_dir / "xdl_strings.py").write_text("# stub", encoding="utf-8")

    with patch("scripts.extract_strings.extract_xdl_strings.main", side_effect=write_stub):
        ret = generate_pot(pot_only=False, root_path=tmp_path)
        assert ret == 1
        assert not (plugin_dir / "xdl_strings.py").exists()


def test_merge_translations_argv_and_stop_on_msgmerge_failure(
    tmp_path: Path,
) -> None:
    po_dir = tmp_path / "locales" / "de" / "LC_MESSAGES"
    po_dir.mkdir(parents=True)
    po_file = po_dir / "writeragent.po"
    po_file.write_text("dummy", encoding="utf-8")

    with patch("shutil.which", return_value="/usr/bin/tool"):
        # msgmerge fails with returncode 1
        with patch("subprocess.run", return_value=MagicMock(returncode=1)) as mock_run:
            ret = merge_translations(root_path=tmp_path)
            assert ret == 1
            # msgmerge called once; msgattrib skipped because msgmerge failed
            assert mock_run.call_count == 1
            first_cmd = mock_run.call_args_list[0][0][0]
            assert first_cmd[0] == "msgmerge"
            assert "--add-location=file" in first_cmd
            assert "--update" in first_cmd
            assert "--backup=none" in first_cmd


def test_merge_translations_replaces_with_tmp_on_success(tmp_path: Path) -> None:
    po_dir = tmp_path / "locales" / "de" / "LC_MESSAGES"
    po_dir.mkdir(parents=True)
    po_file = po_dir / "writeragent.po"
    po_file.write_text("original content", encoding="utf-8")

    def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[0] == "msgattrib":
            tmp_target = Path(cmd[3])
            tmp_target.write_text("cleaned content", encoding="utf-8")
        return MagicMock(returncode=0)

    with patch("shutil.which", return_value="/usr/bin/tool"):
        with patch("subprocess.run", side_effect=side_effect) as mock_run:
            ret = merge_translations(root_path=tmp_path)
            assert ret == 0
            assert mock_run.call_count == 2
            attrib_cmd = mock_run.call_args_list[1][0][0]
            assert attrib_cmd[0] == "msgattrib"
            assert "--no-obsolete" in attrib_cmd
            assert po_file.read_text(encoding="utf-8") == "cleaned content"


def test_missing_msgattrib_or_msgmerge_skips(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_which(tool: str) -> str | None:
        if tool == "msgmerge":
            return "/usr/bin/msgmerge"
        return None

    with patch("shutil.which", side_effect=fake_which):
        with patch("subprocess.run") as mock_run:
            ret = merge_translations(root_path=tmp_path)
            assert ret == 0
            out = capsys.readouterr().out
            assert "Skipping .po merge (msgmerge not found; install gettext: choco install gettext.install)" in out
            mock_run.assert_not_called()


@patch("scripts.extract_strings.run_default_pipeline")
@patch("subprocess.run")
def test_auto_translate_gated_by_openrouter_api_key(
    mock_run: MagicMock,
    mock_pipeline: MagicMock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_run.return_value = MagicMock(returncode=0)

    # Without API key
    with patch.dict(os.environ, {}, clear=True):
        ret = auto_translate(root_path=tmp_path)
        assert ret == 0
        out = capsys.readouterr().out
        assert "Regenerating translation templates (.pot)..." in out
        assert "Auto-translating missing strings with AI..." not in out
        assert mock_run.call_count == 1
        assert "--preview" in mock_run.call_args_list[0][0][0]

    mock_run.reset_mock()

    # With non-empty API key
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=True):
        ret = auto_translate(root_path=tmp_path)
        assert ret == 0
        out = capsys.readouterr().out
        assert "Auto-translating missing strings with AI..." in out
        assert mock_run.call_count == 2
        assert "--preview" in mock_run.call_args_list[0][0][0]
        assert "--execute" in mock_run.call_args_list[1][0][0]
        assert "--skip-initial-status" in mock_run.call_args_list[1][0][0]
