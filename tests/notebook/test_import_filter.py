# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Jupyter Notebook native import filter component."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from typing import Any
from unittest.mock import Mock, patch


# We mock these before importing JupyterNotebookImportFilter so it works without real UNO.
import sys
import types
if "com.sun.star.document" not in sys.modules:
    class MockUnoBase:
        pass
    class MockXFilter:
        pass
    class MockXImporter:
        pass
    class MockXServiceInfo:
        pass
    class MockXServiceDisplayName:
        pass
    class MockXServiceName:
        pass

    mock_uno = types.ModuleType("uno")
    mock_uno.fileUrlToSystemPath = lambda x: x
    class MockImplementationHelper:
        def addImplementation(self, *args, **kwargs):
            pass

    mock_unohelper = types.ModuleType("unohelper")
    mock_unohelper.Base = MockUnoBase
    mock_unohelper.ImplementationHelper = MockImplementationHelper
    
    mock_com = types.ModuleType("com")
    mock_com.sun = types.ModuleType("com.sun")
    mock_com.sun.star = types.ModuleType("com.sun.star")
    mock_com.sun.star.document = types.ModuleType("com.sun.star.document")
    mock_com.sun.star.document.XFilter = MockXFilter
    mock_com.sun.star.document.XImporter = MockXImporter
    mock_com.sun.star.lang = types.ModuleType("com.sun.star.lang")
    mock_com.sun.star.lang.XServiceInfo = MockXServiceInfo
    mock_com.sun.star.lang.XServiceDisplayName = MockXServiceDisplayName
    mock_com.sun.star.lang.XServiceName = MockXServiceName

    sys.modules["uno"] = mock_uno
    sys.modules["unohelper"] = mock_unohelper
    sys.modules["com.sun.star.document"] = mock_com.sun.star.document
    sys.modules["com.sun.star.lang"] = mock_com.sun.star.lang

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
         patch("uno.fileUrlToSystemPath", return_value="/fake/path/notebook.ipynb"):
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
         patch("uno.fileUrlToSystemPath", return_value="/fake/path/notebook.ipynb"):
        mock_import.side_effect = Exception("Test Exception")
        
        result = filter_comp.filter(media_descriptor)
        
        assert result is False
        mock_import.assert_called_once()


def _repo_root() -> str:
    _resolved = os.path.abspath(os.path.dirname(__file__))
    if "tests" in _resolved.split(os.sep):
        return os.path.abspath(os.path.join(_resolved, "..", ".."))
    return os.path.abspath(os.path.join(_resolved, "..", "..", ".."))


def test_types_xcu_structural():
    path = os.path.join(
        _repo_root(),
        "extension",
        "registry",
        "org",
        "openoffice",
        "TypeDetection",
        "Types.xcu",
    )
    root = ET.parse(path).getroot()
    assert root.tag.endswith("component-data")
    assert root.get("{http://openoffice.org/2001/registry}name") == "Types"
    
    types_node = None
    for child in root:
        if child.get("{http://openoffice.org/2001/registry}name") == "Types":
            types_node = child
            break
            
    assert types_node is not None
    
    filter_node = None
    for child in types_node:
        if child.get("{http://openoffice.org/2001/registry}name") == "writer_WriterAgent_Jupyter_Notebook":
            filter_node = child
            break
            
    assert filter_node is not None
    
    found_ext = False
    for prop in filter_node:
        if prop.get("{http://openoffice.org/2001/registry}name") == "Extensions":
            val = prop.find("value")
            if val is not None and val.text == "ipynb":
                found_ext = True
    assert found_ext


def test_filters_xcu_structural():
    path = os.path.join(
        _repo_root(),
        "extension",
        "registry",
        "org",
        "openoffice",
        "TypeDetection",
        "Filters.xcu",
    )
    root = ET.parse(path).getroot()
    assert root.tag.endswith("component-data")
    
    filters_node = None
    for child in root:
        if child.get("{http://openoffice.org/2001/registry}name") == "Filters":
            filters_node = child
            break
            
    assert filters_node is not None
    
    filter_node = None
    for child in filters_node:
        if child.get("{http://openoffice.org/2001/registry}name") == "writer_WriterAgent_Jupyter_Notebook":
            filter_node = child
            break
            
    assert filter_node is not None
    
    props = {}
    for prop in filter_node:
        name = prop.get("{http://openoffice.org/2001/registry}name")
        val = prop.find("value")
        if val is not None:
            props[name] = val.text
            
    assert props.get("FilterService") == "org.extension.writeragent.JupyterNotebookImportFilter"
    assert "IMPORT" in props.get("Flags", "")
    assert "ALIEN" in props.get("Flags", "")
    assert "3RDPARTYFILTER" in props.get("Flags", "")
    assert "EXPORT" not in props.get("Flags", "")


def test_generated_manifest_includes_import_filter():
    mf = os.path.join(_repo_root(), "extension", "META-INF", "manifest.xml")
    with open(mf, encoding="utf-8") as f:
        body = f.read()
    assert "plugin/notebook/import_filter.py" in body
    assert "registry/org/openoffice/TypeDetection/Types.xcu" in body
    assert "registry/org/openoffice/TypeDetection/Filters.xcu" in body
