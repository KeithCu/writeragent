# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import json

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


def _exec_tool(doc, ctx, name, args):
    from plugin.main import get_tools
    from plugin.framework.tool import ToolContext
    
    active_idx = _active_draw_page_index(doc)
    tctx = ToolContext(doc, ctx, "draw", get_tools()._services, "test", active_page_index=active_idx)
    res = get_tools().execute(name, tctx, **args)
    return json.dumps(res) if isinstance(res, dict) else res


def _active_draw_page_index(doc):
    """Index of the page the Draw controller treats as current (0 if unknown)."""
    if doc is None:
        return 0
    pages = doc.getDrawPages()
    ctrl = doc.getCurrentController()
    if ctrl is None or not hasattr(ctrl, "getCurrentPage"):
        return 0
    page = ctrl.getCurrentPage()
    if page is None:
        return 0
    for i in range(pages.getCount()):
        if pages.getByIndex(i) == page:
            return i
    return 0


@native_test
@with_native_doc("draw")
def test_list_pages(ctx, doc):
    result = _exec_tool(doc, ctx, "list_pages", {})
    data = json.loads(result)
    assert data.get("status") == "ok", f"list_pages failed: {result}"
    num_pages = data.get("count", len(data.get("pages", [])))
    assert num_pages > 0, "No pages found"


@native_test
@with_native_doc("draw")
def test_upsert_and_verify_shape(ctx, doc):
    # 0. Test add_slide
    initial_page_count = doc.getDrawPages().getCount()
    result = _exec_tool(doc, ctx, "add_slide", {})
    data = json.loads(result)
    assert data.get("status") == "ok", f"add_slide failed: {result}"
    new_page_count = doc.getDrawPages().getCount()
    assert new_page_count == initial_page_count + 1, "Page count did not increase after add_slide"
    inserted = doc.getDrawPages().getByIndex(new_page_count - 1)
    cur = doc.getCurrentController().getCurrentPage()
    assert cur is not None and cur == inserted, "add_slide should activate the new slide for subsequent tools"

    # 1. Create shape
    active_page = doc.getCurrentController().getCurrentPage()
    if active_page is None:
        active_page = doc.getDrawPages().getByIndex(0)
    initial_shape_count = active_page.getCount()

    result = _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create",
        "shape_type": "rectangle",
        "x": 1000, "y": 1000, "width": 5000, "height": 3000,
        "text": "Hello Draw",
        "fill_color": "#FF0000"
    })
    data = json.loads(result)
    assert data.get("status") == "ok", f"shape_upsert create failed: {result}"

    new_shape_count = active_page.getCount()
    assert new_shape_count == initial_shape_count + 1, "Shape count did not increase after shape_upsert"

    # Query the created shape's Position and Size properties via UNO
    created_shape = active_page.getByIndex(new_shape_count - 1)
    pos = created_shape.getPosition()
    size = created_shape.getSize()
    assert pos.X == 1000, f"Expected X=1000, got {pos.X}"
    assert pos.Y == 1000, f"Expected Y=1000, got {pos.Y}"
    assert size.Width == 5000, f"Expected Width=5000, got {size.Width}"
    assert size.Height == 3000, f"Expected Height=3000, got {size.Height}"

    # 2. Get draw summary to find shape_id
    result = _exec_tool(doc, ctx, "shape_summary", {"page": new_page_count - 1})
    data = json.loads(result)
    assert data.get("status") == "ok", f"shape_summary failed: {result}"
    shapes = data.get("shapes", [])

    shape_id = None
    for s in shapes:
        if "RectangleShape" in s.get("type", ""):
            shape_id = s.get("index")
    assert shape_id is not None, "Summary missing the created rectangle"

    # 3. Edit shape
    result = _exec_tool(doc, ctx, "shape_upsert", {
        "action": "edit",
        "index": shape_id,
        "x": 3000, "y": 3000,
        "fill_color": "#00FF00"
    })
    data = json.loads(result)
    assert data.get("status") == "ok", f"shape_upsert edit failed: {result}"

    # 4. Delete shape
    result = _exec_tool(doc, ctx, "shape_delete", {"index": shape_id})
    data = json.loads(result)
    assert data.get("status") == "ok", f"shape_delete failed: {result}"


@native_test
@with_native_doc("draw")
def test_create_custom_shape_octagon(ctx, doc):
    """Enhanced CustomShape types need CustomShapeEngine + geometry Type (e.g. octagon)."""
    active_page = doc.getCurrentController().getCurrentPage()
    if active_page is None:
        active_page = doc.getDrawPages().getByIndex(0)
    initial_shape_count = active_page.getCount()

    result = _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create",
        "shape_type": "octagon",
        "x": 1000,
        "y": 1000,
        "width": 5000,
        "height": 5000,
        "fill_color": "none",
        "line_color": "black",
        "line_width": 100,
    })
    data = json.loads(result)
    assert data.get("status") == "ok", f"shape_upsert octagon failed: {result}"
    assert data.get("geometry_applied") is True, data
    assert "warning" not in data, data

    assert active_page.getCount() == initial_shape_count + 1, "Octagon not added to page"

    created = active_page.getByIndex(active_page.getCount() - 1)
    pos = created.getPosition()
    size = created.getSize()
    assert pos.X == 1000 and pos.Y == 1000, (pos.X, pos.Y)
    assert size.Width == 5000 and size.Height == 5000, (size.Width, size.Height)
    assert size.Width > 0 and size.Height > 0, "Shape must have non-zero size"


@native_test
@with_native_doc("draw")
def test_get_draw_context_for_chat(ctx, doc):
    from plugin.draw.bridge import get_draw_context_for_chat
    ctx_str = get_draw_context_for_chat(doc, 8000, ctx)
    has_doc_type = "Draw Document" in ctx_str or "Impress Presentation" in ctx_str
    has_total = "Total" in ctx_str and ("Pages" in ctx_str or "Slides" in ctx_str)
    assert has_doc_type and has_total, "get_draw_context_for_chat missing expected headers"


@native_test
@with_native_doc("draw")
def test_duplicate_slide_copies_shapes(ctx, doc):
    page0 = doc.getDrawPages().getByIndex(0)
    before = page0.getCount()
    result = _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create",
        "shape_type": "rectangle",
        "page": 0,
        "x": 1000, "y": 1000, "width": 2000, "height": 1500,
        "text": "dup-me",
        "fill_color": "#FF0000",
    })
    data = json.loads(result)
    assert data.get("status") == "ok", result
    src_count = page0.getCount()
    assert src_count == before + 1

    result = _exec_tool(doc, ctx, "duplicate_slide", {"page": 0, "activate": False})
    data = json.loads(result)
    assert data.get("status") == "ok", result
    copy = doc.getDrawPages().getByIndex(1)
    assert copy.getCount() == src_count
    texts = []
    for i in range(copy.getCount()):
        shape = copy.getByIndex(i)
        try:
            texts.append(shape.getString())
        except Exception:
            pass
    assert "dup-me" in texts


@native_test
@with_native_doc("draw")
def test_duplicate_rename_move_slide(ctx, doc):
    initial = doc.getDrawPages().getCount()
    result = _exec_tool(doc, ctx, "duplicate_slide", {"page": 0})
    data = json.loads(result)
    assert data.get("status") == "ok", result
    assert doc.getDrawPages().getCount() == initial + 1

    result = _exec_tool(doc, ctx, "rename_slide", {"page": 1, "name": "Copy"})
    data = json.loads(result)
    assert data.get("status") == "ok", result
    page = doc.getDrawPages().getByIndex(1)
    if hasattr(page, "Name"):
        assert page.Name == "Copy"

    result = _exec_tool(doc, ctx, "move_slide", {"from_page": 1, "to_page": 0})
    data = json.loads(result)
    assert data.get("status") == "ok", result


@native_test
@with_native_doc("draw")
def test_align_and_insert_table(ctx, doc):
    page = doc.getCurrentController().getCurrentPage()
    if page is None:
        page = doc.getDrawPages().getByIndex(0)
    _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create", "shape_type": "rectangle",
        "x": 1000, "y": 2000, "width": 2000, "height": 1000, "text": "L",
    })
    _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create", "shape_type": "rectangle",
        "x": 5000, "y": 4000, "width": 2000, "height": 1000, "text": "R",
    })
    n = page.getCount()
    result = _exec_tool(doc, ctx, "align_shapes", {
        "indices": [n - 2, n - 1], "alignment": "top",
    })
    data = json.loads(result)
    assert data.get("status") == "ok", result
    a = page.getByIndex(n - 2).getPosition()
    b = page.getByIndex(n - 1).getPosition()
    assert a.Y == b.Y == 2000

    result = _exec_tool(doc, ctx, "table_insert", {
        "rows": 2, "columns": 2, "data": [["A", "B"], ["C", "D"]],
    })
    data = json.loads(result)
    assert data.get("status") == "ok", result
    assert data.get("cells_written") == 4, result


@native_test
@with_native_doc("draw")
def test_master_slides(ctx, doc):
    # 1. List master slides
    result = _exec_tool(doc, ctx, "list_master_slides", {})
    data = json.loads(result)
    assert data.get("status") == "ok", f"list_master_slides failed: {result}"
    master_slides = data.get("master_slides", [])
    assert len(master_slides) > 0, "No master slides found"

    first_master_name = master_slides[0].get("name")
    assert first_master_name is not None, "Master slide name is missing"

    # 2. Get slide master for page 0
    result = _exec_tool(doc, ctx, "get_slide_master", {"page": 0})
    data = json.loads(result)
    assert data.get("status") == "ok", f"get_slide_master failed: {result}"

    # 3. Set slide master for page 0 to the first master we found
    result = _exec_tool(doc, ctx, "set_slide_master", {"page": 0, "master": first_master_name})
    data = json.loads(result)
    assert data.get("status") == "ok", f"set_slide_master failed: {result}"

    # 4. Verify it was set
    result = _exec_tool(doc, ctx, "get_slide_master", {"page": 0})
    data = json.loads(result)
    assert data.get("status") == "ok", f"get_slide_master verify failed: {result}"
    assert data.get("master") == first_master_name, f"Master name mismatch: {data.get('master')}"


@native_test
@with_native_doc("draw")
def test_get_draw_tree(ctx, doc):
    # Ensure there is at least one shape to build a tree with
    _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create",
        "shape_type": "rectangle",
        "x": 1000, "y": 1000, "width": 5000, "height": 3000,
        "text": "Tree Shape",
        "fill_color": "#FF0000"
    })

    result = _exec_tool(doc, ctx, "get_draw_tree", {"page": _active_draw_page_index(doc)})
    data = json.loads(result)
    assert data.get("status") == "ok", f"get_draw_tree failed: {result}"
    tree = data.get("tree", [])
    assert len(tree) > 0, "Draw tree is empty"

    # Check if the shape we just created is in the tree
    found = False
    for node in tree:
        if node.get("text") == "Tree Shape":
            found = True
            break
    assert found, "Created shape not found in draw tree"


@native_test
@with_native_doc("draw")
def test_insert_math_draw(ctx, doc):
    # Insert math (formula_type + formula + page + x + y; size from UNO/heuristic)
    result = _exec_tool(doc, ctx, "insert_math", {
        "formula_type": "latex",
        "formula": "E = mc^2",
        "page": 0,
        "x": 2000,
        "y": 2000,
    })

    data = json.loads(result)
    assert data.get("status") == "ok", f"insert_math failed: {result}"

    # insert_math used page_index 0 — read shape from that page (current slide may differ after prior tests).
    target_page = doc.getDrawPages().getByIndex(0)
    shape = target_page.getByIndex(data.get("index"))

    assert shape.CLSID == "078B7ABA-54FC-457F-8551-6147e776a997"
    sz = shape.getSize()
    assert sz.Width >= 400 and sz.Height >= 300, f"expected plausible size, got {sz.Width}x{sz.Height}"


@native_test
def test_shape_upsert_validation():
    from plugin.main import get_tools
    shape_upsert_tool = get_tools().get("shape_upsert")
    assert shape_upsert_tool is not None

    # Test validation when action is missing
    ok, err = shape_upsert_tool.validate()
    assert not ok
    assert "Missing required parameter" in err

    # Test validation when action='create' but missing required parameters
    ok, err = shape_upsert_tool.validate(action="create")
    assert not ok
    assert "required when action is 'create'" in err

    # Test validation when action='edit' but missing index
    ok, err = shape_upsert_tool.validate(action="edit")
    assert not ok
    assert "Parameter 'index' is required" in err

    # Test validation when action='create' and all required parameters are present
    ok, err = shape_upsert_tool.validate(action="create", shape_type="rectangle", x=1000, y=1000, width=5000, height=3000)
    assert ok
    assert err is None

