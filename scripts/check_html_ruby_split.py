#!/usr/bin/env python3
"""Byte-check moved function and class bodies for the HTML Ruby split. Exit 1 on any mismatch."""
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
                fromfile=f"master:{name}",
                tofile=f"new:{name}",
            )
        )
        return False
    return True


EXPORT_MOVES = [
    "_RubySpan",
    "_ruby_parts",
    "_inside_ruby_element",
    "_index_in_tag",
    "_find_contiguous",
    "_only_tags_or_ws",
    "_ruby_markup",
    "inject_ruby_into_html",
    "_iter_ruby_spans_in_paragraph",
    "iter_ruby_spans",
    "_ruby_spans_in_window",
    "_inject_ruby_from_model",
]

IMPORT_MOVES = [
    "_ImportRubySpan",
    "_visible_html_text",
    "extract_and_strip_ruby",
    "_go_right",
    "_set_ruby_on_range",
    "_prefix_char_count",
    "_ruby_skip",
    "_apply_ruby_spans",
]


def main() -> int:
    old_export = subprocess.check_output(
        ["git", "show", "origin/master:plugin/writer/html_export.py"]
    ).decode("utf-8")
    old_import = subprocess.check_output(
        ["git", "show", "origin/master:plugin/writer/html_import.py"]
    ).decode("utf-8")

    with open("plugin/writer/html_ruby.py", encoding="utf-8") as f:
        new_ruby = f.read()

    all_ok = True
    print("=== Checking html_export -> html_ruby ===")
    for name in EXPORT_MOVES:
        ok = check_pair(old_export, new_ruby, name)
        if ok:
            print(f"OK: {name}")
        else:
            all_ok = False

    print("\n=== Checking html_import -> html_ruby ===")
    for name in IMPORT_MOVES:
        ok = check_pair(old_import, new_ruby, name)
        if ok:
            print(f"OK: {name}")
        else:
            all_ok = False

    if all_ok:
        total = len(EXPORT_MOVES) + len(IMPORT_MOVES)
        print(f"\nALL {total} MOVED BODIES MATCH BYTE-FOR-BYTE!")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
