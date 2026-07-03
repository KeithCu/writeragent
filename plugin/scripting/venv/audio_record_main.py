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

from plugin.scripting.venv.audio_recorder import record_to_wav
from plugin.scripting.ipc import write_json_line


def _emit(payload: dict[str, object]) -> None:
    write_json_line(sys.stdout, payload)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WriterAgent venv audio recorder")
    parser.add_argument("--output", required=True, help="Path to write the WAV file")
    args = parser.parse_args(argv)
    output_path = os.path.abspath(args.output)

    stop_event = threading.Event()
    reader = threading.Thread(target=_stdin_stop_reader, args=(stop_event,), daemon=True)
    reader.start()

    ready_emitted = threading.Event()

    def on_started() -> None:
        _emit({"status": "ready"})
        ready_emitted.set()

    try:
        record_to_wav(output_path, stop_event, on_stream_started=on_started)
        if not ready_emitted.is_set():
            _emit({"status": "error", "message": "Audio stream failed to start."})
            return 1
        _emit({"status": "ok", "path": output_path})
        return 0
    except RuntimeError as exc:
        _emit({"status": "error", "message": str(exc)})
        return 1
    except Exception as exc:
        _emit({"status": "error", "message": f"Audio recording failed: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
