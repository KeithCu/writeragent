"""Count plugin/writer sites that could use specialized_base helpers with byte-identical output (AST)."""
import ast
import sys
import pathlib
OLD = "Text to find and replace if target = 'search'."
hits = []
for f in sorted(pathlib.Path("plugin/writer").rglob("*.py")):
    if "embeddings" in f.parts or f.name == "specialized_base.py": continue
    for n in ast.walk(ast.parse(f.read_text())):
        if isinstance(n, ast.Dict) and all(isinstance(k, ast.Constant) for k in n.keys):
            ks = [k.value for k in n.keys]
            if ks[:3] == ["type", "enum", "description"] and isinstance(n.values[0], ast.Constant) and n.values[0].value == "string":
                hits.append(f"{f}:{n.lineno}: enum_param")
            if "target" in ks and "old_content" in ks:
                oc = n.values[ks.index("old_content")]
                if isinstance(oc, ast.Dict) and any(isinstance(v, ast.Constant) and v.value == OLD for v in oc.values):
                    hits.append(f"{f}:{n.lineno}: target_params")
        if isinstance(n, ast.JoinedStr) and "Invalid index" in ast.unparse(n) and "The document has" in ast.unparse(n):
            hits.append(f"{f}:{n.lineno}: index_error")
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("Invalid index") and "The document has" in n.value:
            hits.append(f"{f}:{n.lineno}: index_error")
if "-v" in sys.argv: print("\n".join(hits))
print(len(hits))
