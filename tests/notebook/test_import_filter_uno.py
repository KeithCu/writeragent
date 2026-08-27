# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live UNO tests for the Jupyter Notebook native import filter."""

from __future__ import annotations

import os

import uno

from plugin.doc.doc_type import is_writer
from plugin.framework.uno_context import get_desktop
from plugin.notebook.cell_registry import load_registry
from plugin.testing_runner import native_test


@native_test
def test_import_filter_uno_load_component(ctx):
    """Load an .ipynb via desktop.loadComponentFromURL using the native filter."""
    desktop = get_desktop(ctx)
    assert desktop is not None

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    fixture_path = os.path.join(
        repo_root, "tests", "fixtures", "introduction-to-numpy-small.ipynb"
    )
    assert os.path.exists(fixture_path), f"Fixture not found: {fixture_path}"

    file_url = uno.systemPathToFileUrl(fixture_path)

    from com.sun.star.beans import PropertyValue

    prop = PropertyValue()
    prop.Name = "FilterName"
    prop.Value = "writer_WriterAgent_Jupyter_Notebook"

    prop_hidden = PropertyValue()
    prop_hidden.Name = "Hidden"
    prop_hidden.Value = True

    doc = desktop.loadComponentFromURL(file_url, "_blank", 0, (prop, prop_hidden))
    try:
        assert doc is not None
        assert is_writer(doc)
        state = load_registry(doc)
        assert state is not None
        assert len(state.code_cells) > 0
        assert "In [" in doc.getText().getString()
    finally:
        if doc is not None:
            doc.close(True)


@native_test
def test_import_filter_uno_detect_without_filtername(ctx):
    """Load an .ipynb via desktop.loadComponentFromURL without explicit FilterName to test detect()."""
    desktop = get_desktop(ctx)
    assert desktop is not None

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    fixture_path = os.path.join(
        repo_root, "tests", "fixtures", "introduction-to-numpy-small.ipynb"
    )
    assert os.path.exists(fixture_path), f"Fixture not found: {fixture_path}"

    file_url = uno.systemPathToFileUrl(fixture_path)

    from com.sun.star.beans import PropertyValue

    prop_hidden = PropertyValue()
    prop_hidden.Name = "Hidden"
    prop_hidden.Value = True

    doc = desktop.loadComponentFromURL(file_url, "_blank", 0, (prop_hidden,))
    try:
        assert doc is not None
        assert is_writer(doc)
        state = load_registry(doc)
        assert state is not None
        assert len(state.code_cells) > 0

        doc_text = doc.getText().getString()
        assert "In [" in doc_text
        assert '"cell_type"' not in doc_text

        assert doc.ApplyFormDesignMode is False
    finally:
        if doc is not None:
            doc.close(True)
