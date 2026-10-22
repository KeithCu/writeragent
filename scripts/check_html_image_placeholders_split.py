#!/usr/bin/env python3
"""Byte-check moved function, class, and constant definitions for the HTML image placeholders split. Exit 1 on any mismatch."""
import ast
import difflib
import subprocess
import sys
import textwrap


def full_def_text(src: str, name: str) -> str:
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name:
            chunk = "".join(lines[n.lineno - 1 : n.end_lineno])
            return textwrap.dedent(chunk)
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
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
    "_IMAGE_PLACEHOLDER_RE",
    "_EMPTY_IMG_RE",
    "_KEPT_IMAGE_PROPS",
    "_KeptImage",
    "_swap_image_placeholders",
    "_deleted_by_the_edit",
    "_restore_image_placeholders",
    "_keep_placeholder_images",
    "_is_recording_changes",
]


def main() -> int:
    base_ref = "origin/master"
    old_import = subprocess.check_output(
        ["git", "show", f"{base_ref}:plugin/writer/html_import.py"]
    ).decode("utf-8")

    with open("plugin/writer/image_placeholders.py", encoding="utf-8") as f:
        new_placeholders = f.read()

    all_ok = True
    print(f"=== Checking html_import -> image_placeholders (base: {base_ref}) ===")
    for name in MOVED_SYMBOLS:
        ok = check_pair(old_import, new_placeholders, name)
        if ok:
            print(f"OK: {name}")
        else:
            all_ok = False

    # Check for unexpected/new definitions in image_placeholders.py
    tree = ast.parse(new_placeholders)
    defined_names: set[str] = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined_names.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    defined_names.add(t.id)
        elif isinstance(n, ast.AnnAssign):
            if isinstance(n.target, ast.Name):
                defined_names.add(n.target.id)
    new_names = sorted(n for n in defined_names if n not in MOVED_SYMBOLS and n != "log")
    print("\n=== Checking for new names in image_placeholders.py ===")
    if new_names:
        print(f"FAILED: Found {len(new_names)} new names: {new_names}")
        all_ok = False
    else:
        print("OK: 0 new names (all symbols moved mechanically from origin/master)")

    if all_ok:
        total = len(MOVED_SYMBOLS)
        print(f"\nALL {total} MOVED BODIES MATCH BYTE-FOR-BYTE (0 new names)!")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
