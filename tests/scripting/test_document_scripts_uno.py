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
from plugin.tests.testing_utils import TestingFactory, with_native_doc

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
@with_native_doc("calc")
def test_research_read_does_not_change_workbook_pick_uno(ctx, doc):
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

    def _uid(model):
        return get_runtime_uid(model) if model is not None else ""

    with tempfile.TemporaryDirectory(prefix="wa_research_calc_") as temp_dir:
        copy_path = os.path.join(temp_dir, "research_copy.ods")
        doc.storeToURL(uno.systemPathToFileUrl(copy_path), ())
        user_uid = get_runtime_uid(doc)
        before_doc_scripts_uid = _uid(get_calc_document_from_ctx(ctx))
        before_focused_uid = _uid(focused_calc_document(ctx))
        before_uids = {get_runtime_uid(m) for m in _enumerated_matching_models(ctx, is_calc)}
        assert user_uid in before_uids

        model, _doc_type, err, opened = open_document_for_read(ctx, copy_path)
        try:
            assert err is None and opened, (err, opened)
            assert model is not None
            research_uid = get_runtime_uid(model)
            assert research_uid != user_uid
            assert _uid(get_calc_document_from_ctx(ctx)) == before_doc_scripts_uid
            assert _uid(focused_calc_document(ctx)) == before_focused_uid
            after_uids = {get_runtime_uid(m) for m in _enumerated_matching_models(ctx, is_calc)}
            assert after_uids == before_uids
            assert research_uid not in after_uids
        finally:
            if model is not None:
                close_document_research_document(model, opened_for_document_research=opened)
