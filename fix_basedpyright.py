import re

with open("plugin/writer/specialized/tables.py", "r") as f:
    content = f.read()

old_code = """                        for c_idx, val in enumerate(r_data):
                            if val is None or str(val) == "":
                                continue
                            try:
                                c_name = _cell_name(c_idx, r_idx)
                                cell = table.getCellByName(c_name)
                                cell.setString(str(val))
                                written[0] += 1
                            except Exception as exc:
                                log.debug("Failed to set cell %s during insert", c_name, exc_info=True)"""

new_code = """                        for c_idx, val in enumerate(r_data):
                            if val is None or str(val) == "":
                                continue
                            c_name = _cell_name(c_idx, r_idx)
                            try:
                                cell = table.getCellByName(c_name)
                                cell.setString(str(val))
                                written[0] += 1
                            except Exception:
                                log.debug("Failed to set cell %s during insert", c_name, exc_info=True)"""

content = content.replace(old_code, new_code)

with open("plugin/writer/specialized/tables.py", "w") as f:
    f.write(content)
