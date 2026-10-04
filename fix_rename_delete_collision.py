import re

with open("plugin/calc/python/function.py", "r") as f:
    c = f.read()

funcs = '''def rename_spill_registry_sheet(doc: Any, old_sheet_name: str, new_sheet_name: str) -> None:
    """Update SPILL_REGISTRY keys when a sheet is renamed."""
    doc_key = _spill_registry_doc_key(doc)
    if not doc_key:
        return
    with _SPILL_REGISTRY_LOCK:
        for key in list(SPILL_REGISTRY.keys()):
            if key[0] == doc_key and key[1] == old_sheet_name:
                spills = SPILL_REGISTRY.pop(key)
                new_key = (doc_key, new_sheet_name, key[2], key[3])
                SPILL_REGISTRY[new_key] = spills
    save_spill_registry_for_doc(doc)


def delete_spill_registry_sheet(doc: Any, sheet_name: str) -> None:
    """Remove SPILL_REGISTRY keys when a sheet is deleted."""
    doc_key = _spill_registry_doc_key(doc)
    if not doc_key:
        return
    with _SPILL_REGISTRY_LOCK:
        for key in list(SPILL_REGISTRY.keys()):
            if key[0] == doc_key and key[1] == sheet_name:
                SPILL_REGISTRY.pop(key, None)
    save_spill_registry_for_doc(doc)

'''

c = c.replace('def _coerce_spill_value(', funcs + 'def _coerce_spill_value(')

check_func = '''def _check_spill_collisions(sheet: Any, formula_row: int, formula_col: int, num_rows: int, num_cols: int, prev_spill_set: set[tuple[int, int]]) -> bool:
    """Check if the required spill area intersects with occupied cells."""
    try:
        from com.sun.star.table.CellContentType import EMPTY
    except ImportError:
        EMPTY = cast("Any", 0)

    for r_idx in range(num_rows):
        for c_idx in range(num_cols):
            if r_idx == 0 and c_idx == 0:
                continue
            target_r = formula_row + r_idx
            target_c = formula_col + c_idx
            if target_r >= 1048576 or target_c >= 1024:
                log.debug("Spill: collision: target coordinate %r is out of bounds", (target_r, target_c))
                return True
            if (target_r, target_c) == (formula_row, formula_col):
                continue
            if (target_r, target_c) in prev_spill_set:
                continue
            cell = sheet.getCellByPosition(target_c, target_r)
            cell_type = cell.getType()
            if cell_type != EMPTY:
                log.debug("Spill: collision: cell at %r (type=%s, val=%r, formula=%r) is not empty", (target_r, target_c), cell_type, cell.getValue() or cell.getString(), cell.getFormula())
                return True
    return False

'''

c = c.replace('def _prepare_auto_spill(', check_func + 'def _prepare_auto_spill(')

old_prepare_loop = '''    try:
        from com.sun.star.table.CellContentType import EMPTY
    except ImportError:
        EMPTY = cast("Any", 0)

    for r_idx in range(num_rows):
        for c_idx in range(num_cols):
            if r_idx == 0 and c_idx == 0:
                continue
            target_r = formula_row + r_idx
            target_c = formula_col + c_idx
            if target_r >= 1048576 or target_c >= 1024:
                log.debug("Spill: collision: target coordinate %r is out of bounds", (target_r, target_c))
                return "#SPILL!"
            if (target_r, target_c) == (formula_row, formula_col):
                continue
            if (target_r, target_c) in prev_spill_set:
                continue
            cell = sheet.getCellByPosition(target_c, target_r)
            cell_type = cell.getType()
            if cell_type != EMPTY:
                log.debug("Spill: collision: cell at %r (type=%s, val=%r, formula=%r) is not empty", (target_r, target_c), cell_type, cell.getValue() or cell.getString(), cell.getFormula())
                return "#SPILL!"'''

new_prepare_loop = '''    if _check_spill_collisions(sheet, formula_row, formula_col, num_rows, num_cols, prev_spill_set):
        return "#SPILL!"'''

c = c.replace(old_prepare_loop, new_prepare_loop)

old_perform_clear = '''            # 1. Clear previously spilled cells
            with _SPILL_REGISTRY_LOCK:
                previous_spills = SPILL_REGISTRY.get(reg_key, [])
            for r, c in previous_spills:'''

new_perform_clear = '''            # 0. Re-check for collisions that may have occurred between locate and deferred write
            num_rows = len(grid)
            num_cols = max(len(row) for row in grid) if num_rows > 0 else 0
            with _SPILL_REGISTRY_LOCK:
                previous_spills = SPILL_REGISTRY.get(reg_key, [])
            prev_spill_set = set(previous_spills)

            if _check_spill_collisions(sheet, formula_row, formula_col, num_rows, num_cols, prev_spill_set):
                # We missed the collision window (user typed during the 0.1s debounce).
                # We cannot return #SPILL! to the matrix formula anymore, but we MUST NOT overwrite the user's new data.
                # Just clear our old spills and abandon writing the array.
                for r, c in previous_spills:
                    if (r, c) != (formula_row, formula_col):
                        try:
                            cell = sheet.getCellByPosition(c, r)
                            cell.clearContents(23)
                        except Exception:
                            pass
                with _SPILL_REGISTRY_LOCK:
                    SPILL_REGISTRY[reg_key] = []
                save_spill_registry_for_doc(doc)
                return

            # 1. Clear previously spilled cells
            for r, c in previous_spills:'''

c = c.replace(old_perform_clear, new_perform_clear)

c = c.replace('''            # 2. Determine bounds
            num_rows = len(grid)
            num_cols = max(len(row) for row in grid) if num_rows > 0 else 0
            if num_rows == 0 or num_cols == 0:''',
'''            # 2. Check bounds bounds (already determined above)
            if num_rows == 0 or num_cols == 0:''')

with open("plugin/calc/python/function.py", "w") as f:
    f.write(c)

with open("plugin/calc/sheets.py", "r") as f:
    s = f.read()

s = s.replace(
'''            sheet.setName(new_name)
            log.info("Sheet renamed from '%s' to '%s'.", old_name, new_name)
            return {"status": "ok", "message": f"Sheet renamed to '{new_name}'."}''',
'''            sheet.setName(new_name)
            try:
                from plugin.calc.python.function import rename_spill_registry_sheet
                rename_spill_registry_sheet(doc, old_name, new_name)
            except Exception:
                log.exception("Failed to update spill registry on sheet rename")
            log.info("Sheet renamed from '%s' to '%s'.", old_name, new_name)
            return {"status": "ok", "message": f"Sheet renamed to '{new_name}'."}'''
)

s = s.replace(
'''            sheets.removeByName(sheet_name)
            log.info("Sheet deleted: %s", sheet_name)
            return {"status": "ok", "message": f"Sheet '{sheet_name}' deleted."}''',
'''            sheets.removeByName(sheet_name)
            try:
                from plugin.calc.python.function import delete_spill_registry_sheet
                delete_spill_registry_sheet(doc, sheet_name)
            except Exception:
                log.exception("Failed to update spill registry on sheet delete")
            log.info("Sheet deleted: %s", sheet_name)
            return {"status": "ok", "message": f"Sheet '{sheet_name}' deleted."}'''
)

with open("plugin/calc/sheets.py", "w") as f:
    f.write(s)
