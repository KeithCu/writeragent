#!/usr/bin/env python3
"""
Update the audio contrib files (sounddevice, cffi, pycparser) and their binaries.
This script downloads wheels for various platforms and python versions,
extracts the necessary files, and places them in plugin/contrib/audio.

Assumes 'uv' is installed and used for package management.
"""

import platform
import shutil
import struct
import subprocess
import tempfile
import zipfile
from pathlib import Path

# Configuration
REPO_ROOT = Path(__file__).parent.parent.resolve()
CONTRIB_AUDIO_DIR = REPO_ROOT / "contrib" / "audio"
PYTHON_VERSIONS = ["3.11", "3.12", "3.13", "3.14"]

# macOS: Apple Silicon only — Intel/universal wheels dropped to shrink the OXT.
MACOS_PLATFORMS = ["macosx_11_0_arm64"]

# Platforms to fetch for cffi backend binaries (and sounddevice on non-macOS).
PLATFORMS = [
    "win_amd64",
    "win_arm64",
    *MACOS_PLATFORMS,
    "manylinux2014_x86_64",
    "manylinux2014_aarch64",
]

# Mach-O constants for assert_macos_arm64 (used at harvest time and in tests).
MH_MAGIC_64 = 0xFEEDFACF
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_SWAPPED = 0xBEBAFECA
CPU_TYPE_ARM64 = 0x0100000C


def assert_macos_arm64(path: Path) -> None:
    """Raise ValueError unless path is a single-arch arm64 Mach-O binary."""
    _check_macos_arm64(path.read_bytes(), path)


def _check_macos_arm64(data: bytes, label: Path | str) -> None:
    if len(data) < 8:
        raise ValueError(f"{label}: too small for Mach-O")

    magic_be = struct.unpack(">I", data[:4])[0]
    if magic_be in (FAT_MAGIC, FAT_MAGIC_SWAPPED):
        raise ValueError(f"{label}: expected single-arch arm64, got fat/universal Mach-O")

    magic = struct.unpack("<I", data[:4])[0]
    if magic != MH_MAGIC_64:
        raise ValueError(f"{label}: expected MH_MAGIC_64, got 0x{magic:08x}")

    cputype = struct.unpack("<i", data[4:8])[0]
    if cputype != CPU_TYPE_ARM64:
        raise ValueError(f"{label}: expected arm64 (cputype 0x{cputype:08x})")


def ensure_macos_arm64_only(path: Path) -> None:
    """Write a single-arch arm64 Mach-O, thinning universal/fat inputs when needed."""
    data = path.read_bytes()
    magic_be = struct.unpack(">I", data[:4])[0]
    if magic_be in (FAT_MAGIC, FAT_MAGIC_SWAPPED):
        nfat = struct.unpack(">I", data[4:8])[0]
        arm64_slice = None
        for index in range(nfat):
            base = 8 + index * 20
            cputype, _cpusubtype, offset, size, _align = struct.unpack(">iiIII", data[base : base + 20])
            if cputype == CPU_TYPE_ARM64:
                arm64_slice = data[offset : offset + size]
                break
        if arm64_slice is None:
            raise ValueError(f"{path}: fat Mach-O has no arm64 slice")
        path.write_bytes(arm64_slice)
        print(f"  Thinned to arm64-only: {path.name} ({path.stat().st_size} bytes)")
        data = arm64_slice
    _check_macos_arm64(data, path)

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
        for platform_tag in ["win_amd64", "win_arm64", *MACOS_PLATFORMS]:
            cmd = base_cmd + [
                "download", "sounddevice",
                "--platform", platform_tag,
                "--python-version", "3.12",
                "--only-binary=:all:",
                "--dest", str(download_dir)
            ]
            run_command(cmd)

        # 2. Download cffi wheels for all platforms and python versions
        for py_ver in PYTHON_VERSIONS:
            for platform_tag in PLATFORMS:
                cmd = base_cmd + [
                    "download", "cffi",
                    "--platform", platform_tag,
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
                        dest = binaries_dest / f.name
                        shutil.copy2(f, dest)
                        if f.suffix == ".dylib":
                            ensure_macos_arm64_only(dest)
                            print(f"  Verified arm64: {dest.name}")
                
                if not pure_python_updated["sounddevice"]:
                    for f in ["sounddevice.py", "_sounddevice.py"]:
                        if (wheel_extract_dir / f).exists():
                            shutil.copy2(wheel_extract_dir / f, CONTRIB_AUDIO_DIR / f)
                    pure_python_updated["sounddevice"] = True

            if "cffi" in wheel_name:
                for f in wheel_extract_dir.glob("_cffi_backend.*"):
                    print(f"Updating cffi backend: {f.name}")
                    dest = CONTRIB_AUDIO_DIR / f.name
                    shutil.copy2(f, dest)
                    if "-darwin" in f.name:
                        assert_macos_arm64(dest)
                        print(f"  Verified arm64: {dest.name}")
                    # Strip debug symbols to reduce file size
                    strip_binary(dest)
                
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

    # Create audio_source.zip
    import os
    zip_path = REPO_ROOT / "contrib" / "audio_source.zip"
    print(f"Creating {zip_path}...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in ["sounddevice.py", "_sounddevice.py"]:
            p = CONTRIB_AUDIO_DIR / item
            if p.exists():
                zf.write(p, item)
        for folder in ["cffi", "pycparser"]:
            f_path = CONTRIB_AUDIO_DIR / folder
            if f_path.exists():
                for root, dirs, files in os.walk(f_path):
                    for file in files:
                        full_p = Path(root) / file
                        rel_p = full_p.relative_to(CONTRIB_AUDIO_DIR)
                        zf.write(full_p, rel_p)
    print("  Created audio_source.zip successfully.")

    print("\nUpdate successful!")
    print(f"Updated files in {CONTRIB_AUDIO_DIR}")

if __name__ == "__main__":
    main()
