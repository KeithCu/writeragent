# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from typing import Any

import uno  # noqa: F401

from plugin.doc.text_helpers import TrackedDeletions
from plugin.framework.uno_context import enum_has_more
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import native_writer_tool_context, with_native_doc
from plugin.writer import html_import, review_authors
from plugin.writer.apply_document_content import FOLDED_EDIT_NOTE, ApplyDocumentContent
from plugin.writer.edit_review import EditReviewSession
from plugin.writer.inline_review import ResolveStatus, resolve_agent_change
from tests.writer.doc_builders import (
    body_paragraphs,
    body_texts,
    find_first,
    list_redlines,
    reject_all_changes,
    reset_body,
    review_mode,
)


@native_test
@with_native_doc("writer", reuse=False)
def test_restore_author_after_crash_new_redline_has_user_name_uno(ctx: Any, doc: Any) -> None:
    access = review_authors._author_access(ctx)
    saved_profile = (str(access.getPropertyValue("givenname")), str(access.getPropertyValue("sn")))
    prior: tuple[str, str] | None = None
    try:
        assert review_authors.set_office_author(ctx, "Alice") is True
        prior = review_authors.begin(ctx)
        assert prior == ("Alice", "")

        review_authors.restore_author_after_crash(ctx)

        doc.setPropertyValue("RecordChanges", True)
        text = doc.getText()
        cur = text.createTextCursor()
        text.insertString(cur, "After crash recovery", False)

        authors: list[str] = []
        enum = doc.getRedlines().createEnumeration()
        while enum_has_more(enum):
            redline = enum.nextElement()
            authors.append(str(redline.getPropertyValue("RedlineAuthor")))

        assert "Alice" in authors
        assert review_authors.office_author(ctx) == "Alice"
    finally:
        review_authors.end(ctx, prior)
        cleanup_access = review_authors._author_access(ctx)
        review_authors._commit_author(cleanup_access, saved_profile[0], saved_profile[1])


def _styles(doc: Any) -> list[tuple[str, str]]:
    return [(str(p.getPropertyValue("ParaStyleName")), p.getString()) for p in body_paragraphs(doc.getText())]


@native_test
@with_native_doc("writer", reuse=False)
def test_edit_inside_own_pending_paragraph_rejects_to_original_styles_uno(ctx: Any, doc: Any) -> None:
    """A word replaced inside the agent's pending inserted paragraph is removed from that insertion,
    so Reject All restores the heading after it instead of giving it the inserted paragraph's style."""
    reset_body(doc, ctx, "Intro.", "Heading Title", "Body text.")
    find_first(doc, "Heading Title").setPropertyValue("ParaStyleName", "Heading 1")
    original = _styles(doc)
    tctx = native_writer_tool_context(doc, ctx)
    with review_mode("record"):
        first = ApplyDocumentContent().execute(
            tctx, target="search", old_content="Heading Title", content="<p>Inserted clause text.</p>", position="before"
        )
        assert first.get("status") == "ok", first
        second = ApplyDocumentContent().execute(tctx, target="search", old_content="clause", content="section")
        assert second.get("status") == "ok", second
    assert FOLDED_EDIT_NOTE in second["message"]
    assert "Inserted section text." in body_texts(doc.getText())
    assert review_authors.DELETE_AUTHOR not in {r["RedlineAuthor"] for r in list_redlines(doc)}
    reject_all_changes(doc, ctx)
    assert _styles(doc) == original


@native_test
@with_native_doc("writer", reuse=False)
def test_clear_own_pending_list_item_rejects_into_original_list_uno(ctx: Any, doc: Any) -> None:
    """Emptying a list item the agent inserted earlier leaves the original item in its own list on Reject All."""
    reset_body(doc, ctx, "Start.")
    tctx = native_writer_tool_context(doc, ctx)
    seeded = ApplyDocumentContent().execute(
        tctx, target="full_document", content="<ol><li>Apple</li><li>Banana</li><li>Cherry</li><li>Damson</li></ol>"
    )
    assert seeded.get("status") == "ok", seeded

    def lists() -> list[tuple[str, str, str]]:
        return [
            (p.getString(), str(p.getPropertyValue("ListId")), str(p.getPropertyValue("ListLabelString")))
            for p in body_paragraphs(doc.getText())
        ]

    original = lists()
    with review_mode("record"):
        first = ApplyDocumentContent().execute(
            tctx, target="search", old_content="Banana", content=["<ol><li>B-one</li><li>B-two</li></ol>"]
        )
        assert first.get("status") == "ok", first
        second = ApplyDocumentContent().execute(tctx, target="search", old_content="B-two", content="")
        assert second.get("status") == "ok", second
    reject_all_changes(doc, ctx)
    assert lists() == original


@native_test
@with_native_doc("writer", reuse=False)
def test_replace_inside_own_pending_insertion_registers_no_change_uno(ctx: Any, doc: Any) -> None:
    """An edit that only changes the agent's pending insertion leaves no new redline, so a Wait has
    nothing to report instead of a Delete-only change whose outcome reads wrong either way."""
    reset_body(doc, ctx, "Beta two.")
    with review_mode("record"):
        first = ApplyDocumentContent().execute(
            native_writer_tool_context(doc, ctx),
            target="search", old_content="two", content=" inserted agent words", position="after",
        )
        assert first.get("status") == "ok", first
        session = EditReviewSession(doc, ctx, enabled=True)
        with session:
            session.record_mutation(
                lambda: html_import.replace_preserving_format(doc, find_first(doc, "agent"), "AGENT", in_undo_context=True)
            )
    assert session.changes == []
    assert "Beta two inserted AGENT words." in body_texts(doc.getText())
    reject_all_changes(doc, ctx)
    assert body_texts(doc.getText()) == ["Beta two."]


@native_test
@with_native_doc("writer", reuse=False)
def test_replace_last_word_of_own_pending_insertion_stays_rejectable_uno(ctx: Any, doc: Any) -> None:
    """New text after the last word of an earlier tagged insertion stays a separate Insert, so the
    old word is struck, not removed: rejecting only the new change brings that word back."""
    reset_body(doc, ctx, "Beta two.")
    tctx = native_writer_tool_context(doc, ctx)
    with review_mode("record"):
        first = ApplyDocumentContent().execute(
            tctx, target="search", old_content="two", content=" inserted agent words", position="after"
        )
        assert first.get("status") == "ok", first
        tokens = {r["RedlineComment"] for r in list_redlines(doc)}
        second = ApplyDocumentContent().execute(tctx, target="search", old_content="words", content="terms")
        assert second.get("status") == "ok", second
    assert FOLDED_EDIT_NOTE not in second["message"]
    assert review_authors.DELETE_AUTHOR in {r["RedlineAuthor"] for r in list_redlines(doc)}
    (new_token,) = {r["RedlineComment"] for r in list_redlines(doc)} - tokens
    rejected, _after = resolve_agent_change(doc, ctx, new_token, accept=False)
    assert rejected.status is ResolveStatus.RESOLVED, rejected
    assert TrackedDeletions(doc).visible_text(doc.getText()) == "Beta two inserted agent words."
    reject_all_changes(doc, ctx)
    assert body_texts(doc.getText()) == ["Beta two."]
