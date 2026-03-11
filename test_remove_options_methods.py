import re

with open("plugin/options_handler.py", "r") as f:
    content = f.read()

# We'll see if `XServiceInfo` is actually imported.
import_pattern = r'from com\.sun\.star\.lang import XServiceInfo'

print("Is XServiceInfo imported?", bool(re.search(import_pattern, content)))
