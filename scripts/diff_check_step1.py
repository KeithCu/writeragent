#!/usr/bin/env python3
"""Byte-check moved method bodies for Step 1 (PanelVoiceController). Exit 1 on any mismatch."""
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
    ("SendButtonListener.clear_pending_audio_wav", "PanelVoiceController.clear_pending_audio_wav", "panel"),
    ("SendButtonListener._kill_inflight_stt", "PanelVoiceController._kill_inflight_stt", "panel"),
    ("SendButtonListener.sync_audio_slice", "PanelVoiceController.sync_audio_slice", "panel"),
    ("SendButtonListener.exit_hands_free_record", "PanelVoiceController.exit_hands_free_record", "panel"),
    ("SendButtonListener.stop_during_take", "PanelVoiceController.stop_during_take", "panel"),
    ("SendButtonListener._apply_record_gesture", "PanelVoiceController._apply_record_gesture", "panel"),
    ("SendButtonListener._arm_record_hold_timer", "PanelVoiceController._arm_record_hold_timer", "panel"),
    ("SendButtonListener._spawn_record_hold_wait", "PanelVoiceController._spawn_record_hold_wait", "panel"),
    ("SendButtonListener._on_record_hold_elapsed", "PanelVoiceController._on_record_hold_elapsed", "panel"),
    ("SendButtonListener._release_open_microphone", "PanelVoiceController._release_open_microphone", "panel"),
    ("SendButtonListener._flush_sticky_restart", "PanelVoiceController._flush_sticky_restart", "panel"),
    ("SendButtonListener._begin_sticky_rerecord", "PanelVoiceController._begin_sticky_rerecord", "panel"),
    ("SendButtonListener._spawn_sticky_tts_wait", "PanelVoiceController._spawn_sticky_tts_wait", "panel"),
    ("SendButtonListener._poll_sticky_rerecord", "PanelVoiceController._poll_sticky_rerecord", "panel"),
    ("SendButtonListener._on_audio_auto_stop", "PanelVoiceController._on_audio_auto_stop", "panel"),
    ("SendButtonListener._on_audio_recorder_error", "PanelVoiceController._on_audio_recorder_error", "panel"),
    ("SendButtonListener._on_audio_silence_progress", "PanelVoiceController._on_audio_silence_progress", "panel"),
    ("_stop_tts", "_stop_tts", "static"),
]


def main() -> int:
    old_src = subprocess.check_output(["git", "show", "origin/master:plugin/chatbot/panel.py"]).decode("utf-8")
    with open("plugin/chatbot/panel_voice.py", encoding="utf-8") as f:
        new_src = f.read()

    all_ok = True
    for old_qual, new_qual, mode in MOVES:
        ok = check_pair(old_src, new_src, old_qual, new_qual, mode)
        if ok:
            print(f"OK: {old_qual} -> {new_qual}")
        else:
            all_ok = False

    if all_ok:
        print("ALL 18 MOVED BODIES MATCH BYTE-FOR-BYTE!")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
