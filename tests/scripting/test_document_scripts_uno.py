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


@native_test
def test_research_read_does_not_change_workbook_pick_uno(ctx):
    import sys
    import unittest

    if sys.platform == "win32":
        # Leftover hidden Calc loads hang on Windows.
        raise unittest.SkipTest("skipped on win32")

    from plugin.doc.document_research import close_document_research_document, open_document_for_read
    from plugin.framework.doc_type import is_calc
    from plugin.framework.uno_context import get_runtime_uid
    from plugin.scripting.document_scripts import get_calc_document_from_ctx
    from plugin.scripting.session_manager import _enumerated_matching_models, focused_calc_document
    from plugin.tests.testing_utils import with_native_doc

    @with_native_doc("calc")
    def _run(uno_ctx, doc):
        with tempfile.TemporaryDirectory(prefix="wa_research_calc_") as temp_dir:
            copy_path = os.path.join(temp_dir, "research_copy.ods")
            doc.storeToURL(uno.systemPathToFileUrl(copy_path), ())
            user_uid = get_runtime_uid(doc)
            assert get_runtime_uid(get_calc_document_from_ctx(uno_ctx)) == user_uid
            assert get_runtime_uid(focused_calc_document(uno_ctx)) == user_uid

            opened = open_document_for_read(uno_ctx, copy_path)
            research_doc = opened["doc"]
            try:
                research_uid = get_runtime_uid(research_doc)
                assert research_uid != user_uid
                assert get_runtime_uid(get_calc_document_from_ctx(uno_ctx)) == user_uid
                assert get_runtime_uid(focused_calc_document(uno_ctx)) == user_uid
                enumerated_uids = [get_runtime_uid(m) for m in _enumerated_matching_models(uno_ctx, is_calc)]
                assert research_uid not in enumerated_uids
            finally:
                close_document_research_document(research_doc, opened["opened_hidden"])

    _run(ctx)
