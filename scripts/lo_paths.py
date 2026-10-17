#!/usr/bin/env python3
# WriterAgent — locate LibreOffice programs and build profile URLs
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Find LibreOffice's soffice, unopkg and python; build ``-env:UserInstallation`` URLs.

Standard library only, so it runs under the project ``.venv`` and under
LibreOffice's bundled python.

    python scripts/lo_paths.py soffice|unopkg|python
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# File names relative to the program dir (the one holding soffice).
# Windows: unopkg.com is the console launcher; unopkg.exe is GUI-subsystem, so
# its stdout and exit code cannot be captured.
_NAMES = {"win32": {"soffice": "soffice.exe", "unopkg": "unopkg.com", "python": "python.exe"}, "darwin": {"soffice": "soffice", "unopkg": "unopkg", "python": "../Resources/python"}, "linux": {"soffice": "soffice", "unopkg": "unopkg", "python": "python"}}

# Linux distro and TDF tarball locations, tried after soffice on PATH.
_LINUX_DIRS = ("/usr/lib/libreoffice/program", "/usr/lib64/libreoffice/program")
_LINUX_OPT_GLOB = "libreoffice*/program"

# Distro builds have no program/python; python3-uno binds to the system interpreter.
_LINUX_SYSTEM_PYTHON = "/usr/bin/python3"


def _platform() -> str:
    return sys.platform if sys.platform in ("win32", "darwin") else "linux"


def _registry_install_path() -> Path | None:
    """``HKLM\\SOFTWARE\\LibreOffice\\UNO\\InstallPath`` (the program dir), if set."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\LibreOffice\UNO\InstallPath") as key:
            value, _kind = winreg.QueryValueEx(key, "")
    except OSError:
        return None
    return Path(str(value))


def _candidate_dirs() -> Iterator[Path]:
    plat = _platform()
    if plat == "win32":
        reg = _registry_install_path()
        if reg is not None:
            yield reg
        for var in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(var)
            if root:
                yield Path(root) / "LibreOffice" / "program"
    elif plat == "darwin":
        for apps in (Path("/Applications"), Path.home() / "Applications"):
            yield apps / "LibreOffice.app" / "Contents" / "MacOS"
    else:
        on_path = shutil.which("soffice")
        if on_path:
            yield Path(on_path).resolve().parent
        yield from (Path(d) for d in _LINUX_DIRS)
        yield from sorted(Path("/opt").glob(_LINUX_OPT_GLOB), reverse=True)


def program_dir() -> Path | None:
    """The LibreOffice program dir (holds soffice), or None if not installed."""
    soffice = _NAMES[_platform()]["soffice"]
    for candidate in _candidate_dirs():
        if (candidate / soffice).is_file():
            return candidate
    return None


def find(name: str) -> Path | None:
    """Path to ``soffice``, ``unopkg`` or ``python`` for the installed LibreOffice."""
    plat = _platform()
    prog = program_dir()
    if prog is not None:
        path = Path(os.path.normpath(prog / _NAMES[plat][name]))
        if path.is_file():
            return path
    if name == "python" and plat == "linux" and Path(_LINUX_SYSTEM_PYTHON).is_file():
        return Path(_LINUX_SYSTEM_PYTHON)
    return None


def profile_url(profile: Path | str) -> str:
    """``file://`` URL for ``-env:UserInstallation``. Build it here and nowhere else.

    unopkg and soffice must get the same string: LibreOffice names its
    single-instance pipe after an MD5 of it. *profile* is the dir that will
    hold ``user/``; passing ``.../user`` makes LibreOffice nest ``user/user``.
    """
    path = Path(profile).resolve()
    if path.name == "user":
        raise ValueError("%s is a profile's inner user/ dir; pass its parent" % path)
    return path.as_uri()


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in _NAMES["linux"]:
        print("usage: lo_paths.py soffice|unopkg|python", file=sys.stderr)
        return 2
    path = find(argv[0])
    if path is None:
        print("LibreOffice %s not found" % argv[0], file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
