import sys
import os
sys.path.insert(0, os.path.abspath("."))
from plugin.calc.formula_fill import _try_match_ref, _is_ident_char, _read_col, adjust_a1_formula, _MAX_COL

print("adjust_a1_formula Tax2024_Total:", adjust_a1_formula("=Tax2024_Total", 0, 1))
