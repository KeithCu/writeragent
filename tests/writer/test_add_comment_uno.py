# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# add_comment returns structured fields (matched, comment_added, anchor_text) so the agent
# can tell whether the anchor was found and the comment actually inserted, instead of having
# to parse the message string.
import uno  # noqa: F401

from plugin.testing_runner import native_test, setup, teardown
from plugin.writer.specialized.comments import AddComment
from plugin.tests.testing_utils import TestingFactory

_test_doc = None
_test_ctx = None


@setup
def my_setup(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    _test_doc = TestingFactory.create_native_doc(ctx, doc_type="writer", hidden=True)


@teardown
def my_teardown(ctx):
    global _test_doc
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None


def _set_body(text_value):
    text = _test_doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    text.insertString(cur, text_value, False)


def _ctx():
    return TestingFactory.create_context(doc=_test_doc, ctx=_test_ctx, env="native")


@native_test
def test_add_comment_reports_anchor_found_uno():
    _set_body("Anchor here please")
    res = AddComment().execute(_ctx(), content="a note", search_text="Anchor")
    assert res.get("status") == "ok", res
    assert res.get("matched") is True, res
    assert res.get("comment_added") is True, res
    assert res.get("anchor_text") == "Anchor", res


@native_test
def test_add_comment_reports_anchor_not_found_uno():
    _set_body("nothing relevant here")
    res = AddComment().execute(_ctx(), content="a note", search_text="DOES_NOT_EXIST_XYZ")
    # An anchor miss is a failure (status="error"), not a silent "not_found" the MCP host /
    # chat FSM would treat as success. anchor_text is returned on success only.
    assert res.get("status") == "error", res
    assert res.get("matched") is False, res
    assert res.get("comment_added") is False, res
