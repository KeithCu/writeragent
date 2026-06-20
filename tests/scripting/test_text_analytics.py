# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the high-quality spaCy text analytics helpers.

These require spaCy (and ideally textdescriptives) in the test environment.
They are skipped gracefully if the packages are not present.
"""

from __future__ import annotations

import pytest

from plugin.scripting import text_analytics as ta


def test_loads_a_model_and_analyzes():
    # This should succeed as long as at least one model (xx_sent_ud_sm or en_*) is installed
    # in the environment running the tests.
    try:
        result = ta.analyze_text(
            "The quick brown fox jumps over the lazy dog. "
            "This is a second sentence for testing multilingual spaCy pipelines."
        )
    except RuntimeError as e:
        if "Could not load any suitable spaCy model" in str(e):
            pytest.skip("No spaCy model available for text analytics tests")
        raise
    except ImportError:
        pytest.skip("spacy package not installed in test environment")

    assert result["status"] == "ok"
    data = result["result"]
    assert isinstance(data, dict)
    # We should at least get entities and key_phrases lists (even if empty)
    assert "entities" in data
    assert "key_phrases" in data
    assert "meta" in data


def test_run_text_analytics_dispatcher():
    try:
        out = ta.run_text_analytics("full", "SpaCy delivers high quality multilingual NLP.")
    except RuntimeError as e:
        if "Could not load any suitable spaCy model" in str(e):
            pytest.skip("No spaCy model available for text analytics tests")
        raise
    except ImportError:
        pytest.skip("spacy package not installed in test environment")

    assert out["status"] == "ok"
    assert "result" in out


# --- Host-side (no model required) ---


def test_text_analytics_templates_and_header_roundtrip():
    temps = ta.get_text_analytics_script_templates()
    assert isinstance(temps, dict)
    # Should have at least the shipped helpers
    for h in ("full", "readability", "entities", "key_phrases"):
        if h in temps:
            code = temps[h]
            assert ta.TEXT_ANALYTICS_HEADER_PREFIX in code
            meta = ta.parse_text_analytics_script_header(code)
            assert meta is not None
            assert meta.helper == h
            assert isinstance(meta.params, dict)


def test_text_analytics_is_result_shapes():
    fullish = {"status": "ok", "result": {"readability": {}, "entities": [], "meta": {}}}
    assert ta.is_text_analytics_result(fullish) is True

    narrow = {"status": "ok", "result": {"entities": [{"text": "x", "label": "ORG"}]}}
    assert ta.is_text_analytics_result(narrow) is True

    not_ok = {"status": "error", "message": "boom"}
    assert ta.is_text_analytics_result(not_ok) is False

    junk = {"foo": "bar"}
    assert ta.is_text_analytics_result(junk) is False


def test_text_analytics_supports_and_names():
    assert hasattr(ta, "HELPER_NAMES")
    assert "full" in ta.HELPER_NAMES

    # supports should not crash and return bool for common inputs
    assert ta.supports_text_analytics_manual(None) is False
    # A dummy object should return bool without raising
    val = ta.supports_text_analytics_manual(object())
    assert isinstance(val, bool)


def test_check_diagnostics():
    try:
        out = ta.check_diagnostics()
    except ImportError:
        pytest.skip("spacy package not installed in test environment")

    assert out["status"] in ("ok", "error")
    if out["status"] == "ok":
        assert "spacy_version" in out
        assert "has_textdescriptives" in out
        assert "models" in out
        assert isinstance(out["models"], list)


def test_run_text_analytics_diagnostics_dispatch():
    try:
        out = ta.run_text_analytics("diagnostics")
    except ImportError:
        pytest.skip("spacy package not installed in test environment")

    assert out["status"] == "ok"
    assert "result" in out
    assert out["result"]["status"] in ("ok", "error")

