# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Unit tests for plugin.doc.temp_files helpers (no UNO)."""

import os
import sys
import tempfile as tempfile_mod

from plugin.doc.temp_files import (
    _resolve_temp_dir,
    with_temp_buffer,
)


class _UnoSimulatedException(Exception):
    """Simulates a pyuno exception whose __setattr__ delegates to a UNO struct."""

    def __setattr__(self, name: str, value: object) -> None:
        if name == "__traceback__":
            raise RuntimeError(
                f"Couldn't convert {value!r} to a UNO type; caught exception: <class 'AttributeError'>: 'traceback' object has no attribute 'getTypes'"
            )
        super().__setattr__(name, value)


def test_with_temp_buffer_yields_three_slash_file_url():
    with with_temp_buffer(content="x", ext=".html") as (_path, url):
        assert url.startswith("file:///")


def test_resolve_temp_dir_avoids_gettempdir_under_crosshair(monkeypatch):
    """CrossHair FQN cover must not call gettempdir (auditwall SideEffectDetected)."""
    monkeypatch.setitem(sys.modules, "crosshair", object())
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)
    monkeypatch.setattr(tempfile_mod, "tempdir", None)

    def boom() -> str:
        raise AssertionError("gettempdir must not be called under CrossHair")

    monkeypatch.setattr(tempfile_mod, "gettempdir", boom)
    assert _resolve_temp_dir() == os.curdir
    monkeypatch.setenv("TMPDIR", "/custom/tmp")
    assert _resolve_temp_dir() == "/custom/tmp"


def test_with_temp_buffer_passes_uno_style_exception_through_unchanged():
    recorded_path: str | None = None
    exc_instance = _UnoSimulatedException("UnknownPropertyException: Formula")
    try:
        with with_temp_buffer(content="dummy", ext=".html") as (path, _url):
            recorded_path = path
            assert os.path.exists(recorded_path)
            raise exc_instance
    except Exception as caught:
        assert caught is exc_instance

    assert recorded_path is not None
    assert not os.path.exists(recorded_path)
