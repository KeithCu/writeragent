import re

with open("plugin/prompt_function.py", "r") as f:
    content = f.read()

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
    # Match the method and its body until the next def or end of class
    pattern = r"(\s+def " + method + r"\(self.*?\):.*?)(?=\n\s+def |\n\s+# |\Z)"
    content = re.sub(pattern, "", content, flags=re.DOTALL)

with open("plugin/prompt_function.py", "w") as f:
    f.write(content)
