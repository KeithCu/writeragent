# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for scripting config limits (timeout, max data cells)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from plugin.framework.deal_shim import DEAL_MAX_ARGV
from plugin.framework.errors import ConfigError
from plugin.scripting.config_limits import (
    _bounds,
    _clamp,
    configured_python_max_data_cells,
    python_exec_timeout,
    python_exec_timeout_default,
)


def test_timeout_fallbacks_match_manifest():
    from plugin._manifest import MODULES

    field = None
    for module in MODULES:
        if isinstance(module, dict) and module.get("name") == "scripting":
            config = module.get("config") or {}
            field = config.get("python_exec_timeout")
            break
    assert isinstance(field, dict)
    with (
        patch("plugin.framework.config_schema.get_config_schema", return_value=None),
        patch("plugin.framework.config.get_config", side_effect=ConfigError("read failed")),
    ):
        assert python_exec_timeout_default() == field["default"]
        assert python_exec_timeout(0) == field["min"]
        assert python_exec_timeout(10**6) == field["max"]


def test_python_exec_timeout_requested_wins_and_clamps():
    with patch("plugin.framework.config.get_config", return_value=10) as mock_get:
        assert python_exec_timeout(100) == 100
        assert python_exec_timeout(1000) == 600
        assert python_exec_timeout(0) == 1
        mock_get.assert_not_called()
        assert python_exec_timeout(None) == 10


@patch(
    "plugin.framework.config.get_config",
    side_effect=lambda key, *a, **k: {"scripting.python_exec_timeout": 37}[key],
)
def test_configured_python_exec_timeout(mock_get):
    assert python_exec_timeout() == 37


@patch("plugin.framework.config.get_config", return_value=1000)
def test_configured_python_exec_timeout_clamps_legacy_high(mock_get):
    # Above schema max (600) but within DEAL_MAX_ARGV (4096) so @deal.pre on _clamp_timeout still runs.
    assert python_exec_timeout() == 600


@patch("plugin.framework.config_schema.resolve_default", side_effect=ConfigError("no schema default"))
@patch("plugin.framework.config.get_config", side_effect=ConfigError("read failed"))
def test_configured_python_exec_timeout_failed_read_uses_default_not_min(mock_get, mock_resolve_default):
    """Failed read uses the schema/fallback default, not the 1s floor.

    The resolve_default patch is not used by the fixed function. It is what
    makes get_config return 0 if the function goes back to that helper.
    In this checkout the manifest is present, so without this patch the old
    helper still returns 10 and the assertion would pass on the bug.
    """
    assert python_exec_timeout_default() != _bounds("python_exec_timeout")[0]
    assert python_exec_timeout() == python_exec_timeout_default()
    assert mock_resolve_default.call_count == 0


@patch("plugin.framework.config.get_config", return_value=0)
def test_configured_python_exec_timeout_stored_zero_clamps_to_min(mock_get):
    assert python_exec_timeout() == _bounds("python_exec_timeout")[0]


@patch(
    "plugin.framework.config.get_config",
    side_effect=lambda key, *a, **k: {"scripting.python_max_data_cells": 300_000}[key],
)
def test_configured_python_max_data_cells(mock_get):
    assert configured_python_max_data_cells() == 300_000


@patch("plugin.framework.config.get_config", return_value=9_999_999)
def test_configured_python_max_data_cells_clamps_high(mock_get):
    assert configured_python_max_data_cells() == 2_000_000


@patch("plugin.framework.config.get_config", return_value=0)
def test_configured_python_max_data_cells_clamps_low(mock_get):
    assert configured_python_max_data_cells() == 1000


@patch("plugin.framework.config.get_config", side_effect=RuntimeError("read failed"))
def test_configured_python_max_data_cells_failed_read_uses_default_not_min(mock_get):
    from plugin._manifest import MODULES

    field = next(
        m["config"]["python_max_data_cells"]
        for m in MODULES
        if isinstance(m, dict) and m.get("name") == "scripting"
    )
    assert configured_python_max_data_cells() == field["default"]


@pytest.mark.verification
def test_clamp_timeout_overflow_clamps() -> None:
    """A timeout past DEAL_MAX_ARGV is user config. Clamp it; do not PreContract."""
    t_min, t_max = _bounds("python_exec_timeout")
    assert _clamp("python_exec_timeout", DEAL_MAX_ARGV + 1) == t_max
    assert t_min <= _clamp("python_exec_timeout", 1) <= t_max
    assert python_exec_timeout(DEAL_MAX_ARGV + 1) == python_exec_timeout(10**9)


@pytest.mark.verification
def test_python_exec_timeout_is_total() -> None:
    default = python_exec_timeout_default()
    t_max = python_exec_timeout(10**9)
    for raw, expected in (
        ("20", 20),
        (float("nan"), default),
        (DEAL_MAX_ARGV + 1, t_max),
        (33, 33),
        (0, 1),
    ):
        with patch("plugin.framework.config.get_config", return_value=raw):
            assert python_exec_timeout() == expected
