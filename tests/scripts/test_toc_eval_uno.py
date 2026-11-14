# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO test for TOC fixture export and ODT row reading."""

from __future__ import annotations

import os
import re
import tempfile
import zipfile

import uno
from com.sun.star.beans import PropertyValue

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import native_writer_tool_context, with_native_doc
from plugin.writer.specialized.indexes import IndexesCreate
from scripts.prompt_optimization.toc_eval import read_toc_rows_from_odt
from scripts.prompt_optimization.toc_fixture import FIXTURE_ROWS, build_toc_fixture


@native_test
@with_native_doc("writer")
def test_toc_fixture_export_and_read_rows_uno(ctx, doc):
    """build_toc_fixture exports to ODT with 40 outline links and 40 heading bookmarks."""
    tctx = native_writer_tool_context(doc, ctx)

    def _execute(name, args):
        if name == "indexes_create":
            return IndexesCreate().execute(tctx, **args)
        raise ValueError(name)

    build_toc_fixture(doc, _execute)

    fd, path = tempfile.mkstemp(suffix=".odt")
    os.close(fd)
    try:
        pv = PropertyValue()
        pv.Name = "FilterName"
        pv.Value = "writer8"
        doc.storeToURL(uno.systemPathToFileUrl(path), (pv,))

        with zipfile.ZipFile(path) as zf:
            xml = zf.read("content.xml").decode("utf-8")
        bookmark_count = len(re.findall(r'<text:bookmark-start\b[^>]*\btext:name="(__RefHeading__[^"]*)"', xml))
        assert bookmark_count == 40, f"Expected 40 heading bookmarks, got {bookmark_count}"

        rows = read_toc_rows_from_odt(path)
        assert len(rows) == 40, f"Expected 40 TOC rows, got {len(rows)}"
        for actual, expected in zip(rows, FIXTURE_ROWS):
            assert actual[0] == expected.title, f"Title mismatch: {actual[0]} != {expected.title}"
            assert actual[1] == expected.url, f"URL mismatch: {actual[1]} != {expected.url}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
