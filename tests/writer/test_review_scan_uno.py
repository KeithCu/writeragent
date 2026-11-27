# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO tests for review_scan same-second split-piece tagging."""

from __future__ import annotations

import datetime
import time

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


def _align_second() -> None:
    now = datetime.datetime.now()
    time.sleep(max(0.0, 1.0 - now.microsecond / 1_000_000))


def _redline_seconds(doc) -> list[int]:
    redlines = doc.getRedlines()
    held = [redlines.getByIndex(i) for i in range(redlines.getCount())]
    return [int(rl.getPropertyValue("RedlineDateTime").Seconds) for rl in held]


@native_test
@with_native_doc("writer")
def test_edit_in_same_second_as_untagged_table_insert_is_fully_tagged_uno(ctx, doc):
    from plugin.app import get_services
    from plugin.framework.config import get_config, save
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from plugin.writer.review_scan import is_agent_token
    from tests.writer.doc_builders import list_redlines

    prev = get_config("doc.agent_edit_review_mode")
    tctx = TestingFactory.create_context(
        doc=doc, ctx=ctx, env="native", doc_type="writer", services=get_services()
    )
    try:
        for _attempt in range(3):
            doc.setPropertyValue("RecordChanges", False)
            doc.getText().setString("First one two three.")
            save({"doc.agent_edit_review_mode": "record"})
            _align_second()
            r1 = ApplyDocumentContent().execute(
                tctx, target="end", content="<table><tr><td>a</td><td>b</td></tr></table>"
            )
            assert r1.get("status") == "ok", r1
            r2 = ApplyDocumentContent().execute(tctx, target="search", old_content="two", content="2")
            assert r2.get("status") == "ok", r2
            secs = _redline_seconds(doc)
            if len(set(secs)) == 1:
                break
        else:
            raise AssertionError(f"redlines did not land in the same clock second: {secs}")

        rows = list_redlines(doc)
        del_row = next(r for r in rows if r["type"] == "Delete" and r["RedlineAuthor"] == "WriterAgent (deletions)")
        ins_row = next(r for r in rows if r["type"] == "Insert" and is_agent_token(r["RedlineComment"]))
        assert is_agent_token(del_row["RedlineComment"])
        assert del_row["RedlineComment"] == ins_row["RedlineComment"]
    finally:
        save({"doc.agent_edit_review_mode": prev})


@native_test
@with_native_doc("writer")
def test_all_matches_after_stacked_delete_tags_later_matches_uno(ctx, doc):
    import uno
    from plugin.app import get_services
    from plugin.framework.config import get_config, save
    from plugin.writer import review_authors
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from plugin.writer.inline_review import _resolve_all_agent_changes
    from tests.writer.doc_builders import body_texts

    _para_break = uno.getConstantByName("com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK")
    prev = get_config("doc.agent_edit_review_mode")
    tctx = TestingFactory.create_context(
        doc=doc, ctx=ctx, env="native", doc_type="writer", services=get_services()
    )
    try:
        for _attempt in range(3):
            doc.setPropertyValue("RecordChanges", False)
            text = doc.getText()
            text.setString("")
            cur = text.createTextCursor()
            text.insertString(cur, "First apple here.", False)
            text.insertControlCharacter(cur, _para_break, False)
            text.insertString(cur, "Middle apple here.", False)
            text.insertControlCharacter(cur, _para_break, False)
            text.insertString(cur, "Last line: .", False)
            save({"doc.agent_edit_review_mode": "record"})
            _align_second()
            cur = text.createTextCursor()
            cur.gotoEnd(False)
            cur.goLeft(1, False)
            prior = review_authors.begin(ctx)
            doc.setPropertyValue("RecordChanges", True)
            try:
                assert review_authors.set_office_author(ctx, "Alice") is True
                text.insertString(cur, "apple pie", False)
            finally:
                doc.setPropertyValue("RecordChanges", False)
                review_authors.end(ctx, prior)

            res = ApplyDocumentContent().execute(
                tctx, target="search", old_content="apple", content="pear", all_matches=True
            )
            assert res.get("status") == "ok", res
            secs = _redline_seconds(doc)
            if len(set(secs)) == 1:
                break
        else:
            raise AssertionError(f"redlines did not land in the same clock second: {secs}")

        resolved = _resolve_all_agent_changes(doc, ctx, False)
        assert resolved.resolved == 2, resolved
        texts = body_texts(text)
        assert texts[:2] == ["First apple here.", "Middle apple here."], texts
    finally:
        save({"doc.agent_edit_review_mode": prev})
