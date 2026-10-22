#!/usr/bin/env python3
"""Byte-check moved ChatSession class for Step 3. Exit 1 on any mismatch."""
import ast
import difflib
import subprocess
import sys


EXPECTED_LAZY_IMPORTS = [
    "from plugin.doc.document_helpers import get_document_context_for_chat\n",
    "from plugin.framework.config import get_config\n",
    "from plugin.framework.constants import CHAT_DOCUMENT_CONTEXT_MAX_CHARS\n",
    "from plugin.framework.prompts import get_chat_system_prompt_for_document\n",
]


def extract_class_source(src: str, cls_name: str) -> str:
    tree = ast.parse(src)
    nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name]
    if not nodes:
        raise SystemExit(f"missing class {cls_name}")
    node = nodes[0]
    lines = src.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def main() -> int:
    old_src = subprocess.check_output(
        ["git", "show", "origin/master:plugin/chatbot/panel.py"]
    ).decode("utf-8")
    with open("plugin/chatbot/chat_session.py", encoding="utf-8") as f:
        new_src = f.read()

    old_class = extract_class_source(old_src, "ChatSession")
    new_class = extract_class_source(new_src, "ChatSession")

    if old_class != new_class:
        print("MISMATCH: ChatSession class differs between origin/master:panel.py and chat_session.py")
        sys.stdout.writelines(
            difflib.unified_diff(
                old_class.splitlines(True),
                new_class.splitlines(True),
                fromfile="origin/master:plugin/chatbot/panel.py:ChatSession",
                tofile="plugin/chatbot/chat_session.py:ChatSession",
            )
        )
        return 1

    # Verify that the four lazy imports inside refresh_document_context are present in order
    idx = 0
    for imp in EXPECTED_LAZY_IMPORTS:
        new_idx = new_class.find(imp, idx)
        if new_idx == -1:
            print(f"FAILED: missing lazy import in ChatSession: {imp.strip()}")
            return 1
        idx = new_idx + len(imp)

    print(f"OK: ChatSession extracted byte-identical ({len(new_class.encode('utf-8'))} bytes, {len(new_class.splitlines())} lines)")
    print("ALL CHECKS PASSED BYTE-FOR-BYTE!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
