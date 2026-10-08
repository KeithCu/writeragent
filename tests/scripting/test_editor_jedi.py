# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the persistent Jedi completions engine in Monaco child process."""

from __future__ import annotations

from unittest.mock import MagicMock

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


def test_monaco_api_get_completions_when_jedi_missing(monkeypatch):
    monkeypatch.setattr(ej, "jedi", None)
    api = ej.MonacoEditorApi()
    assert api.is_jedi_available() is False
    assert api.get_completions("def ", 1, 5) == {"items": []}


def test_jedi_completions_caps_docstrings(monkeypatch):
    mock_jedi = MagicMock()
    monkeypatch.setattr(ej, "jedi", mock_jedi)

    comps = []
    for i in range(25):
        c = MagicMock()
        c.name = f"func_{i}"
        c.type = "function"
        c.description = f"def func_{i}()"
        c.docstring.return_value = f"doc_{i}"
        comps.append(c)

    mock_script = MagicMock()
    mock_script.complete.return_value = comps
    mock_jedi.Script.return_value = mock_script

    session = ej.JediSession()
    res = session.get_completions("x.", 1, 3)

    assert len(res["items"]) == 25
    # First 20 have docstrings, subsequent items do not
    assert res["items"][0]["documentation"] == "doc_0"
    assert res["items"][19]["documentation"] == "doc_19"
    assert res["items"][20]["documentation"] == ""
    assert comps[20].docstring.call_count == 0


def test_monaco_api_get_completions_drops_stale_request(monkeypatch):
    api = ej.MonacoEditorApi()
    mock_jedi_session = MagicMock()
    # Simulate a slow request that completes after generation advances
    def slow_completions(*args, **kwargs):
        api._completion_gen += 1  # newer request arrives
        return {"items": [{"label": "stale"}]}

    mock_jedi_session.get_completions.side_effect = slow_completions
    api._jedi = mock_jedi_session

    res = api.get_completions("x.", 1, 3)
    assert res == {"items": []}

