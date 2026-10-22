#!/usr/bin/env python3
"""Diff-check script verifying mechanical byte-equality for content.py split.

Verifies:
1. Slices extracted from origin/master:plugin/writer/content.py match the corresponding
   sections in plugin/writer/get_document_content.py and plugin/writer/apply_document_content.py
   byte-for-byte.
2. AST verification: classes, methods, and functions are preserved with exact ordering.
3. Import cleanliness: verifies no references to plugin.writer.content remain in the tree.
"""

from __future__ import annotations

import ast
import subprocess
import sys


def main() -> int:
    print("Running diff-check verification for content.py mechanical split...")

    # Fetch origin/master snapshot of plugin/writer/content.py
    try:
        orig_bytes = subprocess.check_output(
            ["git", "show", "origin/master:plugin/writer/content.py"]
        )
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Failed to read origin/master:plugin/writer/content.py: {e}")
        return 1

    orig_lines = orig_bytes.decode("utf-8").splitlines(keepends=True)
    total_lines = len(orig_lines)
    print(f"Baseline origin/master:plugin/writer/content.py has {total_lines} lines.")

    # Slice 1: Apply helpers (lines 72-186, 1-based -> lines[71:186])
    s1 = "".join(orig_lines[71:186])
    # Slice 2: Get helper (lines 189-199, 1-based -> lines[188:199])
    s2 = "".join(orig_lines[188:199])
    # Slice 3: GetDocumentContent class (lines 202-312, 1-based -> lines[201:312])
    s3 = "".join(orig_lines[201:312])
    # Slice 4: Apply banner + ApplyDocumentContent (lines 315-1592, 1-based -> lines[314:1592])
    s4 = "".join(orig_lines[314:1592])

    with open("plugin/writer/get_document_content.py", "r", encoding="utf-8") as f:
        get_text = f.read()

    with open("plugin/writer/apply_document_content.py", "r", encoding="utf-8") as f:
        apply_text = f.read()

    # 1. Byte-for-byte slice verification
    errors = 0

    print("Checking Slice 1 (apply helpers, 72-186) in apply_document_content.py...")
    if s1 not in apply_text:
        print("FAIL: Slice 1 is not byte-identical in apply_document_content.py")
        errors += 1
    else:
        print(f"PASS: Slice 1 byte-identical ({len(s1.encode('utf-8'))} bytes)")

    print("Checking Slice 2 (_note_unlisted_tracked_changes, 189-199) in get_document_content.py...")
    if s2 not in get_text:
        print("FAIL: Slice 2 is not byte-identical in get_document_content.py")
        errors += 1
    else:
        print(f"PASS: Slice 2 byte-identical ({len(s2.encode('utf-8'))} bytes)")

    print("Checking Slice 3 (GetDocumentContent, 202-312) in get_document_content.py...")
    if s3 not in get_text:
        print("FAIL: Slice 3 is not byte-identical in get_document_content.py")
        errors += 1
    else:
        print(f"PASS: Slice 3 byte-identical ({len(s3.encode('utf-8'))} bytes)")

    print("Checking Slice 4 (ApplyDocumentContent + helpers, 315-1592) in apply_document_content.py...")
    if s4 not in apply_text:
        print("FAIL: Slice 4 is not byte-identical in apply_document_content.py")
        errors += 1
    else:
        print(f"PASS: Slice 4 byte-identical ({len(s4.encode('utf-8'))} bytes)")

    # 2. AST symbol & method order verification
    print("Verifying AST symbol preservation and method ordering...")
    orig_ast = ast.parse("".join(orig_lines))
    get_ast = ast.parse(get_text)
    apply_ast = ast.parse(apply_text)

    def get_definitions(tree: ast.AST) -> dict[str, ast.AST]:
        defs = {}
        for node in getattr(tree, "body", []):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defs[node.name] = node
        return defs

    orig_defs = get_definitions(orig_ast)
    get_defs = get_definitions(get_ast)
    apply_defs = get_definitions(apply_ast)

    for name, node in orig_defs.items():
        if name in get_defs:
            dest = "get_document_content.py"
            dest_node = get_defs[name]
        elif name in apply_defs:
            dest = "apply_document_content.py"
            dest_node = apply_defs[name]
        else:
            print(f"FAIL: Definition {name} missing from both destination files!")
            errors += 1
            continue

        if isinstance(node, ast.ClassDef):
            orig_methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            dest_methods = [n.name for n in dest_node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            if orig_methods != dest_methods:
                print(f"FAIL: Class {name} method mismatch in {dest}: {orig_methods} != {dest_methods}")
                errors += 1
            else:
                print(f"PASS: Class {name} in {dest} preserved with all {len(orig_methods)} methods in identical order")
        elif isinstance(node, ast.FunctionDef):
            print(f"PASS: Function {name} preserved in {dest}")

    # 3. Verify no lingering imports of plugin.writer.content
    print("Verifying absence of plugin.writer.content imports across codebase...")
    git_grep = subprocess.run(
        ["git", "grep", "-E", r"plugin\.writer\.content\b|from \. import content\b|import \.content\b"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Filter out check_content_split_diff.py itself
    remaining = [
        line for line in git_grep.stdout.splitlines()
        if "check_content_split_diff.py" not in line and "docs/archive/" not in line
    ]
    if remaining:
        print(f"FAIL: Found {len(remaining)} remaining references to plugin.writer.content:")
        for r in remaining:
            print("  " + r)
        errors += 1
    else:
        print("PASS: Zero remaining imports or patch targets of plugin.writer.content")

    if errors > 0:
        print(f"\nVerification FAILED with {errors} error(s).")
        return 1

    print("\nVerification SUCCESS: All slices 100% byte-identical and all symbols verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
