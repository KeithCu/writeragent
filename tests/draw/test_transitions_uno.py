# WriterAgent — native checks that slide layout names are LibreOffice AutoLayout.
# SPDX-License-Identifier: GPL-3.0-or-later
"""slide_set_layout / slide_get_layout must write and read AutoLayout ids.

The layout table holds LibreOffice AutoLayout ids. PowerPoint PpSlideLayout
numbers minus one would give title_only id 10 (two content boxes) and blank
id 11 (an OLE shape). These cases pin the placeholders this LibreOffice build
actually creates.
"""

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from tests.draw.draw_tool_helpers import exec_tool


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
def test_slide_layout_set_get_placeholders_match_autolayout(ctx, doc):
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
        added = exec_tool(doc, ctx, "slide_add", {"layout": "blank"})
        assert added.get("status") == "ok", added
        page_idx = added["active_page_index"]
        applied = exec_tool(doc, ctx, "slide_set_layout", {"page": page_idx, "layout": name})
        assert applied.get("status") == "ok", applied
        assert applied.get("layout") == name, applied
        page = doc.getDrawPages().getByIndex(page_idx)
        assert page.Layout == expected_id, "layout=%s Layout=%s" % (name, page.Layout)
        assert _shape_types(page) == expected_shapes, "layout=%s shapes=%s" % (name, _shape_types(page))
        got = exec_tool(doc, ctx, "slide_get_layout", {"page": page_idx})
        assert got.get("layout_name") == name, got
        assert got.get("layout_id") == expected_id, got

    # A fresh insert is already AUTOLAYOUT_NONE. get must say blank, not
    # two_column_and_object (id 20 in PowerPoint-minus-one numbering).
    fresh = exec_tool(doc, ctx, "slide_add", {"layout": "none"})
    assert fresh.get("layout") == "blank", fresh
    page_idx = fresh["active_page_index"]
    page = doc.getDrawPages().getByIndex(page_idx)
    assert page.Layout == 20
    assert page.getCount() == 0
    labeled = exec_tool(doc, ctx, "slide_get_layout", {"page": page_idx})
    assert labeled.get("layout_name") == "blank", labeled
    assert labeled.get("layout_id") == 20, labeled
