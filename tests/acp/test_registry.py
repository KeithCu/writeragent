"""normalize_backend_id warns on a stale or unknown id and still returns builtin."""

import logging

from plugin.acp.builtin import BuiltinBackend
from plugin.acp.registry import get_backend, normalize_backend_id


def test_known_and_empty_ids_do_not_warn(caplog):
    caplog.set_level(logging.WARNING, logger="plugin.acp.registry")
    assert normalize_backend_id("hermes") == "hermes"
    assert normalize_backend_id("Hermes") == "hermes"
    assert normalize_backend_id("builtin") == "builtin"
    assert normalize_backend_id("") == "builtin"
    assert normalize_backend_id(None) == "builtin"
    assert normalize_backend_id("   ") == "builtin"
    assert caplog.text == ""


def test_unknown_id_warns_and_returns_builtin(caplog):
    caplog.set_level(logging.WARNING, logger="plugin.acp.registry")
    assert normalize_backend_id("gemini") == "builtin"
    assert normalize_backend_id("nonexistent") == "builtin"
    assert "gemini" in caplog.text
    assert "nonexistent" in caplog.text
    assert "using builtin" in caplog.text
    assert isinstance(get_backend("gemini"), BuiltinBackend)
