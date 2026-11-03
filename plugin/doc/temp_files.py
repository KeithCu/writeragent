# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Temp file helpers (LibrePy-safe)."""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from typing import Any, Generator

from plugin.framework.errors import best_effort
from plugin.framework.url_utils import path_to_file_url


def _resolve_temp_dir() -> str:
    if "crosshair" in sys.modules:
        return (
            os.environ.get("TMPDIR")
            or os.environ.get("TEMP")
            or os.environ.get("TMP")
            or tempfile.tempdir
            or os.curdir
        )
    return tempfile.gettempdir()


@contextlib.contextmanager
def with_temp_buffer(content: Any = None, ext: str = ".html") -> Generator[tuple[str, str], None, None]:
    """Context manager that yields ``(path, file_url)`` for a temp file
    with the correct format extension.

    If *content* is not ``None`` it is written to the file.
    *ext* overrides the file extension (e.g. ``".xhtml"`` for the read filter).
    The file is deleted on exit.
    """
    fd, path = tempfile.mkstemp(suffix=ext, dir=_resolve_temp_dir())
    try:
        if content is not None:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
        else:
            os.close(fd)
        yield (path, path_to_file_url(path))
    finally:
        best_effort(lambda: os.unlink(path), "temp buffer unlink failed")
