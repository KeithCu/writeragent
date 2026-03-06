import time
import sys
from unittest.mock import MagicMock

# Mock UNO constants
com = MagicMock()
sys.modules['com'] = com
sys.modules['com.sun'] = com.sun
sys.modules['com.sun.star'] = com.sun.star

from plugin.modules.calc.manipulator import CellManipulator

class MockCell:
    def setPropertyValue(self, name, value):
        pass

class MockCellRange:
    def setPropertyValue(self, name, value):
        pass

class MockSheet:
    def getCellByPosition(self, col, row):
        return MockCell()

class MockFormats:
    def queryKey(self, f, l, b): return -1
    def addNew(self, f, l): return 123

class MockDoc:
    def getNumberFormats(self):
        return MockFormats()
    def getPropertyValue(self, name):
        return "en-US"

class MockBridge:
    def get_active_sheet(self):
        return MockSheet()
    def get_active_document(self):
        return MockDoc()
    def parse_range_string(self, range_str):
        # Let's say A1:Z1000 -> (0, 0) to (25, 999) -> 26 * 1000 = 26000 cells
        return (0, 0), (25, 999)
    def get_cell_range(self, sheet, range_str):
        return MockCellRange()

bridge = MockBridge()
manipulator = CellManipulator(bridge)

start_time = time.time()
manipulator._set_range_number_format("A1:Z1000", "#,##0.00")
end_time = time.time()

print(f"Time taken for old approach: {end_time - start_time:.6f} seconds")

def optimized_set_range_number_format(self, range_str: str, format_str: str):
    sheet = self.bridge.get_active_sheet()
    cell_range = self.bridge.get_cell_range(sheet, range_str)
    doc = self.bridge.get_active_document()
    formats = doc.getNumberFormats()
    locale = doc.getPropertyValue("CharLocale")
    format_id = formats.queryKey(format_str, locale, False)
    if format_id == -1:
        format_id = formats.addNew(format_str, locale)
    cell_range.setPropertyValue("NumberFormat", format_id)

CellManipulator._set_range_number_format = optimized_set_range_number_format

start_time = time.time()
manipulator._set_range_number_format("A1:Z1000", "#,##0.00")
end_time = time.time()

print(f"Time taken for new approach: {end_time - start_time:.6f} seconds")
