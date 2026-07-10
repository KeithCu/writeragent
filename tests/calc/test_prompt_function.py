# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.calc.prompt_function import execute_prompt_addin
from plugin.framework.prompts import CALC_PYTHON_FORMULA_LLM_HINT


@patch("plugin.calc.prompt_function.run_blocking_in_thread")
@patch("plugin.calc.prompt_function.LlmClient")
@patch("plugin.calc.prompt_function.get_api_config")
@patch("plugin.calc.prompt_function.get_config_str")
def test_prompt_default_system_includes_sandbox_policy(mock_get_config_str, mock_api, mock_client_cls, mock_run):
    mock_get_config_str.return_value = ""
    mock_run.return_value = "ok"
    mock_client_cls.return_value.chat_completion_sync.return_value = "ok"

    execute_prompt_addin(
        MagicMock(),
        "Write a =PYTHON formula for np.mean of B1:B10",
        None,
        None,
        None,
        client_holder=[None],
    )

    messages = mock_run.call_args[0][2]
    assert messages[0]["role"] == "system"
    assert "PYTHON VENV SANDBOX" in messages[0]["content"]
    assert messages[0]["content"] == CALC_PYTHON_FORMULA_LLM_HINT


@patch("plugin.calc.prompt_function.run_blocking_in_thread")
@patch("plugin.calc.prompt_function.LlmClient")
@patch("plugin.calc.prompt_function.get_api_config")
@patch("plugin.calc.prompt_function.get_config_str")
def test_prompt_respects_custom_extend_system_prompt(mock_get_config_str, mock_api, mock_client_cls, mock_run):
    mock_get_config_str.return_value = "Custom calc prompt"
    mock_run.return_value = "ok"
    mock_client_cls.return_value.chat_completion_sync.return_value = "ok"

    execute_prompt_addin(MagicMock(), "hello", None, None, None, client_holder=[None])

    messages = mock_run.call_args[0][2]
    assert messages[0]["content"] == "Custom calc prompt"


@patch("plugin.calc.prompt_function.run_blocking_in_thread")
@patch("plugin.calc.prompt_function.LlmClient")
@patch("plugin.calc.prompt_function.get_api_config")
def test_prompt_formula_system_arg_overrides_default(mock_api, mock_client_cls, mock_run):
    mock_run.return_value = "ok"
    mock_client_cls.return_value.chat_completion_sync.return_value = "ok"

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
