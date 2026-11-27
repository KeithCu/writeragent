# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# comment_add returns structured fields (matched, comment_added, anchor_text) so the agent
# can tell whether the anchor was found and the comment actually inserted, instead of having
# to parse the message string. Native tests also enumerate TextFields to assert the
# Annotation registered (and that the spanning insert left the matched passage in the body).
import uno  # noqa: F401

from plugin.framework.uno_context import enum_has_more
from plugin.testing_runner import native_test
from plugin.writer.specialized.comments import AddComment, CommentList
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from tests.writer.doc_builders import set_body_text


def _annotation_contents(doc):
    out = []
    enum = doc.getTextFields().createEnumeration()
    while enum_has_more(enum):
        field = enum.nextElement()
        if field.supportsService("com.sun.star.text.textfield.Annotation"):
            out.append(field.getPropertyValue("Content"))
    return out


def _annotations(doc):
    out = []
    enum = doc.getTextFields().createEnumeration()
    while enum_has_more(enum):
        field = enum.nextElement()
        if not field.supportsService("com.sun.star.text.textfield.Annotation"):
            continue
        out.append({
            "content": field.getPropertyValue("Content"),
            "name": field.getPropertyValue("Name"),
            "parent_name": field.getPropertyValue("ParentName") or "",
            "resolved": bool(field.getPropertyValue("Resolved")),
        })
    return out


def _by_name(comments, name):
    for item in comments:
        if item.get("name") == name:
            return item
    return None


@native_test
@with_native_doc("writer")
def test_add_comment_reports_anchor_found_uno(ctx, doc):
    set_body_text(doc, "Anchor here please")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = AddComment().execute(tool_ctx, content="a note", search="Anchor", author="WriterAgent", occurrence=0)
    assert res.get("status") == "ok", res
    assert res.get("matched") is True, res
    assert res.get("comment_added") is True, res
    assert res.get("anchor_text") == "Anchor", res
    assert "a note" in _annotation_contents(doc), _annotation_contents(doc)
    # Spanning insert: Annotation registered as a TextField; matched passage stays in the body.
    assert "Anchor here please" in doc.getText().getString()
    assert res.get("name"), res
    listed = CommentList().execute(tool_ctx, start_index=0, max_comments=100)
    assert listed.get("status") == "ok", listed
    listed_names = [c.get("name") for c in listed.get("comments") or []]
    assert res["name"] in listed_names, listed


@native_test
@with_native_doc("writer")
def test_add_comment_reports_anchor_not_found_uno(ctx, doc):
    set_body_text(doc, "nothing relevant here")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = AddComment().execute(tool_ctx, content="a note", search="DOES_NOT_EXIST_XYZ", author="WriterAgent", occurrence=0)
    # An anchor miss is a failure (status="error"), not a silent "not_found" the MCP host /
    # chat FSM would treat as success. anchor_text is returned on success only.
    assert res.get("status") == "error", res
    assert res.get("matched") is False, res
    assert res.get("comment_added") is False, res


@native_test
@with_native_doc("writer")
def test_add_comment_reply_via_parent_name_uno(ctx, doc):
    """Reply with parent_name only (no search). Parent stays unresolved."""
    set_body_text(doc, "Passage under review")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    root = AddComment().execute(tool_ctx, content="please revise", search="Passage", author="WriterAgent", occurrence=0)
    assert root.get("status") == "ok" and root.get("name"), root

    reply = AddComment().execute(tool_ctx, content="Approved, thank you", parent_name=root["name"], author="WriterAgent")
    assert reply.get("status") == "ok", reply
    assert reply.get("comment_added") is True, reply
    assert reply.get("name"), reply
    assert reply.get("parent_name") == root["name"], reply
    assert reply["name"] != root["name"], reply

    listed = CommentList().execute(tool_ctx, start_index=0, max_comments=100)
    assert listed.get("status") == "ok", listed
    parent = _by_name(listed["comments"], root["name"])
    child = _by_name(listed["comments"], reply["name"])
    assert parent is not None and child is not None, listed
    assert child["parent_name"] == root["name"], child
    assert child["content"] == "Approved, thank you", child
    assert parent["resolved"] is False, parent
    fields = _annotations(doc)
    assert _by_name(fields, root["name"])["resolved"] is False, fields


@native_test
@with_native_doc("writer")
def test_add_comment_reply_to_reply_nests_uno(ctx, doc):
    """C parented to B, B parented to A — do not flatten to the thread root."""
    set_body_text(doc, "Title sentence")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    a = AddComment().execute(tool_ctx, content="root A", search="Title", author="WriterAgent", occurrence=0)
    assert a.get("status") == "ok" and a.get("name"), a
    b = AddComment().execute(tool_ctx, content="reply B", parent_name=a["name"], author="WriterAgent")
    assert b.get("status") == "ok" and b.get("name"), b
    assert b.get("parent_name") == a["name"], b
    c = AddComment().execute(tool_ctx, content="reply C", parent_name=b["name"], author="WriterAgent")
    assert c.get("status") == "ok" and c.get("name"), c
    assert c.get("parent_name") == b["name"], c
    assert c["parent_name"] != a["name"], c

    listed = CommentList().execute(tool_ctx, start_index=0, max_comments=100)
    listed_b = _by_name(listed["comments"], b["name"])
    listed_c = _by_name(listed["comments"], c["name"])
    assert listed_b["parent_name"] == a["name"], listed
    assert listed_c["parent_name"] == b["name"], listed


@native_test
@with_native_doc("writer")
def test_add_comment_reply_unknown_parent_uno(ctx, doc):
    set_body_text(doc, "Some body text")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = AddComment().execute(tool_ctx, content="orphan reply", parent_name="no-such-annotation", author="WriterAgent")
    assert res.get("status") == "error", res
    assert res.get("comment_added") is False, res
    assert "no-such-annotation" in (res.get("message") or ""), res
    assert _annotation_contents(doc) == []
