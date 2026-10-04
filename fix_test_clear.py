import re

with open("tests/calc/python/test_workbook_lifecycle.py", "r") as f:
    c = f.read()

c = re.sub(
    r'( +)python_function\.SPILL_REGISTRY\[\("key-spill", "Sheet1", 0, 0\)\] = \[\(0, 1\)\]',
    r'\1python_function.SPILL_REGISTRY.clear()\n\1python_function.SPILL_REGISTRY[("key-spill", "Sheet1", 0, 0)] = [(0, 1)]',
    c
)

with open("tests/calc/python/test_workbook_lifecycle.py", "w") as f:
    f.write(c)
