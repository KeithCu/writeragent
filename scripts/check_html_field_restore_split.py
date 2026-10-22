#!/usr/bin/env python3
"""Byte-check moved function, class, and constant definitions for the HTML field restore split. Exit 1 on any mismatch."""
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
        if isinstance(n, ast.AnnAssign):
            if isinstance(n.target, ast.Name) and n.target.id == name:
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
    "_EXPORTED_FIELD_TITLES",
    "_FIELD_SPAN_RE",
    "_FIELD_TOKEN_RE",
    "_FIELD_TITLE_TO_SERVICE",
    "_PAGE_NUMBER_SUBTYPE_VALS",
    "rewrite_exported_field_spans",
    "_insert_restored_field",
    "_cursor_over",
    "_parse_field_token_props",
    "_check_no_remaining_field_placeholders",
    "_restore_fields_by_cursor",
    "_restore_field_placeholders",
]


def main() -> int:
    base_ref = sys.argv[1] if len(sys.argv) > 1 else "origin/master"
    try:
        old_import = subprocess.check_output(
            ["git", "show", f"{base_ref}:plugin/writer/html_import.py"]
        ).decode("utf-8")
    except subprocess.CalledProcessError:
        base_ref = "HEAD~1"
        old_import = subprocess.check_output(
            ["git", "show", f"{base_ref}:plugin/writer/html_import.py"]
        ).decode("utf-8")

    with open("plugin/writer/field_restore.py", encoding="utf-8") as f:
        new_field_restore = f.read()

    all_ok = True
    print(f"=== Checking html_import -> field_restore (base: {base_ref}) ===")
    for name in MOVED_SYMBOLS:
        ok = check_pair(old_import, new_field_restore, name)
        if ok:
            print(f"OK: {name}")
        else:
            all_ok = False

    # Also check exact slice byte comparison
    slice_start = "# XHTML export of Writer fields"
    next_fn = "def _restore_xtext_string"
    start_idx = old_import.find(slice_start)
    end_idx = old_import.find(next_fn, start_idx)
    old_slice = old_import[start_idx:end_idx].rstrip() + "\n"

    new_start_idx = new_field_restore.find(slice_start)
    new_slice = new_field_restore[new_start_idx:].rstrip() + "\n"

    if old_slice == new_slice:
        print(f"OK: raw slice comparison ({len(old_slice.encode('utf-8'))} bytes)")
    else:
        print("MISMATCH: raw slice comparison")
        all_ok = False

    if all_ok:
        total = len(MOVED_SYMBOLS)
        print(f"\nALL {total} MOVED BODIES MATCH BYTE-FOR-BYTE!")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
