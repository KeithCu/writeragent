#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Dedicated venv subprocess entry for sidebar microphone recording.

Line-delimited JSON on stdout; host sends ``stop`` on stdin to finalize the WAV.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from plugin.framework.uno_bootstrap import register_alias_importer

register_alias_importer()

from typing import IO

from plugin.scripting.audio_silence_detector import SilenceDetectorConfig
from plugin.scripting.venv.audio_recorder import record_to_wav
from plugin.scripting.ipc import claim_ipc_channel, get_child_ipc_stream, write_json_line

_ipc_stream: IO[bytes] | None = None
_emit_lock = threading.Lock()


def _emit(payload: dict[str, object]) -> None:
    # Write only on the private dup'd IPC channel. Library prints and ALSA
    # logs on raw stdout break line-delimited JSON framing.
    stream = _ipc_stream or get_child_ipc_stream() or sys.stdout
    with _emit_lock:
        write_json_line(stream, payload)


def _is_stop_command(line: str) -> bool:
    stripped = line.strip()
    if stripped.lower() == "stop":
        return True
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("command") == "stop"


def _stdin_stop_reader(stop_event: threading.Event) -> None:
    for line in sys.stdin:
        if _is_stop_command(line):
            stop_event.set()
            return
    stop_event.set()


def main(argv: list[str] | None = None) -> int:
    global _ipc_stream
    _ipc_stream = claim_ipc_channel()
    if _SCRIPT_DIR in sys.path:
        sys.path.remove(_SCRIPT_DIR)

    parser = argparse.ArgumentParser(description="WriterAgent venv audio recorder")
    parser.add_argument("--output", required=True, help="Path to write the WAV file")
    parser.add_argument("--silence-stop-ms", type=int, default=3000)
    args = parser.parse_args(argv)
    output_path = os.path.abspath(args.output)
    silence_config = SilenceDetectorConfig(silence_stop_ms=max(0, args.silence_stop_ms))

    stop_event = threading.Event()
    if os.name != "nt":
        import signal
        signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    reader = threading.Thread(target=_stdin_stop_reader, args=(stop_event,), daemon=True)
    reader.start()

    ready_emitted = threading.Event()

    def on_started() -> None:
        _emit({"status": "ready"})
        ready_emitted.set()

    try:
        auto_stopped = record_to_wav(
            output_path,
            stop_event,
            on_stream_started=on_started,
            silence_config=silence_config,
            on_ipc_emit=_emit,
        )
        if not ready_emitted.is_set():
            _emit({"status": "error", "message": "Audio stream failed to start."})
            return 1
        _emit({"status": "ok", "path": output_path, "auto_stopped": auto_stopped})
        return 0
    except RuntimeError as exc:
        _emit({"status": "error", "message": str(exc)})
        return 1
    except Exception as exc:
        _emit({"status": "error", "message": f"Audio recording failed: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
