#!/usr/bin/env python3
"""Byte-check moved method bodies for Step 2 (PanelEventsController). Exit 1 on any mismatch."""
import ast
import difflib
import subprocess
import sys
import textwrap


def body_text(src: str, qual: str) -> str:
    tree = ast.parse(src)
    if "." in qual:
        cls_name, meth = qual.split(".", 1)
        nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name]
        pool = nodes[0].body if nodes else tree.body
        name = meth
    else:
        pool = tree.body
        name = qual
    for n in pool:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            lines = src.splitlines(keepends=True)
            chunk = "".join(lines[n.body[0].lineno - 1 : n.end_lineno])
            return textwrap.dedent(chunk)
    raise SystemExit(f"missing {qual}")


def reverse_self_panel(text: str) -> str:
    """self._panel.X -> self.X and bare self._panel -> self.

    Does not touch self._panel_teardown or self._panel_obj.
    """
    key, out, i = "self._panel", [], 0
    while True:
        j = text.find(key, i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        after = j + len(key)
        nxt = text[after] if after < len(text) else ""
        if nxt.isalnum() or nxt == "_":
            out.append(key)
            i = after
            continue
        out.append("self")
        i = after
    return "".join(out)


def check_pair(old_src: str, new_src: str, old_qual: str, new_qual: str, mode: str) -> bool:
    old = body_text(old_src, old_qual)
    new = body_text(new_src, new_qual)
    if mode == "panel":
        got = reverse_self_panel(new)
    elif mode == "static":
        got = new
    else:
        raise SystemExit(mode)
    if got != old:
        print(f"MISMATCH {old_qual} -> {new_qual}")
        sys.stdout.writelines(
            difflib.unified_diff(
                old.splitlines(True),
                got.splitlines(True),
                fromfile="original",
                tofile="reversed-new",
            )
        )
        return False
    return True


MOVES = [
    ("_relabel_button", "_relabel_button", "static"),
    ("SendButtonListener._mcp_subscribe_must_defer", "PanelEventsController._mcp_subscribe_must_defer", "static"),
    ("SendButtonListener._post_to_main_thread", "PanelEventsController._post_to_main_thread", "static"),
    ("SendButtonListener._subscribe_mcp_events", "PanelEventsController._subscribe_mcp_events", "panel"),
    ("SendButtonListener.begin_inline_web_approval", "PanelEventsController.begin_inline_web_approval", "panel"),
    ("SendButtonListener._open_web_search_change_dialog", "PanelEventsController._open_web_search_change_dialog", "panel"),
    ("SendButtonListener._finish_inline_web_approval", "PanelEventsController._finish_inline_web_approval", "panel"),
    ("SendButtonListener._on_http_server_started", "PanelEventsController._on_http_server_started", "panel"),
    ("SendButtonListener._on_http_server_stopped", "PanelEventsController._on_http_server_stopped", "panel"),
    ("SendButtonListener._post_server_status", "PanelEventsController._post_server_status", "panel"),
    ("SendButtonListener._on_mcp_request", "PanelEventsController._on_mcp_request", "panel"),
    ("SendButtonListener._on_mcp_result", "PanelEventsController._on_mcp_result", "panel"),
]


def main() -> int:
    old_src = subprocess.check_output(["git", "show", "origin/master:plugin/chatbot/panel.py"]).decode("utf-8")
    with open("plugin/chatbot/panel_events.py", encoding="utf-8") as f:
        new_src = f.read()

    all_ok = True
    for old_qual, new_qual, mode in MOVES:
        ok = check_pair(old_src, new_src, old_qual, new_qual, mode)
        if ok:
            print(f"OK: {old_qual} -> {new_qual}")
        else:
            all_ok = False

    if all_ok:
        print("ALL 12 MOVED BODIES MATCH BYTE-FOR-BYTE!")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
