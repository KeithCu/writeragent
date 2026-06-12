# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the persistent Jedi completions engine in Monaco child process."""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from plugin.scripting.venv import editor_main as ej


def test_jedi_completions_mapping(monkeypatch):
    """Verify that Jedi completions are correctly mapped and adjusted for 0-indexed columns."""
    mock_jedi = MagicMock()
    monkeypatch.setattr(ej, "jedi", mock_jedi)

    mock_comp1 = MagicMock()
    mock_comp1.name = "my_func"
    mock_comp1.type = "function"
    mock_comp1.description = "def my_func(x)"
    mock_comp1.docstring.return_value = "This is a function docstring."

    mock_comp2 = MagicMock()
    mock_comp2.name = "MyClass"
    mock_comp2.type = "class"
    mock_comp2.description = "class MyClass"
    mock_comp2.docstring.return_value = "This is a class docstring."

    mock_script = MagicMock()
    mock_script.complete.return_value = [mock_comp1, mock_comp2]
    mock_jedi.Script.return_value = mock_script

    session = ej.JediSession()
    res = session.get_completions("def ", 1, 5)

    # Assert that jedi.Script was called with expected arguments and environment
    from plugin.scripting.venv.venv_sandbox import apply_auto_imports
    expected_code, lines_added = apply_auto_imports("def ")
    mock_jedi.Script.assert_called_once_with(expected_code, environment=session._env)
    mock_script.complete.assert_called_once_with(1 + lines_added, 4)  # Monaco 5 maps to Jedi 4

    # Assert correct structure of returned items
    assert len(res["items"]) == 2
    assert res["items"][0] == {
        "label": "my_func",
        "kind": "function",
        "insertText": "my_func",
        "detail": "def my_func(x)",
        "documentation": "This is a function docstring.",
    }
    assert res["items"][1] == {
        "label": "MyClass",
        "kind": "class",
        "insertText": "MyClass",
        "detail": "class MyClass",
        "documentation": "This is a class docstring.",
    }


def test_jedi_graceful_fallback(monkeypatch):
    """Verify that the engine handles missing jedi module without crashing."""
    monkeypatch.setattr(ej, "jedi", None)
    session = ej.JediSession()
    assert session.is_available() is False
    res = session.get_completions("def ", 1, 5)
    assert res == {"items": []}


def test_jedi_docstring_exception(monkeypatch):
    """Verify that exceptions in comp.docstring() do not cause completion failure."""
    mock_jedi = MagicMock()
    monkeypatch.setattr(ej, "jedi", mock_jedi)

    mock_comp = MagicMock()
    mock_comp.name = "broken_func"
    mock_comp.type = "function"
    mock_comp.description = "def broken_func()"
    mock_comp.docstring.side_effect = Exception("failed to read docstring")

    mock_script = MagicMock()
    mock_script.complete.return_value = [mock_comp]
    mock_jedi.Script.return_value = mock_script

    session = ej.JediSession()
    res = session.get_completions("def ", 1, 5)

    assert len(res["items"]) == 1
    assert res["items"][0] == {
        "label": "broken_func",
        "kind": "function",
        "insertText": "broken_func",
        "detail": "def broken_func()",
        "documentation": "",  # Falls back to empty string
    }


def test_jedi_complete_exception(monkeypatch):
    """Verify that general exceptions in completion query are handled gracefully."""
    mock_jedi = MagicMock()
    monkeypatch.setattr(ej, "jedi", mock_jedi)
    mock_jedi.Script.side_effect = Exception("General jedi error")

    session = ej.JediSession()
    res = session.get_completions("def ", 1, 5)

    assert res == {"items": []}
