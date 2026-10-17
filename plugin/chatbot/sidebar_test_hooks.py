# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Debug-only sidebar hooks for mock-LLM native tests.

Release OXTs replace this module with a stub (see ``scripts/strip_code.py``).
Do not synthesize clicks: drive the same listeners as the widgets.

See docs/chat/rich-text-control-sidebar.md (Hooks).
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from weakref import WeakSet

from plugin.chatbot.panel import (
    StopButtonListener,
    notify_record_mouse_pressed,
    notify_record_mouse_released,
    notify_stop_mouse_pressed,
)
from plugin.chatbot.send_state import SendEvent, SendEventKind
from plugin.framework.constants import EXTENSION_ID_WRITERAGENT
from plugin.framework.worker_pool import run_in_background

log = logging.getLogger("writeragent.sidebar_test_hooks")

_HOOKS_UNAVAILABLE = "sidebar test hooks are not in release builds"
_DEBUG_SIDEBAR_PREFIX = "chatbot.debug_sidebar"
_DEBUG_SNAPSHOT_NAME = "writeragent_debug_sidebar.json"
# Packet K: URP tests cannot stream 24k HTML through the rich control and
# still land those bytes on ChatSession.messages. Inflate in soffice so
# estimate crosses the mock 32768×75% gate (and force-compact can cut).
_INFLATE_TARGET_TOKENS = 28000
_INFLATE_PAIR_TOKENS = 4000
_INFLATE_MAX_PAIRS = 16

# Debug-only. This module is replaced by a stub in release OXTs (no WeakSet).
_LIVE_CHAT_PANELS: WeakSet[Any] = WeakSet()
# Listeners created by the installed OXT factory may not share this WeakSet.
_LIVE_SEND_LISTENERS: list[Any] = []
# Last native-test ctx so URP fallbacks can executeDispatch without get_ctx().
_HOOK_CTX: Any = None
# Out-of-process mock-sidebar: controller bound to the live XDL ListBox over URP.
_URP_SLASH_POPUP: Any = None


def register_live_panel(element: Any) -> None:
    _require_debug()
    if element is not None:
        _LIVE_CHAT_PANELS.add(element)


def unregister_live_panel(element: Any) -> None:
    _require_debug()
    _LIVE_CHAT_PANELS.discard(element)


def iter_live_chat_panels() -> list[Any]:
    _require_debug()
    from plugin.chatbot.panel_factory import iter_debug_live_chat_panels

    merged: list[Any] = []
    seen: set[int] = set()
    for panel in list(iter_debug_live_chat_panels()) + list(_LIVE_CHAT_PANELS):
        ident = id(panel)
        if ident in seen:
            continue
        seen.add(ident)
        merged.append(panel)
    return merged


def debug_hooks_available() -> bool:
    """False in release OXTs (this file is omitted). True in dev trees.

    ``make test-mock-sidebar`` sets ``WRITERAGENT_UNO_THREAD_GUARD=0`` on soffice,
    so the thread_guard stub has no ``_designated_main_thread``. Still allow
    Packet G protocol dispatch in that process (``WRITERAGENT_TESTING=1``).
    """
    from plugin.framework import thread_guard as tg

    if hasattr(tg, "_designated_main_thread"):
        return True
    return os.environ.get("WRITERAGENT_TESTING") == "1"


def _require_debug() -> None:
    if not debug_hooks_available():
        raise RuntimeError(_HOOKS_UNAVAILABLE)


def debug_sidebar_snapshot_path() -> str:
    return os.path.join(tempfile.gettempdir(), _DEBUG_SNAPSHOT_NAME)


def _history_user_tail(sl: Any) -> str:
    session = getattr(sl, "session", None)
    if session is None:
        return ""
    db = getattr(session, "db", None)
    rows = db.get_messages() if db is not None else list(getattr(session, "messages", None) or [])
    from plugin.chatbot.history_db import message_to_dict

    for msg in reversed(list(rows)):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            return str(message_to_dict("user", content).get("content") or "")
        return str(content or "")
    return ""


def _session_snapshot_fields(sl: Any) -> dict[str, Any]:
    """Packet K: URP cannot read ``session.messages``; snapshot from soffice."""
    session = getattr(sl, "session", None) if sl is not None else None
    messages = list(getattr(session, "messages", None) or []) if session is not None else []
    compaction = getattr(session, "compaction", None) if session is not None else None
    return {
        "session_n_messages": len(messages),
        "session_roles": [str(m.get("role") or "") for m in messages if isinstance(m, dict)],
        "session_content_chars": [
            len(str(m.get("content") or "")) for m in messages if isinstance(m, dict)
        ],
        "has_compaction": compaction is not None,
        # Per-turn now (TurnController); None once the drain dropped the turn.
        "last_compact_reason": getattr(getattr(sl, "_turn", None), "_last_compact_reason", None) if sl is not None else None,
    }


def _inflate_session_history(session: Any) -> int:
    """Append large user/assistant pads onto ``session.messages`` (no UI, no DB).

    Streaming ``flood history`` through the rich control completed HTTP 200
    but did not grow the model-facing list enough for proactive compact or
    overflow retry (``nothing_to_compact`` / ``below_threshold``). Direct
    append is the URP-safe grow path.
    """
    if session is None:
        return 0
    messages = getattr(session, "messages", None)
    if not isinstance(messages, list):
        return 0
    from plugin.chatbot.compaction import estimate_tokens, messages_for_llm

    if not messages:
        messages.append({"role": "system", "content": "Packet K inflate"})
    added = 0
    while estimate_tokens(messages_for_llm(session)) < _INFLATE_TARGET_TOKENS and added < _INFLATE_MAX_PAIRS:
        added += 1
        messages.append({"role": "user", "content": "inflate history %d" % added})
        messages.append({"role": "assistant", "content": "x" * (_INFLATE_PAIR_TOKENS * 4)})
    return added



def _all_live_panels() -> list[Any]:
    from plugin.doc.live_panels import get_live_panel, iter_live_panel_uids

    panels = list(iter_live_chat_panels())
    for uid in iter_live_panel_uids():
        panel = get_live_panel(uid)
        if panel is not None and panel not in panels:
            panels.append(panel)
    return panels


def _voice_checkboxes() -> list[Any]:
    chks: list[Any] = []
    for panel in _all_live_panels():
        root = getattr(panel, "m_panelRootWindow", None)
        if root is None or not hasattr(root, "getControl"):
            continue
        chk = root.getControl("chk_voice")
        if chk is not None:
            chks.append(chk)
    return chks


def _tts_snapshot_fields() -> dict[str, Any]:
    """Sidebar Voice checkbox vs config (Settings → Speech audio.tts_enabled)."""
    from plugin.framework.config import get_config_bool_safe

    enabled = bool(get_config_bool_safe("audio.tts_enabled"))
    chk_state: int | None = None
    panels = _all_live_panels()
    for chk in _voice_checkboxes():
        if hasattr(chk, "getState"):
            chk_state = int(chk.getState())
            break
    fake_st = read_fake_tts_state()
    import plugin.audio.tts_service as tts_service

    is_speaking = bool(tts_service.is_speaking())
    return {
        "tts_enabled": enabled,
        "chk_voice_state": chk_state,
        "tts_sidebar_in_sync": (chk_state is None) or (bool(chk_state) == enabled),
        "tts_live_panels": len(panels),
        "tts_fake_mode": fake_st.get("mode", "off"),
        "tts_fake_spawn_count": int(fake_st.get("spawn_count", 0) or 0),
        "tts_fake_alive": list(fake_st.get("alive", []) or []),
        "tts_speaking": is_speaking,
        "stt_delay": _FAKE_STT_DELAY,
    }


_FAKE_TTS_NAME = "writeragent_fake_tts.json"
_FAKE_TTS_LOCK = threading.Lock()
_FAKE_TTS_MODE = "off"
_FAKE_TTS_SAVED: dict[str, Any] | None = None
_FAKE_TTS_ORIG: dict[str, Any] | None = None
_FAKE_TTS_PROCS: dict[int, subprocess.Popen[Any]] = {}
_FAKE_TTS_STATE: dict[str, Any] = {}



_FAKE_STT_NAME = "writeragent_fake_stt.json"
_FAKE_STT_LOCK = threading.Lock()
_FAKE_STT_MODE = "off"
_FAKE_STT_SAVED: dict[str, Any] | None = None
_FAKE_STT_ORIG: dict[str, Any] | None = None
_FAKE_STT_PROCS: dict[int, subprocess.Popen[Any]] = {}
_FAKE_STT_STATE: dict[str, Any] = {}
_FAKE_STT_DELAY = 0.0

def fake_stt_state_path() -> str:
    return os.path.join(tempfile.gettempdir(), _FAKE_STT_NAME)


def _read_json_dict(path: str, *, retry: bool = False) -> dict[str, Any]:
    attempts = 2 if retry else 1
    for attempt in range(attempts):
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
            return {}
        except (ValueError, OSError):
            if retry and attempt == 0:
                time.sleep(0.01)
                continue
            return {}
    return {}


def _remove_quiet(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


def read_fake_stt_state() -> dict[str, Any]:
    return _read_json_dict(fake_stt_state_path(), retry=True)

def _fake_stt_refresh_locked() -> None:
    path = fake_stt_state_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(_FAKE_STT_STATE, handle)
        handle.flush()
    os.replace(tmp_path, path)

def _fake_stt_note_spawn_locked(cmd: list[str], proc: subprocess.Popen[Any]) -> None:
    _FAKE_STT_STATE["spawn_count"] = _FAKE_STT_STATE.get("spawn_count", 0) + 1
    _FAKE_STT_PROCS[proc.pid] = proc
    _FAKE_STT_STATE["pids"] = list(_FAKE_STT_PROCS.keys())
    _FAKE_STT_STATE["argv0s"] = _FAKE_STT_STATE.get("argv0s", []) + [cmd[0]]
    _FAKE_STT_STATE["requested_basenames"] = _FAKE_STT_STATE.get("requested_basenames", []) + [os.path.basename(cmd[0])]
    _FAKE_STT_STATE["alive"] = [p for p, pr in _FAKE_STT_PROCS.items() if pr.poll() is None]

    current_alive = len(_FAKE_STT_STATE["alive"])
    if current_alive > 1:
        _FAKE_STT_STATE["overlap_spawns"] = _FAKE_STT_STATE.get("overlap_spawns", 0) + 1
    if current_alive > _FAKE_STT_STATE.get("max_alive", 0):
        _FAKE_STT_STATE["max_alive"] = current_alive

    _fake_stt_refresh_locked()

    # The URP test process polls this file for reaping (S5/S6), so publish
    # the exit when the child goes, not only on the next spawn.
    from plugin.framework.worker_pool import run_in_background

    run_in_background(_fake_stt_watch_exit, proc, name="fake-stt-exit-watch", dedicated=True)


def _fake_stt_watch_exit(proc: subprocess.Popen[Any]) -> None:
    returncode = proc.wait()
    with _FAKE_STT_LOCK:
        if _FAKE_STT_PROCS.get(proc.pid) is not proc:
            return
        _FAKE_STT_STATE["alive"] = [p for p, pr in _FAKE_STT_PROCS.items() if pr.poll() is None]
        _FAKE_STT_STATE.setdefault("returncodes", {})[str(proc.pid)] = returncode
        _fake_stt_refresh_locked()

def set_late_transcript_delay(delay: float) -> None:
    _require_debug()
    sl = send_listener()
    if sl is None:
        execute_debug_sidebar_op("FAKE_STT_DELAY_" + str(delay))
        return
    global _FAKE_STT_DELAY
    with _FAKE_STT_LOCK:
        _FAKE_STT_DELAY = delay

def _clear_fake_stt_in_process() -> None:
    global _FAKE_STT_MODE, _FAKE_STT_SAVED, _FAKE_STT_ORIG, _FAKE_STT_PROCS, _FAKE_STT_STATE

    with _FAKE_STT_LOCK:
        if _FAKE_STT_ORIG is None and _FAKE_STT_SAVED is None:
            return

        import plugin.audio.stt_service as stt_service
        import plugin.scripting.audio_recorder_service as audio_recorder_service

        for proc in _FAKE_STT_PROCS.values():
            if proc.poll() is None:
                proc.kill()

        if _FAKE_STT_ORIG is not None:
            setattr(audio_recorder_service.subprocess, "Popen", _FAKE_STT_ORIG["Popen"])
            setattr(stt_service.subprocess, "Popen", _FAKE_STT_ORIG["Popen_stt"])
            setattr(stt_service, "_run_cmd", _FAKE_STT_ORIG["_run_cmd"])
            import plugin.chatbot.audio_recorder as audio_recorder

            setattr(audio_recorder, "resolve_recording_python", _FAKE_STT_ORIG["resolve_recording_python"])
            setattr(stt_service, "transcribe", _FAKE_STT_ORIG["transcribe"])
            _FAKE_STT_ORIG = None

        if _FAKE_STT_SAVED is not None:
            from plugin.framework.config import set_config
            for k, v in _FAKE_STT_SAVED.items():
                set_config(k, v)
            _FAKE_STT_SAVED = None

        _remove_quiet(fake_stt_state_path())

        _FAKE_STT_MODE = "off"
        _FAKE_STT_PROCS = {}
        _FAKE_STT_STATE = {}

_FAKE_RECORDER_SCRIPT = """
import json, sys, threading, wave

mode, out = sys.argv[1], sys.argv[2]


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()


def write_wav():
    # One second of 16 kHz mono: zeros for silence, a loud square wave otherwise.
    frames = bytearray()
    for i in range(16000):
        value = 0 if mode == "silence" else (12000 if (i // 40) % 2 else -12000)
        frames += int(value).to_bytes(2, "little", signed=True)
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(bytes(frames))


stopped = threading.Event()


def read_stdin():
    for line in sys.stdin:
        if "stop" in line:
            break
    stopped.set()


threading.Thread(target=read_stdin, daemon=True).start()
emit({"status": "ready"})
auto = mode in ("silence", "noisy") and not stopped.wait(0.5)
if not auto:
    stopped.wait(120)
write_wav()
if auto:
    emit({"status": "auto_stopped", "path": out})
emit({"status": "ok", "path": out, "auto_stopped": auto})
"""


def _fake_recorder_output(cmd: list[Any]) -> str:
    """The ``--output`` WAV path the plugin passed to audio_record_main.py."""
    args = [str(part) for part in cmd]
    if "--output" in args and args.index("--output") + 1 < len(args):
        return args[args.index("--output") + 1]
    return os.path.join(tempfile.gettempdir(), "writeragent_fake_recorder.wav")


def _fake_recorder_script(mode: str, output_path: str) -> str:
    """Child source that speaks the audio_record_main.py stdout/stdin protocol."""
    return "import sys; sys.argv = %r\n%s" % (["fake_recorder", mode, output_path], _FAKE_RECORDER_SCRIPT)


def _apply_fake_stt(mode: str) -> None:
    _require_debug()
    if mode == "off":
        _clear_fake_stt_in_process()
        return

    from plugin.framework.config import get_config
    import plugin.audio.stt_service as stt_service
    import plugin.scripting.audio_recorder_service as audio_recorder_service
    import plugin.chatbot.audio_recorder as audio_recorder

    global _FAKE_STT_MODE, _FAKE_STT_SAVED, _FAKE_STT_ORIG, _FAKE_STT_STATE

    # The fake hooks the real recorder spawn. A Packet G stub (control file
    # skip=True, or the flags stub_recorder_child / inject_wav set on the live
    # recorder) never spawns, and the flags outlive the file, so drop both.
    audio_recorder.clear_stub_recorder_control()
    rec = _live_audio_recorder()
    if rec is not None:
        rec._test_skip_spawn = False
        rec._test_inject_wav = None
        rec._test_fail_start = None
        rec._test_missing_wav = False
        rec._test_hang_ready = False

    with _FAKE_STT_LOCK:
        if _FAKE_STT_SAVED is None:
            _FAKE_STT_SAVED = {
                "audio.stt_provider": get_config("audio.stt_provider"),
            }

        # Keep existing config for STT provider to allow testing both local and endpoint.
        # set_config("audio.stt_provider", "local")

        _FAKE_STT_MODE = mode

        if not _FAKE_STT_STATE or _FAKE_STT_STATE.get("mode") != mode:
            _FAKE_STT_STATE = {
                "mode": mode,
                "interpreter": sys.executable,
                "spawn_count": 0,
                "pids": [],
                "alive": [],
                "returncodes": {},
                "overlap_spawns": 0,
                "max_alive": 0,
                "argv0s": [],
                "requested_basenames": [],
                "saved": dict(_FAKE_STT_SAVED) if _FAKE_STT_SAVED is not None else None,
            }
        else:
            _FAKE_STT_STATE["mode"] = mode
            _FAKE_STT_STATE["saved"] = dict(_FAKE_STT_SAVED) if _FAKE_STT_SAVED is not None else None

        if _FAKE_STT_ORIG is None:
            _FAKE_STT_ORIG = {
                "Popen": audio_recorder_service.subprocess.Popen,
                "Popen_stt": stt_service.subprocess.Popen,
                "_run_cmd": stt_service._run_cmd,
                "resolve_recording_python": audio_recorder.resolve_recording_python,
                "transcribe": stt_service.transcribe,
            }

        orig_popen = _FAKE_STT_ORIG["Popen"]
        orig_run_cmd = _FAKE_STT_ORIG["_run_cmd"]

        if not getattr(audio_recorder_service.subprocess.Popen, "_writeragent_fake_stt", False):

            def fake_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
                cmd_arg = args[0] if args else kwargs.get("args")
                if isinstance(cmd_arg, (list, tuple)):
                    requested_cmd = list(cmd_arg)
                elif isinstance(cmd_arg, str):
                    requested_cmd = [cmd_arg]
                else:
                    return orig_popen(*args, **kwargs)

                is_recorder = "audio_record_main.py" in str(requested_cmd)

                if _FAKE_STT_MODE == "missing":
                    raise FileNotFoundError(errno.ENOENT, "No such file or directory", "recorder")

                script = "import time; time.sleep(120)"
                if _FAKE_STT_MODE == "crash":
                    script = "import sys; sys.exit(2)"
                elif is_recorder and _FAKE_STT_MODE in ("silence", "noisy", "hold"):
                    script = _fake_recorder_script(_FAKE_STT_MODE, _fake_recorder_output(requested_cmd))

                new_cmd = [sys.executable, "-c", script]
                new_args = list(args)
                if new_args:
                    new_args[0] = new_cmd
                else:
                    kwargs["args"] = new_cmd

                if is_recorder:
                    # Provide a pipe to the fake recorder so it doesn't block
                    kwargs["stdin"] = subprocess.PIPE
                    kwargs["stdout"] = subprocess.PIPE
                    kwargs["stderr"] = subprocess.PIPE

                    proc = orig_popen(*new_args, **kwargs)
                    with _FAKE_STT_LOCK:
                        _fake_stt_note_spawn_locked(requested_cmd, proc)
                    return proc
                return orig_popen(*args, **kwargs)

            setattr(fake_popen, "_writeragent_fake_stt", True)

            def fake_run_cmd(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str] | None:
                cmd = args[0]
                if "whisper_transcribe.py" in str(cmd):
                    if _FAKE_STT_DELAY > 0:
                        time.sleep(_FAKE_STT_DELAY)
                    if _FAKE_STT_MODE == "silence":
                        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='{"status": "ok", "text": ""}\n', stderr='')
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='{"status": "ok", "text": "Fake STT transcript."}\n', stderr='')
                return orig_run_cmd(*args, **kwargs)

            setattr(audio_recorder_service.subprocess, "Popen", fake_popen)
            setattr(stt_service.subprocess, "Popen", fake_popen)
            setattr(stt_service, "_run_cmd", fake_run_cmd)

            # Record only spawns the venv recorder when a venv python resolves.
            # The fake replaces the child command anyway, so hand back the
            # interpreter the fake children run under.
            def fake_resolve_recording_python(ctx: Any) -> tuple[str | None, str]:
                del ctx
                return sys.executable, ""

            setattr(audio_recorder, "resolve_recording_python", fake_resolve_recording_python)

            orig_transcribe = _FAKE_STT_ORIG["transcribe"]

            # Late-transcript cases (S9/S10/R10) also hold the endpoint STT
            # call; fake_run_cmd only covers local Whisper.
            def fake_transcribe(*args: Any, **kwargs: Any) -> str:
                if _FAKE_STT_DELAY > 0 and not stt_service.uses_local_stt():
                    time.sleep(_FAKE_STT_DELAY)
                return orig_transcribe(*args, **kwargs)

            setattr(stt_service, "transcribe", fake_transcribe)

        _fake_stt_refresh_locked()

def install_fake_stt(mode: str) -> None:
    _require_debug()
    if mode not in ("hold", "crash", "missing", "silence", "noisy"):
        raise ValueError(f"Invalid fake STT mode: {mode}")
    sl = send_listener()
    if sl is None:
        execute_debug_sidebar_op("FAKE_STT_" + mode.upper())
        return
    _apply_fake_stt(mode)

def _clear_fake_out_of_process(op: str, read_state: Callable[[], dict[str, Any]], path: str) -> None:
    execute_debug_sidebar_op(op)
    st = read_state()
    if os.path.isfile(path) and st.get("mode") != "off":
        pids = st.get("pids", [])
        for pid in pids:
            if sys.platform != "win32":
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        saved = st.get("saved")
        if isinstance(saved, dict):
            from plugin.framework.config import set_config

            for k, v in saved.items():
                set_config(k, v)
        _remove_quiet(path)


def clear_fake_stt() -> None:
    _require_debug()
    sl = send_listener()
    if sl is None:
        _clear_fake_out_of_process("FAKE_STT_OFF", read_fake_stt_state, fake_stt_state_path())
    else:
        _apply_fake_stt("off")


def fake_tts_state_path() -> str:
    return os.path.join(tempfile.gettempdir(), _FAKE_TTS_NAME)


def read_fake_tts_state() -> dict[str, Any]:
    return _read_json_dict(fake_tts_state_path(), retry=True)


def _fake_tts_refresh_locked() -> None:
    alive: list[int] = []
    rcs = _FAKE_TTS_STATE.setdefault("returncodes", {})
    for pid, proc in list(_FAKE_TTS_PROCS.items()):
        rc = proc.poll()
        if rc is None:
            alive.append(pid)
        else:
            rcs[str(pid)] = rc
    _FAKE_TTS_STATE["alive"] = alive
    _FAKE_TTS_STATE["mode"] = _FAKE_TTS_MODE
    _FAKE_TTS_STATE["interpreter"] = sys.executable
    _FAKE_TTS_STATE.setdefault("spawn_count", 0)
    _FAKE_TTS_STATE.setdefault("pids", [])
    _FAKE_TTS_STATE.setdefault("overlap_spawns", 0)
    _FAKE_TTS_STATE.setdefault("max_alive", 0)
    _FAKE_TTS_STATE.setdefault("argv0s", [])
    _FAKE_TTS_STATE.setdefault("requested_basenames", [])
    _FAKE_TTS_STATE.setdefault("speak_calls", 0)
    if _FAKE_TTS_SAVED is not None:
        _FAKE_TTS_STATE["saved"] = dict(_FAKE_TTS_SAVED)
    else:
        _FAKE_TTS_STATE.setdefault("saved", None)
    import plugin.audio.tts_service as tts_service

    _FAKE_TTS_STATE["speaking"] = bool(tts_service.is_speaking())

    path = fake_tts_state_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(_FAKE_TTS_STATE, handle)
        handle.flush()
    os.replace(tmp_path, path)


def _fake_tts_note_spawn_locked(requested: list[str], proc: subprocess.Popen[Any]) -> None:
    earlier_alive = any(p.poll() is None for p in _FAKE_TTS_PROCS.values())
    if earlier_alive:
        _FAKE_TTS_STATE["overlap_spawns"] = _FAKE_TTS_STATE.get("overlap_spawns", 0) + 1

    current_alive = sum(1 for p in _FAKE_TTS_PROCS.values() if p.poll() is None) + 1
    _FAKE_TTS_STATE["max_alive"] = max(_FAKE_TTS_STATE.get("max_alive", 0), current_alive)

    pid = proc.pid
    _FAKE_TTS_PROCS[pid] = proc
    _FAKE_TTS_STATE.setdefault("pids", []).append(pid)
    _FAKE_TTS_STATE["spawn_count"] = _FAKE_TTS_STATE.get("spawn_count", 0) + 1
    _FAKE_TTS_STATE.setdefault("argv0s", []).append(sys.executable)
    base = os.path.basename(requested[0]) if requested else ""
    _FAKE_TTS_STATE.setdefault("requested_basenames", []).append(base)
    _fake_tts_refresh_locked()


def _clear_fake_tts_in_process() -> None:
    global _FAKE_TTS_MODE, _FAKE_TTS_SAVED, _FAKE_TTS_ORIG, _FAKE_TTS_PROCS, _FAKE_TTS_STATE
    with _FAKE_TTS_LOCK:
        if _FAKE_TTS_ORIG is None and _FAKE_TTS_SAVED is None:
            return

        try:
            if _FAKE_TTS_ORIG is not None:
                _FAKE_TTS_ORIG["stop_speech"]()
            else:
                import plugin.audio.tts_service as tts_service

                tts_service.stop_speech()
        except Exception:
            log.debug("clear_fake_tts: stop_speech failed", exc_info=True)

        for proc in _FAKE_TTS_PROCS.values():
            if proc.poll() is None:
                proc.kill()

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if all(p.poll() is not None for p in _FAKE_TTS_PROCS.values()):
                break
            time.sleep(0.02)

        if _FAKE_TTS_ORIG is not None:
            import plugin.audio.tts_service as tts_service

            setattr(tts_service.subprocess, "Popen", _FAKE_TTS_ORIG["Popen"])
            setattr(tts_service.shutil, "which", _FAKE_TTS_ORIG["which"])
            setattr(tts_service, "stop_speech", _FAKE_TTS_ORIG["stop_speech"])
            setattr(tts_service, "_speak_system", _FAKE_TTS_ORIG["speak_system"])
            setattr(tts_service, "_end_utterance", _FAKE_TTS_ORIG["end_utterance"])
            _FAKE_TTS_ORIG = None

        if _FAKE_TTS_SAVED is not None:
            from plugin.framework.config import set_config

            for k, v in _FAKE_TTS_SAVED.items():
                set_config(k, v)
            _FAKE_TTS_SAVED = None

        _remove_quiet(fake_tts_state_path())

        _FAKE_TTS_MODE = "off"
        _FAKE_TTS_PROCS = {}
        _FAKE_TTS_STATE = {}


def _apply_fake_tts(mode: str) -> None:
    """Apply fake TTS backend.

    The child is a real process so stop_speech's kill is the production kill,
    the argv is rewritten so no player binary and no audio device is opened,
    and the state file exists because a URP SNAPSHOT loop hangs the pipe.
    """
    _require_debug()
    if mode == "off":
        _clear_fake_tts_in_process()
        return

    from plugin.framework.config import get_config, set_config
    import plugin.audio.tts_service as tts_service

    global _FAKE_TTS_MODE, _FAKE_TTS_SAVED, _FAKE_TTS_ORIG, _FAKE_TTS_STATE

    with _FAKE_TTS_LOCK:
        if _FAKE_TTS_SAVED is None:
            _FAKE_TTS_SAVED = {
                "audio.tts_enabled": get_config("audio.tts_enabled"),
                "audio.tts_provider": get_config("audio.tts_provider"),
                "audio.tts_sentence_mode": get_config("audio.tts_sentence_mode"),
            }

        set_config("audio.tts_enabled", True)
        set_config("audio.tts_provider", "system")
        set_config("audio.tts_sentence_mode", False)

        _FAKE_TTS_MODE = mode

        if not _FAKE_TTS_STATE or _FAKE_TTS_STATE.get("mode") != mode:
            _FAKE_TTS_STATE = {
                "mode": mode,
                "interpreter": sys.executable,
                "spawn_count": 0,
                "pids": [],
                "alive": [],
                "returncodes": {},
                "overlap_spawns": 0,
                "max_alive": 0,
                "argv0s": [],
                "requested_basenames": [],
                "speak_calls": 0,
                "speaking": False,
                "saved": dict(_FAKE_TTS_SAVED) if _FAKE_TTS_SAVED is not None else None,
            }
        else:
            _FAKE_TTS_STATE["mode"] = mode
            _FAKE_TTS_STATE["saved"] = dict(_FAKE_TTS_SAVED) if _FAKE_TTS_SAVED is not None else None

        if _FAKE_TTS_ORIG is None:
            _FAKE_TTS_ORIG = {
                "Popen": tts_service.subprocess.Popen,
                "which": tts_service.shutil.which,
                "stop_speech": tts_service.stop_speech,
                "speak_system": tts_service._speak_system,
                "end_utterance": tts_service._end_utterance,
            }

        orig_popen = _FAKE_TTS_ORIG["Popen"]
        orig_which = _FAKE_TTS_ORIG["which"]
        orig_stop_speech = _FAKE_TTS_ORIG["stop_speech"]
        orig_speak_system = _FAKE_TTS_ORIG["speak_system"]
        orig_end_utterance = _FAKE_TTS_ORIG["end_utterance"]

        if not getattr(tts_service.subprocess.Popen, "_writeragent_fake_tts", False):
            def fake_which(cmd: str, *args: Any, **kwargs: Any) -> str | None:
                if _FAKE_TTS_MODE == "missing":
                    return None
                if _FAKE_TTS_MODE in ("hold", "crash"):
                    if os.path.basename(cmd) == "espeak":
                        return sys.executable
                return orig_which(cmd, *args, **kwargs)

            setattr(fake_which, "_writeragent_fake_tts", True)

            def fake_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
                if _FAKE_TTS_MODE == "missing":
                    cmd_arg = args[0] if args else kwargs.get("args")
                    target_name = "tts"
                    if isinstance(cmd_arg, (list, tuple)) and cmd_arg:
                        target_name = str(cmd_arg[0])
                    elif isinstance(cmd_arg, str):
                        target_name = cmd_arg
                    raise FileNotFoundError(errno.ENOENT, "No such file or directory", target_name)

                if _FAKE_TTS_MODE == "hold":
                    script = "import time; time.sleep(120)"
                elif _FAKE_TTS_MODE == "crash":
                    script = "import sys; sys.exit(2)"
                else:
                    return orig_popen(*args, **kwargs)

                cmd_arg = args[0] if args else kwargs.get("args")
                if isinstance(cmd_arg, (list, tuple)):
                    requested_cmd = list(cmd_arg)
                elif isinstance(cmd_arg, str):
                    requested_cmd = [cmd_arg]
                else:
                    requested_cmd = []

                new_cmd = [sys.executable, "-c", script]
                new_args = list(args)
                if new_args:
                    new_args[0] = new_cmd
                else:
                    kwargs["args"] = new_cmd

                proc = orig_popen(*new_args, **kwargs)
                with _FAKE_TTS_LOCK:
                    _fake_tts_note_spawn_locked(requested_cmd, proc)
                return proc

            setattr(fake_popen, "_writeragent_fake_tts", True)

            def fake_end_utterance(generation: int) -> bool:
                # Paired with fake_stop_speech: the check must not run until the rewind is published.
                with _FAKE_TTS_LOCK:
                    return orig_end_utterance(generation)

            setattr(fake_end_utterance, "_writeragent_fake_tts", True)

            def fake_stop_speech(*args: Any, **kwargs: Any) -> None:
                # What was wrong: on_complete was skipped after a fake hold/stop even though the child was reaped.
                # How it happened: stop_speech bumps _speech_generation and SIGKILLs before it returns.
                # The tts-speak thread wakes from communicate/wait, _end_utterance sees the bump, returns
                # False, and never calls on_complete. The rewind below that call ran too late. A slow
                # tts-cleanup thread start (CI xdist) makes the window likely. The 2s wait cannot help.
                # Why this fixes it: the rewind is published before this lock is released, and
                # fake_end_utterance takes the same lock, so the worker observes the restored generation.
                with _FAKE_TTS_LOCK:
                    try:
                        cur_gen = getattr(tts_service, "_speech_generation", None)
                        orig_stop_speech(*args, **kwargs)
                        if cur_gen is not None:
                            tts_service._speech_generation = cur_gen
                    finally:
                        for p in _FAKE_TTS_PROCS.values():
                            if p.poll() is None:
                                p.kill()
                        for _ in range(50):
                            if all(p.poll() is not None for p in _FAKE_TTS_PROCS.values()):
                                break
                            time.sleep(0.01)
                        _fake_tts_refresh_locked()

            setattr(fake_stop_speech, "_writeragent_fake_tts", True)

            def fake_speak_system(*args: Any, **kwargs: Any) -> None:
                with _FAKE_TTS_LOCK:
                    _FAKE_TTS_STATE["speak_calls"] = _FAKE_TTS_STATE.get("speak_calls", 0) + 1
                    _fake_tts_refresh_locked()
                try:
                    orig_speak_system(*args, **kwargs)
                finally:
                    with _FAKE_TTS_LOCK:
                        _fake_tts_refresh_locked()

                    def _poll_idle() -> None:
                        for _ in range(100):
                            time.sleep(0.02)
                            with _FAKE_TTS_LOCK:
                                if _FAKE_TTS_MODE == "off":
                                    break
                            if not tts_service.is_speaking():
                                with _FAKE_TTS_LOCK:
                                    if _FAKE_TTS_MODE != "off":
                                        _fake_tts_refresh_locked()
                                break

                    run_in_background(_poll_idle)

            setattr(fake_speak_system, "_writeragent_fake_tts", True)

            setattr(tts_service.shutil, "which", fake_which)
            setattr(tts_service.subprocess, "Popen", fake_popen)
            setattr(tts_service, "stop_speech", fake_stop_speech)
            setattr(tts_service, "_speak_system", fake_speak_system)
            setattr(tts_service, "_end_utterance", fake_end_utterance)

        _fake_tts_refresh_locked()


def install_fake_tts(mode: str) -> None:
    _require_debug()
    if mode not in ("hold", "crash", "missing"):
        raise ValueError(f"Invalid fake TTS mode: {mode}")
    sl = send_listener()
    if sl is None:
        execute_debug_sidebar_op("FAKE_TTS_" + mode.upper())
        return
    _apply_fake_tts(mode)


def clear_fake_tts() -> None:
    _require_debug()
    sl = send_listener()
    if sl is None:
        _clear_fake_out_of_process("FAKE_TTS_OFF", read_fake_tts_state, fake_tts_state_path())
    else:
        _apply_fake_tts("off")


def _write_debug_snapshot(sl: Any) -> dict[str, Any]:
    send = sl.sidebar_state.send if sl is not None else None
    audio = sl.sidebar_state.audio if sl is not None else None
    rec = getattr(sl, "audio_recorder", None) if sl is not None else None
    # The paint counters live on RichTextChatWidget, not on the raw response control.
    rich_widget = getattr(sl, "rich_text_widget", None) if sl is not None else None
    data: dict[str, Any] = {
        "is_busy": bool(getattr(send, "is_busy", False)),
        "is_recording": bool(getattr(send, "is_recording", False)),
        "has_text": bool(getattr(send, "has_text", False)),
        "has_audio": bool(getattr(send, "has_audio", False)),
        "audio_supported": bool(getattr(send, "audio_supported", False)),
        "send_label": _control_label(getattr(sl, "send_control", None)) if sl is not None else "",
        "stop_label": _control_label(getattr(sl, "stop_control", None)) if sl is not None else "",
        "status": getattr(audio, "status", "idle") if audio is not None else "idle",
        "error_message": getattr(audio, "error_message", None) if audio is not None else None,
        "stub_start_count": int(getattr(rec, "_stub_start_count", 0) or 0),
        "history_user_tail": _history_user_tail(sl) if sl is not None else "",
        "approval_active": bool(getattr(sl, "_approval_event", None)) if sl is not None else False,
        **_slash_snapshot_fields(sl),
        **_session_snapshot_fields(sl),
        **_tts_snapshot_fields(),
        "slash_lru": _slash_lru_names(),
        "paint_session_count": int(getattr(rich_widget, "_debug_paint_session_count", 0) or 0),
        "stream_session_count": int(getattr(rich_widget, "_debug_stream_session_count", 0) or 0),
    }
    with open(debug_sidebar_snapshot_path(), "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    return data


def _read_debug_snapshot() -> dict[str, Any]:
    return _read_json_dict(debug_sidebar_snapshot_path())


def _parse_debug_sidebar_command(command: str) -> tuple[str, str]:
    """Split ``chatbot.debug_sidebar.<OP>`` / ``?OP&uid=`` into ``(op, uid)``.

    ``DispatchHandler`` joins Path+Query with ``.``. LO often leaves the op
    in Path as ``chatbot.debug_sidebar?INFLATE_HISTORY`` (Query empty). The
    URP client may append ``&uid=<RuntimeUID>`` so soffice inflate/snapshot
    bind that deck — not leftover Calc from ``getCurrentComponent()``.
    Parse *before* ``upper()`` so a hyphenated uid is not rewritten.
    """
    rest = command
    if command.startswith(_DEBUG_SIDEBAR_PREFIX):
        rest = command[len(_DEBUG_SIDEBAR_PREFIX) :]
    rest = rest.lstrip(".?")
    op_part, _, extra = rest.partition("&")
    op = (op_part or "SNAPSHOT").upper().replace("-", "_")
    uid = ""
    for part in extra.split("&"):
        key, sep, val = part.partition("=")
        if sep and key.lower() == "uid":
            uid = val
    return op, uid


_SAVED_REQUEST_TIMEOUT: Any = None


def handle_debug_sidebar_command(command: str) -> None:
    """Run inside soffice (protocol handler). Packet G URP FSM ops + OPEN_CALC.

    ``DispatchHandler`` runs on the URP thread. ``WRITERAGENT_TESTING=1`` makes
    ``QueueExecutor.post`` inline, so Stop Rec used to start ``_do_send`` off
    the VCL thread and freeze on ``Getting document...``. Marshal FSM work onto
    the listener's executor (VCL) before StartSendEffect posts the drain.
    ``OPEN_CALC`` uses the same post-to-VCL rule: factory ``scalc`` over URP
    after a Writer deck never returns.
    """
    _require_debug()
    adopt_runtime_send_listeners()
    op, target_uid = _parse_debug_sidebar_command(command)
    # Prefer the URP client's document uid (executeDispatch frame). #802 bound
    # INFLATE to soffice getCurrentComponent(); leftover Calc after Packet P /
    # E12 is often still current there, so pads missed the Writer Send deck.
    sl = send_listener_for_uid(target_uid) if target_uid else None
    if sl is None:
        sl = (
            _listener_for_current_doc()
            if op == "INFLATE_HISTORY"
            else _listener_with_slash_popup(send_listener())
        )
    if op == "SNAPSHOT":
        _write_debug_snapshot(sl)
        return
    # Packet K: mutate ChatSession in soffice (URP cannot touch .messages).
    if op == "INFLATE_HISTORY":
        if sl is None:
            log.warning("debug_sidebar %s: no SendButtonListener", op)
            _write_debug_snapshot(None)
            return
        _inflate_session_history(getattr(sl, "session", None))
        ms = getattr(sl, "model_selector", None)
        if ms is not None:
            from plugin.chatbot.dialogs import set_control_text
            from plugin.framework.client.model_fetcher import set_text_model

            set_control_text(ms, "writeragent-mock")
            set_text_model("writeragent-mock", update_lru=False)
        _write_debug_snapshot(sl)
        return
    # Factory scalc over URP after a Writer deck never returns (Dummy-thread
    # load vs VCL). Post the load onto soffice VCL; the URP client polls.
    if op == "OPEN_CALC":
        log.info("debug_sidebar OPEN_CALC posting factory/scalc to VCL sl=%s", sl is not None)
        _post_to_soffice_vcl(_load_visible_calc_factory, sl=sl)
        _write_debug_snapshot(sl)
        return
    # Packet O: fake vision backend inside soffice (the OCR venv is not installed in CI).
    if op.startswith("FAKE_VISION_"):
        _apply_fake_vision(op[len("FAKE_VISION_"):].lower())
        _write_debug_snapshot(sl)
        return
    if op == "KICK_PEERS":
        # Packet P URP: queues live in soffice; the test-process kick is a no-op.
        # Post to VCL via _post_to_soffice_vcl so peer sends drain on the
        # main UI thread. handle_debug_sidebar_command runs on a URP bridge
        # thread; calling kick_pending_peer_starts() there runs
        # start_extracted_peer_send and the stream drain without a working
        # VCL event pump and deadlocks URP.
        from plugin.doc.peer_message import kick_pending_peer_starts

        _post_to_soffice_vcl(kick_pending_peer_starts, sl=sl)
        _write_debug_snapshot(sl)
        return
    # Packet I5: a short request_timeout read by the soffice sidebar (URP set_config is
    # not seen there); RESTORE puts back the value SHORT replaced.
    if op in ("REQUEST_TIMEOUT_SHORT", "REQUEST_TIMEOUT_RESTORE"):
        global _SAVED_REQUEST_TIMEOUT
        from plugin.framework.config import get_config, set_config

        if op == "REQUEST_TIMEOUT_SHORT":
            if _SAVED_REQUEST_TIMEOUT is None:
                _SAVED_REQUEST_TIMEOUT = get_config("request_timeout")
            set_config("request_timeout", 5)
        elif _SAVED_REQUEST_TIMEOUT is not None:
            set_config("request_timeout", _SAVED_REQUEST_TIMEOUT)
            _SAVED_REQUEST_TIMEOUT = None
        _write_debug_snapshot(sl)
        return
    # Packet R: agent edits record as tracked changes only when soffice itself reads
    # doc.agent_edit_review_mode=record; set it in-process (URP set_config is not seen).
    if op in ("AGENT_REVIEW_RECORD", "AGENT_REVIEW_OFF"):
        from plugin.framework.config import set_config

        set_config("doc.agent_edit_review_mode", "record" if op == "AGENT_REVIEW_RECORD" else "off")
        _write_debug_snapshot(sl)
        return
    # Packet R8: HITL approval for web_research, read by the soffice sidebar.
    if op in ("WEB_RESEARCH_PROMPT_ON", "WEB_RESEARCH_PROMPT_OFF"):
        from plugin.framework.config import set_config

        set_config("chatbot.prompt_for_web_research", op == "WEB_RESEARCH_PROMPT_ON")
        _write_debug_snapshot(sl)
        return
    # Settings→Speech writes audio.tts_enabled then emits config:changed; these
    # ops exercise the same path so headed tests can prove sidebar chk_voice sync.
    if op in ("SET_TTS_ON", "SET_TTS_OFF"):
        from plugin.framework.config import set_config

        set_config("audio.tts_enabled", op == "SET_TTS_ON")
        _write_debug_snapshot(sl)
        return
    if op == "TOGGLE_SIDEBAR_TTS":
        # Drive the sidebar Voice checkbox (reverse of Settings→sidebar).
        from plugin.framework.config import set_config

        for chk in _voice_checkboxes():
            if not hasattr(chk, "getState") or not hasattr(chk, "setState"):
                continue
            try:
                cur = int(chk.getState())
                want = 0 if cur else 1
                chk.setState(want)
                # Ensure config matches even if ItemEvent.Selected was missing.
                set_config("audio.tts_enabled", bool(want))
            except Exception:
                log.exception("TOGGLE_SIDEBAR_TTS setState failed")
            break
        _write_debug_snapshot(sl)
        return
    if op in ("FAKE_TTS_HOLD", "FAKE_TTS_CRASH", "FAKE_TTS_MISSING", "FAKE_TTS_OFF"):
        mode = op.removeprefix("FAKE_TTS_").lower()
        _apply_fake_tts(mode)
        _write_debug_snapshot(sl)
        return
    if op in ("FAKE_STT_HOLD", "FAKE_STT_CRASH", "FAKE_STT_MISSING", "FAKE_STT_SILENCE", "FAKE_STT_NOISY", "FAKE_STT_OFF"):
        mode = op.removeprefix("FAKE_STT_").lower()
        _apply_fake_stt(mode)
        _write_debug_snapshot(sl)
        return
    if op.startswith("FAKE_STT_DELAY_"):
        global _FAKE_STT_DELAY
        _FAKE_STT_DELAY = float(op.removeprefix("FAKE_STT_DELAY_"))
        _write_debug_snapshot(sl)
        return
    # Slash ops only touch the Ask ListBox. Run inline like SNAPSHOT —
    # queue_executor.post is not drained on this URP path (no AsyncCallback).
    if op in ("SLASH_REFRESH", "SLASH_ENTER", "SLASH_ESC"):
        if sl is None:
            log.warning("debug_sidebar %s: no SendButtonListener", op)
            _write_debug_snapshot(None)
            return
        if op == "SLASH_REFRESH":
            _slash_refresh_in_soffice(sl)
        elif op == "SLASH_ENTER":
            _slash_key_in_soffice(sl, 1280, 0)
        else:
            _slash_key_in_soffice(sl, 1281, 0)
        _write_debug_snapshot(sl)
        return
    if sl is None:
        log.warning("debug_sidebar %s: no SendButtonListener", op)
        _write_debug_snapshot(None)
        return

    def _apply() -> None:
        if op == "RECORD_CLICKED":
            sl.dispatch(SendEvent(SendEventKind.RECORD_CLICKED))
        elif op == "STOP_REC_CLICKED":
            sl.dispatch(SendEvent(SendEventKind.STOP_REC_CLICKED))
        elif op == "SEND_CLICKED":
            sl.dispatch(SendEvent(SendEventKind.SEND_CLICKED))
        elif op == "STOP_CLICKED":
            sl.dispatch(SendEvent(SendEventKind.STOP_CLICKED))
        elif op == "STOP_ACTION":
            # The Stop button's own ActionEvent path. During a take it steps
            # the take down (cancel a one-shot take) instead of STOP_CLICKED,
            # which the FSM ignores while not busy.
            StopButtonListener(sl).on_action_performed(None)
        elif op == "SET_AUDIO_0":
            set_audio_supported(False, listener=sl)
        elif op == "SET_AUDIO_1":
            set_audio_supported(True, listener=sl)
        elif op == "AUTO_STOP":
            fire_audio_auto_stop(listener=sl)
        elif op == "SET_TEXT_EMPTY":
            from plugin.chatbot.dialogs import set_control_text

            query = getattr(sl, "query_control", None)
            if query is not None:
                set_control_text(query, "")
            ss = sl.sidebar_state
            send = dataclasses.replace(ss.send, has_audio=False, has_text=False)
            sl.sidebar_state = dataclasses.replace(ss, send=send)
            sl.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": False}))
        elif op == "SET_TEXT_NONEMPTY":
            sl.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": True}))
        elif op == "SET_CHAT_MODE":
            apply_fn = getattr(sl, "_apply_sidebar_mode_fn", None)
            if apply_fn is not None:
                from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_CHAT

                apply_fn(CHAT_MODE_CHAT)
        else:
            log.warning("debug_sidebar unknown op %s", op)
        _write_debug_snapshot(sl)

    if getattr(sl, "queue_executor", None) is None:
        _apply()
        return
    # Post, do not execute(): URP executeDispatch + blocking VCL wait deadlocks
    # (office sits idle, tests wait forever). AsyncCallback runs _apply on VCL.
    # _post_to_soffice_vcl initializes AsyncCallback before the post.
    # Force-marshal straight to the listener's executor skips that init, so
    # on a fresh panel the poke is a no-op and the op sits queued. Packet P's
    # opening STOP_CLICKED then runs seconds later, when KICK_PEERS first
    # pokes that executor, and cancels Calc's peer turn mid-flight (P1 never
    # sees write_formula_range).
    _post_to_soffice_vcl(_apply, sl=sl)


def _debug_sidebar_query(op: str, uid: str = "") -> str:
    """``INFLATE_HISTORY`` or ``INFLATE_HISTORY&uid=34`` for executeDispatch."""
    token = (op or "SNAPSHOT").strip()
    uid = str(uid or "").strip()
    if uid:
        return "%s&uid=%s" % (token, uid)
    return token


def _doc_frame(doc: Any) -> Any:
    if doc is None:
        return None
    try:
        controller = doc.getCurrentController()
        return controller.getFrame() if controller is not None else None
    except Exception:
        return None


def execute_debug_sidebar_op(op: str, *, ctx: Any = None) -> dict[str, Any]:
    """URP client: dispatch ``org.extension.writeragent:chatbot.debug_sidebar.<OP>`` in soffice."""
    _require_debug()
    uno_ctx = ctx if ctx is not None else _HOOK_CTX
    if uno_ctx is None:
        from plugin.framework.uno_context import get_ctx

        uno_ctx = get_ctx()
    doc = current_component(uno_ctx)
    frame = _doc_frame(doc)
    if frame is None:
        raise RuntimeError("debug_sidebar: no frame for executeDispatch")
    uid = ""
    try:
        from plugin.framework.uno_context import get_runtime_uid

        uid = get_runtime_uid(doc) or ""
    except Exception:
        uid = ""
    url = "%s:%s?%s" % (EXTENSION_ID_WRITERAGENT, _DEBUG_SIDEBAR_PREFIX, _debug_sidebar_query(op, uid))
    smgr = uno_ctx.getServiceManager()
    helper = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", uno_ctx)
    helper.executeDispatch(frame, url, "", 0, ())
    if op.upper() != "SNAPSHOT":
        time.sleep(0.25)
        snap_url = "%s:%s?%s" % (
            EXTENSION_ID_WRITERAGENT,
            _DEBUG_SIDEBAR_PREFIX,
            _debug_sidebar_query("SNAPSHOT", uid),
        )
        helper.executeDispatch(frame, snap_url, "", 0, ())
    return _read_debug_snapshot()


def _urp_send_control() -> Any:
    ctx = _HOOK_CTX
    if ctx is None:
        return None
    controls = chat_dialog_controls(ctx, current_component(ctx)) or {}
    return controls.get("send")


def _try_click_send_for_kind(kind: SendEventKind) -> bool:
    """G1 path: Record / Stop Rec are the same widget. Click when the label matches."""
    send = _urp_send_control()
    if send is None:
        return False
    label = _control_label(send).lower()
    if kind == SendEventKind.RECORD_CLICKED and "record" in label and "stop rec" not in label:
        uno_click(send)
        return True
    if kind == SendEventKind.STOP_REC_CLICKED and "stop rec" in label:
        uno_click(send)
        return True
    return False


def _send_label_lower() -> str:
    return _control_label(_urp_send_control()).lower()


def _urp_record_click_took() -> bool:
    """True once a URP Record click left the idle Record state.

    Stop Rec is the usual sign. A stub silence auto-stop can flip the label
    straight on to Send (busy, Stop enabled) between two polls.
    """
    label = _send_label_lower()
    if "stop rec" in label or (label and "record" not in label):
        return True
    ctx = _HOOK_CTX
    if ctx is None:
        return False
    controls = chat_dialog_controls(ctx, current_component(ctx)) or {}
    return control_enabled(controls.get("stop")) is True


def _send_event_or_urp(kind: SendEventKind, *, listener: Any = None) -> None:
    sl = listener if listener is not None else send_listener()
    if sl is not None:
        sl.dispatch(SendEvent(kind))
        return
    if kind == SendEventKind.RECORD_CLICKED:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if "record" in _send_label_lower() and "stop rec" not in _send_label_lower():
                break
            time.sleep(0.05)
        spawns_before = read_fake_stt_state().get("spawn_count", 0)
        if _try_click_send_for_kind(kind):
            # Any move off Record, or Stop enabled, means it took. Counting
            # only Stop Rec misses G4's stub auto-stop, which sends the take
            # before a poll sees Stop Rec. The fallback op then starts a
            # second take that never stops (hidden while Stop is greyed
            # during takes, since idle = Stop off).
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                # A fake recorder that crashes at once puts the label back on
                # Record before a poll sees Stop Rec. Its spawn still shows the
                # click took; the fallback op would start a second take (S7).
                if _urp_record_click_took() or read_fake_stt_state().get("spawn_count", 0) > spawns_before:
                    return
                time.sleep(0.05)
        execute_debug_sidebar_op(kind.name)
        return
    if kind == SendEventKind.STOP_REC_CLICKED:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if "stop rec" in _send_label_lower():
                break
            time.sleep(0.05)
        if _try_click_send_for_kind(kind):
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                if "stop rec" not in _send_label_lower():
                    return
                time.sleep(0.05)
        execute_debug_sidebar_op(kind.name)
        return
    if _try_click_send_for_kind(kind):
        return
    execute_debug_sidebar_op(kind.name)


def _panel_frame(panel: Any) -> Any:
    return getattr(panel, "xFrame", None) or getattr(panel, "Frame", None)


def _frames_match(left: Any, right: Any) -> bool:
    """True when *left* and *right* are the same frame.

    PyUNO hands out distinct wrappers; bare ``is`` misses after Packet P / E12
    reopen the Writer deck. ``uno_same`` is the product identity test.

    Off the main thread, identity is ``is`` only. ``uno_same`` is
    main-thread-only. ``handle_debug_sidebar_command`` runs on the URP
    thread, the guard raises, and this ``except`` turns that into a miss.
    ``sidebar_panel`` then returns ``panels[0]``.
    """
    if left is None or right is None:
        return False
    if left is right:
        return True
    from plugin.framework.thread_guard import on_main_thread

    if not on_main_thread():
        return False
    try:
        from plugin.framework.uno_context import uno_same

        return bool(uno_same(left, right))
    except Exception:
        return False


def _listener_torn_down(listener: Any) -> bool:
    """True when deck close already set ``_panel_teardown`` on the listener."""
    return bool(getattr(listener, "_panel_teardown", False))


def _panel_send_listener(panel: Any) -> Any:
    if panel is None:
        return None
    listener = getattr(panel, "send_listener", None)
    if listener is None or _listener_torn_down(listener):
        return None
    return listener


def _prune_teardown_listeners() -> None:
    """Drop closed decks from the strong adopt list so they can be collected."""
    if not _LIVE_SEND_LISTENERS:
        return
    kept = [obj for obj in _LIVE_SEND_LISTENERS if not _listener_torn_down(obj)]
    if len(kept) != len(_LIVE_SEND_LISTENERS):
        _LIVE_SEND_LISTENERS[:] = kept


def _current_frame() -> Any:
    """Frame of ``desktop.getCurrentComponent()``, or None.

    Packet K inflate / URP debug ops must bind this frame. WeakSet[0] is
    leftover Calc after Packet P / E12 (those panels stay alive because
    ``_LIVE_SEND_LISTENERS`` holds a strong ref).
    """
    try:
        ctx = _HOOK_CTX
        if ctx is None:
            from plugin.framework.uno_context import get_ctx

            ctx = get_ctx()
        return _doc_frame(current_component(ctx))
    except Exception:
        return None


def sidebar_panel(frame: Any = None, *, uid: str = "") -> Any:
    """Return the live ``ChatPanelElement`` for *uid* or *frame*.

    When *frame* is omitted and several decks are live, prefer the current
    component. A single live panel is still returned when nothing was named.

    A named frame that does not match returns None. ``panels[0]`` is
    only the no-frame, one-deck debug fallback. A frame miss (including
    ``uno_same`` raising off the URP thread) that falls through to
    ``panels[0]`` is the leftover-Calc pad from Packet K: both the match
    branch and the single-panel branch used to return the first panel
    even when the caller had already named a frame.
    """
    _require_debug()
    panels = iter_live_chat_panels()
    if not panels:
        return None
    token = str(uid or "").strip()
    if token:
        for panel in panels:
            if str(getattr(panel, "_live_panel_uid", "") or "") == token:
                return panel
        return None
    if frame is not None:
        for panel in panels:
            if _frames_match(_panel_frame(panel), frame):
                return panel
        return None
    target = _current_frame()
    if target is not None:
        for panel in panels:
            if _frames_match(_panel_frame(panel), target):
                return panel
    if len(panels) == 1:
        return panels[0]
    return None


def desktop_from_ctx(ctx: Any) -> Any:
    """Desktop from the remote ``ctx`` without ``get_ctx()`` (avoids disposed fallbacks)."""
    _require_debug()
    smgr = ctx.getServiceManager()
    return smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)


def current_component(ctx: Any) -> Any:
    _require_debug()
    return desktop_from_ctx(ctx).getCurrentComponent()


def component_is_calc(doc: Any) -> bool:
    """``supportsService`` over URP. Do not use ``is_calc`` (``@main_thread_only``)."""
    _require_debug()
    if doc is None:
        return False
    try:
        return bool(doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"))
    except Exception:
        return False


def iter_desktop_components(ctx: Any) -> list[Any]:
    """Open models from ``XDesktop.getComponents()`` (URP-safe enumeration)."""
    _require_debug()
    desktop = desktop_from_ctx(ctx)
    comps = getattr(desktop, "getComponents", lambda: None)()
    if comps is None or not hasattr(comps, "createEnumeration"):
        return []
    enum = comps.createEnumeration()
    out: list[Any] = []
    while True:
        try:
            if not enum.hasMoreElements():
                break
            out.append(enum.nextElement())
        except Exception:
            break
    return out


def find_calc_component(ctx: Any) -> Any:
    """First open Calc model, or None. Does not load a document."""
    _require_debug()
    for doc in iter_desktop_components(ctx):
        if component_is_calc(doc):
            return doc
    return None


def close_component(doc: Any) -> None:
    """Close *doc* if it is still alive. Swallows dispose-after-close."""
    _require_debug()
    if doc is None:
        return
    try:
        if hasattr(doc, "close"):
            doc.close(True)
        elif hasattr(doc, "dispose"):
            doc.dispose()
    except Exception:
        pass


def _load_visible_calc_factory() -> None:
    """Create a visible Calc on soffice VCL. ``_blank`` keeps the Writer window.

    URP ``loadComponentFromURL('private:factory/scalc')`` after a Writer deck
    never returns (120s watchdog; File→New Spreadsheet on VCL is fine). This
    job must run via :func:`_post_to_soffice_vcl`, not on the Dummy URP thread.
    """
    from plugin.framework.uno_context import get_ctx, get_desktop

    try:
        desktop = get_desktop(get_ctx())
        desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, ())
        log.info("OPEN_CALC: loaded factory/scalc _blank on VCL")
    except Exception:
        log.exception("OPEN_CALC: factory/scalc _blank failed")
        raise


def _post_to_soffice_vcl(fn: Callable[[], None], *, sl: Any = None) -> None:
    """Enqueue *fn* on soffice VCL. Do not run it inline on the URP Dummy thread.

    ``WRITERAGENT_TESTING=1`` makes ``QueueExecutor.post`` inline. Force-marshal
    so AsyncCallback runs the work on VCL. ``execute()`` from URP dispatch
    deadlocks (office idle, tests wait forever) — same as Packet G FSM ops.
    """
    qe = getattr(sl, "queue_executor", None) if sl is not None else None
    if qe is None:
        from plugin.framework.queue_executor import default_executor

        qe = default_executor
    # force_marshal skips _get_async_callback inside post(); without a prior
    # init, _poke_main_thread is a no-op and the factory load never runs
    # (E12 2026-09-08: "poke skipped (no AsyncCallback)" after OPEN_CALC).
    init_cb = getattr(qe, "_get_async_callback", None)
    if callable(init_cb):
        try:
            init_cb()
        except Exception:
            log.exception("debug_sidebar: AsyncCallback init failed")
    from plugin.framework.queue_executor import set_force_marshal_mode

    set_force_marshal_mode(True)
    try:
        qe.post(fn)
    finally:
        set_force_marshal_mode(False)


def open_calc_document(ctx: Any, *, timeout: float = 30.0) -> Any:
    """Open a visible Calc after a Writer deck without blocking URP on factory/scalc.

    Dual-peer / E12 / G17 recipe: keep Writer open, call this, then
    :func:`adopt_chat_sidebar` on the returned model. Never call
    ``desktop.loadComponentFromURL('private:factory/scalc', …)`` from the
    URP test process after ``show_writeragent_chat_deck``.
    """
    _require_debug()
    existing = find_calc_component(ctx)
    if existing is not None:
        return existing
    execute_debug_sidebar_op("OPEN_CALC", ctx=ctx)
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() <= deadline:
        found = find_calc_component(ctx)
        if found is not None:
            return found
        time.sleep(0.25)
    raise RuntimeError(
        "Calc did not appear after OPEN_CALC (factory/scalc was posted to VCL; "
        "do not loadComponentFromURL factory/scalc over URP after a Writer deck)"
    )


def adopt_chat_sidebar(ctx: Any, doc: Any, *, timeout: float = 20.0) -> tuple[Any, Any]:
    """Show WriterAgentDeck on *doc* and return ``(controls, send_listener)``."""
    _require_debug()
    controls = wait_for_chat_dialog_controls(ctx, timeout=timeout, doc=doc)
    adopt_runtime_send_listeners()
    return controls, send_listener(_doc_frame(doc))


def uno_click(control: Any) -> None:
    """Fire the control's default accessible action (button click) over URP."""
    _require_debug()
    acc = control.getAccessibleContext()
    if acc is not None and hasattr(acc, "doAccessibleAction"):
        acc.doAccessibleAction(0)
        return
    raise RuntimeError("control has no accessible click")


def _query_uno_interface(obj: Any, typename: str) -> Any:
    """PyUNO ``queryInterface`` needs ``uno.getTypeByName``, not the IDL class."""
    if obj is None or not hasattr(obj, "queryInterface"):
        return None
    import uno

    return obj.queryInterface(uno.getTypeByName(typename))


def sidebar_provider(controller: Any) -> Any:
    """Return ``XSidebarProvider`` (decks / setVisible), or None.

    On ``SwXTextView``, ``getDecks`` is ``controller.Sidebar`` (the property),
    not a method on the controller. ``queryInterface(XSidebarProvider)`` on
    the controller is None. Prefer the property, then a controller that
    already has ``getDecks``.
    """
    _require_debug()
    if controller is None:
        return None
    sidebar = getattr(controller, "Sidebar", None)
    if sidebar is not None and callable(getattr(sidebar, "getDecks", None)):
        return sidebar
    if callable(getattr(controller, "getDecks", None)):
        return controller
    return _query_uno_interface(controller, "com.sun.star.ui.XSidebarProvider")


def sidebar_deck_names(ctx: Any, doc: Any) -> list[str]:
    """Deck ids from XSidebarProvider, or empty if the API is unavailable."""
    _require_debug()
    if doc is None:
        return []
    try:
        controller = doc.getCurrentController()
        provider = sidebar_provider(controller)
        if provider is None:
            return []
        decks = provider.getDecks()
        if hasattr(decks, "getElementNames"):
            return [str(n) for n in decks.getElementNames()]
    except Exception:
        return []
    return []


def _panel_root_window(panel: Any) -> Any:
    if panel is None:
        return None
    for attr in ("getDialog", "getWindow"):
        getter = getattr(panel, attr, None)
        if not callable(getter):
            continue
        try:
            win = getter()
        except Exception:
            continue
        if win is not None:
            return win
    return getattr(panel, "Window", None) or getattr(panel, "PanelWindow", None)


def _control_container(window: Any) -> Any:
    if window is None:
        return None
    if hasattr(window, "getControl"):
        return window
    return _query_uno_interface(window, "com.sun.star.awt.XControlContainer") or window


_CHAT_CONTROL_NAMES = (
    "query",
    "send",
    "stop",
    "clear",
    "response",
    "response_rich",
    "status",
    "model_selector",
    "chat_mode_selector",
    "slash_popup",
)


def _controls_from_window(window: Any) -> dict[str, Any] | None:
    root = _control_container(window)
    if root is None or not hasattr(root, "getControl"):
        return None
    out: dict[str, Any] = {}
    for name in _CHAT_CONTROL_NAMES:
        ctrl = root.getControl(name)
        if ctrl is not None:
            out[name] = ctrl
    if "query" in out and "send" in out:
        return out
    return None


def chat_dialog_controls(ctx: Any, doc: Any) -> dict[str, Any] | None:
    """Controls on the live WriterAgent chat panel dialog (out-of-process URP)."""
    _require_debug()
    if doc is None:
        return None
    try:
        controller = doc.getCurrentController()
        provider = sidebar_provider(controller)
        if provider is None:
            return None
        decks = provider.getDecks()
        deck = None
        if hasattr(decks, "hasByName") and decks.hasByName("WriterAgentDeck"):
            deck = decks.getByName("WriterAgentDeck")
        if deck is None:
            return None
        panels = deck.getPanels()
        panel = None
        if hasattr(panels, "hasByName") and panels.hasByName("ChatPanel"):
            panel = panels.getByName("ChatPanel")
        elif hasattr(panels, "getByIndex"):
            panel = panels.getByIndex(0)
        return _controls_from_window(_panel_root_window(panel))
    except Exception:
        log.debug("chat_dialog_controls failed", exc_info=True)
    return None


def send_listener(frame: Any = None, *, uid: str = "") -> Any:
    _require_debug()
    _prune_teardown_listeners()
    panels = iter_live_chat_panels()
    panel = sidebar_panel(frame, uid=uid)
    sl = _panel_send_listener(panel)
    if sl is not None:
        # Do not steal a leftover slash-popup listener from another deck.
        # Packet K inflate + URP Send must share this panel's ChatSession.
        return sl
    # A named frame or uid already picked a deck. Do not substitute another
    # window's listener when that deck missed (uno_same off the URP thread
    # used to fall through to panels[0] / the last adopted listener).
    if frame is not None or panel is not None or uid or len(panels) > 1:
        return None
    with_popup = [
        obj for obj in _LIVE_SEND_LISTENERS if getattr(obj, "slash_popup", None) is not None and not _listener_torn_down(obj)
    ]
    if with_popup:
        return with_popup[-1]
    if _LIVE_SEND_LISTENERS:
        return _LIVE_SEND_LISTENERS[-1]
    return None


def _listener_for_current_doc() -> Any:
    """SendButtonListener for the current component (Packet K inflate)."""
    try:
        ctx = _HOOK_CTX
        if ctx is None:
            from plugin.framework.uno_context import get_ctx

            ctx = get_ctx()
        sl = send_listener_for_doc(current_component(ctx))
        if sl is not None:
            return sl
    except Exception:
        pass
    return send_listener()


def _listener_with_slash_popup(sl: Any) -> Any:
    """Prefer a SendButtonListener that already has the Ask-box controller."""
    if sl is not None and not _listener_torn_down(sl) and getattr(sl, "slash_popup", None) is not None:
        return sl
    for obj in list(_LIVE_SEND_LISTENERS):
        if _listener_torn_down(obj):
            continue
        if getattr(obj, "slash_popup", None) is not None:
            return obj
    for panel in iter_live_chat_panels():
        cand = _panel_send_listener(panel)
        if cand is not None and getattr(cand, "slash_popup", None) is not None:
            return cand
    if _listener_torn_down(sl):
        return None
    return sl


def adopt_runtime_send_listeners() -> int:
    """Find ``SendButtonListener`` instances already wired by the installed factory.

    UNO may load ``panel_factory`` from the OXT cache while tests import the
    checkout copy, so the debug WeakSet can be empty even with a live sidebar.
    """
    _require_debug()
    import gc

    found = 0
    for obj in gc.get_objects():
        try:
            if type(obj).__name__ != "SendButtonListener":
                continue
            if getattr(obj, "dispatch", None) is None:
                continue
            if getattr(obj, "query_control", None) is None:
                continue
        except Exception:
            continue
        if _listener_torn_down(obj):
            continue
        if obj not in _LIVE_SEND_LISTENERS:
            _LIVE_SEND_LISTENERS.append(obj)
            found += 1
    _prune_teardown_listeners()
    return found


def _writeragent_deck(provider: Any) -> Any:
    """Return the WriterAgent XDeck from *provider*, or None."""
    if provider is None:
        return None
    try:
        decks = provider.getDecks()
        if decks is None:
            return None
        name = "WriterAgentDeck"
        if hasattr(decks, "hasByName") and decks.hasByName(name):
            return decks.getByName(name)
        names = list(decks.getElementNames()) if hasattr(decks, "getElementNames") else []
        for deck_name in names:
            if "WriterAgent" in str(deck_name):
                return decks.getByName(deck_name)
    except Exception:
        return None
    return None


def _activate_writeragent_deck(provider: Any) -> None:
    """Switch to WriterAgent via XDeck.activate (no toggle)."""
    deck = _writeragent_deck(provider)
    if deck is None:
        return
    try:
        deck.activate(True)
    except Exception:
        log.debug("activate WriterAgentDeck failed", exc_info=True)


def show_writeragent_chat_deck(ctx: Any, doc: Any) -> None:
    """Make the WriterAgent sidebar deck visible on *doc* (debug tests).

    ``.uno:SidebarDeck.WriterAgentDeck`` is LibreOffice OpenThenToggleDeck
    (tdf#67627): if WriterAgent is already the visible deck, a second summon
    *hides* the sidebar. Skip that dispatch when ``XSidebarProvider.isVisible()``
    is already true; use ``showDecks`` / ``XDeck.activate`` instead. When the
    sidebar is off, dispatch once to open it. Do not dispatch ``.uno:Sidebar`` —
    that also toggles. ``--norestore`` skips crash-recovery so this path is what
    reopens the deck for mock-sidebar tests.
    """
    _require_debug()
    if doc is None:
        return
    frame = _doc_frame(doc)
    if frame is None:
        return
    provider = sidebar_provider(doc.getCurrentController())
    already_visible = False
    if provider is not None and hasattr(provider, "isVisible"):
        already_visible = bool(provider.isVisible())

    # OpenThenToggleDeck: same-deck second time closes the sidebar — skip when on.
    if already_visible and provider is not None:
        try:
            provider.showDecks(True)
        except Exception:
            pass
        deck = _writeragent_deck(provider)
        if deck is not None:
            if hasattr(deck, "isActive") and deck.isActive():
                return
            try:
                deck.activate(True)
            except Exception:
                log.debug("show_writeragent_chat_deck activate while visible failed", exc_info=True)
        return

    smgr = ctx.getServiceManager()
    helper = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    try:
        helper.executeDispatch(frame, ".uno:SidebarDeck.WriterAgentDeck", "", 0, ())
    except Exception:
        log.debug("show_writeragent_chat_deck dispatch WriterAgentDeck failed", exc_info=True)
    if provider is None:
        return
    if hasattr(provider, "setVisible"):
        provider.setVisible(True)
    try:
        provider.showDecks(True)
    except Exception:
        pass
    _activate_writeragent_deck(provider)


def wait_for_chat_dialog_controls(
    ctx: Any, timeout: float = 20.0, *, doc: Any = None
) -> dict[str, Any] | None:
    """Show WriterAgentDeck until query+send exist. Does not pump VCL over URP.

    Pass *doc* to target a specific model (Calc after :func:`open_calc_document`,
    Packet P dual Writer+Calc). Default is ``current_component``.
    """
    global _HOOK_CTX
    _HOOK_CTX = ctx
    _require_debug()
    deadline = time.monotonic() + max(0.0, timeout)
    last: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        try:
            target = doc if doc is not None else current_component(ctx)
            show_writeragent_chat_deck(ctx, target)
            last = chat_dialog_controls(ctx, target)
            if last is not None:
                return last
        except Exception:
            log.debug("wait_for_chat_dialog_controls attempt failed", exc_info=True)
        time.sleep(0.4)
    return last


def control_enabled(control: Any) -> bool | None:
    """``model.Enabled`` over URP, or None if unreadable."""
    _require_debug()
    if control is None:
        return None
    try:
        model = control.getModel()
        return bool(getattr(model, "Enabled"))
    except Exception:
        return None


def send_listener_for_doc(doc: Any) -> Any:
    """``send_listener`` bound to *doc*'s frame (dual-deck Packet P)."""
    _require_debug()
    frame = _doc_frame(doc)
    if frame is None:
        # A closing doc has no frame. send_listener(None) would fall back to
        # another window's listener.
        return None
    return send_listener(frame)


def send_listener_for_uid(uid: str) -> Any:
    """``SendButtonListener`` for *uid* (production live-panel map, then debug walk).

    Packet K inflate must not use soffice ``getCurrentComponent()`` when the
    URP client already named the Writer RuntimeUID. Leftover Calc after
    Packet P / E12 stays current in soffice and used to eat INFLATE_HISTORY.
    """
    _require_debug()
    token = str(uid or "").strip()
    if not token:
        return None
    from plugin.doc.live_panels import get_live_panel

    panel = get_live_panel(token)
    sl = _panel_send_listener(panel)
    if sl is not None:
        return sl
    # Debug WeakSet panels store the uid at register. Match that before
    # get_runtime_uid, which is main-thread-only and raises on the URP thread.
    sl = _panel_send_listener(sidebar_panel(uid=token))
    if sl is not None:
        return sl
    from plugin.framework.thread_guard import on_main_thread

    if not on_main_thread():
        return None
    from plugin.framework.uno_context import get_runtime_uid

    for sl in iter_send_listeners():
        frame = getattr(sl, "frame", None)
        if frame is None:
            continue
        try:
            model = frame.getController().getModel()
            if str(get_runtime_uid(model) or "") == token:
                return sl
        except Exception:
            continue
    return None


def iter_send_listeners() -> list[Any]:
    """All live SendButtonListeners (panels first, then adopted OXT copies)."""
    _require_debug()
    adopt_runtime_send_listeners()
    _prune_teardown_listeners()
    out: list[Any] = []
    seen: set[int] = set()
    for panel in iter_live_chat_panels():
        sl = _panel_send_listener(panel)
        if sl is None:
            continue
        ident = id(sl)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(sl)
    for sl in list(_LIVE_SEND_LISTENERS):
        if _listener_torn_down(sl):
            continue
        ident = id(sl)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(sl)
    return out


def ensure_sidebar_chat_mode(
    controls: dict[str, Any] | None,
    *,
    doc_type: str = "writer",
    listener: Any = None,
) -> None:
    """Select main Chat (not Librarian) so Packet F hits the chat completions path."""
    _require_debug()
    if not controls:
        return
    sel = controls.get("chat_mode_selector")
    if sel is not None:
        from plugin.chatbot.chat_sidebar_mode import (
            CHAT_MODE_CHAT,
            set_selector_mode_with_flags,
            sidebar_mode_flags_for_doc_type,
        )

        set_selector_mode_with_flags(sel, CHAT_MODE_CHAT, sidebar_mode_flags_for_doc_type(doc_type))
    sl = listener if listener is not None else send_listener()
    if sl is not None:
        apply_fn = getattr(sl, "_apply_sidebar_mode_fn", None)
        if apply_fn is not None:
            from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_CHAT

            apply_fn(CHAT_MODE_CHAT)
    else:
        execute_debug_sidebar_op("SET_CHAT_MODE")


def set_query_text_via_controls(controls: dict[str, Any], text: str, *, listener: Any = None) -> None:
    """Set the query box over URP so QueryTextListener can enable Send."""
    _require_debug()
    from plugin.chatbot.dialogs import set_control_text

    if "query" in controls:
        set_control_text(controls["query"], text)
    sl = listener if listener is not None else send_listener()
    if sl is not None:
        sl.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": bool(text.strip())}))
    else:
        execute_debug_sidebar_op("SET_TEXT_NONEMPTY" if text.strip() else "SET_TEXT_EMPTY")


def wait_controls_send_finished(
    controls: dict[str, Any],
    timeout: float = 60.0,
    *,
    transcript_fn: Callable[[], str] | None = None,
    wait_for: str | None = None,
    before: str = "",
) -> bool:
    """Wait until Stop is idle and optional new transcript text appeared.

    Out-of-process tests cannot read ``SendButtonListener.is_busy``. Stop is
    enabled while a send is in flight (Packet F HTTP errors included).
    """
    _require_debug()
    deadline = time.monotonic() + max(0.0, timeout)
    stop = controls.get("stop")
    # Let the click start; HTTP 500 can finish before the first poll.
    time.sleep(0.25)
    saw_busy = False
    while time.monotonic() <= deadline:
        en = control_enabled(stop) if stop is not None else None
        busy = en is True
        if busy:
            saw_busy = True
        body = transcript_fn() if transcript_fn is not None else ""
        suffix = body[len(before) :] if before and body.startswith(before) else body
        if wait_for:
            asst_idx = suffix.lower().rfind("assistant:")
            if asst_idx >= 0:
                found = wait_for.lower() in suffix[asst_idx:].lower()
            elif "[" in suffix:
                found = wait_for.lower() in suffix.lower()
            else:
                found = False
            # Only search the whole control when a rich rerender dropped the prefix.
            # ``body != before`` is too weak: any new character plus a needle already
            # in earlier turns (Packet C truncated banner) would look finished.
            if not found and before and body and not body.startswith(before):
                asst_idx = body.lower().rfind("assistant:")
                if asst_idx >= 0:
                    found = wait_for.lower() in body[asst_idx:].lower()
                elif "[" in body:
                    found = wait_for.lower() in body.lower()
            fake_st = read_fake_tts_state()
            speech_busy = bool(fake_st.get("speaking") or fake_st.get("alive"))
            if found and (not busy or speech_busy):
                return True
        else:
            found = bool(suffix.strip()) or (not before and bool(body.strip()))
            fake_st = read_fake_tts_state()
            speech_busy = bool(fake_st.get("speaking") or fake_st.get("alive"))
            if (saw_busy or found) and (not busy or (found and speech_busy)):
                return True
        time.sleep(0.15)
    if wait_for and transcript_fn is not None:
        body = transcript_fn()
        suffix = body[len(before) :] if before and body.startswith(before) else body
        asst_idx = suffix.lower().rfind("assistant:")
        if asst_idx >= 0 and wait_for.lower() in suffix[asst_idx:].lower():
            return True
        if "[" in suffix and wait_for.lower() in suffix.lower():
            return True
        if before and body and not body.startswith(before):
            asst_idx = body.lower().rfind("assistant:")
            if asst_idx >= 0 and wait_for.lower() in body[asst_idx:].lower():
                return True
            if "[" in body and wait_for.lower() in body.lower():
                return True
        return False
    en = control_enabled(stop) if stop is not None else None
    return en is not True


def _control_label(control: Any) -> str:
    try:
        model = control.getModel() if control is not None else None
        if model is not None:
            return str(model.Label or "")
    except Exception:
        log.debug("control label read failed", exc_info=True)
    return ""


def set_query_text(text: str, *, listener: Any = None) -> None:
    """Set the query box and dispatch ``TEXT_UPDATED`` (same as ``QueryTextListener``)."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is not None:
        from plugin.chatbot.dialogs import set_control_text

        query = getattr(sl, "query_control", None)
        set_control_text(query, text)
        stripped = (text or "").strip()
        sl.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": bool(stripped)}))
        popup = getattr(sl, "slash_popup", None)
        on_text = getattr(popup, "on_query_text", None) if popup is not None else None
        if callable(on_text):
            on_text(text or "")
        return
    ctx = _HOOK_CTX
    if ctx is not None:
        controls = chat_dialog_controls(ctx, current_component(ctx)) or {}
        set_query_text_via_controls(controls, text)
        # QueryTextListener may not fire over URP setText; refresh inside soffice.
        popup = ensure_slash_popup()
        on_text = getattr(popup, "on_query_text", None) if popup is not None else None
        if callable(on_text):
            on_text(text or "")
        elif (text or "").lstrip().startswith("/") or not (text or "").strip():
            execute_debug_sidebar_op("SLASH_REFRESH")


def query_text(*, listener: Any = None) -> str:
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        return ""
    from plugin.chatbot.dialogs import get_control_text

    return get_control_text(getattr(sl, "query_control", None), default="") or ""


def _slash_control_near_query(query: Any) -> Any:
    if query is None:
        return None
    for getter_name in ("getContext", "getPeer"):
        getter = getattr(query, getter_name, None)
        if not callable(getter):
            continue
        try:
            cur = getter()
        except Exception:
            continue
        hops = 0
        while cur is not None and hops < 6:
            hops += 1
            get_control = getattr(cur, "getControl", None)
            if callable(get_control):
                try:
                    ctrl = get_control("slash_popup")
                except Exception:
                    ctrl = None
                if ctrl is not None:
                    return ctrl
            parent = getattr(cur, "getParent", None)
            cur = parent() if callable(parent) else None
    return None


def _attach_slash_popup_on_listener(sl: Any) -> Any:
    if sl is None:
        return None
    popup = getattr(sl, "slash_popup", None)
    if popup is not None:
        return popup
    query = getattr(sl, "query_control", None)
    ctrl = _slash_control_near_query(query)
    if ctrl is None:
        return None
    from plugin.chatbot.slash_popup import SlashPopupController

    popup = SlashPopupController(ctrl, sl, query)
    sl.slash_popup = popup
    return popup


def _slash_lru_names() -> list[str]:
    try:
        from plugin.chatbot.slash_commands import load_slash_lru

        return list(load_slash_lru())
    except Exception:
        return []


def _slash_state_from_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "visible": bool(data.get("slash_visible")),
        "items": list(data.get("slash_items") or []),
        "selected": data.get("slash_selected"),
        "available": bool(data.get("slash_available")),
        "lru": list(data.get("slash_lru") or []),
    }


def _slash_snapshot_fields(sl: Any) -> dict[str, Any]:
    popup = _attach_slash_popup_on_listener(sl) if sl is not None else None
    if popup is None:
        return {"slash_available": False, "slash_visible": False, "slash_items": [], "slash_selected": None}
    return {
        "slash_available": True,
        "slash_visible": bool(getattr(popup, "is_open", False)),
        "slash_items": list(getattr(popup, "visible_names", None) or []),
        "slash_selected": getattr(popup, "selected_name", None),
    }


def _slash_refresh_in_soffice(sl: Any) -> None:
    from plugin.chatbot.dialogs import get_control_text

    popup = _attach_slash_popup_on_listener(sl)
    if popup is None:
        return
    popup.on_query_text(get_control_text(getattr(sl, "query_control", None), default="") or "")


def _slash_key_in_soffice(sl: Any, key_code: int, modifiers: int) -> None:
    popup = _attach_slash_popup_on_listener(sl)
    if popup is None:
        return
    popup.handle_key(int(key_code), int(modifiers))


class _UrpSlashHost:
    """Stand-in SendButtonListener so the popup can run over URP without gc."""

    query_control: Any
    response_control: Any
    clear_listener: Any
    _stop: Any

    def __init__(self, query: Any, response: Any, clear: Any, stop: Any) -> None:
        self.query_control = query
        self.response_control = response
        self.slash_popup: Any = None
        self.clear_listener = type(
            "ClearProxy",
            (),
            {"on_action_performed": staticmethod(lambda _ev: uno_click(clear) if clear is not None else None)},
        )()
        self._stop = stop

    def dispatch(self, event: Any) -> None:
        kind = getattr(event, "kind", None)
        if kind == SendEventKind.STOP_CLICKED and self._stop is not None:
            uno_click(self._stop)

    def _append_response(self, text: str, role: str = "assistant") -> None:
        from plugin.chatbot.dialogs import get_control_text, set_control_text

        # ``role`` matches SendButtonListener._append_response (run_slash_command).
        current = get_control_text(self.response_control, default="") or ""
        set_control_text(self.response_control, current + ("" if role is None else text))


def _urp_slash_controller() -> Any:
    """Bind ``SlashPopupController`` to the live sidebar ListBox over URP."""
    global _URP_SLASH_POPUP
    ctx = _HOOK_CTX
    if ctx is None:
        return None
    controls = chat_dialog_controls(ctx, current_component(ctx)) or {}
    ctrl = controls.get("slash_popup")
    query = controls.get("query")
    if ctrl is None or query is None:
        return None
    # URP getControl returns a new proxy each time; do not compare with ``is``.
    if _URP_SLASH_POPUP is not None:
        return _URP_SLASH_POPUP
    from plugin.chatbot.slash_popup import SlashPopupController

    response = controls.get("response_rich") or controls.get("response")
    host = _UrpSlashHost(query, response, controls.get("clear"), controls.get("stop"))
    popup = SlashPopupController(ctrl, host, query)
    host.slash_popup = popup
    _URP_SLASH_POPUP = popup
    return popup


def ensure_slash_popup(*, listener: Any = None) -> Any:
    """Return the Ask-box slash controller, attaching it if the XDL ListBox exists.

    OXT factory and checkout tests can see different ``SendButtonListener``
    objects. Re-bind from the live ``slash_popup`` control so hooks can drive
    the menu even when the listener we found was not the one wiring attached.
    Out-of-process mock-sidebar has no in-process listener; bind over URP.
    """
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is not None:
        popup = getattr(sl, "slash_popup", None)
        if popup is not None:
            return popup
        ctrl = None
        ctx = _HOOK_CTX
        if ctx is not None:
            controls = chat_dialog_controls(ctx, current_component(ctx)) or {}
            ctrl = controls.get("slash_popup")
        if ctrl is None:
            return _urp_slash_controller()
        from plugin.chatbot.slash_popup import SlashPopupController

        popup = SlashPopupController(ctrl, sl, getattr(sl, "query_control", None) or ctrl)
        sl.slash_popup = popup
        return popup
    return _urp_slash_controller()


def slash_popup_state(*, listener: Any = None) -> dict[str, Any]:
    """Visible names and selection for the Ask-box slash completion menu."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    popup = getattr(sl, "slash_popup", None) if sl is not None else None
    if popup is None:
        popup = ensure_slash_popup(listener=sl)
    if popup is None:
        # Last resort: in-soffice snapshot (listener lives only in soffice).
        return _slash_state_from_snapshot(execute_debug_sidebar_op("SNAPSHOT"))
    visible = bool(getattr(popup, "is_open", False))
    items = list(getattr(popup, "visible_names", None) or [])
    selected = getattr(popup, "selected_name", None)
    if not items:
        ctrl = getattr(popup, "control", None)
        if ctrl is not None and hasattr(ctrl, "getItemCount"):
            count = int(ctrl.getItemCount() or 0)
            items = [str(ctrl.getItem(i)) for i in range(count)]
    return {
        "visible": visible,
        "items": items,
        "selected": selected,
        "available": True,
        "lru": _slash_lru_names(),
    }


def press_query_key(key_code: int, modifiers: int = 0, *, listener: Any = None) -> None:
    """Drive ``QueryKeyListener`` (Enter / Esc / arrows / Tab) on the Ask field."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        popup = ensure_slash_popup()
        handle = getattr(popup, "handle_key", None) if popup is not None else None
        if callable(handle) and handle(int(key_code), int(modifiers)) is True:
            return
        if int(key_code) == 1280 and int(modifiers) == 0:
            execute_debug_sidebar_op("SLASH_ENTER")
            return
        if int(key_code) == 1281:
            execute_debug_sidebar_op("SLASH_ESC")
            return
        raise RuntimeError("no live SendButtonListener")
    from plugin.chatbot.panel import QueryKeyListener

    event = type("KeyEvent", (), {"KeyCode": int(key_code), "Modifiers": int(modifiers), "Consume": False})()
    QueryKeyListener(sl).on_key_pressed(event)


def transcript_text(*, listener: Any = None) -> str:
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        return ""
    from plugin.chatbot.dialogs import get_control_text

    widget = getattr(sl, "rich_text_widget", None)
    control = getattr(widget, "control", None) if widget is not None else None
    if control is None:
        control = getattr(sl, "response_control", None)
    return get_control_text(control, default="") or ""


def transcript_contains(needle: str, *, listener: Any = None) -> bool:
    _require_debug()
    return needle in transcript_text(listener=listener)


def inflate_sidebar_history(*, ctx: Any = None) -> dict[str, Any]:
    """Grow ChatSession.messages in soffice past the mock compaction gate."""
    _require_debug()
    sl = None
    if ctx is not None:
        sl = send_listener_for_doc(current_component(ctx))
    if sl is None:
        sl = send_listener()
    session = getattr(sl, "session", None) if sl is not None else None
    messages = getattr(session, "messages", None)
    # In-process listener only. A URP proxy has no ChatSession.messages list.
    if isinstance(messages, list):
        added = _inflate_session_history(session)
        data = _write_debug_snapshot(sl)
        data["inflate_pairs"] = added
        return data
    return execute_debug_sidebar_op("INFLATE_HISTORY", ctx=ctx)


def clear_sidebar_chat(*, listener: Any = None) -> None:
    """New-chat: same path as the Clear button.

    Packet G canned-string asserts must not see leftover Packet E/F body
    (``hello``, ``look up cats``, ``document_research``). URP clicks the
    Clear control. In-process, ``clear_listener.on_action_performed`` stops
    speech, drops hands-free, releases the mic, and latches Stop when busy.

    ``ClearButtonListener`` is the path that latches Stop first.
    ``session.clear`` and ``clear_and_greeting("")`` let a drain still
    paint the reply onto the wiped transcript.
    """
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is not None:
        clear_listener = getattr(sl, "clear_listener", None)
        action = getattr(clear_listener, "on_action_performed", None)
        if callable(action):
            action(None)
            return
        session = getattr(sl, "session", None)
        clearer = getattr(session, "clear", None) if session is not None else None
        if callable(clearer):
            clearer()
        widget = getattr(sl, "rich_text_widget", None)
        greet = getattr(widget, "clear_and_greeting", None) if widget is not None else None
        if callable(greet):
            greet("")
            return
        from plugin.chatbot.dialogs import set_control_text

        response = getattr(sl, "response_control", None)
        if response is not None:
            set_control_text(response, "")
        return
    ctx = _HOOK_CTX
    if ctx is None:
        return
    controls = chat_dialog_controls(ctx, current_component(ctx)) or {}
    clear_btn = controls.get("clear")
    if clear_btn is not None:
        try:
            uno_click(clear_btn)
            return
        except Exception:
            log.debug("clear_sidebar_chat URP Clear click failed", exc_info=True)
    from plugin.chatbot.dialogs import set_control_text

    response = controls.get("response_rich") or controls.get("response")
    if response is not None:
        set_control_text(response, "")


def press_send(*, listener: Any = None) -> None:
    """Primary Send button path (also Accept when HITL owns the label)."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        raise RuntimeError("no live SendButtonListener")
    sl.on_action_performed(None)


def press_stop(*, listener: Any = None) -> None:
    """Windows / ActionEvent Stop path (``StopButtonListener.on_action_performed``)."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        label = _send_label_lower()
        # Stop during a recording take (S6): same listener as in-process. The
        # Stop Rec label can lag is_recording by a tick, so ask the state while
        # it still reads Record. A busy reply (Send label) skips the snapshot.
        if "stop rec" in label or ("record" in label and send_state().is_recording):
            execute_debug_sidebar_op("STOP_ACTION")
            return
        _send_event_or_urp(SendEventKind.STOP_CLICKED, listener=None)
        return
    StopButtonListener(sl).on_action_performed(None)


def press_stop_mouse(*, listener: Any = None) -> None:
    """GTK Stop ``mousePressed`` path. No-op while web-search approval is active."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    notify_stop_mouse_pressed(sl)


def press_accept(*, listener: Any = None) -> None:
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        raise RuntimeError("no live SendButtonListener")
    sl.on_action_performed(None)


def press_change(query_override: str | None = None, *, listener: Any = None) -> None:
    """HITL Change without the modal edit dialog (Packet E9c). Not ``STOP_CLICKED``."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        raise RuntimeError("no live SendButtonListener")
    if query_override is None:
        query_override = getattr(sl, "_approval_query_for_engine", None) or ""
    sl._finish_inline_web_approval(True, query_override=query_override)


def press_reject(*, listener: Any = None) -> None:
    """HITL Reject (Clear-button overlay). Not ``STOP_CLICKED``."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        raise RuntimeError("no live SendButtonListener")
    sl._finish_inline_web_approval(False)


def approval_active(*, listener: Any = None) -> bool:
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        return False
    return getattr(sl, "_approval_event", None) is not None


def press_record(*, listener: Any = None) -> None:
    """Short Record click. No-op unless the Send button label is Record.

    No-op unless the Send button label is Record. Dispatching
    ``RECORD_CLICKED`` when the label is Send does not match the URP click,
    which already requires the Record label. A real short click is mouse
    press then release (``actionPerformed`` is swallowed when that release
    already dispatched).
    """
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is not None:
        notify_record_mouse_pressed(sl)
        notify_record_mouse_released(sl)
        return
    _send_event_or_urp(SendEventKind.RECORD_CLICKED, listener=None)


def press_stop_rec(*, listener: Any = None) -> None:
    """Stop Rec click. Dispatches only when the label is Stop Rec.

    Mouse press/release owns the Record hold. On a Stop Rec label those
    notifies do not dispatch; ``actionPerformed`` does, and it refuses Send
    and Record labels. The URP branch clicks only when the label matches.
    """
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is not None:
        from plugin.framework.i18n import _

        notify_record_mouse_pressed(sl)
        notify_record_mouse_released(sl)
        if _control_label(getattr(sl, "send_control", None)) == _("Stop Rec"):
            action = getattr(sl, "on_action_performed", None)
            if callable(action):
                action(None)
        return
    _send_event_or_urp(SendEventKind.STOP_REC_CLICKED, listener=None)


def press_send_clicked(*, listener: Any = None) -> None:
    """Always ``SEND_CLICKED`` (ignore Record / Stop Rec / Accept labels). Packet G15."""
    _require_debug()
    _send_event_or_urp(SendEventKind.SEND_CLICKED, listener=listener)


def set_audio_supported(supported: bool, *, listener: Any = None) -> None:
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        execute_debug_sidebar_op("SET_AUDIO_1" if supported else "SET_AUDIO_0")
        return
    ss = sl.sidebar_state
    send = dataclasses.replace(ss.send, audio_supported=bool(supported), has_audio=False)
    sl.sidebar_state = dataclasses.replace(ss, send=send)
    sl.dispatch(
        SendEvent(
            SendEventKind.TEXT_UPDATED,
            {"has_text": bool(send.has_text)},
        )
    )


def audio_status(*, listener: Any = None) -> dict[str, Any]:
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        data = execute_debug_sidebar_op("SNAPSHOT")
        return {
            "status": data.get("status", "idle"),
            "has_audio": bool(data.get("has_audio")),
            "is_recording": bool(data.get("is_recording")),
            "error_message": data.get("error_message"),
            "stub_start_count": int(data.get("stub_start_count") or 0),
            "history_user_tail": str(data.get("history_user_tail") or ""),
        }
    send = sl.sidebar_state.send
    audio = sl.sidebar_state.audio
    rec = getattr(sl, "audio_recorder", None)
    return {
        "status": getattr(audio, "status", "idle"),
        "has_audio": bool(send.has_audio),
        "is_recording": bool(send.is_recording),
        "error_message": getattr(audio, "error_message", None),
        "stub_start_count": int(getattr(rec, "_stub_start_count", 0) or 0),
        "history_user_tail": _history_user_tail(sl),
    }


def _live_audio_recorder(*, listener: Any = None) -> Any:
    sl = listener if listener is not None else send_listener()
    if sl is None:
        return None
    return getattr(sl, "audio_recorder", None)


def inject_wav(path_or_bytes: Any, *, listener: Any = None) -> None:
    """Point the stub capture child at a finished WAV (path or bytes). No mic."""
    _require_debug()
    from plugin.chatbot.audio_recorder import write_stub_recorder_control

    wav_path = path_or_bytes
    if isinstance(path_or_bytes, (bytes, bytearray)):
        dest = os.path.join(__import__("tempfile").gettempdir(), "writeragent_stub_inject.wav")
        with open(dest, "wb") as handle:
            handle.write(path_or_bytes)
        wav_path = dest
    write_stub_recorder_control(wav=wav_path, skip=True)
    rec = _live_audio_recorder(listener=listener)
    if rec is None:
        return
    rec._test_skip_spawn = True
    rec._test_inject_wav = wav_path
    if rec.temp_filename and wav_path is not None:
        rec._write_injected_wav()


def stub_recorder_child(
    *,
    listener: Any = None,
    fail_start: str | None = None,
    missing_wav: bool = False,
    hang_ready: bool = False,
) -> None:
    """Skip venv/PortAudio spawn; InitializeDeviceEffect fakes a ready child."""
    _require_debug()
    from plugin.chatbot.audio_recorder import clear_stub_recorder_control, write_stub_recorder_control

    # Replace the control file so G4 auto_stop / G12 fail_start / G21 hang_ready cannot leak.
    clear_stub_recorder_control()
    write_stub_recorder_control(
        skip=True,
        fail_start=fail_start,
        missing_wav=bool(missing_wav),
        hang_ready=bool(hang_ready),
    )
    rec = _live_audio_recorder(listener=listener)
    if rec is None:
        return
    rec._test_skip_spawn = True
    rec._test_fail_start = fail_start
    rec._test_missing_wav = bool(missing_wav)
    rec._test_hang_ready = bool(hang_ready)
    rec._stub_start_count = 0


def fire_audio_auto_stop(*, listener: Any = None) -> None:
    """Same host path as IPC auto_stopped (silence detector), no wall-clock wait."""
    _require_debug()
    from plugin.chatbot.audio_recorder import write_stub_recorder_control

    write_stub_recorder_control(auto_stop=True, skip=True)
    rec = _live_audio_recorder(listener=listener)
    if rec is None:
        return
    rec._notify_auto_stop(rec.temp_filename)


@dataclass(frozen=True)
class SidebarHookSendView:
    is_busy: bool
    is_recording: bool
    has_text: bool
    has_audio: bool
    audio_supported: bool
    send_label: str
    stop_label: str


def send_state(*, listener: Any = None) -> SidebarHookSendView:
    _require_debug()
    sl = listener if listener is not None else send_listener()
    if sl is None:
        data = execute_debug_sidebar_op("SNAPSHOT")
        return SidebarHookSendView(
            is_busy=bool(data.get("is_busy")),
            is_recording=bool(data.get("is_recording")),
            has_text=bool(data.get("has_text")),
            has_audio=bool(data.get("has_audio")),
            audio_supported=bool(data.get("audio_supported")),
            send_label=str(data.get("send_label") or ""),
            stop_label=str(data.get("stop_label") or ""),
        )
    send = sl.sidebar_state.send
    return SidebarHookSendView(
        is_busy=bool(send.is_busy),
        is_recording=bool(send.is_recording),
        has_text=bool(send.has_text),
        has_audio=bool(send.has_audio),
        audio_supported=bool(send.audio_supported),
        send_label=_control_label(getattr(sl, "send_control", None)),
        stop_label=_control_label(getattr(sl, "stop_control", None)),
    )


def pump_until(pred: Callable[[], bool], timeout: float = 30.0, *, ctx: Any = None) -> bool:
    """Idle-pump until *pred* is true. Uses ``force=True`` so native tests still pump VCL."""
    _require_debug()
    from plugin.framework.uno_context import get_ctx, process_events_to_idle

    deadline = time.monotonic() + max(0.0, timeout)
    uno_ctx = ctx
    if uno_ctx is None:
        sl = send_listener()
        uno_ctx = getattr(sl, "ctx", None) if sl is not None else None
        if uno_ctx is None:
            try:
                uno_ctx = get_ctx()
            except Exception:
                uno_ctx = None
    while time.monotonic() <= deadline:
        if pred():
            return True
        # Visible user-profile soffice: processEventsToIdle over URP can hang the pipe.
        if os.environ.get("WRITERAGENT_UNO_USER_PROFILE") == "1" or uno_ctx is None:
            time.sleep(0.05)
        else:
            process_events_to_idle(uno_ctx, rounds=1, force=True)
    return pred()


def wait_idle(*, listener: Any = None, timeout: float = 30.0) -> bool:
    _require_debug()
    sl0 = listener if listener is not None else send_listener()
    if sl0 is None:
        # Let Stop Rec / Send enable Stop before the first idle poll (else we
        # return immediately and Packet G never waits for the mock reply).
        time.sleep(0.35)

    def _idle() -> bool:
        sl = listener if listener is not None else send_listener()
        if sl is None:
            # Do not executeDispatch SNAPSHOT in a loop — that hangs the URP pipe.
            ctx = _HOOK_CTX
            if ctx is None:
                return False
            controls = chat_dialog_controls(ctx, current_component(ctx)) or {}
            stop = controls.get("stop")
            send = controls.get("send")
            if control_enabled(stop) is True:
                return False
            return control_enabled(send) is True
        send = sl.sidebar_state.send
        return (not send.is_busy) and (not send.is_recording)

    ctx = getattr(listener, "ctx", None) if listener is not None else None
    return pump_until(_idle, timeout, ctx=ctx)




_THREAD_VIOLATION_COUNT = 0
_THREAD_VIOLATION_ORIG: Any = None


def get_thread_guard_violations() -> int:
    """Count thread-guard violations reported since the first call (Packet R7).

    Wraps ``thread_guard._notify_thread_violation`` (looked up at call time by the
    guard) so every violation in this soffice process is counted, not log-scraped.
    """
    _require_debug()
    global _THREAD_VIOLATION_ORIG
    import plugin.framework.thread_guard as tg

    if _THREAD_VIOLATION_ORIG is None:
        _THREAD_VIOLATION_ORIG = tg._notify_thread_violation

        def _counting_notify(msg: str) -> None:
            global _THREAD_VIOLATION_COUNT
            _THREAD_VIOLATION_COUNT += 1
            _THREAD_VIOLATION_ORIG(msg)

        setattr(tg, "_notify_thread_violation", _counting_notify)
    return _THREAD_VIOLATION_COUNT


def list_redlines(doc: Any) -> list[dict[str, Any]]:
    """Return a list of redlines in the document as dictionaries."""
    redlines: list[dict[str, Any]] = []
    if not hasattr(doc, "getRedlines"):
        return redlines
    rls = doc.getRedlines()
    enum = rls.createEnumeration()
    while enum.hasMoreElements():
        rl = enum.nextElement()
        entry: dict[str, Any] = {}
        for prop in ("RedlineAuthor", "RedlineComment", "RedlineType", "RedlineIdentifier"):
            entry[prop] = rl.getPropertyValue(prop)
        entry["token"] = entry.get("RedlineIdentifier", "")
        start = rl.getPropertyValue("RedlineStart")
        txt = ""
        if start is not None:
            try:
                end = rl.getPropertyValue("RedlineEnd")
                stext = start.getText()
                cur = stext.createTextCursorByRange(start)
                cur.gotoRange(end, True)
                txt = cur.getString() or ""
            except Exception:
                pass
        if not txt:
            try:
                rt = rl.getPropertyValue("RedlineText")
                txt = (rt.getString() if hasattr(rt, "getString") else str(rt)) if rt is not None else ""
            except Exception:
                pass
        entry["text"] = txt
        redlines.append(entry)
    return redlines


def toggle_record_changes(doc: Any, enable: bool) -> None:
    doc.setPropertyValue("RecordChanges", enable)


def insert_user_redline(doc: Any, ctx: Any, text: str, start_idx: int, end_idx: int) -> None:
    was_recording = bool(doc.getPropertyValue("RecordChanges"))
    doc.setPropertyValue("RecordChanges", True)

    from plugin.writer import review_authors
    prior = review_authors.begin(ctx)
    review_authors._set_office_author(ctx, "Test User")

    try:
        body = doc.getText()
        cursor = body.createTextCursor()
        cursor.gotoStart(False)
        cursor.goRight(start_idx, False)
        cursor.goRight(end_idx - start_idx, True)
        cursor.setString(text)
    finally:
        review_authors.end(ctx, prior)
        doc.setPropertyValue("RecordChanges", was_recording)


def undo(frame: Any, ctx: Any) -> None:
    smgr = ctx.getServiceManager()
    dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    dispatcher.executeDispatch(frame, ".uno:Undo", "", 0, ())


def next_hello_ok(*, listener: Any = None, timeout: float = 60.0) -> bool:
    """Send ``hello``, wait until idle, require assistant HTML or hello text in the transcript."""
    _require_debug()
    sl = listener if listener is not None else send_listener()
    set_query_text("hello", listener=sl)
    press_send(listener=sl)
    if not wait_idle(listener=sl, timeout=timeout):
        return False
    text = transcript_text(listener=sl).lower()
    if "hello" in text or "<p" in text or "<ul" in text or "<ol" in text:
        return True
    log.warning("next_hello_ok: idle but transcript did not look like a hello reply")
    return False


# --- Packet O: fake vision OCR backend (runs inside soffice) -----------------
#
# The trusted OCR venv (Docling / PaddleOCR) is not installed in CI. These modes
# replace only the venv boundary -- ``vision_runner.run_vision`` and the venv
# gate ``vision_availability.vision_venv_configured`` -- so the sidebar,
# delegate gateway, per-image discovery, export and insert all run for real.
# Every fake call is logged to a JSON state file the URP test process reads.

_FAKE_VISION_NAME = "writeragent_fake_vision.json"
_FAKE_VISION_LOCK = threading.Lock()
_FAKE_VISION_ORIG: dict[tuple[Any, str], Any] = {}
_FAKE_VISION_STATE: dict[str, Any] = {}
FAKE_VISION_MODES = ("ok", "fail_second", "timeout", "slow", "export_fail", "unavailable", "off")


def fake_vision_state_path() -> str:
    return os.path.join(tempfile.gettempdir(), _FAKE_VISION_NAME)


def read_fake_vision_state() -> dict[str, Any]:
    """URP side: ``{"mode", "calls": [{"image_name", "status"}...]}`` or ``{}``."""
    return _read_json_dict(fake_vision_state_path())


def _write_fake_vision_state() -> None:
    path = fake_vision_state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_FAKE_VISION_STATE, f)
    os.replace(tmp, path)


def fake_ocr_text(image_name: str) -> str:
    """The text the fake backend 'reads' from *image_name*."""
    return f"OCR_TEXT_{image_name}"


def _apply_fake_vision(mode: str) -> None:
    _require_debug()
    if mode not in FAKE_VISION_MODES:
        raise ValueError(f"unknown fake vision mode {mode!r}")
    import plugin.vision.vision_availability as availability
    import plugin.vision.vision_runner as runner
    import plugin.writer.images.image_tools as image_tools

    with _FAKE_VISION_LOCK:
        for (module, attr), orig in list(_FAKE_VISION_ORIG.items()):
            setattr(module, attr, orig)
        _FAKE_VISION_ORIG.clear()
        _FAKE_VISION_STATE.clear()
        if mode == "off":
            _remove_quiet(fake_vision_state_path())
            return
        _FAKE_VISION_STATE.update({"mode": mode, "calls": []})
        _write_fake_vision_state()

        def _patch(module: Any, attr: str, value: Any) -> None:
            _FAKE_VISION_ORIG[(module, attr)] = getattr(module, attr)
            setattr(module, attr, value)

        _patch(availability, "vision_venv_configured", lambda ctx=None: mode != "unavailable")
        if mode == "export_fail":
            # Fail inside the real resolve_vision_image_bytes so its own error surfaces.
            def _no_export(*_a: Any, **_k: Any) -> Any:
                raise RuntimeError("fake export failure")

            _patch(runner, "export_graphic_object_to_bytes", _no_export)
            _patch(image_tools, "export_graphic_object_to_bytes", _no_export)

        def _fake_run_vision(ctx: Any, spec: Any, image: Any = None, *, context: dict[str, Any] | None = None,
                             stop_checker: Any = None) -> dict[str, Any]:
            name = str((context or {}).get("image_name") or "")
            with _FAKE_VISION_LOCK:
                index = len(_FAKE_VISION_STATE["calls"])
                result: dict[str, Any]
                if mode == "timeout":
                    result = {"status": "error", "code": "VISION_ERROR", "message": "Vision timed out after 120s"}
                elif mode == "fail_second" and index == 1:
                    result = {"status": "error", "code": "VISION_ERROR", "message": f"fake OCR crash on {name}"}
                else:
                    result = {"status": "ok"}
            if mode == "slow":
                deadline = time.monotonic() + 6.0
                while time.monotonic() < deadline:
                    if stop_checker is not None and stop_checker():
                        result = {"status": "error", "code": "USER_STOPPED", "message": "Cancelled by user."}
                        break
                    time.sleep(0.05)
            if result["status"] == "ok":
                text = fake_ocr_text(name)
                result = {"status": "ok", "helper": "extract_structure", "full_text": text, "html": f"<p>{text}</p>"}
            with _FAKE_VISION_LOCK:
                _FAKE_VISION_STATE["calls"].append({"image_name": name, "status": result["status"],
                                                    "bytes": len(image or b"")})
                _write_fake_vision_state()
            return result

        _patch(runner, "run_vision", _fake_run_vision)


def set_fake_vision(mode: str, *, ctx: Any = None) -> dict[str, Any]:
    """URP client: switch the soffice fake vision backend (``FAKE_VISION_MODES``)."""
    _require_debug()
    if mode not in FAKE_VISION_MODES:
        raise ValueError(f"unknown fake vision mode {mode!r}")
    execute_debug_sidebar_op("FAKE_VISION_" + mode.upper(), ctx=ctx)
    state = read_fake_vision_state()
    if mode != "off" and state.get("mode") != mode:
        raise AssertionError(f"fake vision {mode!r} not installed in soffice: {state}")
    return state


def get_writer_graphics_count(*, ctx: Any = None) -> int:
    """Return the number of Writer TextGraphicObjects."""
    doc = current_component(ctx)
    if doc is None or not hasattr(doc, "getGraphicObjects"):
        return 0
    return len(doc.getGraphicObjects().getElementNames())

def get_draw_graphics_count(*, ctx: Any = None) -> int:
    """Return the number of Draw/Impress GraphicObjectShapes on the first page."""
    doc = current_component(ctx)
    if doc is None or not hasattr(doc, "getDrawPages"):
        return 0
    return doc.getDrawPages().getByIndex(0).getCount()

def assert_no_empty_graphics(*, ctx: Any = None) -> None:
    """Assert no empty/partial graphics exist in Writer."""
    doc = current_component(ctx)
    if doc is None or not hasattr(doc, "getGraphicObjects"):
        return
    for name in doc.getGraphicObjects().getElementNames():
        g = doc.getGraphicObjects().getByName(name)
        if hasattr(g, "GraphicURL"):
            assert g.GraphicURL, f"Found graphic {name} with empty GraphicURL"

def dispatch_undo(*, ctx: Any = None) -> None:
    """Dispatch .uno:Undo on the current document frame."""
    doc = current_component(ctx)
    if doc is None or not hasattr(doc, "getCurrentController"):
        return
    frame = doc.getCurrentController().getFrame()
    if frame is None:
        return
    uno_ctx = ctx if ctx is not None else _HOOK_CTX
    if uno_ctx is None:
        from plugin.framework.uno_context import get_ctx

        uno_ctx = get_ctx()
    helper = uno_ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", uno_ctx)
    helper.executeDispatch(frame, ".uno:Undo", "", 0, ())
