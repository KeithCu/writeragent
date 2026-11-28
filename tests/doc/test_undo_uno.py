# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for the document_undo tool against LibreOffice's real undo titles."""

from plugin.doc.undo import Undo
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import native_writer_tool_context, with_native_doc
from plugin.writer.edit_review import EditReviewSession
from tests.writer.doc_builders import find_first, list_redlines, reject_all_changes, reset_body


def _replace_fn(doc, old, new):
    def fn():
        found = find_first(doc, old)
        cursor = found.getText().createTextCursorByRange(found)
        cursor.setString("")
        found.getText().insertString(cursor, new, False)
    return fn


@native_test
@with_native_doc("writer")
def test_undo_refuses_the_users_reject_of_an_agent_change_uno(ctx, doc):
    reset_body(doc, ctx, "Alpha two three.")
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(_replace_fn(doc, "two", "2"))
    reject_all_changes(doc, ctx)
    assert list_redlines(doc) == []

    res = Undo().execute(native_writer_tool_context(doc, ctx))

    assert res["status"] == "error" and res["code"] == "UNDO_USER_REVIEW", res
    assert list_redlines(doc) == [], "the rejected agent change must not come back"
    assert doc.getText().getString() == "Alpha two three."
