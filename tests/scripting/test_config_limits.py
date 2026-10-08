# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for scripting config limits (timeout, max data cells)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting.config_limits import (
    configured_python_exec_timeout,
    configured_python_max_data_cells,
    python_exec_timeout_default,
    python_exec_timeout_max,
    python_exec_timeout_min,
    python_max_data_cells_default,
    python_max_data_cells_max,
    python_max_data_cells_min,
    resolve_python_exec_timeout,
)
import pytest


def test_timeout_fallbacks_match_manifest():
    from plugin._manifest import MODULES
    from plugin.scripting.config_limits import (
        _TIMEOUT_FALLBACK_DEFAULT,
        _TIMEOUT_FALLBACK_MAX,
        _TIMEOUT_FALLBACK_MIN,
    )

    field = None
    for module in MODULES:
        if isinstance(module, dict) and module.get("name") == "scripting":
            config = module.get("config") or {}
            field = config.get("python_exec_timeout")
            break
    assert isinstance(field, dict)
    assert field["default"] == _TIMEOUT_FALLBACK_DEFAULT
    assert field["min"] == _TIMEOUT_FALLBACK_MIN
    assert field["max"] == _TIMEOUT_FALLBACK_MAX


def test_timeout_schema_limits_from_manifest():
    assert python_exec_timeout_default() == 10
    assert python_exec_timeout_min() == 1
    assert python_exec_timeout_max() == 600


def test_resolve_python_exec_timeout_clamp_and_fallback():
    assert resolve_python_exec_timeout(None, configured=10) == 10
    assert resolve_python_exec_timeout(100, configured=10) == 100
    assert resolve_python_exec_timeout(1000, configured=10) == 600
    assert resolve_python_exec_timeout(0, configured=10) == 1
    assert resolve_python_exec_timeout("bad", configured=25) == 25
    assert resolve_python_exec_timeout(None) == 10


@patch("plugin.framework.config.get_config_int", return_value=10)
def test_configured_python_exec_timeout(mock_get):
    assert configured_python_exec_timeout() == 10
    mock_get.assert_called_once_with("scripting.python_exec_timeout")


@patch("plugin.framework.config.get_config_int", return_value=1000)
def test_configured_python_exec_timeout_clamps_legacy_high(mock_get):
    # Above schema max (600) but within DEAL_MAX_ARGV (4096) so @deal.pre on _clamp_timeout still runs.
    assert configured_python_exec_timeout() == 600


def test_run_venv_python_script_schema_has_no_timeout_sec():
    from plugin.calc.python.venv import RunVenvPythonScript
    from plugin.framework.tool import ToolRegistry

    registry = ToolRegistry(services={})
    registry.register(RunVenvPythonScript())
    mock_sheet = MagicMock()

    def supports(svc):
        return svc == "com.sun.star.sheet.SpreadsheetDocument"

    mock_sheet.supportsService = supports
    schemas = registry.get_schemas("openai", doc=mock_sheet, active_domain="python")
    py_schema = next(s for s in schemas if s["function"]["name"] == "run_venv_python_script")
    props = py_schema["function"]["parameters"]["properties"]
    assert "timeout_sec" not in props


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("scripting__python_exec_timeout", id="test_settings_field_specs_include_python_exec_timeout"),
        pytest.param("scripting__python_max_data_cells", id="test_settings_field_specs_include_python_max_data_cells"),
    ],
)
def test_settings_field_specs_include_python_exec_timeout(value):
    from plugin.chatbot.settings_dialog import get_settings_field_specs

    names = {f["name"] for f in get_settings_field_specs(MagicMock())}
    assert value in names


def test_max_data_cells_schema_limits_from_manifest():
    assert python_max_data_cells_default() == 250_000
    assert python_max_data_cells_min() == 1000
    assert python_max_data_cells_max() == 2_000_000


def test_get_config_python_max_data_cells_uses_schema_default():
    """get_config must resolve module.yaml default without writeragent.json."""
    from plugin.framework.config import get_config, reset_config_for_tests

    reset_config_for_tests()
    assert get_config("scripting.python_max_data_cells") == 250_000


@patch("plugin.framework.config.get_config_int", return_value=250_000)
def test_configured_python_max_data_cells(mock_get):
    assert configured_python_max_data_cells() == 250_000
    mock_get.assert_called_once_with("scripting.python_max_data_cells")


@patch("plugin.framework.config.get_config_int", return_value=9_999_999)
def test_configured_python_max_data_cells_clamps_high(mock_get):
    assert configured_python_max_data_cells() == 2_000_000


@patch("plugin.framework.config.get_config_int", return_value=0)
def test_configured_python_max_data_cells_clamps_low(mock_get):
    assert configured_python_max_data_cells() == 1000


