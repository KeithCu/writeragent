# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.embeddings_soffice_convert."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from plugin.embeddings import embeddings_soffice_convert as convert


def test_legacy_odf_filter_mapping():
    assert convert.legacy_odf_filter(".doc") == "odt"
    assert convert.legacy_odf_filter(".xls") == "ods"
    assert convert.legacy_odf_filter(".ppt") == "odp"
    assert convert.legacy_odf_filter(".pdf") is None


def test_convert_legacy_to_odf_no_soffice(tmp_path: Path):
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"binary")
    with patch.object(convert, "resolve_soffice_executable", return_value=None):
        assert convert.convert_legacy_to_odf(str(source)) is None


def test_convert_legacy_to_odf_success(tmp_path: Path):
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"binary")
    produced = tmp_path / "legacy.odt"
    produced.write_text("converted", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        out_dir = Path(cmd[cmd.index("--outdir") + 1])
        (out_dir / "legacy.odt").write_text("converted", encoding="utf-8")
        return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(convert, "resolve_soffice_executable", return_value="/usr/bin/soffice"):
        with patch("plugin.embeddings.embeddings_soffice_convert.subprocess.run", side_effect=fake_run):
            result = convert.convert_legacy_to_odf(str(source))
    assert result is not None
    assert result.is_file()
    assert result.read_text(encoding="utf-8") == "converted"
    result.unlink(missing_ok=True)
