# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless soffice conversion for legacy binary Office files (.doc/.xls/.ppt).

Uses an isolated UserInstallation profile and temp ODF output — never the user's
running LibreOffice instance or profile. PDF indexing is intentionally deferred.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from plugin.framework.worker_pool import get_subprocess_creationflags
from plugin.scripting.sandbox import wrap_command_for_sandbox

log = logging.getLogger(__name__)

__all__ = [
    "LEGACY_BINARY_EXTENSIONS",
    "convert_legacy_to_odf",
    "legacy_odf_filter",
    "resolve_soffice_executable",
]

LEGACY_BINARY_EXTENSIONS = frozenset({".doc", ".xls", ".ppt"})

_LEGACY_TO_FILTER: dict[str, str] = {
    ".doc": "odt",
    ".xls": "ods",
    ".ppt": "odp",
}


def legacy_odf_filter(ext: str) -> str | None:
    """Return soffice ``--convert-to`` filter name for a legacy binary extension."""
    return _LEGACY_TO_FILTER.get(str(ext or "").lower())


def resolve_soffice_executable() -> str | None:
    """Locate ``soffice`` without connecting to the user's running LO instance."""
    base = os.environ.get("UNO_PATH", "").strip()
    if base:
        candidate = os.path.join(base, "soffice.exe" if sys.platform.startswith("win") else "soffice")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("soffice") or shutil.which("soffice.exe")
    if found:
        return found
    for candidate in (
        "/usr/lib/libreoffice/program/soffice",
        "/usr/lib64/libreoffice/program/soffice",
        "/opt/libreoffice/program/soffice",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _profile_url(profile_dir: Path) -> str:
    profile_dir.mkdir(parents=True, exist_ok=True)
    quoted = urllib.request.pathname2url(str(profile_dir.resolve()))
    if quoted.startswith("/"):
        return "file://" + quoted
    return "file:" + quoted


def convert_legacy_to_odf(source_path: str, *, timeout_sec: int = 120) -> Path | None:
    """Convert one legacy Office file to a temp ODF sibling; caller must delete the file."""
    ext = Path(source_path).suffix.lower()
    filter_name = legacy_odf_filter(ext)
    if filter_name is None:
        return None
    soffice = resolve_soffice_executable()
    if soffice is None:
        log.debug("soffice not found — legacy convert skipped for %s", source_path)
        return None

    source = Path(source_path).resolve()
    if not source.is_file():
        return None

    with tempfile.TemporaryDirectory(prefix="writeragent-embed-out-") as out_dir:
        cmd = [
            soffice,
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            "--convert-to",
            filter_name,
            "--outdir",
            out_dir,
            str(source),
        ]
        try:
            proc = subprocess.run(
                wrap_command_for_sandbox(cmd),
                capture_output=True,
                text=True,
                timeout=max(1, int(timeout_sec)),
                check=False,
                **get_subprocess_creationflags(),
            )
        except (OSError, subprocess.TimeoutExpired):
            log.debug("legacy soffice convert failed for %s", source_path, exc_info=True)
            return None
        if proc.returncode != 0:
            log.debug(
                "legacy soffice convert exit %s for %s: %s",
                proc.returncode,
                source_path,
                (proc.stderr or proc.stdout or "").strip()[:500],
            )
            return None

        # Successful conversion, process output file
        expected_suffix = f".{filter_name}"
        produced = sorted(Path(out_dir).glob(f"*{expected_suffix}"))
        if not produced:
            log.debug("legacy soffice convert produced no %s for %s", expected_suffix, source_path)
            return None

        # Move out of TemporaryDirectory so caller can read after context exits.
        fd, dest_name = tempfile.mkstemp(prefix="writeragent-embed-", suffix=expected_suffix)
        os.close(fd)
        dest = Path(dest_name)
        try:
            shutil.move(str(produced[0]), dest)
        except OSError:
            log.debug("failed to move converted ODF for %s", source_path, exc_info=True)
            dest.unlink(missing_ok=True)
            return None
        return dest


@contextmanager
def temporary_converted_odf(source_path: str, *, timeout_sec: int = 120) -> Iterator[Path | None]:
    """Yield a temp ODF path for legacy sources; delete on exit."""
    converted = convert_legacy_to_odf(source_path, timeout_sec=timeout_sec)
    try:
        yield converted
    finally:
        if converted is not None:
            converted.unlink(missing_ok=True)
