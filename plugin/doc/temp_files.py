# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Temp file helpers (LibrePy-safe)."""

from __future__ import annotations

import os
import sys
import tempfile
from typing import Any, Literal, final

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


@final
class _TempBuffer:
    """Context manager holding a temporary file buffer on disk.

    A class-based context manager avoids modifying in-flight exceptions
    during unwinding. A generator context manager assigns ``__traceback__``
    on the exception via ``contextlib._GeneratorContextManager.__exit__``.
    Because pyuno exception classes route attribute assignment into the UNO
    struct (cite: ``pyuno/source/module/uno.py:_uno_struct__setattr__``), assigning
    a traceback object fails inside pyuno's type conversion and turns the original
    UNO exception into a ``RuntimeException: Couldn't convert <traceback object> to a UNO type``.
    The class form leaves the exception untouched.
    """

    def __init__(self, content: Any = None, ext: str = ".html") -> None:
        self._content: Any = content
        self._ext: str = ext
        self._path: str | None = None

    def __enter__(self) -> tuple[str, str]:
        fd, path = tempfile.mkstemp(suffix=self._ext, dir=_resolve_temp_dir())
        if self._content is not None:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self._content)
        else:
            os.close(fd)
        self._path = path
        return (path, path_to_file_url(path))

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> Literal[False]:
        path = self._path
        if path is not None:
            best_effort(lambda: os.unlink(path), "temp buffer unlink failed")
        return False


def with_temp_buffer(content: Any = None, ext: str = ".html") -> _TempBuffer:
    """Context manager that yields ``(path, file_url)`` for a temp file
    with the correct format extension.

    If *content* is not ``None`` it is written to the file.
    *ext* overrides the file extension (e.g. ``".xhtml"`` for the read filter).
    The file is deleted on exit.
    """
    return _TempBuffer(content=content, ext=ext)
