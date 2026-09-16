# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO assertions for add_slide default layout + placeholder fill.

Pins the headed mercury failure: insertNewByIndex left Layout=20 with 0
shapes, so list_placeholders/set_placeholder_text saw available=[].
Default add_slide now applies layout 'text' (id 1) synchronously.

Replaces the print-only probe from master (48bf73c1) with assertions.
"""
import json

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


def _exec_tool(doc, ctx, name, args):
    res = TestingFactory.execute_tool(doc, ctx, name, args, doc_type="impress")
    return res if isinstance(res, dict) else json.loads(res)


@native_test
@with_native_doc("impress")
def test_add_slide_default_text_layout_placeholders_and_roles(ctx, doc):
    """Fresh add_slide (no blank) gets Title+Content placeholders; role set works."""
    added = _exec_tool(doc, ctx, "add_slide", {})
    assert added.get("status") == "ok", added
    assert added.get("layout") == "text", added
    page_idx = added["active_page_index"]
    assert page_idx == doc.getDrawPages().getCount() - 1, added
    page = doc.getDrawPages().getByIndex(page_idx)
    assert page.Layout == 1, "Layout=%s page_idx=%s added=%s" % (page.Layout, page_idx, added)
    assert page.getCount() >= 2

    listed = _exec_tool(doc, ctx, "list_placeholders", {"page": page_idx})
    assert listed.get("status") == "ok", listed
    assert listed.get("count") >= 2, listed

    title = _exec_tool(
        doc, ctx, "set_placeholder_text", {"page": page_idx, "role": "title", "text": "Probe Title"}
    )
    assert title.get("status") == "ok", title
    body = _exec_tool(
        doc, ctx, "set_placeholder_text", {"page": page_idx, "role": "body", "text": "Probe Body"}
    )
    assert body.get("status") == "ok", body
    assert title.get("index") != body.get("index")

    read_title = _exec_tool(doc, ctx, "get_placeholder_text", {"page": page_idx, "role": "title"})
    read_body = _exec_tool(doc, ctx, "get_placeholder_text", {"page": page_idx, "role": "body"})
    assert read_title.get("text") == "Probe Title", read_title
    assert read_body.get("text") == "Probe Body", read_body


@native_test
@with_native_doc("impress")
def test_add_slide_blank_and_none_escape_empty(ctx, doc):
    """layout=blank / none keep the empty-page contract."""
    for layout_name in ("blank", "none"):
        added = _exec_tool(doc, ctx, "add_slide", {"layout": layout_name})
        assert added.get("status") == "ok", added
        assert added.get("layout") == "blank", added
        page_idx = added["active_page_index"]
        assert page_idx == doc.getDrawPages().getCount() - 1, added
        page = doc.getDrawPages().getByIndex(page_idx)
        # Escape hatch leaves insertNewByIndex (empty), not _LAYOUTS["blank"]=11.
        assert page.getCount() == 0, "shapes=%s Layout=%s added=%s" % (page.getCount(), page.Layout, added)
        listed = _exec_tool(doc, ctx, "list_placeholders", {"page": page_idx})
        assert listed.get("status") == "ok", listed
        assert listed.get("count") == 0, listed
        miss = _exec_tool(
            doc, ctx, "set_placeholder_text", {"page": page_idx, "role": "title", "text": "nope"}
        )
        assert miss.get("status") == "error", miss
        details = miss.get("details") or {}
        assert details.get("available") == [], miss
        assert details.get("suggest_layout") == "text", miss
        assert details.get("shape_text_count") == 0, miss
        assert "set_slide_layout" in (details.get("hint") or ""), miss


@native_test
@with_native_doc("impress")
def test_set_slide_layout_text_creates_placeholders_synchronously(ctx, doc):
    """Root-cause path: Layout=1 instantiates placeholders with no event loop."""
    added = _exec_tool(doc, ctx, "add_slide", {"layout": "blank"})
    page_idx = added["active_page_index"]
    listed = _exec_tool(doc, ctx, "list_placeholders", {"page": page_idx})
    assert listed.get("count") == 0, listed

    applied = _exec_tool(doc, ctx, "set_slide_layout", {"page": page_idx, "layout": "text"})
    assert applied.get("status") == "ok", applied
    assert applied.get("layout") == "text"
    page = doc.getDrawPages().getByIndex(page_idx)
    assert page.Layout == 1
    assert page.getCount() >= 2
    listed = _exec_tool(doc, ctx, "list_placeholders", {"page": page_idx})
    assert listed.get("count") >= 2, listed
