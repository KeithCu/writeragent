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
from plugin.main import get_tools
from plugin.framework.logging import debug_log
from plugin.framework.uno_helpers import get_desktop

def run_draw_tests(ctx, model=None):
    """
    Run Draw tool tests with real UNO.
    ctx: UNO ComponentContext. model: optional XDrawingDocument; if None or not Draw, a new doc is created.
    Returns (passed_count, failed_count, list of message strings).
    """
    log = []
    passed = 0
    failed = 0

    def ok(msg):
        log.append("OK: %s" % msg)

    def fail(msg):
        log.append("FAIL: %s" % msg)

    try:
        smgr = ctx.getServiceManager()
        desktop = get_desktop(ctx)
        doc = model
        
        # Ensure we have a Draw document
        if doc is None or not hasattr(doc, "getDrawPages"):
            try:
                # sdraw for Draw, simpress for Impress. Both use getDrawPages.
                doc = desktop.loadComponentFromURL("private:factory/sdraw", "_blank", 0, ())
            except Exception as e:
                return 0, 1, ["Could not create Draw document: %s" % e]
        
        if not doc or not hasattr(doc, "getDrawPages"):
            return 0, 1, ["No Draw document available."]

        debug_log("draw_tests: starting tests", context="DrawTests")

        # Helper for executing tests
        def exec_tool(name, args):
            from plugin.framework.tool_context import ToolContext
            tctx = ToolContext(doc, ctx, "draw", {}, "test")
            import json
            res = get_tools().execute(name, tctx, **args)
            return json.dumps(res) if isinstance(res, dict) else res

        # Test: list_pages
        try:
            result = exec_tool("list_pages", {})
            data = json.loads(result)
            if data.get("status") == "ok":
                passed += 1
                ok(f"list_pages success: {len(data['result'])} pages")
            else:
                failed += 1
                fail(f"list_pages failed: {result}")
        except Exception as e:
            failed += 1
            log.append(f"FAIL: list_pages raised: {e}")

        # Test: create_shape (Rectangle)
        shape_id = None
        try:
            result = exec_tool("create_shape", {
                "shape_type": "rectangle",
                "x": 2000, "y": 2000, "width": 5000, "height": 3000,
                "text": "Hello Draw",
                "bg_color": "#FF0000"
            })
            data = json.loads(result)
            if data.get("status") == "ok":
                passed += 1
                ok("create_shape (Rectangle) success")
                # Note: create_shape currently returns message, not shape_index
                # We'll get it from summary
            else:
                failed += 1
                fail(f"create_shape failed: {result}")
        except Exception as e:
            failed += 1
            log.append(f"FAIL: create_shape raised: {e}")

        # Test: get_draw_summary
        try:
            result = exec_tool("get_draw_summary", {"page_index": 0})
            data = json.loads(result)
            if data.get("status") == "ok":
                passed += 1
                ok("get_draw_summary success")
                shapes = data.get("result", {}).get("shapes", [])
                if any("RectangleShape" in s.get("type", "") for s in shapes):
                    passed += 1
                    ok("Summary contains the created rectangle")
                    # Find the index of the rectangle we just created
                    for s in shapes:
                        if "RectangleShape" in s.get("type", ""):
                            shape_id = s.get("index")
                else:
                    failed += 1
                    fail("Summary missing the created rectangle")
            else:
                failed += 1
                fail(f"get_draw_summary failed: {result}")
        except Exception as e:
            failed += 1
            log.append(f"FAIL: get_draw_summary raised: {e}")

        # Test: edit_shape (Move/Resize/Color)
        if shape_id is not None:
            try:
                result = exec_tool("edit_shape", {
                    "shape_index": shape_id,
                    "x": 3000, "y": 3000,
                    "bg_color": "#00FF00"
                })
                data = json.loads(result)
                if data.get("status") == "ok":
                    passed += 1
                    ok("edit_shape success")
                else:
                    failed += 1
                    fail(f"edit_shape failed: {result}")
            except Exception as e:
                failed += 1
                log.append(f"FAIL: edit_shape raised: {e}")

        # Test: delete_shape
        if shape_id is not None:
            try:
                result = exec_tool("delete_shape", {"shape_index": shape_id})
                data = json.loads(result)
                if data.get("status") == "ok":
                    passed += 1
                    ok("delete_shape success")
                else:
                    failed += 1
                    fail(f"delete_shape failed: {result}")
            except Exception as e:
                failed += 1
                log.append(f"FAIL: delete_shape raised: {e}")

        # Test: get_draw_context_for_chat
        try:
            from plugin.framework.document import get_draw_context_for_chat
            ctx_str = get_draw_context_for_chat(doc, 8000, ctx)
            if "Draw/Impress Document" in ctx_str and "Total Pages" in ctx_str:
                passed += 1
                ok("get_draw_context_for_chat returns summary")
            else:
                failed += 1
                fail("get_draw_context_for_chat missing expected headers")
        except Exception as e:
            failed += 1
            log.append(f"FAIL: get_draw_context_for_chat raised: {e}")

    except Exception as e:
        failed += 1
        log.append(f"CRITICAL failure in Draw test runner: {e}")

    return passed, failed, log
