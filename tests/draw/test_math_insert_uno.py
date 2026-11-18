# WriterAgent — Draw math_insert sizes the OLE from the formula, not text length.
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("draw")
def test_insert_math_sizes_from_visual_area(ctx, doc):
    """A stacked fraction must use the OLE visual area, not the length box.

    ``getEmbeddedObject()`` is absent on Draw ``OLE2Shape``, so the old
    helper always returned None and ``math_insert`` used ``_heuristic_size_hmm``.
    """
    from plugin.draw.math_insert import _heuristic_size_hmm, _try_ole_shape_content_size_hmm
    from plugin.app import get_tools
    from plugin.framework.tool import ToolContext
    from plugin.tests.testing_utils import mark_windows_math_ole_doc

    formula = r"\frac{a}{\frac{b}{\frac{c}{d}}}"
    tctx = ToolContext(doc, ctx, "draw", get_tools().services, "test", active_page_index=0)
    res = get_tools().execute("math_insert", tctx, formula_type="latex", formula=formula, page=0, x=1500, y=1500)
    data = res if isinstance(res, dict) else json.loads(res)
    assert data.get("status") == "ok", data

    shape = doc.getDrawPages().getByIndex(0).getByIndex(data["index"])
    measured = _try_ole_shape_content_size_hmm(shape)
    size = shape.getSize()
    starmath = str(shape.Model.Formula or "")
    heuristic = _heuristic_size_hmm(starmath or formula)
    # Drop the OLE proxy before the document closes (Windows soffice abort).
    mark_windows_math_ole_doc(doc)
    shape = None

    detail = "size=%sx%s measured=%s heuristic=%s" % (size.Width, size.Height, measured, heuristic)
    assert measured is not None, detail
    # Width may be raised to the minimum box; height must follow the formula.
    assert size.Height == max(400, min(80_000, measured[1])), detail
    assert size.Width == max(500, min(120_000, measured[0])), detail
    # Length heuristic is wide and short; a stacked fraction is the opposite.
    assert size.Height > heuristic[1], detail
    assert size.Width < heuristic[0], detail


@native_test
@with_native_doc("draw")
def test_insert_math_names_shape(ctx, doc):
    from plugin.app import get_tools
    from plugin.framework.tool import ToolContext
    from plugin.tests.testing_utils import mark_windows_math_ole_doc

    tctx = ToolContext(doc, ctx, "draw", get_tools().services, "test", active_page_index=0)
    res1 = get_tools().execute("math_insert", tctx, formula_type="latex", formula="x + 1", page=0, x=1500, y=1500)
    res2 = get_tools().execute("math_insert", tctx, formula_type="latex", formula="y + 2", page=0, x=4500, y=1500)
    data1 = res1 if isinstance(res1, dict) else json.loads(res1)
    data2 = res2 if isinstance(res2, dict) else json.loads(res2)
    assert data1.get("status") == "ok", data1
    assert data2.get("status") == "ok", data2

    name1 = data1.get("name", "")
    name2 = data2.get("name", "")
    page = doc.getDrawPages().getByIndex(0)
    actual1 = page.getByIndex(data1["index"]).Name
    actual2 = page.getByIndex(data2["index"]).Name

    mark_windows_math_ole_doc(doc)
    page = None

    assert name1 and name2 and name1 != name2, (data1, data2)
    assert actual1 == name1
    assert actual2 == name2
