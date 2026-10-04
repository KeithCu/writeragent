def get_result(getValue, getString):
    if getValue != 0:
        return getValue
    # We need to distinguish between numeric 0 and string "0".
    # Wait, the issue says "EvaluateFormula — numeric zero results returned as text "0" because getValue()!=0 else getString(); prefer numeric 0.0."
    # If getValue() == 0, can we just return getValue()?
    # If the formula returns "hello", getValue() is 0.0 and getString() is "hello".
    # So if getValue == 0, we can check if getString() == "" or not?
    # If we "prefer numeric 0.0", maybe we just return 0.0 unless getString() is a non-zero string?

    # Let's check `if getValue == 0 and getString not in ["0", "0.0", "", "FALSE"]:`
    # `return getString()`
    # `else: return 0.0`

    s = getString
    if s not in ("", "0", "0.0", "FALSE", "-0", "-0.0"):
        # if the string is empty but value is 0, it might be an empty cell.
        # But cell type is FORMULA. If it's "", it's a numeric zero or empty string?
        # A formula `=""` evaluates to string `""`. getValue() is 0.0, getString() is `""`.
        # If we return 0.0 for `""`, we might get 0.0 instead of `""`.
        # Is there a property on XCell for formula result type?
        pass
