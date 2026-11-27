# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live UNO tests for inserting a plot result into a document."""

from __future__ import annotations

from typing import Any

from plugin.framework.config import get_config, save
from plugin.scripting.viz import insert_image_payload_for_doc
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import logo_32_path, with_native_doc
from tests.writer.doc_builders import list_redlines, reject_all_changes


@native_test
@with_native_doc("writer")
def test_writer_plot_insert_in_record_mode_rejects_away_uno(ctx: Any, doc: Any) -> None:
    """Record mode: a Run Python Script plot is a tagged tracked insertion, so Reject All removes the picture."""
    doc.getText().setString("Intro.")
    doc.getCurrentController().getViewCursor().gotoEnd(False)
    with open(logo_32_path(), "rb") as fh:
        payload = {"format": "png", "data": fh.read()}
    prev = get_config("doc.agent_edit_review_mode")
    save({"doc.agent_edit_review_mode": "record"})
    try:
        insert_image_payload_for_doc(ctx, doc, payload, title="Plot")
    finally:
        save({"doc.agent_edit_review_mode": prev})
    assert doc.getGraphicObjects().getCount() == 1
    redlines = list_redlines(doc)
    assert redlines and all(rl["RedlineComment"].startswith("wa-review:") for rl in redlines), redlines
    reject_all_changes(doc, ctx)
    assert doc.getGraphicObjects().getCount() == 0
