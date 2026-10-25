# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted Vale style guide linter executing inside the user's virtual environment."""

import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from plugin.framework.grammar_rule_ids import grammar_match

log = logging.getLogger(__name__)

# Both stay under VALE_WORKER_TIMEOUT_SEC's 25 s budget in config_limits.py.
_SYNC_TIMEOUT_SEC = 20
_CHECK_TIMEOUT_SEC = 10


def _get_vale_binary() -> str:
    """Resolve path to the vale binary downloaded by PyPI wrapper."""
    venv_bin = Path(sys.executable).parent
    # On Windows, binaries have .exe extensions
    suffix = ".exe" if os.name == "nt" else ""
    vale_path = venv_bin / f"vale{suffix}"
    if not vale_path.exists():
        raise RuntimeError("Vale binary not found. Please run 'uv pip install vale' or equivalent in your virtual environment.")
    return str(vale_path)


def _wrap_vale_command(cmd: list[str]) -> list[str]:
    # Importing plugin.scripting.sandbox loads smolagents into the venv child,
    # and importing get_subprocess_creationflags from worker_pool loads thread_guard.
    # Vale only needs the no-env flatpak branch, which is flatpak-spawn --host
    # inside Flatpak and the argv unchanged otherwise. The full wrapper stays
    # in sandbox.py for LibreHarper.
    if os.path.exists("/.flatpak-info") or os.environ.get("FLATPAK_ID"):
        return ["flatpak-spawn", "--host", *cmd]
    return cmd


def _subprocess_creationflags() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _atomic_write(path: Path, content: str) -> None:
    tmp = tempfile.NamedTemporaryFile(mode="w", dir=str(path.parent), prefix=path.name + ".", delete=False, encoding="utf-8")
    try:
        with tmp:
            tmp.write(content)
        os.replace(tmp.name, path)
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)


def _ensure_vale_config(vale_bin: str, ini_path: Path, styles_path: Path, styles_list: list[str]) -> None:
    styles_str = ", ".join(styles_list)
    content = f"""StylesPath = {styles_path.as_posix()}
MinAlertLevel = suggestion
Packages = Microsoft, Google, write-good

[*]
BasedOnStyles = {styles_str}
Microsoft.Headings = NO
"""
    styles_path.mkdir(parents=True, exist_ok=True)
    existing = ini_path.read_text(encoding="utf-8") if ini_path.exists() else None
    wrote = existing != content
    if wrote:
        _atomic_write(ini_path, content)

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    marker_path = ini_path.with_name(".vale-sync-ok")

    if not wrote and marker_path.exists():
        marker_text = marker_path.read_text(encoding="utf-8").strip()
        if marker_text == digest:
            return

    try:
        proc = subprocess.run(
            _wrap_vale_command([vale_bin, "--config", str(ini_path), "--no-global", "sync"]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_SYNC_TIMEOUT_SEC,
            **_subprocess_creationflags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Vale style sync timed out after {_SYNC_TIMEOUT_SEC}s") from exc

    if proc.returncode != 0:
        stderr_msg = (proc.stderr or "").strip()
        raise RuntimeError(f"Vale style sync failed (code {proc.returncode}): {stderr_msg}")

    _atomic_write(marker_path, digest)


def _map_alert(text: str, alert: Any) -> dict[str, Any] | None:
    """Map one Vale alert into a grammar_match dict.

    Vale's Span is [begin, end] within the line (internal/core/alert.go) and
    Line is 1-based (internal/core/location.go). Document-scope alerts or
    malformed alerts return None.
    """
    if not isinstance(alert, dict):
        log.debug("Skipping non-dict Vale alert: %r", alert)
        return None

    match = alert.get("Match")
    if not isinstance(match, str) or not match:
        log.debug("Skipping document-scope or missing Match alert: %r", alert)
        return None

    span = alert.get("Span")
    if not (
        isinstance(span, list)
        and len(span) >= 2
        and type(span[0]) is int
        and type(span[1]) is int
        and span[0] >= 1
        and span[1] >= span[0]
    ):
        log.debug("Skipping alert with invalid Span: %r", alert)
        return None

    line = alert.get("Line")
    if line is None:
        log.debug("Skipping alert with missing Line: %r", alert)
        return None
    try:
        line_no = int(line)
    except (TypeError, ValueError):
        log.debug("Skipping alert with invalid Line: %r", alert)
        return None

    if line_no < 1:
        log.debug("Skipping alert with line_no < 1: %r", alert)
        return None

    line_start = 0
    for line_idx in range(line_no - 1):
        nl_pos = text.find("\n", line_start)
        if nl_pos == -1:
            log.debug("Skipping alert beyond line count: %r (at step %d)", alert, line_idx)
            return None
        line_start = nl_pos + 1

    start = line_start + span[0] - 1
    length = max(1, span[1] - span[0] + 1)
    if start >= len(text) or start + length > len(text):
        log.debug("Skipping alert with out-of-bounds span: start=%d, length=%d, text_len=%d", start, length, len(text))
        return None

    severity = str(alert.get("Severity") or "suggestion")
    rule = str(alert.get("Check") or "Style")
    msg = str(alert.get("Message") or "")

    raw_action = alert.get("Action")
    action = raw_action if isinstance(raw_action, dict) else {}
    action_name = action.get("Name", "")
    raw_params = action.get("Params")
    action_params = raw_params if isinstance(raw_params, list) else []

    suggestions = [str(p) for p in action_params[:5]] if action_name == "replace" else []
    correct = suggestions[0] if suggestions else ""

    description = alert.get("Description")

    return grammar_match(
        wrong=match,
        correct=correct,
        n_error_start=start,
        n_error_length=length,
        short_comment=f"[{severity.upper()}] {msg}",
        full_comment=str(description) if description else msg,
        rule_identifier=f"vale||{rule}",
        suggestions=suggestions,
        reason=msg,
        type=f"Style ({severity})",
    )


def run_vale_check(text: str, user_config_dir: str, styles: str) -> dict[str, Any]:
    """Run Vale linter on the text segment and return the style errors list."""
    vale_bin = _get_vale_binary()

    ini_path = Path(user_config_dir) / ".vale.ini"
    styles_path = Path(user_config_dir) / "vale_styles"

    styles_list = [s.strip() for s in styles.split(",") if s.strip()]

    # 1. Ensure .vale.ini and base style directories exist
    _ensure_vale_config(vale_bin, ini_path, styles_path, styles_list)

    # 2. Execute Vale check via stdin
    cmd = [
        vale_bin,
        "--config", str(ini_path),
        "--output=JSON",
        "--ext=.txt",
        "--no-global",
    ]
    try:
        proc = subprocess.run(
            _wrap_vale_command(cmd),
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_CHECK_TIMEOUT_SEC,
            **_subprocess_creationflags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Vale linter timed out after {_CHECK_TIMEOUT_SEC}s") from exc

    # Vale returns status code 1 if it flags style alerts, which is normal.
    if proc.returncode not in (0, 1):
        stderr_msg = (proc.stderr or "").strip()
        raise RuntimeError(f"Vale linter process failed (code {proc.returncode}): {stderr_msg}")

    # 3. Parse JSON output
    try:
        output_data = json.loads(proc.stdout or "{}")
    except Exception as e:
        raise RuntimeError(f"Vale returned invalid JSON: {e}. Output was: {proc.stdout}") from e

    # Minimal guard: null Action or non-list file entry does not raise
    file_errors: Any = []
    if isinstance(output_data, dict):
        file_errors = output_data.get("stdin.txt")
        if file_errors is None and len(output_data) == 1:
            file_errors = next(iter(output_data.values()))
    if not isinstance(file_errors, list):
        file_errors = []

    errors = []
    for err in file_errors:
        mapped = _map_alert(text, err)
        if mapped is not None:
            errors.append(mapped)

    return {"errors": errors}
