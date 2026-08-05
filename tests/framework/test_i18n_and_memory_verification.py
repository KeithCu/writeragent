# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / CrossHair verification for i18n and chatbot memory helpers."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.framework.i18n import _, get_active_locale
from plugin.chatbot.memory import (
    upsert_memory_arguments_dict,
    memory_key_from_tool_arguments,
    format_upsert_memory_chat_line,
    format_upsert_memory_chat_line_from_arguments,
)


@given(msg=st.text())
@settings(max_examples=100)
def test_i18n_translation_contracts(msg: str) -> None:
    res = _(msg)
    assert isinstance(res, str)
    assert isinstance(get_active_locale(), str)


@given(key=st.text(), content=st.text())
def test_format_upsert_memory_chat_line_contracts(key: str, content: str) -> None:
    line = format_upsert_memory_chat_line({"key": key, "content": content})
    assert isinstance(line, str)
    assert line.endswith("\n")
    if len(content) > 500:
        assert "..." in line


@given(arg_json=st.text())
def test_memory_arguments_contracts(arg_json: str) -> None:
    d = upsert_memory_arguments_dict(arg_json)
    assert d is None or isinstance(d, dict)
    
    k = memory_key_from_tool_arguments(arg_json)
    assert k is None or isinstance(k, str)

    line = format_upsert_memory_chat_line_from_arguments(arg_json)
    assert isinstance(line, str)
    assert line.endswith("\n")
