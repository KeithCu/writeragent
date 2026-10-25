# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.prompt_function import (
    _EMPTY_DIAGNOSTIC_MAX_LEN,
    CALC_PROMPT_CELL_SYSTEM_PROMPT,
    _format_empty_prompt_diagnostic,
    _optional_max_tokens,
    _optional_text,
    execute_prompt_addin,
)
from plugin.framework.errors import NetworkError
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
    assert mock_client_cls.call_args.kwargs.get("register_with_send") is False
    assert mock_run.call_args.kwargs.get("prepend_dev_build_system_prefix") is False


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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("", ""),
        ("  ", ""),
        (0.0, ""),
        (0, ""),
        (False, ""),
        (float("inf"), ""),
        (-1, "-1"),
        (1.9, "1.9"),
        (" model ", "model"),
    ],
)
def test_optional_text(value: Any, expected: str):
    assert _optional_text(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 4096),
        (0.0, 4096),
        (False, 4096),
        (-1, 4096),
        (float("inf"), 4096),
        (float("nan"), 4096),
        ("  ", 4096),
        (99, 4096),
        ("500", 500),
        (500.7, 500),
    ],
)
@patch("plugin.calc.prompt_function.get_config_int", return_value=4096)
def test_optional_max_tokens(mock_get_config_int, value: Any, expected: int):
    assert _optional_max_tokens(value) == expected


@patch("plugin.calc.prompt_function.run_blocking_in_thread")
@patch("plugin.calc.prompt_function.LlmClient")
@patch("plugin.calc.prompt_function.get_api_config", return_value={"model": "cfg"})
@patch("plugin.calc.prompt_function.get_config_int", return_value=4096)
def test_prompt_model_override_reaches_new_client(mock_get_config_int, mock_api, mock_client_cls, mock_run):
    mock_run.return_value = {"content": "ok", "finish_reason": "stop", "usage": {}}
    execute_prompt_addin(MagicMock(), "hello", None, "gpt-x", None, client_holder=[None])
    assert mock_client_cls.call_args.args[0]["model"] == "gpt-x"


@pytest.mark.parametrize("blank_model", [0.0, ""])
@patch("plugin.calc.prompt_function.run_blocking_in_thread")
@patch("plugin.calc.prompt_function.LlmClient")
@patch("plugin.calc.prompt_function.get_api_config", return_value={"model": "cfg"})
@patch("plugin.calc.prompt_function.get_config_int", return_value=4096)
def test_prompt_blank_model_does_not_override(mock_get_config_int, mock_api, mock_client_cls, mock_run, blank_model):
    mock_run.return_value = {"content": "ok", "finish_reason": "stop", "usage": {}}
    execute_prompt_addin(MagicMock(), "hello", None, blank_model, None, client_holder=[None])
    assert mock_client_cls.call_args.args[0]["model"] == "cfg"


@pytest.mark.parametrize("blank_system", ["   ", "", 0.0])
@patch("plugin.calc.prompt_function.run_blocking_in_thread")
@patch("plugin.calc.prompt_function.LlmClient")
@patch("plugin.calc.prompt_function.get_api_config")
@patch("plugin.calc.prompt_function.get_config_str", return_value="Custom")
@patch("plugin.calc.prompt_function.get_config_int", return_value=4096)
def test_prompt_blank_system_prompt_uses_fallback(mock_get_config_int, mock_get_config_str, mock_api, mock_client_cls, mock_run, blank_system):
    mock_run.return_value = {"content": "ok", "finish_reason": "stop", "usage": {}}
    execute_prompt_addin(MagicMock(), "hello", blank_system, None, None, client_holder=[None])
    messages = mock_run.call_args[0][2]
    assert messages[0]["content"] == "Custom"


@patch("plugin.calc.prompt_function.run_blocking_in_thread")
@patch("plugin.calc.prompt_function.LlmClient")
def test_prompt_blank_message_returns_error_without_call(mock_client_cls, mock_run):
    res = execute_prompt_addin(MagicMock(), "   ", None, None, None, client_holder=[None])
    assert res == "Error: prompt is empty."
    mock_client_cls.assert_not_called()
    mock_run.assert_not_called()


@patch("plugin.calc.prompt_function.run_blocking_in_thread")
@patch("plugin.calc.prompt_function.LlmClient")
@patch("plugin.calc.prompt_function.get_api_config", return_value={"model": "cfg"})
@patch("plugin.calc.prompt_function.get_config_int", return_value=4096)
def test_prompt_reused_client_clears_stop(mock_get_config_int, mock_api, mock_client_cls, mock_run):
    mock_run.return_value = {"content": "ok", "finish_reason": "stop", "usage": {}}
    client = MagicMock()
    client.config = {}
    execute_prompt_addin(MagicMock(), "hello", None, None, None, client_holder=[client])
    mock_client_cls.assert_not_called()
    assert client.config == {"model": "cfg"}
    client.clear_stop.assert_called_once_with()


@patch("plugin.calc.prompt_function.run_blocking_in_thread")
@patch("plugin.calc.prompt_function.LlmClient")
@patch("plugin.calc.prompt_function.get_api_config")
@patch("plugin.calc.prompt_function.get_config_str", return_value="")
@patch("plugin.calc.prompt_function.get_config_int", return_value=4096)
def test_prompt_empty_response_nested_reasoning_and_used_model(mock_get_config_int, mock_get_config_str, mock_api, mock_client_cls, mock_run):
    mock_api.return_value = {"model": "requested-model"}
    mock_run.return_value = {
        "content": None,
        "finish_reason": "stop",
        "usage": {
            "completion_tokens": 5,
            "completion_tokens_details": {"reasoning_tokens": 7},
        },
        "model": "used-model",
    }
    out = execute_prompt_addin(MagicMock(), "Testing?", None, None, None, client_holder=[None])
    assert "reasoning_tokens=7" in out
    assert "model=used-model" in out


@patch("plugin.calc.prompt_function.run_blocking_in_thread")
@patch("plugin.calc.prompt_function.LlmClient")
@patch("plugin.calc.prompt_function.get_api_config")
@patch("plugin.calc.prompt_function.get_config_int", return_value=4096)
def test_prompt_long_error_is_capped(mock_get_config_int, mock_api, mock_client_cls, mock_run):
    mock_run.side_effect = RuntimeError("x" * 2000)
    out = execute_prompt_addin(MagicMock(), "hello", None, None, None, client_holder=[None])
    assert out.startswith("Error: ")
    assert len(out) <= _EMPTY_DIAGNOSTIC_MAX_LEN


@patch("plugin.calc.prompt_function.run_blocking_in_thread")
@patch("plugin.calc.prompt_function.LlmClient")
@patch("plugin.calc.prompt_function.get_api_config")
@patch("plugin.calc.prompt_function.get_config_int", return_value=4096)
def test_prompt_expected_failure_returns_cell_string(mock_get_config_int, mock_api, mock_client_cls, mock_run):
    mock_run.side_effect = NetworkError("boom")
    out = execute_prompt_addin(MagicMock(), "hello", None, None, None, client_holder=[None])
    assert out.startswith("Error: ")
