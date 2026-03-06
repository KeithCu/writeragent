import time
from unittest.mock import MagicMock, patch
import sys
from types import ModuleType

m = ModuleType("core.calc_address_utils")
m.parse_address = lambda x: (0, 0)
sys.modules["core.calc_address_utils"] = m

m = ModuleType("core.logging")
m.debug_log = MagicMock()
sys.modules["core.logging"] = m

from plugin.modules.calc.manipulator import CellManipulator

class MockCell:
    def setValue(self, v): pass
    def setString(self, s): pass
    def setFormula(self, f): pass

class MockRange:
    def setDataArray(self, data):
        time.sleep(0.0001) # Simulate UNO bridge overhead
        self.data = data
    def setFormulaArray(self, data):
        time.sleep(0.0001)
        self.data = data

class MockSheet:
    def getCellByPosition(self, col, row):
        time.sleep(0.0001) # Simulate UNO bridge overhead
        return MockCell()
    def getCellRangeByPosition(self, col1, row1, col2, row2):
        return MockRange()

def run_bench():
    bridge = MagicMock()
    bridge._index_to_column.return_value = "Z"
    bridge.parse_range_string.return_value = ((0, 0), (9, 999))
    manipulator = CellManipulator(bridge)
    sheet = MockSheet()
    bridge.get_active_sheet.return_value = sheet

    # Generate 10000 values
    values = [str(i) for i in range(10000)]

    start = time.time()
    manipulator.write_formula_range("A1:J1000", values)
    end = time.time()

    print(f"Time taken: {end - start:.4f} seconds")

run_bench()
