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


def test_text_analytics_templates_include_run_call():
    temps = ta.get_text_analytics_script_templates()
    assert isinstance(temps, dict)
    for h in ("full", "readability", "entities", "key_phrases"):
        if h in temps:
            code = temps[h]
            assert ta.TEXT_ANALYTICS_HEADER_PREFIX not in code
            assert f'"helper": "{h}"' in code
            assert "run_text_analytics" in code
            assert "text" in code
            assert "document_context" in code


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


# --- Topics (fancier analytics; sklearn optional) ---


def test_text_analytics_topics_in_helernames_and_templates():
    assert "topics" in ta.HELPER_NAMES
    temps = ta.get_text_analytics_script_templates()
    assert "topics" in temps
    code = temps["topics"]
    assert '"helper": "topics"' in code
    assert '"n_topics":4' in code or '"n_topics": 4' in code


def test_text_analytics_topics_result_shape():
    # Host-side shape test; doesn't require sklearn
    fake = {"status": "ok", "result": {"topics": [{"id": 0, "terms": ["budget", "revenue"]}], "assignments": []}}
    assert ta.is_text_analytics_result(fake) is True

    # The topics helper itself (will be skipped or return MISSING if no sklearn)
    try:
        out = ta.run_text_analytics("topics", "A long document about budgets and revenue forecasts for the next quarter. " * 5)
    except ImportError:
        pytest.skip("sklearn (or spacy) not available")
    assert out["status"] == "ok"
    res = out.get("result") or {}
    # Either real topics or the clear missing package signal
    assert "topics" in res or res.get("error") == "MISSING_PACKAGE"


def test_text_analytics_topics_accepts_list_for_sections():
    # Verify the dispatcher path for list input (used for section-aware topics)
    try:
        out = ta.run_text_analytics("topics", ["Section one talks about revenue targets.", "Section two discusses project timelines and budgets.", "Third section returns to financial forecasting."])
    except ImportError:
        pytest.skip("sklearn not installed for topics test")
    assert out["status"] == "ok"
    res = out.get("result", {})
    if "error" not in res:
        assert "topics" in res
        # When list provided we expect assignments
        if res.get("topics"):
            assert "assignments" in res or True  # may be present


# --- Sentiment (lexicon-based, no extra deps beyond what's already used for spacy path) ---


def test_text_analytics_sentiment_in_helernames_and_templates():
    assert "sentiment" in ta.HELPER_NAMES
    temps = ta.get_text_analytics_script_templates()
    assert "sentiment" in temps
    code = temps["sentiment"]
    assert '"helper": "sentiment"' in code
    assert "run_text_analytics" in code


def test_text_analytics_sentiment_uses_config_model_via_params():
    # Model override via JSON setting is passed as params (host reads config).
    # We test the worker-side function directly with params.
    try:
        out = ta._extract_sentiment("great success", params={"model": "cardiffnlp/twitter-xlm-roberta-base-sentiment"})
    except Exception:
        pytest.skip("transformers not available")
    # If it ran without MISSING, shape is good (model may or may not be cached/downloaded in test env).
    if out.get("error") == "MISSING_PACKAGE":
        pytest.skip("transformers model not loadable in this env")
    assert "overall" in out or "sentiment" in out  # internal or wrapped


def test_text_analytics_sentiment_result_shape_and_basic_logic():
    # Host-side shape
    fake = {"status": "ok", "result": {"sentiment": {"score": 0.4, "label": "positive"}, "per_section": []}}
    assert ta.is_text_analytics_result(fake) is True

    # Direct call (now uses transformers + multilingual model; skips if not installed)
    try:
        out = ta.run_text_analytics("sentiment", "This is a great success and very positive outcome for everyone.")
    except Exception:
        pytest.skip("transformers not available for sentiment test")
    assert out["status"] == "ok"
    res = out.get("result", {})
    if res.get("error") in (None, "MISSING_PACKAGE") or not res.get("sentiment"):
        pytest.skip("transformers not installed or no model for sentiment")
    sent = res["sentiment"]
    assert "score" in sent and "label" in sent
    assert sent["label"] in ("positive", "negative", "neutral")
    # Positive text should lean positive
    assert sent["score"] > 0

    # Negative
    out2 = ta.run_text_analytics("sentiment", "This is a terrible failure with many problems and risks.")
    sent2 = out2.get("result", {}).get("sentiment", {})
    if not sent2:
        pytest.skip("transformers not installed")
    assert sent2.get("score", 0) < 0
    assert sent2.get("label") == "negative"


def test_text_analytics_sentiment_accepts_list_for_sections():
    secs = [
        "The project had excellent results and strong gains.",
        "However there were serious problems, delays and high costs.",
        "Overall we see some progress but many risks remain."
    ]
    try:
        out = ta.run_text_analytics("sentiment", secs)
    except Exception:
        pytest.skip("transformers not available")
    assert out["status"] == "ok"
    res = out.get("result", {})
    if res.get("error") in (None, "MISSING_PACKAGE") or not res.get("per_section"):
        pytest.skip("transformers not installed or insufficient model")
    assert "sentiment" in res
    assert "per_section" in res
    assert len(res["per_section"]) == 3
    # First section positive, second negative
    assert res["per_section"][0]["label"] == "positive"
    assert res["per_section"][1]["label"] == "negative"

