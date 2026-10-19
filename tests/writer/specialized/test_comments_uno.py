# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native LibreOffice tests for specialized comment tools."""

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.writer.specialized.comments import AddComment, CommentList, CommentResolve


def _set_body(doc, text_value: str) -> None:
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    text.insertString(cur, text_value, False)


@native_test
@with_native_doc("writer")
def test_comment_location_and_paragraph_index_uno(ctx, doc):
    """Footer comment has footer location and no paragraph_index; body comment has paragraph_index."""
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    text.insertString(cur, "First paragraph in body text.", False)
    text.insertControlCharacter(cur, 0, False)  # PARAGRAPH_BREAK
    text.insertString(cur, "Second paragraph in body.", False)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")

    # Add a body comment via AddComment
    body_res = AddComment().execute(tool_ctx, content="Body comment", search="Second paragraph")
    assert body_res.get("status") == "ok", body_res

    # Enable footer and insert an annotation into the footer
    styles = doc.getStyleFamilies().getByName("PageStyles")
    style_name = "Standard" if styles.hasByName("Standard") else styles.getElementNames()[0]
    style = styles.getByName(style_name)
    style.setPropertyValue("FooterIsOn", True)
    footer_text = style.getPropertyValue("FooterText")
    footer_cur = footer_text.createTextCursor()
    footer_cur.setString("Footer text content")

    footer_annotation = doc.createInstance("com.sun.star.text.textfield.Annotation")
    footer_annotation.setPropertyValue("Author", "Tester")
    footer_annotation.setPropertyValue("Content", "Footer comment note")
    footer_text.insertTextContent(footer_cur, footer_annotation, False)

    listed = CommentList().execute(tool_ctx)
    assert listed.get("status") == "ok", listed
    comments = listed.get("comments", [])

    body_entry = next((c for c in comments if c.get("content") == "Body comment"), None)
    assert body_entry is not None, "Body comment not found in comment_list"
    assert body_entry.get("location") == "body"
    assert "paragraph_index" in body_entry
    assert body_entry["paragraph_index"] == 1

    footer_entry = next((c for c in comments if c.get("content") == "Footer comment note"), None)
    assert footer_entry is not None, "Footer comment not found in comment_list"
    assert "footer" in footer_entry.get("location", "").lower()
    assert "paragraph_index" not in footer_entry


@native_test
@with_native_doc("writer")
def test_comment_resolve_reply_appears_with_name_uno(ctx, doc):
    """CommentResolve reply appears in comment_list with a non-empty name."""
    _set_body(doc, "Some reviewable sentence here.")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")

    add_res = AddComment().execute(tool_ctx, content="Needs fix", search="reviewable")
    assert add_res.get("status") == "ok", add_res
    parent_name = add_res["name"]
    assert parent_name, "Parent comment should have a name"

    resolve_res = CommentResolve().execute(
        tool_ctx, name=parent_name, resolution="Issue resolved cleanly", author="Resolver"
    )
    assert resolve_res.get("status") == "ok", resolve_res

    listed = CommentList().execute(tool_ctx)
    assert listed.get("status") == "ok", listed
    comments = listed.get("comments", [])

    reply_entry = next((c for c in comments if c.get("content") == "Issue resolved cleanly"), None)
    assert reply_entry is not None, "Resolution reply not found in comment_list"
    assert reply_entry.get("parent_name") == parent_name
    assert reply_entry.get("name"), "Resolution reply must have a non-empty name"


@native_test
@with_native_doc("writer")
def test_add_comment_search_hits_nbsp_uno(ctx, doc):
    """add_comment search with normal space matches document text with NBSP."""
    _set_body(doc, "Prefix text\u00a0with\u00a0nbsp and suffix.")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")

    res = AddComment().execute(tool_ctx, content="Note on nbsp phrase", search="text with nbsp")
    assert res.get("status") == "ok", res
    assert res.get("matched") is True
    assert res.get("comment_added") is True
    assert "text\u00a0with\u00a0nbsp" in res.get("anchor_text", "")
