# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.embeddings_soffice_convert."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plugin.embeddings import embeddings_soffice_convert as convert
from plugin.framework.managed_subprocess import ManagedSubprocess


def test_legacy_odf_filter_mapping():
    assert convert.legacy_odf_filter(".doc") == "odt"
    assert convert.legacy_odf_filter(".xls") == "ods"
    assert convert.legacy_odf_filter(".ppt") == "odp"
    assert convert.legacy_odf_filter(".pdf") is None


def test_convert_legacy_to_odf_no_soffice(tmp_path: Path):
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"binary")
    with patch.object(convert, "resolve_soffice_executable", return_value=None):
        assert convert.convert_legacy_to_odf(str(source)) is None


def test_convert_legacy_to_odf_success(tmp_path: Path):
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"binary")
    produced = tmp_path / "legacy.odt"
    produced.write_text("converted", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        out_dir = Path(cmd[cmd.index("--outdir") + 1])
        (out_dir / "legacy.odt").write_text("converted", encoding="utf-8")
        return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(convert, "resolve_soffice_executable", return_value="/usr/bin/soffice"):
        with patch(
            "plugin.embeddings.embeddings_soffice_convert.ManagedSubprocess.run_with_process_tree",
            side_effect=fake_run,
        ):
            result = convert.convert_legacy_to_odf(str(source))
    assert result is not None
    assert result.is_file()
    assert result.read_text(encoding="utf-8") == "converted"
    result.unlink(missing_ok=True)


def test_run_with_process_tree_timeout_calls_kill_process_tree(monkeypatch):
    import plugin.framework.managed_subprocess as ms

    proc = MagicMock()
    proc.poll.return_value = None
    killed: list[object] = []
    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(ms, "kill_process_tree", lambda p: killed.append(p))
    proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd=["x"], timeout=1.0),
        ("", ""),
    ]

    with pytest.raises(subprocess.TimeoutExpired):
        ManagedSubprocess.run_with_process_tree(["x"], capture_output=True, timeout=1.0)

    assert killed == [proc]


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX process group integration")
def test_run_with_process_tree_timeout_reaps_grandchild():
    """Timeout must not leave a forked grandchild running (soffice.bin pattern)."""
    code = (
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(300)\n"
        "    os._exit(0)\n"
        "sys.stdout.write(str(pid) + '\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(300)\n"
    )
    gpid: int | None = None
    try:
        with pytest.raises(subprocess.TimeoutExpired) as exc_info:
            ManagedSubprocess.run_with_process_tree(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=0.5,
            )
        out = exc_info.value.stdout or ""
        gpid = int(out.strip().splitlines()[0])
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _pid_is_alive(gpid):
            time.sleep(0.05)
        assert not _pid_is_alive(gpid), f"grandchild {gpid} survived convert-style timeout"
    finally:
        if gpid is not None and _pid_is_alive(gpid):
            try:
                os.kill(gpid, signal.SIGKILL)
            except OSError:
                pass
