#!/usr/bin/env python3
# WriterAgent — locate LibreOffice programs and build profile URLs
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Find LibreOffice's soffice, unopkg and python; build ``-env:UserInstallation`` URLs.

Standard library only, so it runs under the project ``.venv`` and under
LibreOffice's bundled python.

    python scripts/lo_paths.py soffice|unopkg|python
    python scripts/lo_paths.py find lo-python
    python scripts/lo_paths.py check-lo-python --python <path>
    python scripts/lo_paths.py run-lo-python --python <path> [--set K=V]... -- <args...>
"""

from __future__ import annotations

import argparse
import os
import shlex
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
_NAMES = {
    "win32": {"soffice": "soffice.exe", "unopkg": "unopkg.com", "python": "python.exe"},
    "darwin": {"soffice": "soffice", "unopkg": "unopkg", "python": "../Resources/python"},
    "linux": {"soffice": "soffice", "unopkg": "unopkg", "python": "python"},
}

# Linux distro and TDF tarball locations, tried after soffice on PATH.
_LINUX_DIRS = ("/usr/lib/libreoffice/program", "/usr/lib64/libreoffice/program")
_LINUX_OPT_GLOB = "libreoffice*/program"

# Distro builds have no program/python; python3-uno binds to the system interpreter.
_LINUX_SYSTEM_PYTHON = "/usr/bin/python3"

# Snap LibreOffice program directory.
_SNAP_PROGRAM_DIR = "/snap/libreoffice/current/lib/libreoffice/program"

# Linux system python library roots searched for uno.py.
_LINUX_PYTHON_LIB_DIRS = ("/usr/lib", "/usr/lib64")

# Standard macOS app locations.
_DARWIN_APPLICATIONS_DIR = Path("/Applications")
_DARWIN_CASKROOM_DIRS = (
    Path("/opt/homebrew/Caskroom/libreoffice"),
    Path("/usr/local/Caskroom/libreoffice"),
)

_STRIP_VENV_KEYS = ("VIRTUAL_ENV", "__PYVENV_LAUNCHER__", "PYTHONHOME")

_CHECK_EMPTY_MSG = """\
error: LO_PYTHON is empty — no interpreter that can import uno was found.
LibreOffice pyuno cannot be hosted by an arbitrary CPython (macOS CI
segfaulted in ~150ms with no output when the project venv was used).
Looked for:
  macOS: /Applications/LibreOffice.app/Contents/Resources/python
         (official wrapper; also Homebrew Caskroom LibreOffice.app)
         fallback: LibreOfficePython.framework/.../bin/python3
  Linux: /usr/bin/python3 (python3-uno), then python3 or python on PATH
  Windows: LibreOffice/program/python.exe
Set LO_PYTHON to LibreOffice's bundled interpreter — not the project .venv.\
"""

_CHECK_FAIL_MSG = """\
error: %s cannot import officehelper and uno.
On macOS use Contents/Resources/python (sets URE_BOOTSTRAP / PYTHONPATH)
or set those vars yourself. officehelper.py lives in Contents/Resources.
Do not fall back to the project .venv — that CPython cannot host pyuno.\
"""


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
        for cask_root in _DARWIN_CASKROOM_DIRS:
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
        for cask_root in _DARWIN_CASKROOM_DIRS:
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


def _darwin_candidate_apps() -> list[Path]:
    apps = [_DARWIN_APPLICATIONS_DIR / "LibreOffice.app"]
    for cask in _DARWIN_CASKROOM_DIRS:
        if cask.is_dir():
            apps.extend(sorted(cask.glob("*/LibreOffice.app")))
    apps.append(Path.home() / "Applications" / "LibreOffice.app")
    return [a for a in apps if a.is_dir()]


def _probe_uno(interpreter: Path | str) -> bool:
    """Check if the interpreter can import uno in a subprocess without venv leaks."""
    env = os.environ.copy()
    for key in _STRIP_VENV_KEYS:
        env.pop(key, None)
    try:
        res = subprocess.run(
            [str(interpreter), "-c", "import uno"],
            env=env,
            capture_output=True,
            timeout=5,
        )
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def find_lo_python() -> Path | None:
    """Discover a Python interpreter capable of importing pyuno."""
    plat = _platform()
    if plat == "darwin":
        apps = _darwin_candidate_apps()
        candidates = [
            *(app / "Contents" / "Resources" / "python" for app in apps),
            *(
                app
                / "Contents"
                / "Frameworks"
                / "LibreOfficePython.framework"
                / "Versions"
                / "Current"
                / "bin"
                / "python3"
                for app in apps
            ),
        ]
        for cand in candidates:
            if cand.is_file():
                return cand
        return None

    if plat == "win32":
        return find("python")

    # Linux: /usr/bin/python3 then shutil.which("python3") and shutil.which("python")
    candidates = [Path(_LINUX_SYSTEM_PYTHON)] if Path(_LINUX_SYSTEM_PYTHON).is_file() else []
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found and Path(found) not in candidates:
            candidates.append(Path(found))
    for cand in candidates:
        if _probe_uno(cand):
            return cand
    return None


def lo_python_env(interpreter: Path | str) -> dict[str, str]:
    """Environment variables for LibreOffice Python, non-empty only on Darwin."""
    if _platform() != "darwin":
        return {}
    interp_path = Path(interpreter)
    paths_to_check = [interp_path]
    try:
        paths_to_check.append(interp_path.resolve())
    except OSError:
        pass
    app = next(
        (p for candidate in paths_to_check for p in (candidate, *candidate.parents) if p.name.endswith(".app")),
        None,
    )
    if app is None:
        return {}
    contents = app / "Contents"
    rc = contents / "Resources" / "fundamentalrc"
    ini = contents / "Resources" / "fundamental.ini"
    fundamental = rc if rc.is_file() else (ini if ini.is_file() else rc)
    return {
        "UNO_PATH": str(contents / "MacOS"),
        "URE_BOOTSTRAP": f"vnd.sun.star.pathname:{fundamental}",
        "PYTHONPATH": f"{contents / 'Resources'}{os.pathsep}{contents / 'Frameworks'}",
    }


def build_child_env(
    interpreter: Path | str,
    *,
    set_vars: dict[str, str] | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Construct execution environment for LibreOffice Python child process."""
    env = (base_env if base_env is not None else os.environ).copy()
    for key in _STRIP_VENV_KEYS:
        env.pop(key, None)

    if "LO_PYTHON_ENV" in env:
        lo_env_str = env.pop("LO_PYTHON_ENV")
        if lo_env_str.strip():
            for item in shlex.split(lo_env_str):
                if "=" in item:
                    k, v = item.split("=", 1)
                    env[k] = v
    else:
        env.update(lo_python_env(interpreter))

    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("WRITERAGENT_PROJECT_ROOT", str(Path(__file__).resolve().parents[1]))
    if set_vars:
        env.update(set_vars)

    return env


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lo_paths.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("soffice", "unopkg", "python"):
        sub.add_parser(name)

    find_p = sub.add_parser("find")
    find_p.add_argument("target", choices=["lo-python"])

    check_p = sub.add_parser("check-lo-python")
    check_p.add_argument("--python", default="")

    run_p = sub.add_parser("run-lo-python")
    run_p.add_argument("--python", default="")
    run_p.add_argument("--set", action="append", default=[], dest="set_vars")
    run_p.add_argument("child_args", nargs=argparse.REMAINDER)

    return parser


def check_lo_python(args: argparse.Namespace | list[str]) -> int:
    """Validate LibreOffice Python interpreter and probe UNO import."""
    parsed: argparse.Namespace
    if isinstance(args, list):
        try:
            parsed = _build_parser().parse_args(["check-lo-python", *args])
        except SystemExit as exc:
            return int(exc.code) if isinstance(exc.code, int) else 2
    else:
        parsed = args

    if not parsed.python or not parsed.python.strip():
        print(_CHECK_EMPTY_MSG, file=sys.stderr)
        return 1

    interp = parsed.python.strip()
    print("LO_PYTHON=%s" % interp)
    snippet = (
        "from plugin.testing_runner import _ensure_libreoffice_python_path; "
        "_ensure_libreoffice_python_path(); "
        "import officehelper, uno; "
        "print('officehelper=' + officehelper.__file__); "
        "print('uno=' + uno.__file__)"
    )
    env = build_child_env(interp)
    try:
        res = subprocess.run([interp, "-c", snippet], env=env)
        if res.returncode != 0:
            print(_CHECK_FAIL_MSG % interp, file=sys.stderr)
            return 1
    except OSError:
        print(_CHECK_FAIL_MSG % interp, file=sys.stderr)
        return 1
    return 0


def run_lo_python(args: argparse.Namespace | list[str]) -> int:
    """Execute command under LibreOffice Python environment."""
    parsed: argparse.Namespace
    if isinstance(args, list):
        try:
            parsed = _build_parser().parse_args(["run-lo-python", *args])
        except SystemExit as exc:
            return int(exc.code) if isinstance(exc.code, int) else 2
    else:
        parsed = args

    set_vars: dict[str, str] = {}
    for pair in parsed.set_vars:
        if "=" not in pair:
            print("error: --set requires K=V", file=sys.stderr)
            return 2
        k, v = pair.split("=", 1)
        set_vars[k] = v

    if not parsed.python or not parsed.python.strip():
        print("error: LO_PYTHON is empty", file=sys.stderr)
        return 1

    interp = parsed.python.strip()
    child_args = parsed.child_args
    if child_args and child_args[0] == "--":
        child_args = child_args[1:]

    env = build_child_env(interp, set_vars=set_vars)
    cmd = [interp, *child_args]
    try:
        res = subprocess.run(cmd, env=env)
        return res.returncode
    except OSError as exc:
        print("error: failed to execute %s: %s" % (interp, exc), file=sys.stderr)
        return 1


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


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    if args.cmd in ("soffice", "unopkg", "python"):
        path = find(args.cmd)
        if path is None:
            print("LibreOffice %s not found" % args.cmd, file=sys.stderr)
            return 1
        print(path)
        return 0

    if args.cmd == "find":
        path = find_lo_python()
        if path is None:
            print("LibreOffice python not found", file=sys.stderr)
            return 1
        print(path)
        return 0

    if args.cmd == "check-lo-python":
        return check_lo_python(args)

    if args.cmd == "run-lo-python":
        return run_lo_python(args)

    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
