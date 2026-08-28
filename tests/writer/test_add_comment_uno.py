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

from plugin.testing_runner import native_test
from plugin.writer.specialized.comments import AddComment
from plugin.tests.testing_utils import TestingFactory, with_native_doc


def _set_body(doc, text_value):
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    text.insertString(cur, text_value, False)


def _annotation_contents(doc):
    out = []
    enum = doc.getTextFields().createEnumeration()
    while enum.hasMoreElements():
        field = enum.nextElement()
        if field.supportsService("com.sun.star.text.textfield.Annotation"):
            out.append(field.getPropertyValue("Content"))
    return out


@native_test
@with_native_doc("writer")
def test_add_comment_reports_anchor_found_uno(ctx, doc):
    _set_body(doc, "Anchor here please")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = AddComment().execute(tool_ctx, content="a note", search="Anchor")
    assert res.get("status") == "ok", res
    assert res.get("matched") is True, res
    assert res.get("comment_added") is True, res
    assert res.get("anchor_text") == "Anchor", res
    assert "a note" in _annotation_contents(doc), _annotation_contents(doc)


@native_test
@with_native_doc("writer")
def test_add_comment_reports_anchor_not_found_uno(ctx, doc):
    _set_body(doc, "nothing relevant here")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = AddComment().execute(tool_ctx, content="a note", search="DOES_NOT_EXIST_XYZ")
    # An anchor miss is a failure (status="error"), not a silent "not_found" the MCP host /
    # chat FSM would treat as success. anchor_text is returned on success only.
    assert res.get("status") == "error", res
    assert res.get("matched") is False, res
    assert res.get("comment_added") is False, res
