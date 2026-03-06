import sys
from types import ModuleType

# Mock the parts of core that we don't want to load or that depend on UNO
m = ModuleType("core.calc_address_utils")
m.parse_address = lambda x: (0, 0)
sys.modules["core.calc_address_utils"] = m

m = ModuleType("core.logging")
import logging
m.debug_log = logging.debug
sys.modules["core.logging"] = m

from plugin.modules.calc.manipulator import CellManipulator

class MockCellRange:
    def __init__(self):
        self.formulas = None
    def setFormulaArray(self, arr):
        self.formulas = arr

class MockBridge:
    def __init__(self):
        self.active_sheet = "Sheet1"
    def get_active_sheet(self):
        return self.active_sheet
    def parse_range_string(self, range_str):
        # A1:B2 -> (0,0) to (1,1)
        return ((0, 0), (1, 1))
    def get_cell_range(self, sheet, range_str):
        return self.rng

manipulator = CellManipulator(MockBridge())
manipulator.bridge.rng = MockCellRange()

values = ["A", 1, "=SUM(A1:A2)", 2.5]
manipulator.write_formula_range("A1:B2", values)

print(manipulator.bridge.rng.formulas)
assert manipulator.bridge.rng.formulas == (("A", 1.0), ("=SUM(A1:A2)", 2.5))
print("Test passed")
