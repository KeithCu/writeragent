# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Jupyter Notebook native import filter component."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from plugin.notebook.import_filter import JupyterNotebookImportFilter


class MockPropertyValue:
    def __init__(self, name: str, value: Any):
        self.Name = name
        self.Value = value


def test_import_filter_success():
    ctx = Mock()
    target_doc = Mock()
    filter_comp = JupyterNotebookImportFilter(ctx)
    filter_comp.setTargetDocument(target_doc)

    media_descriptor = (MockPropertyValue("URL", "file:///fake/path/notebook.ipynb"),)

    with patch("plugin.notebook.import_filter.import_ipynb_to_writer") as mock_import, \
         patch("plugin.notebook.import_filter.uno.fileUrlToSystemPath", return_value="/fake/path/notebook.ipynb"):
        mock_import.return_value = {"cells": 1}
        
        result = filter_comp.filter(media_descriptor)
        
        assert result is True
        mock_import.assert_called_once_with(target_doc, "/fake/path/notebook.ipynb", ctx=ctx)


def test_import_filter_missing_url():
    ctx = Mock()
    target_doc = Mock()
    filter_comp = JupyterNotebookImportFilter(ctx)
    filter_comp.setTargetDocument(target_doc)

    media_descriptor = (MockPropertyValue("ReadOnly", True),)

    result = filter_comp.filter(media_descriptor)
    assert result is False


def test_import_filter_missing_target_doc():
    ctx = Mock()
    filter_comp = JupyterNotebookImportFilter(ctx)

    media_descriptor = (MockPropertyValue("URL", "file:///fake/path/notebook.ipynb"),)

    result = filter_comp.filter(media_descriptor)
    assert result is False


def test_import_filter_exception():
    ctx = Mock()
    target_doc = Mock()
    filter_comp = JupyterNotebookImportFilter(ctx)
    filter_comp.setTargetDocument(target_doc)

    media_descriptor = (MockPropertyValue("URL", "file:///fake/path/notebook.ipynb"),)

    with patch("plugin.notebook.import_filter.import_ipynb_to_writer") as mock_import, \
         patch("plugin.notebook.import_filter.uno.fileUrlToSystemPath", return_value="/fake/path/notebook.ipynb"):
        mock_import.side_effect = Exception("Test Exception")
        
        result = filter_comp.filter(media_descriptor)
        
        assert result is False
        mock_import.assert_called_once()


def _repo_root() -> str:
    _resolved = os.path.abspath(os.path.dirname(__file__))
    if "tests" in _resolved.split(os.sep):
        return os.path.abspath(os.path.join(_resolved, "..", ".."))
    return os.path.abspath(os.path.join(_resolved, "..", "..", ".."))


def _assert_manifest_lists_import_filter(body: str) -> None:
    assert "plugin/notebook/import_filter.py" in body
    assert "registry/org/openoffice/TypeDetection/Types.xcu" in body
    assert "registry/org/openoffice/TypeDetection/Filters.xcu" in body
    assert "registry/org/openoffice/TypeDetection/Misc.xcu" in body


def test_generated_manifest_includes_import_filter():
    # Checkout: regenerate via scripts/. Release tree has no scripts/, only the
    # assembled META-INF/manifest.xml (extension/ prefix already stripped).
    scripts_dir = os.path.join(_repo_root(), "scripts")
    if os.path.isdir(scripts_dir):
        sys.path.insert(0, _repo_root())
        sys.path.insert(0, scripts_dir)
        try:
            from scripts.manifest_registry import generate_manifest_xml

            with patch("scripts.manifest_registry._write_if_changed") as mock_write:
                generate_manifest_xml([], "dummy")
                assert mock_write.called
                body = mock_write.call_args[0][1]
        finally:
            sys.path.pop(0)
            sys.path.pop(0)
        _assert_manifest_lists_import_filter(body)
        return

    for rel in ("META-INF/manifest.xml", "extension/META-INF/manifest.xml"):
        path = Path(_repo_root()) / rel
        if path.is_file():
            _assert_manifest_lists_import_filter(path.read_text(encoding="utf-8"))
            return
    pytest.fail("neither scripts/ nor META-INF/manifest.xml present")


def test_librepy_core_manifest_includes_import_filter():
    path = Path(_repo_root()) / "extension-core" / "META-INF" / "manifest.xml"
    if not path.is_file():
        pytest.skip("extension-core manifest not in this tree")
    _assert_manifest_lists_import_filter(path.read_text(encoding="utf-8"))


def test_detect_method():
    ctx = Mock()
    filter_comp = JupyterNotebookImportFilter(ctx)

    media_descriptor = (MockPropertyValue("URL", "file:///fake/path/notebook.ipynb"),)
    type_name, out_desc = filter_comp.detect(media_descriptor)
    assert type_name == "writer_WriterAgent_Jupyter_Notebook"
    assert isinstance(out_desc, tuple)
    assert len(out_desc) == 1

    media_descriptor = (MockPropertyValue("URL", "file:///fake/path/notebook.IPYNB"),)
    type_name, out_desc = filter_comp.detect(media_descriptor)
    assert type_name == "writer_WriterAgent_Jupyter_Notebook"
    assert isinstance(out_desc, tuple)
    assert len(out_desc) == 1

    media_descriptor = (MockPropertyValue("URL", "file:///fake/path/document.txt"),)
    type_name, out_desc = filter_comp.detect(media_descriptor)
    assert type_name == ""
    assert isinstance(out_desc, tuple)
    assert len(out_desc) == 1

    media_descriptor = (MockPropertyValue("ReadOnly", True),)
    type_name, out_desc = filter_comp.detect(media_descriptor)
    assert type_name == ""
    assert isinstance(out_desc, tuple)
    assert len(out_desc) == 1
