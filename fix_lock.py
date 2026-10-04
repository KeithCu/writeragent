import re

def fix_lock():
    with open("plugin/calc/python/function.py", "r") as f:
        c = f.read()

    # Add lock definition
    c = c.replace('_PENDING_SPILL_LOCK = threading.Lock()', '_SPILL_REGISTRY_LOCK = threading.RLock()\n_PENDING_SPILL_LOCK = threading.Lock()')

    # Fix load_spill_registry_for_doc
    c = c.replace(
'''        for key, value in data.items():
            parts = key.split(":")
            if len(parts) == 2:
                sheet_name, coords = parts
                row_col = coords.split(",")
                if len(row_col) == 2:
                    frow, fcol = int(row_col[0]), int(row_col[1])
                    spill_coords = [(int(r), int(c)) for r, c in value]
                    SPILL_REGISTRY[(doc_key, sheet_name, frow, fcol)] = spill_coords''',
'''        with _SPILL_REGISTRY_LOCK:
            for key, value in data.items():
                parts = key.split(":")
                if len(parts) == 2:
                    sheet_name, coords = parts
                    row_col = coords.split(",")
                    if len(row_col) == 2:
                        frow, fcol = int(row_col[0]), int(row_col[1])
                        spill_coords = [(int(r), int(c)) for r, c in value]
                        SPILL_REGISTRY[(doc_key, sheet_name, frow, fcol)] = spill_coords'''
    )

    # Fix save_spill_registry_for_doc (which is different now due to WIP-Fixes)
    c = c.replace(
'''        doc_spills = {}
        for key, value in SPILL_REGISTRY.items():
            k_url, sheet_name, frow, fcol = key
            if k_url == doc_key:
                doc_spills[f"{sheet_name}:{frow},{fcol}"] = value''',
'''        doc_spills = {}
        with _SPILL_REGISTRY_LOCK:
            for key, value in SPILL_REGISTRY.items():
                k_url, sheet_name, frow, fcol = key
                if k_url == doc_key:
                    doc_spills[f"{sheet_name}:{frow},{fcol}"] = value'''
    )

    # Fix CalcSpillModifyListener.modified
    c = c.replace(
'''            with _undo_lock(doc):
                to_remove = []
                for key, value in list(SPILL_REGISTRY.items()):
                    doc_url, sheet_name, frow, fcol = key
                    # Bugfix: "" matched every unsaved workbook, so a modify on
                    # one untitled book cleared the other's spill cells when
                    # the sheet names matched. Callers pass the file URL or the
                    # lifecycle id. "" is not an identity.
                    if self.doc_url and doc_url == self.doc_url and sheet_name == self.sheet_name:
                        try:
                            cell = sheet.getCellByPosition(fcol, frow)
                            formula = cell.getFormula()
                            if not formula or not is_py_formula_text(str(formula)):
                                # Clear previously spilled cells
                                for r, c in value:
                                    if (r, c) != (frow, fcol):
                                        try:
                                            spill_cell = sheet.getCellByPosition(c, r)
                                            spill_cell.clearContents(23)
                                        except Exception:
                                            pass
                                    to_remove.append(key)
                            except Exception:
                                log.debug("Failed to inspect formula cell %r", key, exc_info=True)

                if to_remove:
                    for key in to_remove:
                        SPILL_REGISTRY.pop(key, None)
                if to_remove and doc is not None:
                    save_spill_registry_for_doc(doc)''',
'''            with _undo_lock(doc):
                to_remove = []
                with _SPILL_REGISTRY_LOCK:
                    for key, value in list(SPILL_REGISTRY.items()):
                        doc_url, sheet_name, frow, fcol = key
                        # Bugfix: "" matched every unsaved workbook, so a modify on
                        # one untitled book cleared the other's spill cells when
                        # the sheet names matched. Callers pass the file URL or the
                        # lifecycle id. "" is not an identity.
                        if self.doc_url and doc_url == self.doc_url and sheet_name == self.sheet_name:
                            try:
                                cell = sheet.getCellByPosition(fcol, frow)
                                formula = cell.getFormula()
                                if not formula or not is_py_formula_text(str(formula)):
                                    # Clear previously spilled cells
                                    for r, c in value:
                                        if (r, c) != (frow, fcol):
                                            try:
                                                spill_cell = sheet.getCellByPosition(c, r)
                                                spill_cell.clearContents(23)
                                            except Exception:
                                                pass
                                    to_remove.append(key)
                            except Exception:
                                log.debug("Failed to inspect formula cell %r", key, exc_info=True)

                    if to_remove:
                        for key in to_remove:
                            SPILL_REGISTRY.pop(key, None)
                if to_remove and doc is not None:
                    save_spill_registry_for_doc(doc)'''
    )

    # Fix perform_deferred_spill
    c = c.replace(
'''            reg_key = (live_key, sheet_name, formula_row, formula_col)

            # 1. Clear previously spilled cells
            previous_spills = SPILL_REGISTRY.get(reg_key, [])''',
'''            reg_key = (live_key, sheet_name, formula_row, formula_col)

            # 1. Clear previously spilled cells
            with _SPILL_REGISTRY_LOCK:
                previous_spills = SPILL_REGISTRY.get(reg_key, [])'''
    )
    c = c.replace(
'''            if num_rows == 0 or num_cols == 0:
                SPILL_REGISTRY[reg_key] = []
                save_spill_registry_for_doc(doc)
                return''',
'''            if num_rows == 0 or num_cols == 0:
                with _SPILL_REGISTRY_LOCK:
                    SPILL_REGISTRY[reg_key] = []
                save_spill_registry_for_doc(doc)
                return'''
    )

    c = c.replace(
'''            SPILL_REGISTRY[reg_key] = new_spills
            save_spill_registry_for_doc(doc)''',
'''            with _SPILL_REGISTRY_LOCK:
                SPILL_REGISTRY[reg_key] = new_spills
            save_spill_registry_for_doc(doc)'''
    )

    # Fix _prepare_auto_spill
    c = c.replace(
'''    reg_key = (doc_key, sheet_name, formula_row, formula_col)
    previous_spills = SPILL_REGISTRY.get(reg_key, [])
    prev_spill_set = set(previous_spills)''',
'''    reg_key = (doc_key, sheet_name, formula_row, formula_col)
    with _SPILL_REGISTRY_LOCK:
        previous_spills = SPILL_REGISTRY.get(reg_key, [])
    prev_spill_set = set(previous_spills)'''
    )

    with open("plugin/calc/python/function.py", "w") as f:
        f.write(c)

fix_lock()
