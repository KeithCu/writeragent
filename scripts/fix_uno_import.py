#!/usr/bin/env python3
"""Link system UNO into the project .venv for static analysis (ty/mypy/pyright).

Safe to run repeatedly: skips work when uno.pth is present and ``import uno`` works.
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys


def _venv_python_version(venv_base: str) -> tuple[int, int] | None:
    venv_python = os.path.join(venv_base, "bin", "python")
    if not os.path.isfile(venv_python):
        return None
    try:
        out = subprocess.run(
            [venv_python, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
            capture_output=True,
            text=True,
            check=True,
        )
        major_s, minor_s = out.stdout.strip().split()
        return int(major_s), int(minor_s)
    except Exception:
        return None


def resolve_venv_paths(venv_base: str) -> tuple[str, str, str]:
    """Return (site_packages, venv_python, pth_file)."""
    lib_dir = os.path.join(venv_base, "lib")
    if not os.path.exists(lib_dir):
        raise FileNotFoundError(f"{lib_dir} not found")
    py_dirs = [d for d in os.listdir(lib_dir) if d.startswith("python")]
    if not py_dirs:
        raise FileNotFoundError(f"No python directory found in {lib_dir}")
    site_packages = os.path.join(lib_dir, py_dirs[0], "site-packages")
    if not os.path.exists(site_packages):
        raise FileNotFoundError(f"{site_packages} not found")
    venv_python = os.path.join(venv_base, "bin", "python")
    pth_file = os.path.join(site_packages, "uno.pth")
    return site_packages, venv_python, pth_file


def uno_import_works(venv_python: str) -> bool:
    try:
        result = subprocess.run(
            [venv_python, "-c", "import uno"],
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        # On macOS, we deliberately only expose uno.py (not pyuno.so) because
        # importing the real pyuno under a different Python version hangs/crashes.
        # This means "import uno" will fail with ModuleNotFoundError/ImportError
        # for 'pyuno'. If 'pyuno' is in the error message, the path to uno.py is
        # successfully configured.
        if sys.platform == "darwin" and "pyuno" in (e.stderr or ""):
            return True
        return False
    except OSError:
        return False


def find_system_uno(*, venv_py: tuple[int, int] | None = None):
    """Find the system's uno.py and LibreOffice program directory."""
    uno_path = None
    lo_program = None

    # 1. Prefer a distro site-packages dir matching the venv's Python minor version.
    if venv_py is not None:
        major, minor = venv_py
        versioned_candidates = [
            f"/usr/lib/python{major}.{minor}/site-packages",
            f"/usr/lib64/python{major}.{minor}/site-packages",
            f"/usr/lib/python{major}.{minor}/dist-packages",
            "/usr/lib/python3/dist-packages",
        ]
        for p in versioned_candidates:
            if os.path.exists(os.path.join(p, "uno.py")):
                uno_path = p
                break

    # 2. Try importing from system python on PATH
    if not uno_path:
        try:
            result = subprocess.run(
                ["python3", "-c", "import uno; print(uno.__file__)"],
                capture_output=True,
                text=True,
                check=True,
            )
            uno_path = os.path.dirname(result.stdout.strip())
        except Exception:
            pass

    # 3. Try common locations if 1–2 fail
    if not uno_path:
        search_paths = [
            "/usr/lib/python3/dist-packages",
            "/usr/lib/python3.14/site-packages",
            "/usr/lib/python3.13/site-packages",
            "/usr/lib/python3.12/site-packages",
            "/usr/lib64/python3.14/site-packages",
            "/usr/lib64/python3.13/site-packages",
        ]
        for p in search_paths:
            if os.path.exists(os.path.join(p, "uno.py")):
                uno_path = p
                break

    # 4. Find LO program dir (containing pyuno.so)
    try:
        result = subprocess.run(
            ["which", "soffice"],
            capture_output=True,
            text=True,
            check=True,
        )
        soffice_path = os.path.realpath(result.stdout.strip())
        lo_program = os.path.dirname(soffice_path)
    except Exception:
        pass

    if not lo_program:
        search_libs = [
            "/usr/lib/libreoffice/program",
            "/usr/lib64/libreoffice/program",
            "/opt/libreoffice/program",
        ]
        for p in search_libs:
            if os.path.exists(os.path.join(p, "pyuno.so")):
                lo_program = p
                break

    # 5. macOS: LibreOffice.app bundle layout. uno.py lives in Contents/Resources
    #    and pyuno.so in Contents/Frameworks, with soffice in Contents/MacOS.
    #    We deliberately expose ONLY uno.py (not pyuno.so): the venv's Python
    #    differs from LibreOffice's bundled Python, so importing the real pyuno
    #    hangs/crashes. Static type-checking (ty) only needs uno.py plus the
    #    types-unopy stubs, and the extension itself runs inside LibreOffice's
    #    own Python, so the venv never needs a working runtime pyuno.
    if sys.platform == "darwin":
        app_candidates = ["/Applications/LibreOffice.app"]
        try:
            sp = subprocess.run(
                ["which", "soffice"], capture_output=True, text=True, check=True
            ).stdout.strip()
            if sp:
                contents = os.path.dirname(os.path.dirname(os.path.realpath(sp)))
                app_candidates.insert(0, os.path.dirname(contents))
        except Exception:
            pass
        for app in app_candidates:
            res = os.path.join(app, "Contents", "Resources")
            if os.path.exists(os.path.join(res, "uno.py")):
                uno_path = res
                lo_program = None  # do not put pyuno.so on the venv path
                break

    return uno_path, lo_program


def _expected_pth_lines(uno_path: str, lo_program: str | None) -> list[str]:
    lines = ["# Added by scripts/fix_uno_import.py", uno_path]
    if lo_program:
        lines.append(lo_program)
    return lines


def _pth_is_current(pth_file: str, uno_path: str, lo_program: str | None) -> bool:
    if not os.path.isfile(pth_file):
        return False
    try:
        with open(pth_file, encoding="utf-8") as f:
            actual = [ln.rstrip("\n") for ln in f.readlines()]
        return actual == _expected_pth_lines(uno_path, lo_program)
    except OSError:
        return False


def needs_uno_fix(venv_base: str) -> bool:
    if not os.path.isdir(venv_base):
        return False
    try:
        _site_packages, venv_python, pth_file = resolve_venv_paths(venv_base)
    except FileNotFoundError:
        return True
    if not os.path.isfile(pth_file):
        return True
    if not uno_import_works(venv_python):
        return True
    venv_py = _venv_python_version(venv_base)
    uno_path, lo_program = find_system_uno(venv_py=venv_py)
    if not uno_path:
        return False
    return not _pth_is_current(pth_file, uno_path, lo_program)


def ensure_uno_import(venv_base: str, *, quiet: bool = False) -> bool:
    """Apply UNO .pth fix when needed. Returns True if a fix was applied."""
    if not os.path.isdir(venv_base):
        if not quiet:
            print(f"Error: Virtual environment not found at {venv_base}")
        return False

    if not needs_uno_fix(venv_base):
        if not quiet:
            print("uno import OK (no fix needed)")
        return False

    return _apply_uno_fix(venv_base, quiet=quiet)


def _apply_uno_fix(venv_base: str, *, quiet: bool = False) -> bool:
    if not quiet:
        print(f"Using virtual environment: {venv_base}")

    venv_py = _venv_python_version(venv_base)
    uno_path, lo_program = find_system_uno(venv_py=venv_py)

    if uno_path and venv_py is not None and not quiet:
        uno_py_ver = None
        for part in uno_path.split(os.sep):
            if part.startswith("python") and len(part) > len("python"):
                uno_py_ver = part[len("python") :]
                break
        if uno_py_ver and uno_py_ver != f"{venv_py[0]}.{venv_py[1]}":
            print(
                f"Note: uno.py comes from Python {uno_py_ver} ({uno_path}); "
                f"venv is {venv_py[0]}.{venv_py[1]}. Static analysis only — runtime uses LibreOffice's Python."
            )

    if not uno_path:
        if not quiet:
            print("Error: Could not find system uno.py. Please ensure LibreOffice python-uno is installed.")
        return False

    if not lo_program and not quiet:
        print("Warning: Could not find LibreOffice program directory (pyuno.so).")

    try:
        site_packages, venv_python, pth_file = resolve_venv_paths(venv_base)
    except FileNotFoundError as exc:
        if not quiet:
            print(f"Error: {exc}")
        return False

    if not quiet:
        print(f"Creating {pth_file}...")
    with open(pth_file, "w", encoding="utf-8") as f:
        for line in _expected_pth_lines(uno_path, lo_program):
            f.write(f"{line}\n")

    if not quiet:
        print(f"Successfully added paths to {pth_file}:")
        print(f"  - {uno_path}")
        if lo_program:
            print(f"  - {lo_program}")

    types_unopy_installed = bool(glob.glob(os.path.join(site_packages, "types_unopy*.dist-info")))
    if not types_unopy_installed:
        if not quiet:
            print("\nChecking types-unopy for static analysis...")
        try:
            subprocess.run(
                ["uv", "pip", "install", "types-unopy", "--python", venv_python],
                check=True,
                capture_output=quiet,
            )
            if not quiet:
                print("Successfully installed types-unopy via uv pip.")
        except Exception as e:
            if not quiet:
                print(f"Warning: Could not install types-unopy: {e}")
                print("Run: uv sync   (types-unopy is in the dev dependency group)")

    if not quiet:
        print("\nVerifying import...")
    if not uno_import_works(venv_python):
        if not quiet:
            print("Warning: Verification import failed inside the venv.")
        return False
    if quiet:
        print("Linked system UNO into .venv (uno.pth)")
    else:
        print("Import successful!")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure system UNO is linked into the project .venv.")
    parser.add_argument("-q", "--quiet", action="store_true", help="Only print errors and when applying a fix")
    parser.add_argument("--check", action="store_true", help="Exit 1 if a fix is needed (no changes)")
    args = parser.parse_args()

    venv_base = os.environ.get("VIRTUAL_ENV") or os.path.abspath(".venv")
    if args.check:
        return 1 if needs_uno_fix(venv_base) else 0

    if ensure_uno_import(venv_base, quiet=args.quiet):
        return 0
    if needs_uno_fix(venv_base):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
