import re

with open("plugin/options_handler.py", "r") as f:
    content = f.read()

# Let's count the occurrences of `XServiceInfo` in the file.
print("XServiceInfo occurrences:", content.count("XServiceInfo"))
print("XContainerWindowEventHandler occurrences:", content.count("XContainerWindowEventHandler"))
