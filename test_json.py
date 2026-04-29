import json
import ast
import re

_LATEX_CLASH_WORDS = [
    # \a (Bell)
    "alpha", "approx", "ast", "angle", "arccos", "arcsin", "arctan", "arg", "aleph", "amalg",
    # \b (Backspace)
    "beta", "begin", "bar", "bot", "bullet", "bmod", "boldsymbol", "bigcup", "bigcap", "bigg", "backslash", "bf", "bm", "big", "bigodot", "bigoplus", "bigotimes", "biguplus", "bigvee", "bigwedge", "box", "breve", "buildrel", "bumpeq",
    # \f (Formfeed)
    "frac", "forall", "varphi", "fbox", "framebox", "flat", "frown",
    # \n (Newline)
    "nabla", "neq", "nu", "norm", "notin", "newline", "nRightarrow", "nleftarrow", "nLeftrightarrow", "natural", "ne", "nearrow", "neg", "ni", "not", "nwarrow",
    # \r (Carriage Return)
    "right", "rho", "rangle", "rightarrow", "rbrace", "rbrack", "rceil", "rfloor", "renewcommand", "require", "Rightarrow", "Re", "rightleftharpoons", "rm", "rtimes",
    # \t (Tab)
    "times", "text", "tau", "theta", "tilde", "tan", "tfrac", "triangle", "to", "textbf", "textit", "texttt", "top", "triangleright",
    # \v (Vertical Tab)
    "vec", "varepsilon", "varpi", "varrho", "varsigma", "vartheta", "vdash", "vee", "vert", "Vert"
]

_LATEX_CLASH_RE = re.compile(
    r"(?<!\\)\\(" + "|".join(_LATEX_CLASH_WORDS) + r")\b"
)

def _repair_latex_clashes(text: str) -> str:
    return _LATEX_CLASH_RE.sub(r"\\\\\1", text)

def test_it(raw_json):
    fixed = _repair_latex_clashes(raw_json)
    print("Original:", repr(raw_json))
    print("Fixed:   ", repr(fixed))
    try:
        parsed = json.loads(fixed)
        print("Parsed:  ", repr(parsed["content"]))
    except Exception as e:
        print("Error:   ", e)
    print("-" * 40)

test_it(r'{"content": "Here is \times and \nabla and \alpha."}')
test_it(r'{"content": "A genuine \n newline and \t tab."}')
test_it(r'{"content": "Properly escaped \\times should stay escaped."}')
