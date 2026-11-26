# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
import json

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from tests.draw.draw_tool_helpers import exec_tool_json


@native_test
@with_native_doc("impress")
def test_page_comment_roundtrip(ctx, doc):
    add_args = {"page": 0, "text": "roundtrip probe"}
    add_res = json.loads(exec_tool_json(doc, ctx, "page_comment_add", add_args))
    assert add_res.get("status") == "ok", add_res
    index = add_res["index"]
    assert isinstance(index, int)

    list_res = json.loads(exec_tool_json(doc, ctx, "page_comment_list", {"page": 0}))
    assert list_res.get("status") == "ok", list_res
    texts = [row["text"] for row in list_res["comments"]]
    assert "roundtrip probe" in texts
    row = next(r for r in list_res["comments"] if r["text"] == "roundtrip probe")
    assert "x" in row and "y" in row
    del_index = row["index"]

    del_res = json.loads(exec_tool_json(doc, ctx, "page_comment_delete", {"page": 0, "index": del_index}))
    assert del_res.get("status") == "ok", del_res

    after = json.loads(exec_tool_json(doc, ctx, "page_comment_list", {"page": 0}))
    assert "roundtrip probe" not in [r["text"] for r in after["comments"]]

    author_res = json.loads(exec_tool_json(doc, ctx, "page_comment_add", {"page": 0, "text": "named", "author": "WA-Test"}))
    assert author_res.get("status") == "ok", author_res
    listed = json.loads(exec_tool_json(doc, ctx, "page_comment_list", {"page": 0}))
    authors = [r["author"] for r in listed["comments"] if r["text"] == "named"]
    assert "WA-Test" in authors
