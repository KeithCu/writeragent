# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for strict embeddings-pool venv resolution (no LO fallback)."""

from __future__ import annotations

import stat
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.constants import WORKER_POOL_DEFAULT, WORKER_POOL_EMBEDDINGS
from plugin.scripting.venv_worker import _resolve_worker_python


def test_embeddings_pool_requires_configured_venv():
    ctx = MagicMock()
    with (
        patch("plugin.scripting.venv_worker.get_config_str", return_value=""),
        patch("plugin.scripting.venv_worker.resolve_libreoffice_python") as mock_lo,
    ):
        exe, err = _resolve_worker_python(ctx, pool=WORKER_POOL_EMBEDDINGS)
    assert exe is None
    assert err is not None
    assert "Embeddings require a configured Python venv" in err["message"]
    mock_lo.assert_not_called()


def test_embeddings_pool_invalid_venv_path():
    ctx = MagicMock()
    with patch("plugin.scripting.venv_worker.get_config_str", return_value="/no/such/venv"):
        exe, err = _resolve_worker_python(ctx, pool=WORKER_POOL_EMBEDDINGS)
    assert exe is None
    assert err is not None
    assert "Embeddings venv not configured or invalid" in err["message"]


def test_embeddings_pool_resolves_valid_venv(tmp_path):
    venv = tmp_path / "embedvenv"
    bindir = venv / "bin"
    bindir.mkdir(parents=True)
    py = bindir / "python3"
    py.write_text("#!/bin/sh\necho ok\n")
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    ctx = MagicMock()
    with patch("plugin.scripting.venv_worker.get_config_str", return_value=str(venv)):
        exe, err = _resolve_worker_python(ctx, pool=WORKER_POOL_EMBEDDINGS)
    assert err is None
    assert exe == str(py)


def test_default_pool_falls_back_to_lo_python_when_venv_empty():
    ctx = MagicMock()
    with (
        patch("plugin.scripting.venv_worker.get_config_str", return_value=""),
        patch("plugin.scripting.venv_worker.resolve_libreoffice_python", return_value=sys.executable) as mock_lo,
    ):
        exe, err = _resolve_worker_python(ctx, pool=WORKER_POOL_DEFAULT)
    assert err is None
    assert exe == sys.executable
    mock_lo.assert_called_once()
