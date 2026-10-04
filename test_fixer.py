import re

with open("tests/calc/python/test_workbook_lifecycle.py", "r") as f:
    c = f.read()

# Ah! In the unload teardown in plugin/calc/python/workbook_lifecycle.py:
# _release_calc_state(self, session_ids: tuple[str, ...], doc_urls: tuple[str, ...], lifecycle_key: str, *, reset_sessions: bool)
# test_workbook_lifecycle sets `listener = _CalcPythonUnloadListener(..., lifecycle_key="key-spill")`
# So `lifecycle_key="key-spill"` is passed down.
# Let's inspect `clear_in_memory_spill_state` call inside `workbook_lifecycle.py`
