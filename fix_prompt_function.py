import re

with open("plugin/prompt_function.py", "r") as f:
    content = f.read()

# Methods to remove (XAddIn methods)
methods_to_remove = [
    "getProgrammaticFunctionName",
    "getDisplayFunctionName",
    "getFunctionDescription",
    "getArgumentDescription",
    "getArgumentName",
    "hasFunctionWizard",
    "getArgumentCount",
    "getArgumentIsOptional",
    "getProgrammaticCategoryName",
    "getDisplayCategoryName",
    "getLocale",
    "setLocale",
    "load",
    "unload"
]

for method in methods_to_remove:
    pattern = r"(\s+def " + method + r"\(self.*?\):.*?)(?=\n\s+def |\n\s+# |\Z)"
    content = re.sub(pattern, "", content, flags=re.DOTALL)

# Let's also remove test_registration
pattern_test = r"(\n# Test function registration\ndef test_registration\(\).*)"
content = re.sub(pattern_test, "", content, flags=re.DOTALL)

with open("plugin/prompt_function.py", "w") as f:
    f.write(content)
