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
from plugin.framework.logging import debug_log
from plugin.framework.uno_helpers import get_desktop
from plugin.testing_runner import setup, teardown, native_test


_test_doc = None
_test_ctx = None


@setup
def setup_draw_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx

    desktop = get_desktop(ctx)
    from com.sun.star.beans import PropertyValue
    hidden_prop = PropertyValue()
    hidden_prop.Name = "Hidden"
    hidden_prop.Value = True

    _test_doc = desktop.loadComponentFromURL("private:factory/sdraw", "_blank", 0, (hidden_prop,))
    assert _test_doc is not None, "Could not create Draw document"
    assert hasattr(_test_doc, "getDrawPages"), "Not a valid Draw document"

    debug_log("draw_tests: starting tests", context="DrawTests")


@teardown
def teardown_draw_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


def _exec_tool(name, args):
    from plugin.main import get_tools
    from plugin.framework.tool_context import ToolContext
    tctx = ToolContext(_test_doc, _test_ctx, "draw", {}, "test")
    res = get_tools().execute(name, tctx, **args)
    return json.dumps(res) if isinstance(res, dict) else res


@native_test
def test_list_pages():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass
    result = _exec_tool("list_pages", {})
    data = json.loads(result)
    assert data.get("status") == "ok", f"list_pages failed: {result}"
    num_pages = data.get("count", len(data.get("pages", [])))
    assert num_pages > 0, "No pages found"


@native_test
def test_create_and_verify_shape():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass

    # 0. Test add_slide
    initial_page_count = _test_doc.getDrawPages().getCount()
    result = _exec_tool("add_slide", {})
    data = json.loads(result)
    assert data.get("status") == "ok", f"add_slide failed: {result}"
    new_page_count = _test_doc.getDrawPages().getCount()
    assert new_page_count == initial_page_count + 1, "Page count did not increase after add_slide"

    # 1. Create shape
    active_page = _test_doc.getCurrentController().getCurrentPage()
    if active_page is None:
        active_page = _test_doc.getDrawPages().getByIndex(0)
    initial_shape_count = active_page.getCount()

    result = _exec_tool("create_shape", {
        "shape_type": "rectangle",
        "x": 1000, "y": 1000, "width": 5000, "height": 3000,
        "text": "Hello Draw",
        "bg_color": "#FF0000"
    })
    data = json.loads(result)
    assert data.get("status") == "ok", f"create_shape failed: {result}"

    new_shape_count = active_page.getCount()
    assert new_shape_count == initial_shape_count + 1, "Shape count did not increase after create_shape"

    # Query the created shape's Position and Size properties via UNO
    created_shape = active_page.getByIndex(new_shape_count - 1)
    pos = created_shape.getPosition()
    size = created_shape.getSize()
    assert pos.X == 1000, f"Expected X=1000, got {pos.X}"
    assert pos.Y == 1000, f"Expected Y=1000, got {pos.Y}"
    assert size.Width == 5000, f"Expected Width=5000, got {size.Width}"
    assert size.Height == 3000, f"Expected Height=3000, got {size.Height}"

    # 2. Get draw summary to find shape_id
    result = _exec_tool("get_draw_summary", {"page_index": 0})
    data = json.loads(result)
    assert data.get("status") == "ok", f"get_draw_summary failed: {result}"
    shapes = data.get("shapes", [])

    shape_id = None
    for s in shapes:
        if "RectangleShape" in s.get("type", ""):
            shape_id = s.get("index")
    assert shape_id is not None, "Summary missing the created rectangle"

    # 3. Edit shape
    result = _exec_tool("edit_shape", {
        "shape_index": shape_id,
        "x": 3000, "y": 3000,
        "bg_color": "#00FF00"
    })
    data = json.loads(result)
    assert data.get("status") == "ok", f"edit_shape failed: {result}"

    # 4. Delete shape
    result = _exec_tool("delete_shape", {"shape_index": shape_id})
    data = json.loads(result)
    assert data.get("status") == "ok", f"delete_shape failed: {result}"


@native_test
def test_get_draw_context_for_chat():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass
    from plugin.framework.document import get_draw_context_for_chat
    ctx_str = get_draw_context_for_chat(_test_doc, 8000, _test_ctx)
    has_doc_type = "Draw Document" in ctx_str or "Impress Presentation" in ctx_str
    has_total = "Total" in ctx_str and ("Pages" in ctx_str or "Slides" in ctx_str)
    assert has_doc_type and has_total, "get_draw_context_for_chat missing expected headers"

@native_test
def test_master_slides():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass

    # 1. List master slides
    result = _exec_tool("list_master_slides", {})
    data = json.loads(result)
    assert data.get("status") == "ok", f"list_master_slides failed: {result}"
    master_slides = data.get("master_slides", [])
    assert len(master_slides) > 0, "No master slides found"

    first_master_name = master_slides[0].get("name")
    assert first_master_name is not None, "Master slide name is missing"

    # 2. Get slide master for page 0
    result = _exec_tool("get_slide_master", {"page_index": 0})
    data = json.loads(result)
    assert data.get("status") == "ok", f"get_slide_master failed: {result}"

    # 3. Set slide master for page 0 to the first master we found
    result = _exec_tool("set_slide_master", {"page_index": 0, "master_name": first_master_name})
    data = json.loads(result)
    assert data.get("status") == "ok", f"set_slide_master failed: {result}"

    # 4. Verify it was set
    result = _exec_tool("get_slide_master", {"page_index": 0})
    data = json.loads(result)
    assert data.get("status") == "ok", f"get_slide_master verify failed: {result}"
    assert data.get("master_name") == first_master_name, f"Master name mismatch: {data.get('master_name')}"
