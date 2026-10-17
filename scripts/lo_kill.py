#!/usr/bin/env python3
# WriterAgent — kill the LibreOffice processes that use one profile
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Kill LibreOffice processes started on one profile, or every one with ``--all``.

    python scripts/lo_kill.py --profile DIR   # only soffice/oosplash with -env:UserInstallation=<DIR url>
    python scripts/lo_kill.py --all           # old ``make lo-kill`` behaviour

``--profile`` matches the exact ``-env:UserInstallation=`` argument, so an office the
user has open on their own profile is never touched. Standard library only: ``ps`` on
Linux/macOS, PowerShell ``Win32_Process`` on Windows.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.lo_paths import profile_url  # noqa: E402

_NAMES = frozenset({"soffice", "soffice.bin", "soffice.exe", "oosplash"})

_PS_ARGS = ["ps", "-axww", "-o", "pid=", "-o", "args="]
# Single quotes only: double quotes do not survive the Windows command line into powershell -Command.
_CIM_SCRIPT = "Get-CimInstance Win32_Process | ForEach-Object { '{0}{3}{1}{3}{2}' -f $_.ProcessId, $_.Name, $_.CommandLine, [char]9 }"


def _parse_ps(text: str) -> list[tuple[int, str, list[str]]]:
    """``ps -o pid= -o args=`` lines -> (pid, name, args). Name is argv[0]'s basename."""
    procs: list[tuple[int, str, list[str]]] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].isdigit():
            procs.append((int(fields[0]), os.path.basename(fields[1]), fields[1:]))
    return procs


def _parse_cim(text: str) -> list[tuple[int, str, list[str]]]:
    """``pid<TAB>Name<TAB>CommandLine`` lines -> (pid, name, args with quotes stripped)."""
    procs: list[tuple[int, str, list[str]]] = []
    for line in text.splitlines():
        fields = line.split("\t", 2)
        if len(fields) == 3 and fields[0].isdigit():
            procs.append((int(fields[0]), fields[1], [arg.strip('"') for arg in fields[2].split()]))
    return procs


def _processes() -> list[tuple[int, str, list[str]]]:
    if sys.platform == "win32":
        out = subprocess.run(["powershell", "-NoProfile", "-Command", _CIM_SCRIPT], capture_output=True, text=True, check=True).stdout
        return _parse_cim(out)
    return _parse_ps(subprocess.run(_PS_ARGS, capture_output=True, text=True, check=True).stdout)


def office_pids(url: str | None) -> list[int]:
    """PIDs of LibreOffice processes started with ``-env:UserInstallation=<url>``; all of them if *url* is None."""
    wanted = None if url is None else "-env:UserInstallation=" + url
    return [pid for pid, name, args in _processes() if pid != os.getpid() and name in _NAMES and (wanted is None or wanted in args)]


def kill(pids: list[int]) -> None:
    for pid in pids:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Kill LibreOffice processes started on one profile, or all of them.")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--profile", help="profile dir passed to soffice as -env:UserInstallation")
    which.add_argument("--all", action="store_true", help="every LibreOffice process, whatever its profile")
    args = parser.parse_args(argv)
    pids = office_pids(None if args.all else profile_url(args.profile))
    kill(pids)
    print("[OK] Killed %d LibreOffice process(es)%s" % (len(pids), (": " + " ".join(map(str, pids))) if pids else "."))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
