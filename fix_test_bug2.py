import sys
import os
sys.path.insert(0, os.path.abspath("."))
import plugin.calc.formula_fill as ff

old_read_col = ff._read_col

def new_read_col(s: str, i: int):
    start = i
    n = len(s)
    while i < n and ff._is_letter(s[i]):
        i += 1
    if i == start:
        return None
    if i - start > 3:
        return None
    letters = s[start:i]
    try:
        col_idx = ff.column_to_index(letters)
        if col_idx > ff._MAX_COL:
            return None
        return i, col_idx
    except Exception:
        return None

old_read_row = ff._read_row
def new_read_row(s: str, i: int):
    start = i
    n = len(s)
    while i < n and ff._is_digit(s[i]):
        i += 1
    if i == start:
        return None
    # Don't try parsing arbitrary huge digits which would fail or consume big ints.
    # _MAX_ROW is 1,048,575 so a length of >7 implies out of bounds immediately
    if i - start > 7:
        return None
    row_num = int(s[start:i])
    if row_num < 1 or row_num - 1 > ff._MAX_ROW:
        return None
    return i, row_num - 1

old_try_match_ref = ff._try_match_ref

def new_try_match_ref(s: str, i: int):
    res = old_try_match_ref(s, i)
    if res is not None:
        end, ref = res
        if end < len(s) and ff._is_ident_char(s[end]):
            return None
    return res

ff._read_col = new_read_col
ff._read_row = new_read_row
ff._try_match_ref = new_try_match_ref

print("=Data1+A1 -> ", ff.adjust_a1_formula("=Data1+A1", 0, 1))
print("=Tax2024+1 -> ", ff.adjust_a1_formula("=Tax2024+1", 0, 1))
