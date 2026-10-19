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
import subprocess
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

# Snap LibreOffice program directory.
_SNAP_PROGRAM_DIR = "/snap/libreoffice/current/lib/libreoffice/program"

# Linux system python library roots searched for uno.py.
_LINUX_PYTHON_LIB_DIRS = ("/usr/lib", "/usr/lib64")

# macOS Homebrew cask roots.
_DARWIN_CASK_ROOTS = (
    Path("/opt/homebrew/Caskroom/libreoffice"),
    Path("/usr/local/Caskroom/libreoffice"),
)


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
        for cask_root in _DARWIN_CASK_ROOTS:
            if cask_root.is_dir():
                for app in sorted(cask_root.glob("*/LibreOffice.app")):
                    yield app / "Contents" / "MacOS"
    else:
        on_path = shutil.which("soffice")
        if on_path:
            yield Path(on_path).resolve().parent
        yield from (Path(d) for d in _LINUX_DIRS)
        yield from sorted(Path("/opt").glob(_LINUX_OPT_GLOB), reverse=True)
        yield Path(_SNAP_PROGRAM_DIR)


def uno_link_dirs(venv_py: tuple[int, int] | None = None) -> tuple[Path | None, Path | None]:
    """Return (uno_dir, extra_lo_program_dir).

    Finds the directory containing uno.py and optionally a separate program
    directory containing pyuno.so needed on sys.path.
    """
    plat = _platform()
    if plat == "darwin":
        darwin_apps: list[Path] = []
        on_path = shutil.which("soffice")
        if on_path:
            resolved = Path(on_path).resolve()
            if resolved.name == "soffice" and resolved.parent.name == "MacOS" and resolved.parent.parent.name == "Contents":
                darwin_apps.append(resolved.parent.parent.parent)
        darwin_apps.extend([
            Path("/Applications/LibreOffice.app"),
            Path.home() / "Applications" / "LibreOffice.app",
        ])
        for cask_root in _DARWIN_CASK_ROOTS:
            if cask_root.is_dir():
                darwin_apps.extend(sorted(cask_root.glob("*/LibreOffice.app")))
        for app in darwin_apps:
            res = app / "Contents" / "Resources"
            if (res / "uno.py").is_file():
                # macOS: expose only Resources (uno.py), not Frameworks (pyuno.so), to avoid runtime pyuno crash in external venv.
                return res, None
        return None, None

    if plat == "win32":
        for exe in ("soffice.exe", "soffice", "unopkg.exe"):
            found = shutil.which(exe)
            if found:
                cand = Path(found).resolve().parent
                if (cand / "uno.py").is_file():
                    return cand, None
        prog = program_dir()
        if prog is not None and (prog / "uno.py").is_file():
            return prog, None
        return None, None

    # Linux uno.py search order
    uno_dir: Path | None = None

    # 1. /usr/lib and /usr/lib64 python{major}.{minor} site-packages and dist-packages for venv_py,
    # then /usr/lib/python3/dist-packages.
    if venv_py is not None:
        major, minor = venv_py
        versioned_candidates: list[Path] = []
        for base in _LINUX_PYTHON_LIB_DIRS:
            versioned_candidates.append(Path(base) / f"python{major}.{minor}" / "site-packages")
        for base in _LINUX_PYTHON_LIB_DIRS:
            versioned_candidates.append(Path(base) / f"python{major}.{minor}" / "dist-packages")
        for base in _LINUX_PYTHON_LIB_DIRS:
            versioned_candidates.append(Path(base) / "python3" / "dist-packages")
        for cand in versioned_candidates:
            if (cand / "uno.py").is_file():
                uno_dir = cand
                break

    # 2. Other /usr/lib/python3.*/site-packages and dist-packages hits.
    if uno_dir is None:
        hits: list[Path] = []
        for base in _LINUX_PYTHON_LIB_DIRS:
            base_path = Path(base)
            if not base_path.is_dir():
                continue
            for pkg in ("site-packages", "dist-packages"):
                for py_dir in sorted(base_path.glob(f"python3.*/{pkg}"), reverse=True):
                    if (py_dir / "uno.py").is_file() and py_dir not in hits:
                        hits.append(py_dir)
        if len(hits) == 1:
            uno_dir = hits[0]
        elif len(hits) > 1:
            probe_dir: Path | None = None
            if Path(_LINUX_SYSTEM_PYTHON).is_file():
                try:
                    res = subprocess.run(
                        [_LINUX_SYSTEM_PYTHON, "-c", "import uno; print(uno.__file__)"],
                        capture_output=True,
                        text=True,
                        timeout=20,
                        check=True,
                    )
                    out_path = Path(res.stdout.strip())
                    if out_path.is_file():
                        probe_dir = out_path.parent
                except (subprocess.SubprocessError, OSError):
                    pass
            uno_dir = probe_dir if probe_dir is not None else hits[0]

    # 3. Snap program dir, then program_dir(), if that directory contains uno.py.
    if uno_dir is None:
        snap_dir = Path(_SNAP_PROGRAM_DIR)
        if (snap_dir / "uno.py").is_file():
            uno_dir = snap_dir
        else:
            prog = program_dir()
            if prog is not None and (prog / "uno.py").is_file():
                uno_dir = prog

    extra_prog: Path | None = None
    if uno_dir is not None:
        prog = program_dir()
        if prog is not None and (prog / "pyuno.so").is_file():
            try:
                is_same = prog.resolve() == uno_dir.resolve()
            except OSError:
                is_same = os.path.normcase(os.path.abspath(prog)) == os.path.normcase(os.path.abspath(uno_dir))
            if not is_same:
                extra_prog = prog

    return uno_dir, extra_prog


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
