# WriterAgent — native checks that slide layout names are LibreOffice AutoLayout.
# SPDX-License-Identifier: GPL-3.0-or-later
"""set_slide_layout / get_slide_layout must write and read AutoLayout ids.

The old table used PowerPoint PpSlideLayout minus one, so title_only (10)
grew two content boxes and blank (11) grew an OLE shape. These cases pin
the placeholders this LibreOffice build actually creates.
"""
import json

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


def _exec_tool(doc, ctx, name, args):
    res = TestingFactory.execute_tool(doc, ctx, name, args, doc_type="impress")
    return res if isinstance(res, dict) else json.loads(res)


def _shape_types(page):
    labels = []
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        label = ""
        try:
            value = getattr(shape, "ShapeType", None)
            if value:
                label = str(value)
        except Exception:
            label = ""
        if not label:
            try:
                label = str(shape.getShapeType() or "")
            except Exception:
                label = ""
        labels.append(label.rsplit(".", 1)[-1])
    return labels


@native_test
@with_native_doc("impress")
def test_set_get_slide_layout_placeholders_match_autolayout(ctx, doc):
    """Names that the PowerPoint table mis-numbered, plus the layouts they collided with."""
    # (name, AutoLayout id, shape services after assignment)
    cases = (
        ("title", 0, ["TitleTextShape", "SubtitleShape"]),
        ("text", 1, ["TitleTextShape", "OutlinerShape"]),
        # AUTOLAYOUT_CHART still reads back as 2; this LO merged it into content.
        ("chart", 2, ["TitleTextShape", "OutlinerShape"]),
        ("two_column_text", 3, ["TitleTextShape", "OutlinerShape", "OutlinerShape"]),
        ("object", 11, ["TitleTextShape", "OLE2Shape"]),
        ("four_objects", 18, ["TitleTextShape", "OutlinerShape", "OutlinerShape", "OutlinerShape", "OutlinerShape"]),
        ("title_only", 19, ["TitleTextShape"]),
        ("blank", 20, []),
    )
    for name, expected_id, expected_shapes in cases:
        added = _exec_tool(doc, ctx, "add_slide", {"layout": "blank"})
        assert added.get("status") == "ok", added
        page_idx = added["active_page_index"]
        applied = _exec_tool(doc, ctx, "set_slide_layout", {"page": page_idx, "layout": name})
        assert applied.get("status") == "ok", applied
        assert applied.get("layout") == name, applied
        page = doc.getDrawPages().getByIndex(page_idx)
        assert page.Layout == expected_id, "layout=%s Layout=%s" % (name, page.Layout)
        assert _shape_types(page) == expected_shapes, "layout=%s shapes=%s" % (name, _shape_types(page))
        got = _exec_tool(doc, ctx, "get_slide_layout", {"page": page_idx})
        assert got.get("layout_name") == name, got
        assert got.get("layout_id") == expected_id, got

    # A fresh insert is already AUTOLAYOUT_NONE. get must say blank, not
    # the old reverse label for id 20 (two_column_and_object).
    fresh = _exec_tool(doc, ctx, "add_slide", {"layout": "none"})
    assert fresh.get("layout") == "blank", fresh
    page_idx = fresh["active_page_index"]
    page = doc.getDrawPages().getByIndex(page_idx)
    assert page.Layout == 20
    assert page.getCount() == 0
    labeled = _exec_tool(doc, ctx, "get_slide_layout", {"page": page_idx})
    assert labeled.get("layout_name") == "blank", labeled
    assert labeled.get("layout_id") == 20, labeled
