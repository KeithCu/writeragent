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


def _darwin_candidate_apps() -> list[Path]:
    apps: list[Path] = []
    main_app = _DARWIN_APPLICATIONS_DIR / "LibreOffice.app"
    if main_app.is_dir():
        apps.append(main_app)
    for cask in _DARWIN_CASKROOM_DIRS:
        if cask.is_dir():
            apps.extend(sorted(cask.glob("*/LibreOffice.app")))
    user_app = Path.home() / "Applications" / "LibreOffice.app"
    if user_app.is_dir():
        apps.append(user_app)
    return apps


def _is_venv_or_self(path: Path) -> bool:
    try:
        resolved = path.resolve()
        if resolved == Path(sys.executable).resolve():
            return True
        if (resolved.parent / "pyvenv.cfg").is_file() or (resolved.parent.parent / "pyvenv.cfg").is_file():
            return True
        venv_env = os.environ.get("VIRTUAL_ENV")
        if venv_env:
            try:
                resolved.relative_to(Path(venv_env).resolve())
                return True
            except ValueError:
                pass
    except (OSError, ValueError):
        pass
    return False


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
        # Prefer official wrapper; fallback to framework binary (CI 33708366478).
        for app in apps:
            py = app / "Contents" / "Resources" / "python"
            if py.is_file():
                return py
        for app in apps:
            fw_current = (
                app
                / "Contents"
                / "Frameworks"
                / "LibreOfficePython.framework"
                / "Versions"
                / "Current"
                / "bin"
                / "python3"
            )
            if fw_current.is_file():
                return fw_current
            versions_dir = app / "Contents" / "Frameworks" / "LibreOfficePython.framework" / "Versions"
            if versions_dir.is_dir():
                for cand in sorted(versions_dir.glob("*/bin/python3")):
                    if cand.is_file() and cand != fw_current:
                        return cand
        return None

    if plat == "win32":
        return find("python")

    # Distro python3-uno before PATH python3 (CI 33453027089, 33456719039).
    sys_py3 = Path(_LINUX_SYSTEM_PYTHON)
    if sys_py3.is_file() and not _is_venv_or_self(sys_py3):
        if _probe_uno(sys_py3):
            return sys_py3

    seen: set[Path] = set()
    if sys_py3.is_file():
        try:
            seen.add(sys_py3.resolve())
        except OSError:
            pass

    path_env = os.environ.get("PATH", "")
    for name in ("python3", "python"):
        for dir_str in path_env.split(os.pathsep):
            if not dir_str:
                continue
            cand = Path(dir_str) / name
            try:
                if not cand.is_file() or not os.access(cand, os.X_OK):
                    continue
                cand_res = cand.resolve()
                # Skip venv interpreters to prevent segfaults (CI 33447724981).
                if cand_res in seen or _is_venv_or_self(cand):
                    continue
                seen.add(cand_res)
                if _probe_uno(cand):
                    return cand
            except OSError:
                continue
    return None


def lo_python_env(interpreter: Path | str) -> dict[str, str]:
    """Environment variables for LibreOffice Python, non-empty only on Darwin."""
    if _platform() != "darwin":
        return {}
    interp_path = Path(interpreter)
    app: Path | None = None
    for parent in (interp_path, *interp_path.parents):
        if parent.suffix == ".app" or parent.name.endswith(".app"):
            app = parent
            break
    if app is None:
        try:
            resolved = interp_path.resolve()
            for parent in (resolved, *resolved.parents):
                if parent.suffix == ".app" or parent.name.endswith(".app"):
                    app = parent
                    break
        except OSError:
            pass
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
        for k, v in lo_python_env(interpreter).items():
            env[k] = v

    env["PYTHONUNBUFFERED"] = "1"
    if "WRITERAGENT_PROJECT_ROOT" not in env:
        env["WRITERAGENT_PROJECT_ROOT"] = str(Path(__file__).resolve().parents[1])

    if set_vars:
        for k, v in set_vars.items():
            env[k] = v

    return env


def check_lo_python(argv: list[str]) -> int:
    """Validate LibreOffice Python interpreter and probe UNO import."""
    python_arg: str | None = None
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg == "--python":
            if idx + 1 < len(argv):
                python_arg = argv[idx + 1]
                idx += 2
                continue
            python_arg = ""
            idx += 1
            continue
        if arg.startswith("--python="):
            python_arg = arg.split("=", 1)[1]
            idx += 1
            continue
        print("usage: lo_paths.py check-lo-python --python <path>", file=sys.stderr)
        return 2

    if not python_arg or not python_arg.strip():
        print(_CHECK_EMPTY_MSG, file=sys.stderr)
        return 1

    interp = python_arg.strip()
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


def run_lo_python(argv: list[str]) -> int:
    """Execute command under LibreOffice Python environment."""
    python_arg: str | None = None
    set_vars: dict[str, str] = {}
    child_args: list[str] = []

    if "--" in argv:
        split_idx = argv.index("--")
        opts = argv[:split_idx]
        child_args = argv[split_idx + 1 :]
    else:
        opts = argv
        child_args = []

    idx = 0
    while idx < len(opts):
        opt = opts[idx]
        if opt == "--python":
            if idx + 1 >= len(opts):
                print("error: --python requires an argument", file=sys.stderr)
                return 2
            python_arg = opts[idx + 1]
            idx += 2
        elif opt.startswith("--python="):
            python_arg = opt.split("=", 1)[1]
            idx += 1
        elif opt == "--set":
            if idx + 1 >= len(opts):
                print("error: --set requires K=V", file=sys.stderr)
                return 2
            pair = opts[idx + 1]
            if "=" not in pair:
                print("error: --set requires K=V", file=sys.stderr)
                return 2
            k, v = pair.split("=", 1)
            set_vars[k] = v
            idx += 2
        elif opt.startswith("--set="):
            pair = opt.split("=", 1)[1]
            if "=" not in pair:
                print("error: --set requires K=V", file=sys.stderr)
                return 2
            k, v = pair.split("=", 1)
            set_vars[k] = v
            idx += 1
        else:
            print(
                "usage: lo_paths.py run-lo-python --python <path> [--set K=V]... -- <args...>",
                file=sys.stderr,
            )
            return 2

    if not python_arg or not python_arg.strip():
        print("error: LO_PYTHON is empty", file=sys.stderr)
        return 1

    interp = python_arg.strip()
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


def main(argv: list[str]) -> int:
    if not argv:
        print(
            "usage: lo_paths.py soffice|unopkg|python | find lo-python | check-lo-python | run-lo-python",
            file=sys.stderr,
        )
        return 2

    verb = argv[0]
    if verb in _NAMES["linux"] and len(argv) == 1:
        path = find(verb)
        if path is None:
            print("LibreOffice %s not found" % verb, file=sys.stderr)
            return 1
        print(path)
        return 0

    if verb == "find":
        if len(argv) == 2 and argv[1] == "lo-python":
            path = find_lo_python()
            if path is None:
                print("LibreOffice python not found", file=sys.stderr)
                return 1
            print(path)
            return 0
        print("usage: lo_paths.py find lo-python", file=sys.stderr)
        return 2

    if verb == "check-lo-python":
        return check_lo_python(argv[1:])

    if verb == "run-lo-python":
        return run_lo_python(argv[1:])

    print(
        "usage: lo_paths.py soffice|unopkg|python | find lo-python | check-lo-python | run-lo-python",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
