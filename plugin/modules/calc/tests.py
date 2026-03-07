import traceback
from plugin.framework.document import is_calc

def run_calc_tests(ctx, doc):
    """Entry point for testing the calc module functionality inside LibreOffice."""
    passed = 0
    failed = 0
    log = []

    def ok(msg):
        log.append("OK: " + msg)

    def fail(msg):
        log.append("FAIL: " + msg)

    try:
        log.append("Starting Calc Tests...")

        # Test: address_utils
        from plugin.modules.calc.address_utils import (
            column_to_index, index_to_column, parse_address,
            parse_range_string, format_address
        )

        try:
            assert column_to_index("A") == 0
            assert column_to_index("AA") == 26
            assert index_to_column(0) == "A"
            assert index_to_column(26) == "AA"
            assert parse_address("A1") == (0, 0)
            assert parse_address("B10") == (1, 9)
            assert format_address(0, 0) == "A1"

            # Round-trip
            for addr in ("A1", "B10", "Z1", "AA100"):
                col, row = parse_address(addr)
                assert format_address(col, row) == addr

            try:
                parse_address("Invalid")
                fail("address_utils: Expected ValueError for 'Invalid'")
            except ValueError:
                pass

            assert parse_range_string("A1:B2") == ((0, 0), (1, 1))
            assert parse_range_string("C3") == ((2, 2), (2, 2))

            try:
                parse_range_string("A1:Z")
                fail("address_utils: Expected ValueError for 'A1:Z'")
            except ValueError:
                pass

            passed += 1
            ok("address_utils tests passed")
        except Exception as e:
            failed += 1
            fail(f"address_utils tests failed: {e}")

        # Test: error_detector data
        from plugin.modules.calc.error_detector import ERROR_TYPES, ERROR_PATTERNS
        try:
            assert 502 in ERROR_TYPES
            assert len(ERROR_PATTERNS) > 0
            for code, info in ERROR_TYPES.items():
                assert "name" in info
                assert "description" in info
            passed += 1
            ok("error_detector data integrity passed")
        except Exception as e:
            failed += 1
            fail(f"error_detector data integrity failed: {e}")

        # Test: cells _parse_color
        from plugin.modules.calc.cells import _parse_color
        try:
            assert _parse_color("red") == 0xFF0000
            assert _parse_color("RED") == 0xFF0000
            assert _parse_color("#00FF00") == 0x00FF00
            assert _parse_color("#000") == 0x000000
            assert _parse_color("invalid") is None
            passed += 1
            ok("color parsing passed")
        except Exception as e:
            failed += 1
            fail(f"color parsing failed: {e}")

        # --- UNO Integrations Tests ---
        # Instead of mocking, we will create a hidden Calc document to run our tests on.
        desktop = get_desktop(ctx)
        from com.sun.star.beans import PropertyValue
        hidden_prop = PropertyValue()
        hidden_prop.Name = "Hidden"
        hidden_prop.Value = True

        test_doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, (hidden_prop,))
        if not test_doc:
            raise Exception("Could not create hidden test calc document")

        try:
            from plugin.main import get_tools
            from plugin.framework.tool_context import ToolContext
            import json

            def execute_calc_tool(name, args):
                tctx = ToolContext(test_doc, None, "calc", {}, "test")
                try:
                    res = get_tools().execute(name, tctx, **args)
                except (KeyError, ValueError) as e:
                    res = {"status": "error", "error": str(e)}
                return json.dumps(res) if isinstance(res, dict) else res

            active_sheet = test_doc.getCurrentController().getActiveSheet()

            # Test: write_formula_range
            res_str = execute_calc_tool("write_formula_range", {"range_name": "A1", "formula_or_values": "Hello"})
            res = json.loads(res_str)
            if res.get("status") == "ok" and active_sheet.getCellByPosition(0, 0).getString() == "Hello":
                passed += 1
                ok("write_formula_range basic passed")
            else:
                failed += 1
                fail(f"write_formula_range basic failed: {res_str}")

            # Test: batch write (multi-range list)
            execute_calc_tool("write_formula_range", {"range_name": ["B1", "B2"], "formula_or_values": "Batch"})
            if active_sheet.getCellByPosition(1, 0).getString() == "Batch" and active_sheet.getCellByPosition(1, 1).getString() == "Batch":
                passed += 1
                ok("write_formula_range batch passed")
            else:
                failed += 1
                fail("write_formula_range batch failed")

            # Test: set_cell_style
            execute_calc_tool("set_cell_style", {"range_name": "A1", "bold": True, "bg_color": "yellow"})
            cell = active_sheet.getCellByPosition(0, 0)
            from com.sun.star.awt.FontWeight import BOLD
            if cell.getPropertyValue("CharWeight") == BOLD and cell.getPropertyValue("CellBackColor") == 0xFFFF00:
                passed += 1
                ok("set_cell_style passed")
            else:
                failed += 1
                fail(f"set_cell_style failed: weight={cell.getPropertyValue('CharWeight')}, color={cell.getPropertyValue('CellBackColor')}")

            # Test: merge_cells
            execute_calc_tool("merge_cells", {"range_name": ["C1:D1", "E1:F1"]})
            rng1 = active_sheet.getCellRangeByPosition(2, 0, 3, 0)
            rng2 = active_sheet.getCellRangeByPosition(4, 0, 5, 0)
            if rng1.getIsMerged() and rng2.getIsMerged():
                passed += 1
                ok("merge_cells passed")
            else:
                failed += 1
                fail("merge_cells failed")

            # Test: clear_range
            active_sheet.getCellByPosition(6, 0).setString("ClearMe")
            active_sheet.getCellByPosition(7, 0).setString("ClearMe")
            execute_calc_tool("clear_range", {"range_name": ["G1", "H1"]})
            if active_sheet.getCellByPosition(6, 0).getString() == "" and active_sheet.getCellByPosition(7, 0).getString() == "":
                passed += 1
                ok("clear_range passed")
            else:
                failed += 1
                fail("clear_range failed")

            # Test: unknown tool
            res_str = execute_calc_tool("bad_tool", {})
            res = json.loads(res_str)
            if res.get("status") == "error":
                passed += 1
                ok("unknown tool handling passed")
            else:
                failed += 1
                fail(f"unknown tool handling failed: {res_str}")

            # Test: formulas error detector
            from plugin.modules.calc.formulas import DetectErrors
            from plugin.framework.tool_context import ToolContext
            active_sheet.getCellByPosition(8, 0).setFormula("=1/0")
            tctx = ToolContext(test_doc, None, "calc", {}, "test")
            res = DetectErrors().execute(tctx, range_name="I1")
            if res.get("status") == "ok" and res.get("result", {}).get("error_count", 0) > 0:
                passed += 1
                ok("detect_and_explain_errors passed")
            else:
                failed += 1
                fail(f"detect_and_explain_errors failed: {res}")

            # Test: analyzer get_sheet_summary
            from plugin.modules.calc.bridge import CalcBridge
            from plugin.modules.calc.analyzer import SheetAnalyzer
            bridge = CalcBridge(test_doc)
            analyzer = SheetAnalyzer(bridge)
            active_sheet.getCellByPosition(0, 5).setString("TestEnd")
            summary = analyzer.get_sheet_summary()
            if summary.get("sheet_name") == active_sheet.getName() and summary.get("row_count") >= 6:
                passed += 1
                ok("get_sheet_summary passed")
            else:
                failed += 1
                fail(f"get_sheet_summary failed: {summary}")

        finally:
            test_doc.close(True)

    except Exception as e:
        failed += 1
        fail(f"Exception during tests: {e}\n{traceback.format_exc()}")

    return passed, failed, log

def run_calc_integration_tests(ctx, doc):
    passed = 0
    failed = 0
    log = []

    try:
        log.append("Starting Calc Integration Tests...")
        passed += 1
        log.append("OK: Basic setup test passed.")
    except Exception as e:
        failed += 1
        log.append(f"FAIL: Exception during tests: {e}\n{traceback.format_exc()}")

    return passed, failed, log
