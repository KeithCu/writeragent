#!/usr/bin/env python3
"""Link system UNO into the project .venv for static analysis (ty/mypy/pyright).

Safe to run repeatedly: skips work when uno.pth contains the marker line,
matches expected path lines, and ``import uno`` works.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum, auto
import glob
import os
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts import lo_paths


class UnoLinkStatus(Enum):
    OK = auto()
    MISSING_VENV = auto()   # no directory, no interpreter, or no site-packages
    NO_UNO = auto()         # venv is usable, import uno fails, no uno.py found
    NEEDS_FIX = auto()
    FIX_FAILED = auto()


@dataclass(frozen=True)
class Probe:
    status: UnoLinkStatus
    venv_python: str | None = None
    site_packages: str | None = None
    pth_file: str | None = None
    uno_path: str | None = None
    lo_program: str | None = None
    venv_py: tuple[int, int] | None = None


def _candidate_venv_pythons(venv_base: str) -> list[str]:
    scripts_candidates = [
        os.path.join(venv_base, "Scripts", "python.exe"),
        os.path.join(venv_base, "Scripts", "python"),
    ]
    bin_candidates = [
        os.path.join(venv_base, "bin", "python"),
        os.path.join(venv_base, "bin", "python.exe"),
    ]
    if sys.platform == "win32":
        return scripts_candidates + bin_candidates
    return bin_candidates + scripts_candidates


def _find_venv_python(venv_base: str) -> str | None:
    for candidate in _candidate_venv_pythons(venv_base):
        if os.path.isfile(candidate):
            return candidate
    return None


def _venv_python_version(venv_base: str) -> tuple[int, int] | None:
    venv_python = _find_venv_python(venv_base)
    if not venv_python:
        return None
    try:
        out = subprocess.run(
            [venv_python, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        major_s, minor_s = out.stdout.strip().split()
        return int(major_s), int(minor_s)
    except subprocess.TimeoutExpired as exc:
        print(f"Error: Command timed out after 20s: {exc}", file=sys.stderr)
        return None
    except Exception:
        return None


def resolve_venv_paths(venv_base: str) -> tuple[str, str, str]:
    """Return (site_packages, venv_python, pth_file)."""
    venv_python = _find_venv_python(venv_base)
    if not venv_python:
        raise FileNotFoundError(f"No venv python found in {venv_base}")

    site_packages = None
    try:
        out = subprocess.run(
            [venv_python, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        candidate = out.stdout.strip()
        if candidate and os.path.exists(candidate):
            site_packages = candidate
    except subprocess.TimeoutExpired as exc:
        print(f"Error: Command timed out after 20s: {exc}", file=sys.stderr)
    except Exception:
        pass

    if site_packages is None:
        fallback_candidates = [os.path.join(venv_base, "Lib", "site-packages")]
        lib_dir = os.path.join(venv_base, "lib")
        if os.path.isdir(lib_dir):
            fallback_candidates.extend(os.path.join(lib_dir, d, "site-packages") for d in os.listdir(lib_dir) if d.startswith("python"))
        for candidate in fallback_candidates:
            if os.path.exists(candidate):
                site_packages = candidate
                break

    if site_packages is None:
        raise FileNotFoundError(f"No site-packages directory found in {venv_base}")

    pth_file = os.path.join(site_packages, "uno.pth")
    return site_packages, venv_python, pth_file


def uno_import_works(venv_python: str) -> bool:
    try:
        subprocess.run(
            [venv_python, "-c", "import uno"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return True
    except subprocess.TimeoutExpired as exc:
        print(f"Error: Command timed out after 20s: {exc}", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as e:
        # On macOS and Windows, we deliberately allow a configured uno.py even
        # when the real pyuno bridge cannot load in the dev venv. Static
        # checkers need import resolution and types-unopy; the extension runtime
        # uses LibreOffice's own Python/bridge.
        output = f"{e.stdout or ''}\n{e.stderr or ''}"
        if sys.platform in {"darwin", "win32"} and "pyuno" in output:
            return True
        return False
    except OSError:
        return False


def find_system_uno(*, venv_py: tuple[int, int] | None = None) -> tuple[str | None, str | None]:
    """Find the system's uno.py and LibreOffice program directory."""
    uno_dir, lo_prog = lo_paths.uno_link_dirs(venv_py=venv_py)
    return (str(uno_dir) if uno_dir is not None else None, str(lo_prog) if lo_prog is not None else None)


def _expected_pth_lines(uno_path: str, lo_program: str | None) -> list[str]:
    lines = ["# Added by scripts/fix_uno_import.py", os.path.normpath(os.path.abspath(uno_path))]
    if lo_program:
        norm_lo = os.path.normcase(os.path.abspath(lo_program))
        norm_uno = os.path.normcase(os.path.abspath(uno_path))
        if norm_lo != norm_uno:
            lines.append(os.path.normpath(os.path.abspath(lo_program)))
    return lines


def _pth_is_current(pth_file: str, uno_path: str, lo_program: str | None) -> bool:
    if not os.path.isfile(pth_file):
        return False
    try:
        with open(pth_file, encoding="utf-8") as f:
            actual = [ln.rstrip("\r\n") for ln in f.readlines()]
        expected = _expected_pth_lines(uno_path, lo_program)
        if len(actual) != len(expected):
            return False
        if actual[0] != expected[0]:
            return False
        return all(
            os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(e))
            for a, e in zip(actual[1:], expected[1:], strict=True)
        )
    except OSError:
        return False


def assess(venv_base: str) -> Probe:
    """Inspect venv and system UNO without writing changes."""
    if not os.path.isdir(venv_base):
        print(f"Error: Virtual environment not found at {venv_base}", file=sys.stderr)
        return Probe(status=UnoLinkStatus.MISSING_VENV)

    try:
        site_packages, venv_python, pth_file = resolve_venv_paths(venv_base)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return Probe(status=UnoLinkStatus.MISSING_VENV)

    venv_py = _venv_python_version(venv_base)
    uno_path, lo_program = find_system_uno(venv_py=venv_py)

    if not uno_path:
        if uno_import_works(venv_python):
            return Probe(
                status=UnoLinkStatus.OK,
                venv_python=venv_python,
                site_packages=site_packages,
                pth_file=pth_file,
                uno_path=None,
                lo_program=lo_program,
                venv_py=venv_py,
            )
        print("Error: Could not find system uno.py. Please ensure LibreOffice python-uno is installed.", file=sys.stderr)
        return Probe(
            status=UnoLinkStatus.NO_UNO,
            venv_python=venv_python,
            site_packages=site_packages,
            pth_file=pth_file,
            uno_path=None,
            lo_program=lo_program,
            venv_py=venv_py,
        )

    if _pth_is_current(pth_file, uno_path, lo_program) and uno_import_works(venv_python):
        return Probe(
            status=UnoLinkStatus.OK,
            venv_python=venv_python,
            site_packages=site_packages,
            pth_file=pth_file,
            uno_path=uno_path,
            lo_program=lo_program,
            venv_py=venv_py,
        )

    return Probe(
        status=UnoLinkStatus.NEEDS_FIX,
        venv_python=venv_python,
        site_packages=site_packages,
        pth_file=pth_file,
        uno_path=uno_path,
        lo_program=lo_program,
        venv_py=venv_py,
    )


def ensure(probe: Probe, *, quiet: bool = False) -> UnoLinkStatus:
    """Apply UNO .pth link when probe status is NEEDS_FIX."""
    if probe.status != UnoLinkStatus.NEEDS_FIX:
        if probe.status == UnoLinkStatus.OK and not quiet:
            print("uno import OK (no fix needed)")
        return probe.status

    assert probe.site_packages is not None
    assert probe.venv_python is not None
    assert probe.pth_file is not None
    assert probe.uno_path is not None

    if not quiet:
        venv_dir = os.path.dirname(os.path.dirname(os.path.abspath(probe.venv_python)))
        print(f"Using virtual environment: {venv_dir}")

    if probe.venv_py is not None and not quiet:
        uno_py_ver = None
        for part in probe.uno_path.split(os.sep):
            if part.startswith("python") and len(part) > len("python"):
                uno_py_ver = part[len("python") :]
                break
        if uno_py_ver and uno_py_ver != f"{probe.venv_py[0]}.{probe.venv_py[1]}":
            print(
                f"Note: uno.py comes from Python {uno_py_ver} ({probe.uno_path}); "
                f"venv is {probe.venv_py[0]}.{probe.venv_py[1]}. Static analysis only — runtime uses LibreOffice's Python."
            )

    if not probe.lo_program and not quiet:
        print("Warning: Could not find LibreOffice program directory (pyuno).")

    lines = _expected_pth_lines(probe.uno_path, probe.lo_program)
    if not quiet:
        print(f"Creating {probe.pth_file}...")

    tmp_file = f"{probe.pth_file}.{os.getpid()}.tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8", newline="\n") as f:
            for line in lines:
                f.write(f"{line}\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, probe.pth_file)
    except OSError as exc:
        if os.path.exists(tmp_file):
            try:
                os.remove(tmp_file)
            except OSError:
                pass
        print(f"Error writing {probe.pth_file}: {exc}", file=sys.stderr)
        return UnoLinkStatus.FIX_FAILED

    if not quiet:
        print(f"Successfully added paths to {probe.pth_file}:")
        for line in lines[1:]:
            print(f"  - {line}")

    types_unopy_installed = bool(glob.glob(os.path.join(probe.site_packages, "types_unopy*.dist-info")))
    if not types_unopy_installed:
        print("types-unopy is not installed. Run: uv sync --frozen --dev", file=sys.stderr)

    if not quiet:
        print("\nVerifying import...")
    if not uno_import_works(probe.venv_python):
        print("Warning: Verification import failed inside the venv.", file=sys.stderr)
        return UnoLinkStatus.FIX_FAILED

    if quiet:
        print("Linked system UNO into .venv (uno.pth)")
    else:
        print("Import successful!")
    return UnoLinkStatus.OK


def needs_uno_fix(venv_base: str) -> bool:
    probe = assess(venv_base)
    return probe.status == UnoLinkStatus.NEEDS_FIX


def ensure_uno_import(venv_base: str, *, quiet: bool = False) -> bool:
    """Apply UNO .pth fix when needed. Returns True if a fix was applied."""
    probe = assess(venv_base)
    status = ensure(probe, quiet=quiet)
    return status == UnoLinkStatus.OK and probe.status == UnoLinkStatus.NEEDS_FIX


def default_venv_base(venv_arg: str | None = None) -> str:
    if venv_arg:
        return venv_arg
    if sys.prefix != sys.base_prefix:
        return sys.prefix
    env_val = os.environ.get("VIRTUAL_ENV")
    if env_val:
        return env_val
    return str(Path(__file__).resolve().parents[1] / ".venv")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ensure system UNO is linked into the project .venv.")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress informational messages; errors still print")
    parser.add_argument("--check", action="store_true", help="Exit 1 if a fix is needed or venv/UNO is missing (no changes)")
    parser.add_argument("--venv", default=None, help="Target virtual environment directory")
    args = parser.parse_args(argv)

    venv_base = default_venv_base(args.venv)
    probe = assess(venv_base)

    if args.check:
        return 0 if probe.status == UnoLinkStatus.OK else 1

    status = ensure(probe, quiet=args.quiet)
    return 0 if status == UnoLinkStatus.OK else 1


if __name__ == "__main__":
    sys.exit(main())
