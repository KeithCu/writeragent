import unittest
import json
from plugin.modules.calc.address_utils import (
    column_to_index, index_to_column, parse_address, 
    parse_range_string, format_address
)
from plugin.modules.calc.error_detector import ERROR_TYPES, ERROR_PATTERNS
from plugin.framework.constants import get_chat_system_prompt_for_document
from plugin.modules.calc.cells import _parse_color
from plugin.main import get_tools

def execute_calc_tool(name, args, doc):
    from plugin.framework.tool_context import ToolContext
    tctx = ToolContext(doc, None, "calc", {}, "test")
    import json
    try:
        res = get_tools().execute(name, tctx, **args)
        return json.dumps(res) if isinstance(res, dict) else res
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
import sys
from unittest.mock import MagicMock

# --- Mock UNO Constants ---
com = MagicMock()
class FontWeight:
    BOLD = 150.0
    NORMAL = 100.0
class FontSlant:
    ITALIC = 2
    NONE = 0
class CellHoriJustify:
    LEFT, CENTER, RIGHT, BLOCK, STANDARD = 1, 2, 3, 4, 0
class CellVertJustify:
    TOP, CENTER, BOTTOM, STANDARD = 1, 2, 3, 0

com.sun.star.awt.FontWeight = FontWeight
com.sun.star.awt.FontSlant = FontSlant
com.sun.star.table.CellHoriJustify = CellHoriJustify
com.sun.star.table.CellVertJustify = CellVertJustify

# Mock modules
sys.modules['com'] = com
sys.modules['com.sun'] = com.sun
sys.modules['com.sun.star'] = com.sun.star
sys.modules['com.sun.star.awt'] = com.sun.star.awt
sys.modules['com.sun.star.awt.FontWeight'] = FontWeight
sys.modules['com.sun.star.awt.FontSlant'] = FontSlant
sys.modules['com.sun.star.table'] = com.sun.star.table
sys.modules['com.sun.star.table.CellHoriJustify'] = CellHoriJustify
sys.modules['com.sun.star.table.CellVertJustify'] = CellVertJustify

# --- Stateful Stubs for Dispatcher Testing ---

class PropertyHolder:
    def __init__(self):
        self.properties = {}
    def setPropertyValue(self, name, value):
        self.properties[name] = value
    def getPropertyValue(self, name):
        return self.properties.get(name)

class CellStub(PropertyHolder):
    def __init__(self, addr):
        super().__init__()
        self.address = addr
        self.value = 0.0
        self.string = ""
        self.formula = ""
    def setValue(self, v): self.value = float(v)
    def setString(self, s): self.string = s
    def setFormula(self, f): self.formula = f
    def getValue(self): return self.value
    def getString(self): return self.string
    def getFormula(self): return self.formula

class RangeStub(PropertyHolder):
    def __init__(self, range_str):
        super().__init__()
        self.range_str = range_str
        self.merged = False
        self.cells_cleared = 0
    def merge(self, b): self.merged = b
    def clearContents(self, flags): self.cells_cleared = flags
    def createSortDescriptor(self):
        return [type("Prop", (), {"Name": n, "Value": None})() 
                for n in ["SortFields", "ContainsHeader"]]
    def sort(self, descriptor):
        self.last_sort = descriptor

class SheetStub:
    def __init__(self, name):
        self.name = name
        self.cells = {}
        self.ranges = {}
    def getName(self): return self.name
    def getCellByPosition(self, c, r):
        key = (c, r)
        if key not in self.cells: self.cells[key] = CellStub(key)
        return self.cells[key]
    def getCellRangeByPosition(self, sc, sr, ec, er):
        s = f"{sc},{sr}:{ec},{er}"
        if s not in self.ranges: self.ranges[s] = RangeStub(s)
        return self.ranges[s]

class DocStub:
    def __init__(self):
        self.sheets = {"Sheet1": SheetStub("Sheet1")}
        self.active_sheet = self.sheets["Sheet1"]
        self.url = "test://doc"
        self.properties = {"CharLocale": "en-US"}
    def getSheets(self):
        class SheetsColl:
            def __init__(self, d): self.d = d
            def getCount(self): return len(self.d.sheets)
            def hasByName(self, n): return n in self.d.sheets
            def getByName(self, n): return self.d.sheets[n]
            def getByIndex(self, i): return list(self.d.sheets.values())[i]
            def insertNewByName(self, n, p): self.d.sheets[n] = SheetStub(n)
        return SheetsColl(self)
    def getCurrentController(self):
        class Controller:
            def __init__(self, d): self.d = d
            def getActiveSheet(self): return self.d.active_sheet
            def setActiveSheet(self, s): self.d.active_sheet = s
            def getSelection(self): return None
        return Controller(self)
    def getURL(self): return self.url
    def supportsService(self, s):
        return s == "com.sun.star.sheet.SpreadsheetDocument"
    def getNumberFormats(self):
        class NF:
            def queryKey(self, f, l, b): return 123
            def addNew(self, f, l): return 123
        return NF()
    def getPropertyValue(self, n): return self.properties.get(n)

# --- Test Cases ---

class TestCalcUnified(unittest.TestCase):

    # 1. Address Utils (from test_calc_address_utils.py)
    def test_address_conversions(self):
        self.assertEqual(column_to_index("A"), 0)
        self.assertEqual(column_to_index("AA"), 26)
        self.assertEqual(index_to_column(0), "A")
        self.assertEqual(index_to_column(26), "AA")
        self.assertEqual(parse_address("A1"), (0, 0))
        self.assertEqual(parse_address("B10"), (1, 9))
        self.assertEqual(format_address(0, 0), "A1")
        
        # Round-trip
        for addr in ("A1", "B10", "Z1", "AA100"):
            col, row = parse_address(addr)
            self.assertEqual(format_address(col, row), addr)

        with self.assertRaises(ValueError):
            parse_address("Invalid")

    def test_range_parsing(self):
        self.assertEqual(parse_range_string("A1:B2"), ((0, 0), (1, 1)))
        self.assertEqual(parse_range_string("C3"), ((2, 2), (2, 2)))
        with self.assertRaises(ValueError):
            parse_range_string("A1:Z") # invalid end cell

    # 2. Error Detector Data (from test_calc_error_detector_data.py)
    def test_error_detector_data_integrity(self):
        self.assertIn(502, ERROR_TYPES)
        self.assertTrue(len(ERROR_PATTERNS) > 0)
        for code, info in ERROR_TYPES.items():
            self.assertIn("name", info)
            self.assertIn("description", info)

    # 3. Prompts (from test_calc_prompts.py)
    def test_prompt_selection(self):
        calc_doc = DocStub()
        # Fixed stub for Writer doc to have supportsService
        writer_doc = type("WriterDoc", (), {"supportsService": lambda self, svc: svc == "com.sun.star.text.TextDocument"})()
        
        calc_prompt = get_chat_system_prompt_for_document(calc_doc, "Be fast")
        self.assertIn("read_cell_range", calc_prompt)
        self.assertIn("FORMULA SYNTAX", calc_prompt)
        self.assertIn("Be fast", calc_prompt)
        
        writer_prompt = get_chat_system_prompt_for_document(writer_doc, "")
        self.assertIn("get_document_content", writer_prompt)
        self.assertNotIn("read_cell_range", writer_prompt)

    # 4. Color Parsing (from test_calc_tools_helpers.py)
    def test_color_parsing(self):
        self.assertEqual(_parse_color("red"), 0xFF0000)
        self.assertEqual(_parse_color("RED"), 0xFF0000)
        self.assertEqual(_parse_color("#00FF00"), 0x00FF00)
        self.assertEqual(_parse_color("#000"), 0x000000)
        self.assertIsNone(_parse_color("invalid"))

    # 5. Dispatcher Logic (replaces test_calc_tool_dispatch.py)
    def test_execute_tools_no_mocking(self):
        doc = DocStub()
        
        # Test: write_formula_range
        res = execute_calc_tool("write_formula_range", {"range_name": "A1", "formula_or_values": "Hello"}, doc)
        self.assertIn("ok", res)
        self.assertEqual(doc.active_sheet.getCellByPosition(0, 0).getString(), "Hello")

        # Test: batch write (multi-range list)
        execute_calc_tool("write_formula_range", {"range_name": ["B1", "B2"], "formula_or_values": "Batch"}, doc)
        self.assertEqual(doc.active_sheet.getCellByPosition(1, 0).getString(), "Batch")
        self.assertEqual(doc.active_sheet.getCellByPosition(1, 1).getString(), "Batch")

        # Test: set_cell_style
        execute_calc_tool("set_cell_style", {"range_name": "A1", "bold": True, "bg_color": "yellow"}, doc)
        cell = doc.active_sheet.getCellByPosition(0, 0)
        from com.sun.star.awt.FontWeight import BOLD
        self.assertEqual(cell.getPropertyValue("CharWeight"), BOLD)
        self.assertEqual(cell.getPropertyValue("CellBackColor"), 0xFFFF00)

        # Test: merge_cells (multi-range list)
        execute_calc_tool("merge_cells", {"range_name": ["C1:D1", "E1:F1"]}, doc)
        rng1 = doc.active_sheet.getCellRangeByPosition(2, 0, 3, 0)
        rng2 = doc.active_sheet.getCellRangeByPosition(4, 0, 5, 0)
        self.assertTrue(rng1.merged)
        self.assertTrue(rng2.merged)

        # Test: clear_range (multi-range list)
        doc.active_sheet.getCellByPosition(6, 0).setString("ClearMe")
        doc.active_sheet.getCellByPosition(7, 0).setString("ClearMe")
        execute_calc_tool("clear_range", {"range_name": ["G1", "H1"]}, doc)
        # 23 = CellFlags common for clearing
        self.assertEqual(doc.active_sheet.getCellRangeByPosition(6, 0, 6, 0).cells_cleared, 23)
        self.assertEqual(doc.active_sheet.getCellRangeByPosition(7, 0, 7, 0).cells_cleared, 23)

        # Test: unknown tool
        res = json.loads(execute_calc_tool("bad_tool", {}, doc))
        self.assertEqual(res["status"], "error")

if __name__ == "__main__":
    unittest.main()
