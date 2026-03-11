import re

with open("plugin/options_handler.py", "r") as f:
    content = f.read()

import sys
print(content.count("XServiceInfo"))

# Look at XContainerWindowEventHandler:
print("XContainerWindowEventHandler:", content.count("XContainerWindowEventHandler"))
