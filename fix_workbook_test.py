with open("tests/calc/python/test_workbook_lifecycle.py", "r") as f:
    c = f.read()

c = c.replace('python_function.SPILL_REGISTRY[("file:///gone.ods", "Sheet1", 0, 0)] = [(0, 1)]', 'python_function.SPILL_REGISTRY[("key-spill", "Sheet1", 0, 0)] = [(0, 1)]')
c = c.replace('python_function.LOADED_DOCUMENTS.add("file:///gone.ods")', 'python_function.LOADED_DOCUMENTS.add("key-spill")')
c = c.replace('assert ("file:///gone.ods", "Sheet1", 0, 0) not in python_function.SPILL_REGISTRY', 'assert ("key-spill", "Sheet1", 0, 0) not in python_function.SPILL_REGISTRY')
c = c.replace('assert "file:///gone.ods" not in python_function.LOADED_DOCUMENTS', 'assert "key-spill" not in python_function.LOADED_DOCUMENTS')

with open("tests/calc/python/test_workbook_lifecycle.py", "w") as f:
    f.write(c)
