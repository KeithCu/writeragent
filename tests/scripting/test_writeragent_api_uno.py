# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import plugin.scripting.writeragent_api as api
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


def _assert_raises(exc_type, fn):
    try:
        fn()
    except exc_type as exc:
        return exc
    raise AssertionError(f"Expected {exc_type.__name__} to be raised")


@native_test
@with_native_doc("writer")
def test_writer_collections_uno(ctx, doc):
    orig = api.call_host_tool
    api.call_host_tool = lambda tool, **kw: TestingFactory.execute_tool(doc, ctx, tool, kw, doc_type="writer")
    try:
        text = doc.getText()
        cursor = text.createTextCursor()
        text.insertString(cursor, "Body", False)
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        bm = doc.createInstance("com.sun.star.text.Bookmark")
        bm.setName("Mark")
        text.insertTextContent(cursor, bm, True)

        assert [b.name for b in api.bookmark] == ["Mark"]
        assert api.bookmark["Mark"].text == "Body"
        del api.bookmark["Mark"]
        assert "Mark" not in api.bookmark
        _assert_raises(KeyError, lambda: api.bookmark["Mark"])

        api.frame.insert(target="beginning", name="F1", text="Inside")
        assert api.frame["F1"].content == "Inside"
        assert api.frame["F1"].name == "F1"
        del api.frame["F1"]
        assert len(api.frame) == 0

        api.section.create("S1", "paragraph:0")
        assert api.section["S1"].column_count == 1
        del api.section["S1"]
        _assert_raises(KeyError, lambda: api.section.__delitem__("S1"))
    finally:
        api.call_host_tool = orig


@native_test
@with_native_doc("calc")
def test_calc_collections_uno(ctx, doc):
    orig = api.call_host_tool
    api.call_host_tool = lambda tool, **kw: TestingFactory.execute_tool(doc, ctx, tool, kw, doc_type="calc")
    try:
        sn = doc.getSheets().getByIndex(0).getName()

        api.sheet.create("Extra")
        assert api.sheet["Extra"].visible is True
        del api.sheet["Extra"]
        _assert_raises(KeyError, lambda: api.sheet["Extra"])

        exc = _assert_raises(RuntimeError, lambda: api.sheet.__delitem__(sn))
        assert getattr(exc, "code", None) == "INVALID_PARAM"

        api.calc.write_range(
            ["A1:B6"],
            values=[
                ["Month", "Sales"],
                ["Jan", "100"],
                ["Feb", "150"],
                ["Mar", "200"],
                ["Apr", "250"],
                ["May", "300"],
            ],
        )
        api.pivot_table.create(
            name="WA_P",
            source_range="A1:B6",
            source_sheet=sn,
            destination_sheet=sn,
            destination_cell="D1",
            row_fields=["Month"],
            column_fields=[],
            data_fields=["Sales"],
            page_fields=[],
        )
        assert api.pivot_table["WA_P"].sheet == sn
        del api.pivot_table["WA_P"]
        assert "WA_P" not in api.pivot_table
    finally:
        api.call_host_tool = orig


@native_test
@with_native_doc("writer")
def test_writer_comments_pythonic_uno(ctx, doc):
    orig = api.call_host_tool
    api.call_host_tool = lambda tool, **kw: TestingFactory.execute_tool(doc, ctx, tool, kw, doc_type="writer")
    try:
        text = doc.getText()
        cursor = text.createTextCursor()
        text.insertString(cursor, "Alpha Beta", False)

        r = api.comment.add("first", search="Alpha")
        assert r.name
        assert "status" not in r

        api.comment.add("second", search="Beta")
        assert sorted(c.content for c in api.comment) == ["first", "second"]

        exc = _assert_raises(RuntimeError, lambda: api.comment.add("x", search="Nowhere"))
        assert getattr(exc, "code", None) == "OLD_CONTENT_NOT_FOUND"
    finally:
        api.call_host_tool = orig
