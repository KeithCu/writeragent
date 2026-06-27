# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted Vale style guide linter executing inside the user's virtual environment."""

import os
import sys
import json
import subprocess
import tempfile
import logging
from pathlib import Path
from typing import Dict, Any

log = logging.getLogger("writeragent.grammar")


def _get_vale_binary() -> str:
    """Resolve path to the vale binary downloaded by PyPI wrapper."""
    venv_bin = Path(sys.executable).parent
    # On Windows, binaries have .exe extensions
    suffix = ".exe" if os.name == "nt" else ""
    vale_path = venv_bin / f"vale{suffix}"
    if not vale_path.exists():
        raise RuntimeError("Vale binary not found. Please run 'uv pip install vale' or equivalent in your virtual environment.")
    return str(vale_path)


def run_vale_check(text: str, user_config_dir: str, styles: str) -> dict:
    """Run Vale linter on the text segment and return the style errors list."""
    try:
        vale_bin = _get_vale_binary()
    except Exception as e:
        raise RuntimeError(str(e))
        
    ini_path = Path(user_config_dir) / ".vale.ini"
    styles_path = Path(user_config_dir) / "vale_styles"
    
    # Clean up commas or spaces in styles config
    styles_list = [s.strip() for s in styles.split(",") if s.strip()]
    styles_str = ", ".join(styles_list)

    # 1. Ensure .vale.ini and base style directories exist
    if not ini_path.exists():
        try:
            styles_path.mkdir(parents=True, exist_ok=True)
            ini_content = f"""StylesPath = {styles_path.as_posix()}
MinAlertLevel = suggestion
Packages = Microsoft, Google, write-good

[*]
BasedOnStyles = {styles_str}
Microsoft.Headings = NO
"""
            ini_path.write_text(ini_content, encoding="utf-8")
            
            # Sync / download style packages via local binary
            log.info("[style] Syncing Vale style guides (first run)...")
            subprocess.run([vale_bin, "--config", str(ini_path), "sync"], check=True, capture_output=True)
        except Exception as e:
            raise RuntimeError(f"Failed to initialize and sync Vale style guides: {e}")

    # 2. Write text segment to a temporary file
    try:
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w+", delete=False, encoding="utf-8") as temp_file:
            temp_file.write(text)
            temp_file_name = temp_file.name
    except Exception as e:
        raise RuntimeError(f"Failed to create temporary file for linting: {e}")

    try:
        # 3. Execute Vale check
        cmd = [
            vale_bin,
            "--config", str(ini_path),
            "--output", "JSON",
            temp_file_name
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        
        # Vale returns status code 1 if it flags style alerts, which is normal.
        if proc.returncode not in (0, 1):
            stderr_msg = (proc.stderr or "").strip()
            raise RuntimeError(f"Vale linter process failed (code {proc.returncode}): {stderr_msg}")

        # 4. Parse JSON output
        try:
            output_data = json.loads(proc.stdout or "{}")
        except Exception as e:
            raise RuntimeError(f"Vale returned invalid JSON: {e}. Output was: {proc.stdout}")

        file_errors = output_data.get(temp_file_name, [])
        errors = []
        
        for err in file_errors:
            span = err.get("Span", [1, 1])
            start = max(0, span[0] - 1)  # 1-indexed to 0-indexed
            length = max(1, span[1] - span[0] + 1)
            
            severity = err.get("Severity", "suggestion")
            rule = err.get("Check", "Style")
            msg = err.get("Message", "")
            
            action = err.get("Action", {})
            action_name = action.get("Name", "")
            action_params = action.get("Params", []) if isinstance(action.get("Params"), list) else []

            correct = action_params[0] if (action_name == "replace" and action_params) else ""
            suggestions = action_params[:5] if (action_name == "replace" and action_params) else []

            errors.append({
                "wrong": text[start:start+length] if start + length <= len(text) else "",
                "correct": correct,
                "n_error_start": start,
                "n_error_length": length,
                "short_comment": f"[{severity.upper()}] {msg}",
                "full_comment": err.get("Description") or msg,
                "rule_identifier": f"vale||{rule}",
                "suggestions": suggestions,
                "reason": msg,
                "type": f"Style ({severity})"
            })
            
        return {"errors": errors}
        
    finally:
        if os.path.exists(temp_file_name):
            try:
                os.remove(temp_file_name)
            except Exception:
                pass
