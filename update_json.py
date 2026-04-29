import re

with open("plugin/framework/json_utils.py", "r") as f:
    content = f.read()

import_idx = content.find("import ast")
assert import_idx != -1

new_imports = """import ast
import re

_LATEX_CLASH_WORDS = [
    # \\a (Bell)
    "alpha", "approx", "ast", "angle", "arccos", "arcsin", "arctan", "arg", "aleph", "amalg",
    # \\b (Backspace)
    "beta", "begin", "bar", "bot", "bullet", "bmod", "boldsymbol", "bigcup", "bigcap", "bigg", "backslash", "bf", "bm", "big", "bigodot", "bigoplus", "bigotimes", "biguplus", "bigvee", "bigwedge", "box", "breve", "buildrel", "bumpeq",
    # \\f (Formfeed)
    "frac", "forall", "varphi", "fbox", "framebox", "flat", "frown",
    # \\n (Newline)
    "nabla", "neq", "nu", "norm", "notin", "newline", "nRightarrow", "nleftarrow", "nLeftrightarrow", "natural", "ne", "nearrow", "neg", "ni", "not", "nwarrow",
    # \\r (Carriage Return)
    "right", "rho", "rangle", "rightarrow", "rbrace", "rbrack", "rceil", "rfloor", "renewcommand", "require", "Rightarrow", "Re", "rightleftharpoons", "rm", "rtimes",
    # \\t (Tab)
    "times", "text", "tau", "theta", "tilde", "tan", "tfrac", "triangle", "to", "textbf", "textit", "texttt", "top", "triangleright",
    # \\v (Vertical Tab)
    "vec", "varepsilon", "varpi", "varrho", "varsigma", "vartheta", "vdash", "vee", "vert", "Vert"
]

_LATEX_CLASH_RE = re.compile(
    r"(?<!\\\\)\\\\(" + "|".join(_LATEX_CLASH_WORDS) + r")\\b"
)

def _repair_latex_clashes(text: str) -> str:
    \"\"\"Escape backslashes for LaTeX commands that conflict with JSON escapes.\"\"\"
    return _LATEX_CLASH_RE.sub(r"\\\\\\\\\\1", text)
"""

content = content.replace("import ast", new_imports, 1)

old_loads = """    # 1. Standard attempt
    try:"""

new_loads = """    # Pre-process string to fix unescaped LaTeX commands that coincide with valid JSON escapes
    # e.g., "\\times" is natively treated as <tab>imes. We replace it with "\\\\times".
    if not strict:
        stripped = _repair_latex_clashes(stripped)

    # 1. Standard attempt
    try:"""

content = content.replace(old_loads, new_loads, 1)

with open("plugin/framework/json_utils.py", "w") as f:
    f.write(content)
