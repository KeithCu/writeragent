import re

with open("plugin/calc/python/function.py", "r") as f:
    c = f.read()

conflict1 = '''<<<<<<< HEAD
        for key, value in SPILL_REGISTRY.items():
            k_url, sheet_name, frow, fcol = key
            if k_url == doc_key:
                doc_spills[f"{sheet_name}:{frow},{fcol}"] = value
        new_val = json.dumps(doc_spills)
        set_document_property(doc, "WriterAgentSpillRegistry", new_val)
        actual = get_document_property(doc, "WriterAgentSpillRegistry", "")
        if actual != new_val:
            log.warning("Spill registry write back mismatch: expected %r, got %r", new_val, actual)
=======
        with _SPILL_REGISTRY_LOCK:
            for key, value in SPILL_REGISTRY.items():
                k_url, sheet_name, frow, fcol = key
                if k_url == doc_key:
                    doc_spills[f"{sheet_name}:{frow},{fcol}"] = value
        set_document_property(doc, "WriterAgentSpillRegistry", json.dumps(doc_spills))
>>>>>>> 784734a7 (Ensure consistent spill registry lifecycle identity and thread safety)'''

resolved1 = '''        with _SPILL_REGISTRY_LOCK:
            for key, value in SPILL_REGISTRY.items():
                k_url, sheet_name, frow, fcol = key
                if k_url == doc_key:
                    doc_spills[f"{sheet_name}:{frow},{fcol}"] = value
        new_val = json.dumps(doc_spills)
        set_document_property(doc, "WriterAgentSpillRegistry", new_val)
        actual = get_document_property(doc, "WriterAgentSpillRegistry", "")
        if actual != new_val:
            log.warning("Spill registry write back mismatch: expected %r, got %r", new_val, actual)'''

c = c.replace(conflict1, resolved1)

conflict2 = '''<<<<<<< HEAD
        # Sheet listeners are keyed by lifecycle id, not the file URL, so an
        # unload that only matched doc_url left the dispatcher registered.
=======
        cancel_pending_spill_timers(lifecycle_key)
>>>>>>> 784734a7 (Ensure consistent spill registry lifecycle identity and thread safety)'''

resolved2 = '''        cancel_pending_spill_timers(lifecycle_key)
        # Sheet listeners are keyed by lifecycle id, not the file URL, so an
        # unload that only matched doc_url left the dispatcher registered.'''

c = c.replace(conflict2, resolved2)

with open("plugin/calc/python/function.py", "w") as f:
    f.write(c)
