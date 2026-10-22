#!/usr/bin/env python3
"""Byte-check moved functions and assignments for range_copy split. Exit 1 on any mismatch."""
import ast
import difflib
import subprocess
import sys

MOVED_FUNCTIONS = [
    "_record_math_placeholder",
    "_copy_properties",
    "_paint_direct_formatting",
    "_supports_service",
    "_goto_doc_end",
    "_copy_field_into",
    "_copy_cell_xtext",
    "_writer_cell_position",
    "_writer_table_copy_layout",
    "_copy_table",
    "_copy_xtext_by_portions",
    "_copy_flow_into_xtext",
    "_anchor_offset",
    "_copy_anchored_graphics",
    "_copy_xtext_into_doc",
]


def function_source(src: str, name: str) -> str:
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return "".join(lines[n.lineno - 1 : n.end_lineno])
    raise SystemExit(f"missing function {name}")


def normalize_function(name: str, code: str) -> str:
    if name in ("_paint_direct_formatting", "_copy_xtext_into_doc"):
        lines = code.splitlines(keepends=True)
        lines[0] = lines[0].replace("  # pyright: ignore[reportUnusedFunction]", "")
        code = "".join(lines)
    if name == "_writer_cell_position":
        code = code.replace("``range_copy``", "``html_export``")
    return code


def check_function(old_src: str, new_src: str, name: str) -> bool:
    old = function_source(old_src, name)
    new = normalize_function(name, function_source(new_src, name))
    if new != old:
        print(f"MISMATCH in function: {name}")
        sys.stdout.writelines(
            difflib.unified_diff(
                old.splitlines(True),
                new.splitlines(True),
                fromfile=f"master:{name}",
                tofile=f"range_copy:{name}",
            )
        )
        return False
    return True


def extract_slice(src: str, start_str: str, end_str: str) -> str:
    start_pos = src.find(start_str)
    if start_pos == -1:
        raise SystemExit(f"missing start: {start_str!r}")
    end_pos = src.find(end_str, start_pos)
    if end_pos == -1:
        raise SystemExit(f"missing end: {end_str!r}")
    end_pos += len(end_str)
    return src[start_pos:end_pos]


def check_assignments(old_src: str, new_src: str) -> bool:
    # 1. Comment + _COPIED_CHAR_PROPERTIES + _COPIED_PARA_PROPERTIES
    start1 = "# Direct formatting the range copy carries into the temp document."
    end1 = '"ParaFirstLineIndent", "ParaAdjust", "ParaBackColor",\n)'
    old_slice1 = extract_slice(old_src, start1, end1)
    new_slice1 = extract_slice(new_src, start1, end1)
    if old_slice1 != new_slice1:
        print("MISMATCH in _COPIED_* assignments")
        sys.stdout.writelines(
            difflib.unified_diff(
                old_slice1.splitlines(True),
                new_slice1.splitlines(True),
                fromfile="master:_COPIED_*",
                tofile="range_copy:_COPIED_*",
            )
        )
        return False

    # 2. Comment + _ANCHORED_GRAPHIC_PROPS
    start2 = "# Same list as page._PAGE_GRAPHIC_PROPS."
    end2 = '"HyperLinkURL", "HyperLinkTarget", "Title", "Description",\n)'
    old_slice2 = extract_slice(old_src, start2, end2)
    new_slice2 = extract_slice(new_src, start2, end2)
    if old_slice2 != new_slice2:
        print("MISMATCH in _ANCHORED_GRAPHIC_PROPS assignment")
        sys.stdout.writelines(
            difflib.unified_diff(
                old_slice2.splitlines(True),
                new_slice2.splitlines(True),
                fromfile="master:_ANCHORED_GRAPHIC_PROPS",
                tofile="range_copy:_ANCHORED_GRAPHIC_PROPS",
            )
        )
        return False
    return True


def check_sentinels(src: str, module_name: str) -> bool:
    failed_target = "_FAILED: Any = object()"
    para_break = "_PARAGRAPH_BREAK = 0"
    if failed_target not in src:
        print(f"MISSING {failed_target} in {module_name}")
        return False
    if para_break not in src:
        print(f"MISSING {para_break} in {module_name}")
        return False
    return True


def check_html_export_purged(html_export_src: str) -> bool:
    tree = ast.parse(html_export_src)
    present_funcs = [
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    all_clean = True
    for fn in MOVED_FUNCTIONS:
        if fn in present_funcs:
            print(f"ERROR: {fn} still defined in html_export.py")
            all_clean = False
    return all_clean


def main() -> int:
    try:
        old_src = subprocess.check_output(
            ["git", "show", "origin/master:plugin/writer/html_export.py"]
        ).decode("utf-8")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"Failed to fetch baseline: {exc}")

    try:
        with open("plugin/writer/range_copy.py", encoding="utf-8") as f:
            range_copy_src = f.read()
    except OSError as exc:
        sys.exit(f"Failed to read plugin/writer/range_copy.py: {exc}")

    try:
        with open("plugin/writer/html_export.py", encoding="utf-8") as f:
            html_export_src = f.read()
    except OSError as exc:
        sys.exit(f"Failed to read plugin/writer/html_export.py: {exc}")

    all_ok = True
    print("=== Checking moved functions (byte-identical) ===")
    for fn in MOVED_FUNCTIONS:
        ok = check_function(old_src, range_copy_src, fn)
        if ok:
            print(f"OK: {fn}")
        else:
            all_ok = False

    print("\n=== Checking moved assignments (byte-identical) ===")
    if check_assignments(old_src, range_copy_src):
        print("OK: _COPIED_CHAR_PROPERTIES and _COPIED_PARA_PROPERTIES")
        print("OK: _ANCHORED_GRAPHIC_PROPS")
    else:
        all_ok = False

    print("\n=== Checking sentinels present in both modules ===")
    if check_sentinels(html_export_src, "html_export.py") and check_sentinels(
        range_copy_src, "range_copy.py"
    ):
        print("OK: _FAILED and _PARAGRAPH_BREAK in both modules")
    else:
        all_ok = False

    print("\n=== Checking html_export.py purged of moved functions ===")
    if check_html_export_purged(html_export_src):
        print("OK: All moved functions purged from html_export.py")
    else:
        all_ok = False

    if all_ok:
        print(f"\nALL {len(MOVED_FUNCTIONS)} FUNCTIONS AND 2 ASSIGNMENT BLOCKS MATCH BYTE-FOR-BYTE!")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
