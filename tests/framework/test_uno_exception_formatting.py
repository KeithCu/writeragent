# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
"""Unit tests for UNO exception formatting and format_error_payload."""

from plugin.framework.errors import (
    ToolExecutionError,
    resolve_exception_message,
    format_error_payload,
)


def test_resolve_exception_message_standard_python():
    e = ValueError("something went wrong")
    assert resolve_exception_message(e) == "something went wrong"


def test_resolve_exception_message_uno_style():
    class ElementExistException(Exception):
        def __init__(self, message):
            self.Message = message
        def __str__(self):
            return ""

    uno_err = ElementExistException("ElementExistException: Chart_0 already exists")
    assert resolve_exception_message(uno_err) == "ElementExistException: Chart_0 already exists"

    # ToolExecutionError wrapping empty str() exception
    wrapper = ToolExecutionError(uno_err)
    payload = format_error_payload(wrapper)
    assert payload["status"] == "error"
    assert payload["message"] == "ElementExistException: Chart_0 already exists"
    assert payload["message"] != ""


def test_resolve_exception_message_completely_blank():
    class BlankException(Exception):
        def __str__(self):
            return ""

    blank = BlankException()
    assert resolve_exception_message(blank) == "BlankException"

    payload = format_error_payload(blank)
    assert payload["status"] == "error"
    assert payload["message"] == "BlankException"
    assert payload["message"] != ""
