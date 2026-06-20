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

pytest.importorskip("spacy")

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

    assert out["status"] == "ok"
    assert "result" in out
