#!/usr/bin/env python3
"""
Update the audio contrib files (sounddevice, cffi, pycparser) and their binaries.
This script downloads wheels for various platforms and python versions,
extracts the necessary files, and places them in plugin/contrib/audio.

Assumes 'uv' is installed and used for package management.
"""

import os
import platform
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

# Configuration
REPO_ROOT = Path(__file__).parent.parent.resolve()
CONTRIB_AUDIO_DIR = REPO_ROOT / "plugin" / "contrib" / "audio"
PYTHON_VERSIONS = ["3.11", "3.12", "3.13", "3.14"]

# Platforms to fetch for sounddevice (PortAudio binaries) and cffi (backend binaries)
PLATFORMS = [
    "win_amd64",
    "win_arm64",
    "macosx_10_9_x86_64",
    "macosx_11_0_arm64",
    "macosx_10_9_universal2",
    "manylinux2014_x86_64",
    "manylinux2014_aarch64",
    "musllinux_1_1_x86_64",
    "musllinux_1_1_aarch64",
]

def run_command(cmd, cwd=None):
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error: {result.stderr}")
    return result


def strip_binary(filepath):
    """Strip debug symbols from a binary file to reduce size.
    
    Uses platform-appropriate strip tool:
    - llvm-strip: Cross-platform, handles any architecture (preferred)
    - strip: Native platform strip tool
    - Windows: llvm-strip or strip (MSYS2/MinGW)
    """
    filepath = Path(filepath)
    
    if not filepath.exists():
        return
    
    # Skip if file is too small to be a meaningful binary
    if filepath.stat().st_size < 1024:
        return
    
    system = platform.system().lower()
    
    # Try llvm-strip first - it's cross-platform and can handle any architecture
    strippers = ["llvm-strip"]
    
    if system == "windows":
        # On Windows, also try strip from MSYS2/MinGW
        strippers.append("strip")
    elif system != "darwin":
        # On Linux/Unix (not macOS), also try native strip
        # Note: native strip on Linux is architecture-specific (e.g., x86_64 strip
        # can't handle aarch64), so we prefer llvm-strip which is universal
        strippers.append("strip")
    # On macOS, only try llvm-strip (native strip may not work well with
    # cross-compiled binaries)
    
    for stripper in strippers:
        try:
            result = subprocess.run(
                [stripper, str(filepath)],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                print(f"  Stripped {filepath.name} ({filepath.stat().st_size} bytes)")
                return
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
    
    # If we get here, stripping failed - not a fatal error
    print(f"  Warning: Could not strip {filepath.name} (no strip tool available)")

def main():
    if not CONTRIB_AUDIO_DIR.exists():
        print(f"Error: {CONTRIB_AUDIO_DIR} does not exist.")
        return

    # Ensure pip is available for downloading (uv pip doesn't support 'download')
    # We use 'uv run pip' to ensure we use the pip in the project's environment.
    # If pip is not installed, we'll try to install it first.
    if subprocess.run(["uv", "run", "pip", "--version"], capture_output=True).returncode != 0:
        print("Installing pip into the environment...")
        subprocess.run(["uv", "pip", "install", "pip"], check=True)

    base_cmd = ["uv", "run", "pip"]

    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        download_dir = tmp_dir / "downloads"
        download_dir.mkdir()

        print(f"Working in temporary directory: {tmp_dir}")

        # 1. Download sounddevice wheels for PortAudio binaries
        for platform in ["win_amd64", "win_arm64", "macosx_10_9_x86_64", "macosx_11_0_arm64", "macosx_10_9_universal2"]:
            cmd = base_cmd + [
                "download", "sounddevice",
                "--platform", platform,
                "--python-version", "3.12",
                "--only-binary=:all:",
                "--dest", str(download_dir)
            ]
            run_command(cmd)

        # 2. Download cffi wheels for all platforms and python versions
        for py_ver in PYTHON_VERSIONS:
            for platform in PLATFORMS:
                cmd = base_cmd + [
                    "download", "cffi",
                    "--platform", platform,
                    "--python-version", py_ver,
                    "--only-binary=:all:",
                    "--dest", str(download_dir)
                ]
                run_command(cmd)

        # 3. Download pycparser (dependency of cffi, pure python)
        cmd = base_cmd + [
            "download", "pycparser",
            "--platform", "any",
            "--only-binary=:all:",
            "--dest", str(download_dir)
        ]
        run_command(cmd)

        # 4. Extract wheels and update files
        print("Extracting wheels and updating files...")
        
        pure_python_updated = {
            "sounddevice": False,
            "cffi": False,
            "pycparser": False
        }

        for wheel in download_dir.glob("*.whl"):
            wheel_extract_dir = tmp_dir / "extract" / wheel.name
            wheel_extract_dir.mkdir(parents=True)
            with zipfile.ZipFile(wheel, 'r') as zip_ref:
                zip_ref.extractall(wheel_extract_dir)

            wheel_name = wheel.name.lower()
            
            if "sounddevice" in wheel_name:
                binaries_src = wheel_extract_dir / "_sounddevice_data" / "portaudio-binaries"
                if binaries_src.exists():
                    binaries_dest = CONTRIB_AUDIO_DIR / "_sounddevice_data" / "portaudio-binaries"
                    binaries_dest.mkdir(parents=True, exist_ok=True)
                    for f in binaries_src.glob("*"):
                        print(f"Updating PortAudio binary: {f.name}")
                        shutil.copy2(f, binaries_dest / f.name)
                
                if not pure_python_updated["sounddevice"]:
                    for f in ["sounddevice.py", "_sounddevice.py"]:
                        if (wheel_extract_dir / f).exists():
                            shutil.copy2(wheel_extract_dir / f, CONTRIB_AUDIO_DIR / f)
                    pure_python_updated["sounddevice"] = True

            if "cffi" in wheel_name:
                for f in wheel_extract_dir.glob("_cffi_backend.*"):
                    print(f"Updating cffi backend: {f.name}")
                    shutil.copy2(f, CONTRIB_AUDIO_DIR / f.name)
                    # Strip debug symbols to reduce file size
                    strip_binary(CONTRIB_AUDIO_DIR / f.name)
                
                if not pure_python_updated["cffi"]:
                    cffi_src = wheel_extract_dir / "cffi"
                    if cffi_src.exists():
                        if (CONTRIB_AUDIO_DIR / "cffi").exists():
                            shutil.rmtree(CONTRIB_AUDIO_DIR / "cffi")
                        shutil.copytree(cffi_src, CONTRIB_AUDIO_DIR / "cffi")
                        pure_python_updated["cffi"] = True

            if "pycparser" in wheel_name and not pure_python_updated["pycparser"]:
                pycparser_src = wheel_extract_dir / "pycparser"
                if pycparser_src.exists():
                    if (CONTRIB_AUDIO_DIR / "pycparser").exists():
                        shutil.rmtree(CONTRIB_AUDIO_DIR / "pycparser")
                    shutil.copytree(pycparser_src, CONTRIB_AUDIO_DIR / "pycparser")
                    pure_python_updated["pycparser"] = True

    print("\nUpdate successful!")
    print(f"Updated files in {CONTRIB_AUDIO_DIR}")

if __name__ == "__main__":
    main()
