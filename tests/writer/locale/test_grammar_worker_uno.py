# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live UNO tests for the grammar worker's document writes."""

from __future__ import annotations

from typing import Any

from plugin.testing_runner import native_test
from plugin.writer import review_authors
from plugin.writer.locale import grammar_persistence as gp
from plugin.writer.locale.grammar_worker import apply_language_change
from tests.testing_utils import with_native_doc
from tests.writer.doc_builders import list_redlines, reject_all_changes


@native_test
@with_native_doc("writer")
def test_language_retag_while_recording_leaves_redlines_alone_uno(ctx: Any, doc: Any) -> None:
    """The detected-language write adds no Format redline, and Alice's pending insertion still rejects away."""
    text = doc.getText()
    text.setString("Intro. ")
    doc.setPropertyValue("RecordChanges", True)
    prior = review_authors.begin(ctx)
    try:
        assert review_authors.set_office_author(ctx, "Alice") is True
        text.insertString(text.getEnd(), "Bonjour le monde.", False)
    finally:
        review_authors.end(ctx, prior)
    doc_id = "test-retag-recording"
    try:
        gp.grammar_registry.clear_all()
        assert gp.get_persistence(doc_id, model=doc) is not None
        doc.getCurrentController().getViewCursor().gotoStart(False)
        assert apply_language_change(
            doc_id, "Bonjour le monde.", "fr-FR", start_pos=7, paragraph_text="Intro. Bonjour le monde."
        ) is True
        assert [rl["type"] for rl in list_redlines(doc)] == ["Insert"]
        assert doc.getPropertyValue("RecordChanges") is True
    finally:
        doc.setPropertyValue("RecordChanges", False)
        gp.grammar_registry.clear_all()
    reject_all_changes(doc, ctx)
    assert text.getString() == "Intro. "
