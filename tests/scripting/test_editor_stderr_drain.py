# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Monaco editor child stderr drain logging."""

from __future__ import annotations

import os
import subprocess
import time
from unittest.mock import MagicMock, patch

from plugin.scripting.editor_bridge import PersistentEditor


def test_stderr_drain_logs_lines_and_preserves_tail():
    editor = PersistentEditor()
    read_fd, write_fd = os.pipe()
    stderr = os.fdopen(read_fd, "rb")
    write_handle = os.fdopen(write_fd, "wb")

    proc = MagicMock(spec=subprocess.Popen)
    proc.poll = MagicMock(return_value=None)
    proc.stderr = stderr
    proc.stdout = MagicMock()
    proc.stdin = MagicMock()

    logged: list[tuple] = []

    def capture_debug(msg, *args):
        logged.append((msg, args))

    def start_thread(fn, **kw):
        import threading

        thread = threading.Thread(target=fn, daemon=True, name=kw.get("name", "t"))
        thread.start()
        return thread

    with patch("plugin.scripting.editor_bridge.run_in_background", side_effect=start_thread):
        with patch("plugin.scripting.editor_bridge.log") as mock_log:
            mock_log.debug.side_effect = capture_debug
            editor.start(proc)
            write_handle.write(b"line one\nline two\n")
            write_handle.flush()
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if any("line two" in str(a) for _, a in logged):
                    break
                time.sleep(0.05)
            proc.poll.return_value = 0
            write_handle.write(b"final line\n")
            write_handle.flush()
            write_handle.close()
            time.sleep(0.2)

    assert any("editor child:" in str(msg) for msg, _ in logged)
    tail = editor.read_stderr_tail()
    assert "line one" in tail or "line two" in tail or "final line" in tail
