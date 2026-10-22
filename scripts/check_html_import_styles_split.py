#!/usr/bin/env python3
"""Byte-check moved function and class bodies for the HTML import styles split. Exit 1 on any mismatch."""
import ast
import difflib
import subprocess
import sys
import textwrap


def full_def_text(src: str, name: str) -> str:
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name:
            lines = src.splitlines(keepends=True)
            chunk = "".join(lines[n.lineno - 1 : n.end_lineno])
            return textwrap.dedent(chunk)
    raise SystemExit(f"missing {name}")


def check_pair(old_src: str, new_src: str, name: str) -> bool:
    old = full_def_text(old_src, name)
    new = full_def_text(new_src, name)
    if new != old:
        print(f"MISMATCH: {name}")
        sys.stdout.writelines(
            difflib.unified_diff(
                old.splitlines(True),
                new.splitlines(True),
                fromfile=f"base:{name}",
                tofile=f"new:{name}",
            )
        )
        return False
    return True


MOVED_SYMBOLS = [
    "_strip_data_lo_style",
    "_BlockLoStyleExtractor",
    "_extract_block_lo_styles",
    "_resolve_paragraph_style_token",
    "_apply_block_lo_styles",
]


def main() -> int:
    # Compare against origin/master
    base_ref = "origin/master"
    old_import = subprocess.check_output(
        ["git", "show", f"{base_ref}:plugin/writer/html_import.py"]
    ).decode("utf-8")

    with open("plugin/writer/html_import_styles.py", encoding="utf-8") as f:
        new_styles = f.read()

    all_ok = True
    print(f"=== Checking html_import -> html_import_styles (base: {base_ref}) ===")
    for name in MOVED_SYMBOLS:
        ok = check_pair(old_import, new_styles, name)
        if ok:
            print(f"OK: {name}")
        else:
            all_ok = False

    if all_ok:
        total = len(MOVED_SYMBOLS)
        print(f"\nALL {total} MOVED BODIES MATCH BYTE-FOR-BYTE!")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
