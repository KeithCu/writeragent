import sys
import os
sys.path.insert(0, os.path.abspath("."))
from plugin.calc.formulas import EvaluateFormula

# XFormulaResult provides getFormulaResultType
# In Uno Python, cell.getFormulaResultType() doesn't exist? Wait, getFormulaResultType is part of com.sun.star.table.XCell or XCellFormat or XCellRangeData or ...
# Let's search uno documentation or methods.
# Actually, if we just check cell.getFormulaResultType(), it returns a CellContentType (EMPTY, VALUE, TEXT, FORMULA).
# For formula, it returns what the formula evaluated to!
# Let's test this in LibreOffice if we can.
