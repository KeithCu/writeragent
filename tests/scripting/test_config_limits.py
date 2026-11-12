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

from plugin.framework.errors import ConfigError
from plugin.scripting.config_limits import (
    _bounds,
    configured_python_max_data_cells,
    python_exec_timeout,
    python_exec_timeout_default,
)
import pytest


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


def test_timeout_schema_limits_from_manifest():
    assert python_exec_timeout_default() == 10
    assert python_exec_timeout(0) == 1
    assert python_exec_timeout(10**9) == 600
    assert _bounds("python_exec_timeout") == (1, 600)


def test_python_exec_timeout_requested_wins_and_clamps():
    with patch("plugin.framework.config.get_config", return_value=10) as mock_get:
        assert python_exec_timeout(100) == 100
        assert python_exec_timeout(1000) == 600
        assert python_exec_timeout(0) == 1
        mock_get.assert_not_called()
        assert python_exec_timeout(None) == 10
        mock_get.assert_called_once_with("scripting.python_exec_timeout")


@patch("plugin.framework.config.get_config", return_value=10)
def test_configured_python_exec_timeout(mock_get):
    assert python_exec_timeout() == 10
    mock_get.assert_called_once_with("scripting.python_exec_timeout")


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
    mock_get.assert_called_once_with("scripting.python_exec_timeout")
    assert mock_resolve_default.call_count == 0


@patch("plugin.framework.config.get_config", return_value=0)
def test_configured_python_exec_timeout_stored_zero_clamps_to_min(mock_get):
    assert python_exec_timeout() == _bounds("python_exec_timeout")[0]
    mock_get.assert_called_once_with("scripting.python_exec_timeout")


def test_schema_getters_match_config_schema():
    from plugin.framework.config_schema import get_config_schema

    schema = get_config_schema("scripting.python_exec_timeout")
    assert schema is not None
    assert python_exec_timeout_default() == schema["default"]
    assert python_exec_timeout(0) == schema["min"]
    assert python_exec_timeout(10**9) == schema["max"]


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
    from plugin.framework.queue_executor import main

    saved = main._async_callback_service
    try:
        names = {f["name"] for f in get_settings_field_specs(MagicMock())}
        assert value in names
    finally:
        main._async_callback_service = saved


def test_get_config_python_max_data_cells_uses_schema_default():
    """get_config must resolve module.yaml default without writeragent.json."""
    from plugin.framework.config import get_config, reset_config_for_tests

    reset_config_for_tests()
    assert get_config("scripting.python_max_data_cells") == 250_000


@patch("plugin.framework.config.get_config", return_value=250_000)
def test_configured_python_max_data_cells(mock_get):
    assert configured_python_max_data_cells() == 250_000
    mock_get.assert_called_once_with("scripting.python_max_data_cells")


@patch("plugin.framework.config.get_config", return_value=9_999_999)
def test_configured_python_max_data_cells_clamps_high(mock_get):
    assert configured_python_max_data_cells() == 2_000_000


@patch("plugin.framework.config.get_config", return_value=0)
def test_configured_python_max_data_cells_clamps_low(mock_get):
    assert configured_python_max_data_cells() == 1000


@patch("plugin.framework.config.get_config", side_effect=RuntimeError("read failed"))
def test_configured_python_max_data_cells_failed_read_uses_default_not_min(mock_get):
    assert configured_python_max_data_cells() == 250_000
