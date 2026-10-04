# Wait, LibreOffice getValue() returns float. "FALSE" is a bool but evaluating it as float returns 0.0. "0" returns 0.0.
# If a cell evaluates to a string "Hello", getValue() returns 0.0 and getString() returns "Hello".
# In formulas.py we have:
# result = cell.getValue() if cell.getValue() != 0 else cell.getString()
# This incorrectly converts numeric 0.0 into string "0" because cell.getValue() != 0 is False for 0.0, so it uses cell.getString() which returns "0".
# It also incorrectly converts bool FALSE into string "FALSE" because cell.getValue() is 0.0 for FALSE, so it uses getString() "FALSE".
# If we "prefer numeric 0.0", we should just return 0.0 when it's numeric 0.
# So `result = cell.getValue() if cell.getValue() != 0.0 else (0.0 if cell.getString() in ("0", "0.0", "FALSE", "") else cell.getString())`
# Let's test this in Python:
print(0.0 if "0" in ("0", "0.0", "FALSE", "") else "0")
