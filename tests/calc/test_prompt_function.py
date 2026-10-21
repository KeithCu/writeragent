# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.calc.prompt_function import (
    _EMPTY_DIAGNOSTIC_MAX_LEN,
    _format_empty_prompt_diagnostic,
    execute_prompt_addin,
)
from plugin.calc.prompt_function import CALC_PROMPT_CELL_SYSTEM_PROMPT
from plugin.framework.prompts import CALC_PYTHON_FORMULA_LLM_HINT


@patch("plugin.calc.prompt_function.run_blocking_in_thread")
@patch("plugin.calc.prompt_function.LlmClient")
@patch("plugin.calc.prompt_function.get_api_config")
@patch("plugin.calc.prompt_function.get_config_str")
@patch("plugin.calc.prompt_function.get_config_int", return_value=4096)
def test_prompt_default_system_is_plain_cell_prompt(mock_get_config_int, mock_get_config_str, mock_api, mock_client_cls, mock_run):
    mock_get_config_str.return_value = ""
    mock_run.return_value = {"content": "ok", "finish_reason": "stop", "usage": {}}

    execute_prompt_addin(
        MagicMock(),
        "What is 2+2?",
        None,
        None,
        None,
        client_holder=[None],
    )

    messages = mock_run.call_args[0][2]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == CALC_PROMPT_CELL_SYSTEM_PROMPT
    assert messages[0]["content"] != CALC_PYTHON_FORMULA_LLM_HINT
    assert mock_run.call_args.kwargs.get("pump_idle") is False


@patch("plugin.calc.prompt_function.run_blocking_in_thread")
@patch("plugin.calc.prompt_function.LlmClient")
@patch("plugin.calc.prompt_function.get_api_config")
@patch("plugin.calc.prompt_function.get_config_str")
@patch("plugin.calc.prompt_function.get_config_int", return_value=4096)
def test_prompt_respects_custom_extend_system_prompt(mock_get_config_int, mock_get_config_str, mock_api, mock_client_cls, mock_run):
    mock_get_config_str.return_value = "Custom calc prompt"
    mock_run.return_value = {"content": "ok", "finish_reason": "stop", "usage": {}}

    execute_prompt_addin(MagicMock(), "hello", None, None, None, client_holder=[None])

    messages = mock_run.call_args[0][2]
    assert messages[0]["content"] == "Custom calc prompt"


@patch("plugin.calc.prompt_function.run_blocking_in_thread")
@patch("plugin.calc.prompt_function.LlmClient")
@patch("plugin.calc.prompt_function.get_api_config")
@patch("plugin.calc.prompt_function.get_config_int", return_value=4096)
def test_prompt_formula_system_arg_overrides_default(mock_get_config_int, mock_api, mock_client_cls, mock_run):
    mock_run.return_value = {"content": "ok", "finish_reason": "stop", "usage": {}}

    execute_prompt_addin(
        MagicMock(),
        "hello",
        "Inline system",
        None,
        None,
        client_holder=[None],
    )

    messages = mock_run.call_args[0][2]
    assert messages[0]["content"] == "Inline system"


@patch("plugin.calc.prompt_function.run_blocking_in_thread")
@patch("plugin.calc.prompt_function.LlmClient")
@patch("plugin.calc.prompt_function.get_api_config")
@patch("plugin.calc.prompt_function.get_config_str", return_value="")
@patch("plugin.calc.prompt_function.get_config_int", return_value=4096)
def test_prompt_empty_response_returns_diagnostic(
    mock_get_config_int, mock_get_config_str, mock_api, mock_client_cls, mock_run
):
    mock_api.return_value = {"model": "mercury-2"}
    mock_run.return_value = {
        "content": None,
        "finish_reason": "length",
        "usage": {"completion_tokens": 0, "reasoning_tokens": 69},
        "reasoning": "Thinking about the user's greeting and model identity question.",
    }

    out = execute_prompt_addin(MagicMock(), "Testing?", None, None, None, client_holder=[None])

    assert out.startswith("Error: model returned no text.")
    assert "finish_reason='length'" in out
    assert "completion_tokens=0" in out
    assert "reasoning_tokens=69" in out
    assert "model=mercury-2" in out
    assert "Reasoning excerpt:" in out
    assert "Thinking about the user's greeting" in out
    assert mock_run.call_count == 1


def test_format_empty_prompt_diagnostic_truncates():
    long_reason = "word " * 200
    msg = _format_empty_prompt_diagnostic(
        {"finish_reason": "stop", "usage": {}, "reasoning_content": long_reason},
        model="m",
    )
    assert len(msg) <= _EMPTY_DIAGNOSTIC_MAX_LEN
    assert msg.startswith("Error: model returned no text.")
