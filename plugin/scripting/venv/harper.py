# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted Harper Rust grammar linter helper executing inside the user's virtual environment."""

import os
import sys
import json
import subprocess
import tempfile
import platform
import logging
import urllib.request
import tarfile
import zipfile
import shutil
from pathlib import Path

log = logging.getLogger("writeragent.grammar")


def _download_harper_binary(dest_path: Path):
    """Download precompiled harper-cli binary from Automattic/harper releases."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    
    if system == "linux":
        if "arm" in machine or "aarch64" in machine:
            asset = "harper-cli-aarch64-unknown-linux-gnu.tar.gz"
        else:
            asset = "harper-cli-x86_64-unknown-linux-gnu.tar.gz"
    elif system == "darwin":
        if "arm" in machine or "aarch64" in machine:
            asset = "harper-cli-aarch64-apple-darwin.tar.gz"
        else:
            asset = "harper-cli-x86_64-apple-darwin.tar.gz"
    elif system == "windows":
        asset = "harper-cli-x86_64-pc-windows-msvc.zip"
    else:
        raise RuntimeError(f"Unsupported OS: {system}")
        
    url = f"https://github.com/Automattic/harper/releases/latest/download/{asset}"
    log.info(f"[harper] Downloading precompiled binary for {system}/{machine} from {url}")
    
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    
    with tempfile.NamedTemporaryFile(suffix=Path(asset).suffix, delete=False) as tmp_file:
        tmp_name = tmp_file.name
        
    try:
        urllib.request.urlretrieve(url, tmp_name)
        
        if asset.endswith(".tar.gz"):
            with tarfile.open(tmp_name, "r:gz") as tar:
                # Extract member to target path directly
                member_found = False
                for member in tar.getmembers():
                    if member.name.endswith("harper-cli"):
                        # Extract and rename
                        f = tar.extractfile(member)
                        if f:
                            dest_path.write_bytes(f.read())
                            member_found = True
                        break
                if not member_found:
                    raise RuntimeError("harper-cli file not found inside tarball")
        elif asset.endswith(".zip"):
            with zipfile.ZipFile(tmp_name, "r") as zip_ref:
                member_found = False
                for file_info in zip_ref.infolist():
                    if file_info.filename.endswith("harper-cli.exe") or file_info.filename.endswith("harper-cli"):
                        dest_path.write_bytes(zip_ref.read(file_info))
                        member_found = True
                        break
                if not member_found:
                    raise RuntimeError("harper-cli file not found inside zip archive")
                    
        if dest_path.exists():
            os.chmod(dest_path, 0o755)  # nosec B103
            log.info(f"[harper] Binary installed successfully at {dest_path}")
    except Exception as e:
        log.error(f"[harper] Failed to download and extract binary: {e}")
        raise RuntimeError(f"Failed to auto-download Harper binary: {e}")
    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except Exception:
                pass


def _get_harper_binary(user_config_dir: str) -> str:
    """Resolve path to harper-cli binary, auto-downloading if missing."""
    # 1. Check if harper-cli is installed globally on the system PATH
    sys_path = shutil.which("harper-cli")
    if sys_path:
        return sys_path

    # 2. Otherwise, check/download to the user profile bin directory
    bin_dir = Path(user_config_dir) / "bin"
    suffix = ".exe" if os.name == "nt" else ""
    binary_path = bin_dir / f"harper-cli{suffix}"
    
    if not binary_path.exists():
        _download_harper_binary(binary_path)
        
    return str(binary_path)


def run_harper_check(text: str, user_config_dir: str) -> dict:
    """Run harper-cli on text segment and return parsed errors."""
    try:
        harper_bin = _get_harper_binary(user_config_dir)
    except Exception as e:
        raise RuntimeError(str(e))

    # Write text segment to a temporary file
    try:
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w+", delete=False, encoding="utf-8") as temp_file:
            temp_file.write(text)
            temp_file_name = temp_file.name
    except Exception as e:
        raise RuntimeError(f"Failed to create temporary file for linting: {e}")

    try:
        # Execute harper-cli check
        cmd = [harper_bin, "lint", "--format", "json", temp_file_name]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        
        # Harper CLI outputs results as JSON on stdout.
        # It may return exit code 1 if issues are found, which is normal for CLI linters.
        if proc.returncode != 0 and not proc.stdout.strip():
            stderr_msg = (proc.stderr or "").strip()
            raise RuntimeError(f"Harper process failed (code {proc.returncode}): {stderr_msg}")

        try:
            output_data = json.loads(proc.stdout or "[]")
        except Exception as e:
            raise RuntimeError(f"Harper returned invalid JSON: {e}. Output was: {proc.stdout}")

        file_lints = []
        if output_data and isinstance(output_data, list):
            file_lints = output_data[0].get("lints", [])

        errors = []
        for lint in file_lints:
            span = lint.get("span", {})
            start = span.get("char_start", 0)
            end = span.get("char_end", 0)
            length = max(1, end - start)
            
            rule = lint.get("rule", "Grammar")
            msg = lint.get("message", "")
            
            # Parse suggestions
            suggestions = []
            for sug in lint.get("suggestions", []):
                # Clean up "Replace with: “This”" formatting
                if "Replace with: “" in sug:
                    cleaned = sug.split("Replace with: “")[1].rstrip("”")
                    suggestions.append(cleaned)
                else:
                    suggestions.append(sug)
                    
            correct = suggestions[0] if suggestions else ""
            
            errors.append({
                "wrong": text[start:start+length] if start + length <= len(text) else lint.get("matched_text", ""),
                "correct": correct,
                "n_error_start": start,
                "n_error_length": length,
                "short_comment": f"[Harper] {msg}",
                "full_comment": msg,
                "rule_identifier": f"harper||{rule}",
                "suggestions": suggestions[:5],
                "reason": msg,
                "type": f"Harper ({rule})"
            })
            
        return {"errors": errors}
        
    finally:
        if os.path.exists(temp_file_name):
            try:
                os.remove(temp_file_name)
            except Exception:
                pass
