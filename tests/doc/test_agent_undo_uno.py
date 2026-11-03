# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for agent undo snapshots (/undo)."""

from plugin.doc import agent_undo as au
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory


@native_test
def test_agent_undo_writer_round_trip(ctx):
    with TestingFactory.native_doc(ctx, "writer") as doc:
        text = doc.getText()
        cursor = text.createTextCursor()
        text.insertString(cursor, "Before agent", False)
        au.record_agent_turn_before(doc)
        cursor.gotoEnd(False)
        text.insertString(cursor, " after agent", False)
        au.record_agent_turn_after(doc)
        msg = au.undo_last_agent_edit(doc)
        assert "steps" in msg
        assert "after agent" not in doc.getText().getString()
