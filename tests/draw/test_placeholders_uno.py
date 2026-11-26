# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO assertions for slide_add default layout + placeholder fill.

``insertNewByIndex`` alone leaves Layout=20 with no shapes, so
placeholder_list/placeholder_set_text would see available=[]. A default
slide_add applies layout 'text' (id 1) synchronously, and these tests pin
the Title and Content placeholders that gives.
"""

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from tests.draw.draw_tool_helpers import exec_tool


@native_test
@with_native_doc("impress")
def test_add_slide_default_text_layout_placeholders_and_roles(ctx, doc):
    """Fresh slide_add (no blank) gets Title+Content placeholders; role set works."""
    added = exec_tool(doc, ctx, "slide_add", {})
    assert added.get("status") == "ok", added
    assert added.get("layout") == "text", added
    page_idx = added["active_page_index"]
    assert page_idx == doc.getDrawPages().getCount() - 1, added
    page = doc.getDrawPages().getByIndex(page_idx)
    assert page.Layout == 1, "Layout=%s page_idx=%s added=%s" % (page.Layout, page_idx, added)
    assert page.getCount() >= 2

    listed = exec_tool(doc, ctx, "placeholder_list", {"page": page_idx})
    assert listed.get("status") == "ok", listed
    assert listed.get("count") >= 2, listed

    title = exec_tool(
        doc, ctx, "placeholder_set_text", {"page": page_idx, "role": "title", "text": "Probe Title"}
    )
    assert title.get("status") == "ok", title
    body = exec_tool(
        doc, ctx, "placeholder_set_text", {"page": page_idx, "role": "body", "text": "Probe Body"}
    )
    assert body.get("status") == "ok", body
    assert title.get("index") != body.get("index")

    read_title = exec_tool(doc, ctx, "placeholder_get_text", {"page": page_idx, "role": "title"})
    read_body = exec_tool(doc, ctx, "placeholder_get_text", {"page": page_idx, "role": "body"})
    assert read_title.get("text") == "Probe Title", read_title
    assert read_body.get("text") == "Probe Body", read_body


@native_test
@with_native_doc("impress")
def test_add_slide_blank_and_none_escape_empty(ctx, doc):
    """layout=blank / none keep the empty-page contract."""
    for layout_name in ("blank", "none"):
        added = exec_tool(doc, ctx, "slide_add", {"layout": layout_name})
        assert added.get("status") == "ok", added
        assert added.get("layout") == "blank", added
        page_idx = added["active_page_index"]
        assert page_idx == doc.getDrawPages().getCount() - 1, added
        page = doc.getDrawPages().getByIndex(page_idx)
        # blank/none assign AUTOLAYOUT_NONE (20); id 11 would be an OLE layout.
        assert page.Layout == 20, "Layout=%s added=%s" % (page.Layout, added)
        assert page.getCount() == 0, "shapes=%s Layout=%s added=%s" % (page.getCount(), page.Layout, added)
        listed = exec_tool(doc, ctx, "placeholder_list", {"page": page_idx})
        assert listed.get("status") == "ok", listed
        assert listed.get("count") == 0, listed
        miss = exec_tool(
            doc, ctx, "placeholder_set_text", {"page": page_idx, "role": "title", "text": "nope"}
        )
        assert miss.get("status") == "error", miss
        details = miss.get("details") or {}
        assert details.get("available") == [], miss
        assert details.get("suggest_layout") == "text", miss
        assert details.get("shape_text_count") == 0, miss


@native_test
@with_native_doc("impress")
def test_slide_layout_set_text_creates_placeholders_synchronously(ctx, doc):
    """Root-cause path: Layout=1 instantiates placeholders with no event loop."""
    added = exec_tool(doc, ctx, "slide_add", {"layout": "blank"})
    page_idx = added["active_page_index"]
    listed = exec_tool(doc, ctx, "placeholder_list", {"page": page_idx})
    assert listed.get("count") == 0, listed

    applied = exec_tool(doc, ctx, "slide_set_layout", {"page": page_idx, "layout": "text"})
    assert applied.get("status") == "ok", applied
    assert applied.get("layout") == "text"
    page = doc.getDrawPages().getByIndex(page_idx)
    assert page.Layout == 1
    assert page.getCount() >= 2
    listed = exec_tool(doc, ctx, "placeholder_list", {"page": page_idx})
    assert listed.get("count") >= 2, listed
