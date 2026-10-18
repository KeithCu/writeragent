# WriterAgent — tests for scripts/lo_kill.py
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Profile-scoped LibreOffice kill: parsing, matching, and real processes we start ourselves."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts import lo_kill, lo_paths

if TYPE_CHECKING:
    from collections.abc import Callable

URL_A = "file:///tmp/profile-a"
URL_B = "file:///tmp/profile-a2"


def _fake(monkeypatch: pytest.MonkeyPatch, procs: list[tuple[int, str, list[str]]]) -> list[int]:
    killed: list[int] = []
    monkeypatch.setattr(lo_kill, "_processes", lambda: procs)
    monkeypatch.setattr(lo_kill, "kill", killed.extend)
    return killed


FAKE = [
    (10, "oosplash", ["/lo/program/oosplash", "--headless", "-env:UserInstallation=" + URL_A]),
    (11, "soffice.bin", ["/lo/program/soffice.bin", "-env:UserInstallation=" + URL_A, "--headless"]),
    (12, "soffice.bin", ["/lo/program/soffice.bin", "-env:UserInstallation=" + URL_B]),
    (13, "soffice.bin", ["/lo/program/soffice.bin", "--writer"]),  # the user's own office
    (14, "python3", ["python3", "-env:UserInstallation=" + URL_A]),  # not LibreOffice
]


def test_profile_matches_exact_url_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, FAKE)
    assert lo_kill.office_pids(URL_A) == [10, 11]
    assert lo_kill.office_pids(URL_B) == [12]
    assert lo_kill.office_pids(None) == [10, 11, 12, 13]


def test_main_profile_and_all(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile = tmp_path / "p"
    url = lo_paths.profile_url(profile)
    killed = _fake(monkeypatch, [*FAKE, (20, "soffice.bin", ["soffice.bin", "-env:UserInstallation=" + url])])
    assert lo_kill.main(["--profile", str(profile)]) == 0
    assert killed == [20]
    killed.clear()
    assert lo_kill.main(["--all"]) == 0
    assert killed == [10, 11, 12, 13, 20]
    with pytest.raises(SystemExit):
        lo_kill.main([])


def test_parse_ps() -> None:
    text = "  101 /usr/lib/libreoffice/program/soffice.bin -env:UserInstallation=file:///t/a --headless\n  7 init\nbogus\n"
    assert lo_kill._parse_ps(text) == [(101, "soffice.bin", ["/usr/lib/libreoffice/program/soffice.bin", "-env:UserInstallation=file:///t/a", "--headless"]), (7, "init", ["init"])]


def test_parse_cim() -> None:
    text = '4242\tsoffice.bin\t"C:\\Program Files\\LibreOffice\\program\\soffice.bin" "-env:UserInstallation=file:///C:/T/a%20b" --headless\r\n4\tSystem\t\r\n'
    procs = lo_kill._parse_cim(text)
    assert procs[0][:2] == (4242, "soffice.bin")
    assert "-env:UserInstallation=file:///C:/T/a%20b" in procs[0][2]
    assert procs[1] == (4, "System", [])


def _wait_for(pred: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.2)
    return False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinked stand-in")
def test_kills_only_our_profile_real_processes(tmp_path: Path) -> None:
    """Two stand-in 'soffice' processes on different profiles: only ours dies."""
    fake = tmp_path / "soffice"
    # macOS framework python3 re-execs into Python.app, so ps would show "Python", not
    # "soffice". Link straight to that inner binary so argv[0] stays our name.
    app_python = os.path.join(sys.base_prefix, "Resources", "Python.app", "Contents", "MacOS", "Python")
    fake.symlink_to(app_python if sys.platform == "darwin" and os.path.isfile(app_python) else os.path.realpath(sys.executable))
    url_a, url_b = lo_paths.profile_url(tmp_path / "a"), lo_paths.profile_url(tmp_path / "a2")
    sleep = "import time; time.sleep(120)"
    ours = subprocess.Popen([str(fake), "-c", sleep, "-env:UserInstallation=" + url_a])
    other = subprocess.Popen([str(fake), "-c", sleep, "-env:UserInstallation=" + url_b])
    try:
        assert _wait_for(lambda: lo_kill.office_pids(url_a) == [ours.pid])
        assert lo_kill.main(["--profile", str(tmp_path / "a")]) == 0
        assert ours.wait(timeout=10) != 0
        assert other.poll() is None
        assert lo_kill.office_pids(url_b) == [other.pid]
    finally:
        for proc in (ours, other):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


@pytest.mark.skipif(lo_paths.find("soffice") is None, reason="LibreOffice not installed")
def test_kills_real_headless_office(tmp_path: Path) -> None:
    """A real headless soffice on a scratch profile and HOME is found by URL and killed."""
    soffice = lo_paths.find("soffice")
    assert soffice is not None
    url = lo_paths.profile_url(tmp_path / "profile")
    env = {**os.environ, "HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "home" / ".config")}
    proc = subprocess.Popen([str(soffice), "--headless", "--norestore", "--nologo", "-env:UserInstallation=" + url], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # oosplash + soffice.bin (Linux), soffice.exe + soffice.bin (Windows), one soffice (macOS).
        want = 1 if sys.platform == "darwin" else 2
        assert _wait_for(lambda: len(lo_kill.office_pids(url)) >= want, timeout=60)
        lo_kill.kill(lo_kill.office_pids(url))
        assert _wait_for(lambda: not lo_kill.office_pids(url))
    finally:
        lo_kill.kill(lo_kill.office_pids(url))
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
