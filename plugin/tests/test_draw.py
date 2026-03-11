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
from plugin.testing_runner import setup, teardown, test


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


@test
def test_list_pages():
    result = _exec_tool("list_pages", {})
    data = json.loads(result)
    assert data.get("status") == "ok", f"list_pages failed: {result}"
    num_pages = data.get("count", len(data.get("pages", [])))
    assert num_pages > 0, "No pages found"


@test
def test_create_and_verify_shape():
    # 1. Create shape
    result = _exec_tool("create_shape", {
        "shape_type": "rectangle",
        "x": 2000, "y": 2000, "width": 5000, "height": 3000,
        "text": "Hello Draw",
        "bg_color": "#FF0000"
    })
    data = json.loads(result)
    assert data.get("status") == "ok", f"create_shape failed: {result}"

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


@test
def test_get_draw_context_for_chat():
    from plugin.framework.document import get_draw_context_for_chat
    ctx_str = get_draw_context_for_chat(_test_doc, 8000, _test_ctx)
    has_doc_type = "Draw Document" in ctx_str or "Impress Presentation" in ctx_str
    has_total = "Total" in ctx_str and ("Pages" in ctx_str or "Slides" in ctx_str)
    assert has_doc_type and has_total, "get_draw_context_for_chat missing expected headers"
