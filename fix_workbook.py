with open("plugin/calc/python/workbook_lifecycle.py", "r") as f:
    c = f.read()

c = c.replace('clear_in_memory_spill_state(doc_url=url, lifecycle_key=lifecycle_key)', 'clear_in_memory_spill_state(lifecycle_key=lifecycle_key)')

with open("plugin/calc/python/workbook_lifecycle.py", "w") as f:
    f.write(c)
