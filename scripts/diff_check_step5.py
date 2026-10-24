#!/usr/bin/env python3
"""Byte-check moved method bodies for Step 5 (SendHandlersMixin / ToolCallingMixin composition).

Exit 1 on any mismatch.
"""
import ast
import difflib
import re
import subprocess
import sys
import textwrap


def body_texts(src: str, qual: str) -> list[str]:
    """Return body text for all functions matching qual in source order."""
    tree = ast.parse(src)
    if "." in qual:
        cls_name, meth = qual.split(".", 1)
        nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name]
        pool = nodes[0].body if nodes else tree.body
        name = meth
    else:
        pool = tree.body
        name = qual

    results = []
    lines = src.splitlines(keepends=True)
    for n in pool:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name:
            chunk = "".join(lines[n.body[0].lineno - 1 : n.end_lineno])
            results.append(textwrap.dedent(chunk))
    if not results:
        raise SystemExit(f"missing {qual}")
    return results


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


SEND_SIBLINGS = {
    "_transcribe_audio",
    "_run_unified_worker_drain_loop",
    "_do_send_direct_image",
    "_execute_direct_image_effect",
    "_do_send_via_agent_backend",
    "_retained_agent_backend",
    "_execute_agent_backend_effect",
    "_run_librarian",
    "_run_brainstorming",
    "_run_writing_plan",
    "_run_ppt_master",
    "_run_web_research",
    "_run_deep_web_research",
    "_execute_web_research_effect",
    "_get_mcp_url",
}

TOOL_SIBLINGS = {
    "_sm_state",
    "_do_send_chat_with_tools",
    "_refresh_active_tools_for_session",
    "_spawn_llm_worker",
    "_spawn_final_stream",
    "_create_event_from_stream_item",
    "_execute_effect",
    "_handle_stream_completion",
    "_handle_stream_stopped",
    "_handle_stream_error",
    "_on_tool_loop_approval_required",
    "_start_tool_calling_async",
}

STUBS_TO_PANEL = {
    "begin_inline_web_approval",
    "rerender_rich_text_session",
}

UNTOUCHED_HELPERS_SEND = [
    "_direct_image_source_arg",
    "_SendWorkerQueue",
    "_send_worker_queues",
    "_turn_session_or_stop",
    "_reset_active_run_flags",
]

UNTOUCHED_HELPERS_TOOL = [
    "_compact_bound_session",
    "_live_text",
]


def check_method(
    old_src: str,
    new_src: str,
    qual: str,
    siblings: set[str],
) -> bool:
    old_bodies = body_texts(old_src, qual)
    new_bodies = body_texts(new_src, qual)
    if len(old_bodies) != len(new_bodies):
        print(f"MISMATCH count for {qual}: old has {len(old_bodies)}, new has {len(new_bodies)}")
        return False

    all_ok = True
    for idx, (old, new) in enumerate(zip(old_bodies, new_bodies)):
        got = reverse_self_panel(new)
        label = f"{qual}[{idx}]" if len(old_bodies) > 1 else qual
        if got != old:
            print(f"MISMATCH {label}")
            sys.stdout.writelines(
                difflib.unified_diff(
                    old.splitlines(True),
                    got.splitlines(True),
                    fromfile="original",
                    tofile="reversed-new",
                )
            )
            all_ok = False
            continue

        # Sibling denylist: no sibling can be called as self._panel.<sibling>
        for sib in siblings:
            pattern = rf"\bself\._panel\.{sib}\b"
            if re.search(pattern, new):
                print(f"ERROR: sibling {sib} wrongly called as self._panel.{sib} in {label}")
                all_ok = False

        # Stub check: begin_inline_web_approval and rerender_rich_text_session must NOT be self.<name>
        for stub in STUBS_TO_PANEL:
            pattern = rf"\bself\.{stub}\b"
            if re.search(pattern, new):
                print(f"ERROR: stub {stub} wrongly called as self.{stub} in {label}")
                all_ok = False

    return all_ok


SEND_METHODS = [
    "SendHandlersMixin._transcribe_audio",
    "SendHandlersMixin._run_unified_worker_drain_loop",
    "SendHandlersMixin._do_send_direct_image",
    "SendHandlersMixin._execute_direct_image_effect",
    "SendHandlersMixin._do_send_via_agent_backend",
    "SendHandlersMixin._retained_agent_backend",
    "SendHandlersMixin._execute_agent_backend_effect",
    "SendHandlersMixin._run_librarian",
    "SendHandlersMixin._run_brainstorming",
    "SendHandlersMixin._run_writing_plan",
    "SendHandlersMixin._run_ppt_master",
    "SendHandlersMixin._run_web_research",
    "SendHandlersMixin._run_deep_web_research",
    "SendHandlersMixin._execute_web_research_effect",
    "SendHandlersMixin._get_mcp_url",
]

TOOL_METHODS = [
    "ToolCallingMixin._sm_state",
    "ToolCallingMixin.rerender_rich_text_session",
    "ToolCallingMixin._do_send_chat_with_tools",
    "ToolCallingMixin._refresh_active_tools_for_session",
    "ToolCallingMixin._spawn_llm_worker",
    "ToolCallingMixin._spawn_final_stream",
    "ToolCallingMixin._create_event_from_stream_item",
    "ToolCallingMixin._execute_effect",
    "ToolCallingMixin._handle_stream_completion",
    "ToolCallingMixin._handle_stream_stopped",
    "ToolCallingMixin._handle_stream_error",
    "ToolCallingMixin._on_tool_loop_approval_required",
    "ToolCallingMixin._start_tool_calling_async",
    "ToolCallingMixin.begin_inline_web_approval",
]


def check_untouched(old_src: str, new_src: str, qual: str) -> bool:
    old_bodies = body_texts(old_src, qual)
    new_bodies = body_texts(new_src, qual)
    if old_bodies != new_bodies:
        print(f"MISMATCH in untouched helper {qual}")
        sys.stdout.writelines(
            difflib.unified_diff(
                old_bodies[0].splitlines(True),
                new_bodies[0].splitlines(True),
                fromfile="original",
                tofile="new",
            )
        )
        return False
    return True


def main() -> int:
    old_send = subprocess.check_output(["git", "show", "origin/master:plugin/chatbot/send_handlers.py"]).decode("utf-8")
    with open("plugin/chatbot/send_handlers.py", encoding="utf-8") as f:
        new_send = f.read()

    old_tool = subprocess.check_output(["git", "show", "origin/master:plugin/chatbot/tool_loop.py"]).decode("utf-8")
    with open("plugin/chatbot/tool_loop.py", encoding="utf-8") as f:
        new_tool = f.read()

    all_ok = True

    # 1. SendHandlersMixin methods
    for qual in SEND_METHODS:
        ok = check_method(old_send, new_send, qual, SEND_SIBLINGS)
        if ok:
            print(f"OK: {qual}")
        else:
            all_ok = False

    # 2. ToolCallingMixin methods
    for qual in TOOL_METHODS:
        ok = check_method(old_tool, new_tool, qual, TOOL_SIBLINGS)
        if ok:
            print(f"OK: {qual}")
        else:
            all_ok = False

    # 3. Untouched helpers
    for helper in UNTOUCHED_HELPERS_SEND:
        ok = check_untouched(old_send, new_send, helper)
        if ok:
            print(f"OK (untouched helper): {helper}")
        else:
            all_ok = False

    for helper in UNTOUCHED_HELPERS_TOOL:
        ok = check_untouched(old_tool, new_tool, helper)
        if ok:
            print(f"OK (untouched helper): {helper}")
        else:
            all_ok = False

    if all_ok:
        print("ALL 29 METHOD BODIES AND UNTOUCHED HELPERS MATCH BYTE-FOR-BYTE!")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
