# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native LibreOffice tests for text frame insert/delete tools."""

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.writer.specialized.textframes import FrameDelete, FrameInsert, FrameList


@native_test
@with_native_doc("writer")
def test_frame_insert_list_delete_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    text.insertString(cur, "Body text for frame anchor.", False)

    ins = FrameInsert().execute(
        tool_ctx,
        target="beginning",
        name="AgentFrame",
        text="Inside the frame",
    )
    assert ins.get("status") == "ok", ins
    assert ins.get("frame_name") == "AgentFrame"

    listed = FrameList().execute(tool_ctx)
    assert listed.get("status") == "ok", listed
    assert listed.get("count") == 1
    assert listed["frames"][0]["name"] == "AgentFrame"
    assert "Inside" in listed["frames"][0]["content_preview"]

    deleted = FrameDelete().execute(tool_ctx, name="AgentFrame")
    assert deleted.get("status") == "ok", deleted
    assert FrameList().execute(tool_ctx).get("count") == 0
