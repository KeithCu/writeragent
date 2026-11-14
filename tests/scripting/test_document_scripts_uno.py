# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for document-attached Run Python Script persistence."""

from __future__ import annotations

import os
import tempfile

import uno

from plugin.scripting.document_scripts import (
    DOCUMENT_SCRIPTS_UDPROP,
    attach_document_script,
    get_document_scripts,
)
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory

_test_ctx = None
_temp_dir = None
_saved_path = None


def _hidden_prop():
    return uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True)


@native_test
def test_document_scripts_survive_save_reopen(ctx):
    from plugin.framework.uno_context import get_desktop

    desktop = get_desktop(ctx)
    with tempfile.TemporaryDirectory(prefix="wa_doc_scripts_") as temp_dir:
        doc = TestingFactory.create_native_doc(ctx, "writer", hidden=True)
        try:
            assert attach_document_script(doc, "RoundTrip", "result = 42") is None
            assert get_document_scripts(doc)["RoundTrip"] == "result = 42"

            saved_path = os.path.join(temp_dir, "doc_scripts_test.odt")
            file_url = uno.systemPathToFileUrl(saved_path)
            doc.storeAsURL(file_url, ())
        finally:
            doc.close(True)

        reopened = desktop.loadComponentFromURL(file_url, "_blank", 0, (_hidden_prop(),))
        try:
            scripts = get_document_scripts(reopened)
            assert scripts.get("RoundTrip") == "result = 42"
            raw = reopened.getDocumentProperties().UserDefinedProperties.getPropertyValue(DOCUMENT_SCRIPTS_UDPROP)
            assert "RoundTrip" in str(raw)
        finally:
            reopened.close(True)
