# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live UNO tests for the Jupyter Notebook native import filter."""

from __future__ import annotations

import os

import uno

from plugin.framework.doc_type import is_writer
from plugin.framework.uno_context import get_desktop
from plugin.notebook.cell_registry import load_registry
from plugin.testing_runner import _progress_verbose as _progress, native_test
from plugin.tests.testing_utils import TestingFactory


def _ipynb_fixture_url() -> str:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    fixture_path = os.path.join(
        repo_root, "tests", "fixtures", "introduction-to-numpy-small.ipynb"
    )
    assert os.path.exists(fixture_path), f"Fixture not found: {fixture_path}"
    return uno.systemPathToFileUrl(fixture_path)


def _has_notebook_filter(ctx) -> bool:
    try:
        filter_factory = ctx.getServiceManager().createInstanceWithContext(
            "com.sun.star.document.FilterFactory", ctx
        )
        return filter_factory is not None and filter_factory.hasByName(
            "writer_WriterAgent_Jupyter_Notebook"
        )
    except Exception:
        return False


def _load_ipynb(ctx, *, filter_name: str | None = None):
    """Hidden .ipynb load."""
    desktop = get_desktop(ctx)
    assert desktop is not None
    from com.sun.star.beans import PropertyValue

    props = []
    if filter_name:
        prop = PropertyValue()
        prop.Name = "FilterName"
        prop.Value = filter_name
        props.append(prop)
    prop_hidden = PropertyValue()
    prop_hidden.Name = "Hidden"
    prop_hidden.Value = True
    props.append(prop_hidden)
    _progress("import_filter_uno: load start filter=%s" % (filter_name or "-"))
    doc = desktop.loadComponentFromURL(
        _ipynb_fixture_url(), "_blank", 0, tuple(props)
    )
    uid = ""
    try:
        uid = str(getattr(doc, "RuntimeUID", None) or "")
    except Exception:
        uid = ""
    _progress("import_filter_uno: load done uid=%s" % (uid or "-"))
    return doc


@native_test
def test_import_filter_uno_load_component(ctx):
    """Load an .ipynb via desktop.loadComponentFromURL using the native filter."""
    if not _has_notebook_filter(ctx):
        # FIXME: testing_runner bootstraps with a throwaway profile (-env:UserInstallation in /tmp)
        # which does not inherit user-level extensions installed via `unopkg add` into ~/.config/libreoffice.
        # Once testing_runner uses a shared extension install or mounts the profile, require has_filter to be True.
        print("Note: writer_WriterAgent_Jupyter_Notebook filter not registered in throwaway profile; skipping loadComponent test")
        return

    doc = _load_ipynb(ctx, filter_name="writer_WriterAgent_Jupyter_Notebook")
    try:
        assert doc is not None
        assert is_writer(doc)
        state = load_registry(doc)
        assert state is not None
        assert len(state.code_cells) > 0
        assert "In [" in doc.getText().getString()
    finally:
        # On Windows, raw close(True) here hangs the next Hidden load (GHA 34619751330),
        # and so does closing while leftover Writers are open (GHA 34646877587);
        # close_doc skips the close when leftovers are open, and with no leftovers it
        # runs GC, settles, closes, and reactivates the keeper frame.
        TestingFactory.close_doc(doc)


@native_test
def test_import_filter_uno_detect_without_filtername(ctx):
    """Load an .ipynb via desktop.loadComponentFromURL without explicit FilterName to test detect()."""
    if not _has_notebook_filter(ctx):
        # FIXME: testing_runner bootstraps with a throwaway profile (-env:UserInstallation in /tmp)
        # which does not inherit user-level extensions installed via `unopkg add` into ~/.config/libreoffice.
        print("Note: writer_WriterAgent_Jupyter_Notebook filter not registered in throwaway profile; skipping detect test")
        return

    doc = _load_ipynb(ctx)
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
        TestingFactory.close_doc(doc)
