with open("plugin/calc/python/function.py", "r") as f:
    c = f.read()

conflict1 = '''<<<<<<< HEAD
        with _SPILL_REGISTRY_LOCK:
            for key, value in SPILL_REGISTRY.items():
                k_url, sheet_name, frow, fcol = key
                if k_url == doc_key:
                    doc_spills[f"{sheet_name}:{frow},{fcol}"] = value
=======
        for key, value in SPILL_REGISTRY.items():
            k_url, sheet_name, frow, fcol = key
            if k_url == doc_key:
                doc_spills[f"{sheet_name}:{frow},{fcol}"] = value
>>>>>>> origin/WIP-Fixes'''

resolved1 = '''        with _SPILL_REGISTRY_LOCK:
            for key, value in SPILL_REGISTRY.items():
                k_url, sheet_name, frow, fcol = key
                if k_url == doc_key:
                    doc_spills[f"{sheet_name}:{frow},{fcol}"] = value'''

c = c.replace(conflict1, resolved1)

with open("plugin/calc/python/function.py", "w") as f:
    f.write(c)
