import re

with open("plugin/prompt_function.py", "r") as f:
    content = f.read()

content = content.replace("except:\n            # Fallback to stdout", "except Exception:\n            # Fallback to stdout")

with open("plugin/prompt_function.py", "w") as f:
    f.write(content)
