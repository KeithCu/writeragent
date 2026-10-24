# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the high-quality spaCy text analytics helpers.

These require spaCy (and ideally textdescriptives) in the test environment.
They are skipped gracefully if the packages are not present.
"""

from __future__ import annotations

from tests.harness.strip_bundle import is_release_build
import sys
from typing import Any

import pytest

from plugin.scripting import text_analytics as ta
from plugin.scripting.text_analytics_ui import format_text_analytics_result_for_display
from plugin.scripting.venv import text_analytics as vta


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
            assert "from writeragent.scripting.text_analytics import run_text_analytics" in code


def test_text_analytics_is_result_shapes():
    fullish = {"status": "ok", "result": {"readability": {}, "entities": [], "meta": {}}}
    assert ta.is_text_analytics_result(fullish) is True

    narrow = {"status": "ok", "result": {"entities": [{"text": "x", "label": "ORG"}]}}
    assert ta.is_text_analytics_result(narrow) is True

    not_ok = {"status": "error", "message": "boom"}
    assert ta.is_text_analytics_result(not_ok) is False

    junk = {"foo": "bar"}
    assert ta.is_text_analytics_result(junk) is False

    assert ta.is_text_analytics_result({"status": "ok", "helper": "diagnostics"}) is True
    assert ta.is_text_analytics_result({"status": "error", "helper": "check"}) is True
    assert ta.is_text_analytics_result(
        {"status": "error", "code": "TEXT_ANALYTICS_ERROR", "helper": "sentiment"}
    ) is True
    assert ta.is_text_analytics_result(
        {"status": "error", "code": "UNKNOWN_HELPER", "helper": "nope"}
    ) is True


def test_run_text_analytics_unknown_helper_is_an_error():
    out = ta.run_text_analytics("not_a_real_helper", "hello")
    assert out["status"] == "error"
    assert out["code"] == "UNKNOWN_HELPER"
    assert out["helper"] == "not_a_real_helper"
    assert ta.is_text_analytics_result(out) is True


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
    assert "scikit-learn" in out["text_analytics_install"]
    if out["status"] == "ok":
        assert "spacy_version" in out
        assert "has_textdescriptives" in out
        assert "models" in out
        assert isinstance(out["models"], list)
        assert isinstance(out["has_sklearn"], bool)
        assert "sklearn_version" in out


def test_check_diagnostics_non_string_lang_returns_dict():
    out = vta.check_diagnostics(lang=123)
    assert isinstance(out, dict)


def test_run_text_analytics_diagnostics_dispatch():
    try:
        out = ta.run_text_analytics("diagnostics")
    except ImportError:
        pytest.skip("spacy package not installed in test environment")

    assert out["status"] == "ok"
    assert "result" in out
    assert out["result"]["status"] in ("ok", "error")


# --- Topics (fancier analytics; sklearn optional) ---


@pytest.mark.parametrize(
    "value, value_2, value_3, value_4, value_5",
    [
        pytest.param("topics", "topics", "topics", "run_text_analytics", "'n_topics': 4", id="test_text_analytics_topics_in_helernames_and_templates"),
        # --- Sentiment (lexicon-based, no extra deps beyond what's already used for spacy path) ---
        pytest.param("sentiment", "sentiment", "sentiment", "from writeragent.scripting.text_analytics import run_text_analytics", "'helper': 'sentiment'", id="test_text_analytics_sentiment_in_helernames_and_templates"),
    ],
)
def test_text_analytics_topics_in_helernames_and_templates(value, value_2, value_3, value_4, value_5):
    assert value in ta.HELPER_NAMES
    temps = ta.get_text_analytics_script_templates()
    assert value_2 in temps
    code = temps[value_3]
    assert value_4 in code
    assert value_5 in code


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
    secs = [
        "Section one talks about annual revenue targets and overall company growth plans.",
        "Section two discusses project timelines, budget allocations and quarterly financial forecasting.",
        "Third section returns to financial forecasting with detailed statistical trend predictions.",
    ]
    try:
        out = ta.run_text_analytics("topics", secs)
    except ImportError:
        pytest.skip("sklearn not installed for topics test")
    assert out["status"] == "ok"
    res = out.get("result", {})
    if "error" not in res:
        assert "topics" in res
        assignments = res.get("assignments", [])
        assert len(assignments) == 3
        assert [a["section_index"] for a in assignments] == [0, 1, 2]
        meta = res.get("meta", {})
        assert meta["n_topics"] == len(res["topics"])


def test_text_analytics_sentiment_result_shape_and_basic_logic():
    # Host-side shape
    fake = {"status": "ok", "result": {"sentiment": {"score": 0.4, "label": "positive"}, "per_section": []}}
    assert ta.is_text_analytics_result(fake) is True

    # Direct call (now uses transformers + multilingual model; skips if not installed)
    try:
        out = ta.run_text_analytics("sentiment", "This is a great success and very positive outcome for everyone.")
    except Exception:
        pytest.skip("transformers not available for sentiment test")
    # A missing model used to come back as status ok. It is an error now.
    if out.get("status") == "error":
        pytest.skip("transformers not installed or no model for sentiment")
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
    if out.get("status") == "error":
        pytest.skip("transformers not installed or no model for sentiment")
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


def test_result_to_html_table_double_escapes_markup():
    html = ta._result_to_html_table({"key_phrases": [{"lemma": "<script>"}]})
    assert "&amp;lt;script&amp;gt;" in html
    assert "<script>" not in html


def test_model_candidates_try_sent_once_and_ent_last():
    en_cands = vta._model_candidates("en")
    assert en_cands[0] == "en_core_web_sm"
    assert en_cands.count("xx_sent_ud_sm") == 1
    assert en_cands[-1] == "xx_ent_wiki_sm"

    none_cands = vta._model_candidates(None)
    assert none_cands == ["xx_sent_ud_sm", "xx_ent_wiki_sm"]
    assert vta._model_candidates(123) == ["xx_sent_ud_sm", "xx_ent_wiki_sm"]

    ja_cands = vta._model_candidates("ja")
    assert ja_cands[0] == "ja_core_news_sm"

    assert vta._LANG_MODELS["mk"] == ("mk_core_news_sm",)


def test_normalize_sentiment_label():
    for lab in ("positive", "1", "5", "POS"):
        assert vta._normalize_sentiment_label(lab) == "positive"
    for lab in ("0", "negative"):
        assert vta._normalize_sentiment_label(lab) == "negative"
    for lab in ("2", "3", "4"):
        assert vta._normalize_sentiment_label(lab) == "neutral"
    assert vta._normalize_sentiment_label("LABEL_1", num_labels=None) == "neutral"

    # num_labels = 2
    assert vta._normalize_sentiment_label("LABEL_0", num_labels=2) == "negative"
    assert vta._normalize_sentiment_label("LABEL_1", num_labels=2) == "positive"

    # num_labels = 3
    assert vta._normalize_sentiment_label("LABEL_0", num_labels=3) == "negative"
    assert vta._normalize_sentiment_label("LABEL_1", num_labels=3) == "neutral"
    assert vta._normalize_sentiment_label("LABEL_2", num_labels=3) == "positive"

    # num_labels = 5
    assert vta._normalize_sentiment_label("LABEL_0", num_labels=5) == "negative"
    assert vta._normalize_sentiment_label("LABEL_1", num_labels=5) == "negative"
    assert vta._normalize_sentiment_label("LABEL_2", num_labels=5) == "neutral"
    assert vta._normalize_sentiment_label("LABEL_3", num_labels=5) == "positive"
    assert vta._normalize_sentiment_label("LABEL_4", num_labels=5) == "positive"

    # star ratings
    assert vta._normalize_sentiment_label("1 star") == "negative"
    assert vta._normalize_sentiment_label("3 stars") == "neutral"
    assert vta._normalize_sentiment_label("5 stars") == "positive"

    # out-of-range label
    assert vta._normalize_sentiment_label("LABEL_9", num_labels=3) == "neutral"


def test_extract_sentiment_label_n_and_neutral_score(monkeypatch):
    vta._SENTIMENT_PIPELINE.clear()
    import types
    fake_transformers = types.ModuleType("transformers")
    recorded: dict[str, Any] = {}

    class FakePipeline:
        def __init__(self, task, model=None):
            recorded["task"] = task
            recorded["model"] = model
            self.model = types.SimpleNamespace(config=types.SimpleNamespace(num_labels=3))
            self.call_count = 0

        def __call__(self, text):
            self.call_count += 1
            if self.call_count == 1:
                return [{"label": "LABEL_2", "score": 0.8}]
            return [{"label": "LABEL_1", "score": 0.9}]

    fake_transformers.pipeline = FakePipeline  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    out = vta.run_text_analytics({"helper": "sentiment", "params": {"model": "org/custom"}}, ["good text", "middle text"])
    assert recorded["model"] == "org/custom"
    assert out["status"] == "ok"
    res = out["result"]
    assert res["per_section"][0]["label"] == "positive"
    assert res["per_section"][0]["score"] == 0.8
    assert res["per_section"][1]["label"] == "neutral"
    assert res["per_section"][1]["score"] == 0.0
    assert res["sentiment"]["score"] == 0.4
    assert res["sentiment"]["label"] == "positive"
    assert set(res["sentiment"]) == {"score", "label"}


def test_sentiment_pipeline_is_cached_and_single_string_has_no_section_index(monkeypatch):
    import types
    vta._SENTIMENT_PIPELINE.clear()
    fake_transformers = types.ModuleType("transformers")
    init_count = 0

    class FakePipeline:
        def __init__(self, task, model=None):
            nonlocal init_count
            init_count += 1
            self.model = types.SimpleNamespace(config=types.SimpleNamespace(num_labels=2))

        def __call__(self, text):
            return [{"label": "LABEL_1", "score": 0.9}]

    fake_transformers.pipeline = FakePipeline  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    # Two calls with the same model string construct the pipeline once
    vta._extract_sentiment(["hello world"], params={"model": "model-a"})
    assert init_count == 1
    vta._extract_sentiment(["hello again"], params={"model": "model-a"})
    assert init_count == 1

    # Third call with that model and a single string still has constructor count 1,
    # and overall keys are exactly score and label
    res3 = vta._extract_sentiment("single string text", params={"model": "model-a"})
    assert init_count == 1
    assert set(res3["overall"]) == {"score", "label"}

    # Fourth call with a different model string has constructor count 2
    vta._extract_sentiment(["new model test"], params={"model": "model-b"})
    assert init_count == 2


def test_sentence_count_and_key_phrases_without_parser():
    class FakeDocNoAnnot:
        def has_annotation(self, name):
            return False

    doc1 = FakeDocNoAnnot()
    assert vta._sentence_count(doc1) == 0
    assert vta._extract_key_phrases(doc1) == []

    class FakeDocNotImpl:
        def has_annotation(self, name):
            return name == "DEP"

        @property
        def noun_chunks(self):
            raise NotImplementedError("E894")

    doc2 = FakeDocNotImpl()
    assert vta._extract_key_phrases(doc2) == []

    class FakeDocSentStart:
        def has_annotation(self, name):
            return name == "SENT_START"

        @property
        def sents(self):
            return ["one sentence"]

    doc3 = FakeDocSentStart()
    assert vta._sentence_count(doc3) == 1


def test_topics_meta_is_topic_count_not_section_count():
    docs = [
        "quantum physics particles energy mechanics theory physics quantum measurement state",
        "astronomy galaxy cosmos stars telescope nebula galaxy cosmos stars universe",
        "biology genetics dna organism cells evolution biology genetics cells mutation",
        "chemistry molecules reaction catalyst bonding organic chemistry molecules reaction compound",
        "geology rocks minerals tectonic earth crust geology rocks minerals mantle",
        "meteorology climate weather forecast cyclone atmosphere meteorology climate weather rain",
    ]
    res = vta.run_text_analytics({"helper": "topics", "params": {"n_topics": 4}}, docs)
    result = res.get("result", {})
    if result.get("error") == "MISSING_PACKAGE":
        pytest.skip("scikit-learn not available")
    assert res["status"] == "ok"
    topics = result.get("topics", [])
    assignments = result.get("assignments", [])
    meta = result.get("meta", {})
    assert len(topics) == 4
    assert meta["n_topics"] == 4
    assert meta["n_sections"] == 6
    assert len(assignments) == 6
    assert [a["section_index"] for a in assignments] == list(range(6))


def test_topics_skip_short_sections_keep_original_indexes():
    docs = [
        "quantum physics particles energy mechanics theory physics quantum measurement state",
        "too short",
        "astronomy galaxy cosmos stars telescope nebula galaxy cosmos stars universe",
    ]
    res = vta.run_text_analytics({"helper": "topics", "params": {"n_topics": 4}}, docs)
    result = res.get("result", {})
    if result.get("error") == "MISSING_PACKAGE":
        pytest.skip("scikit-learn not available")
    assert res["status"] == "ok"
    topics = result.get("topics", [])
    assignments = result.get("assignments", [])
    meta = result.get("meta", {})
    assert meta["n_sections"] == 3
    assert meta["n_topics"] == len(topics) == 2
    assert [a["section_index"] for a in assignments] == [0, 2]


def test_topics_all_short_keeps_first_section_only():
    docs = ["alpha beta gamma delta", "epsilon zeta eta theta"]
    res = vta.run_text_analytics({"helper": "topics", "params": {"n_topics": 4}}, docs)
    result = res.get("result", {})
    if result.get("error") == "MISSING_PACKAGE":
        pytest.skip("scikit-learn not available")
    assert res["status"] == "ok"
    err = result.get("error", "")
    assert not str(err).startswith("topic_model_failed")
    assignments = result.get("assignments", [])
    assert len(assignments) == 1
    assert assignments[0]["section_index"] == 0


def test_topics_caps_components_by_features():
    docs = [("alpha " * 8).strip()] * 8
    res = vta.run_text_analytics({"helper": "topics", "params": {"n_topics": 8}}, docs)
    result = res.get("result", {})
    if result.get("error") == "MISSING_PACKAGE":
        pytest.skip("scikit-learn not available")
    assert res["status"] == "ok"
    err = result.get("error", "")
    assert not str(err).startswith("topic_model_failed")
    assert result.get("meta", {}).get("n_topics") == 2
    assignments = result.get("assignments", [])
    assert len(assignments) == 8
    assert [a["section_index"] for a in assignments] == list(range(8))


def test_topics_note_is_visible_when_terms_are_insufficient():
    res = vta.run_text_analytics("topics", ["hello"])
    result = res.get("result", {})
    if result.get("error") == "MISSING_PACKAGE":
        pytest.skip("scikit-learn not available")
    assert result.get("note") == "insufficient distinct terms"
    assert result.get("topics") == []


def test_topic_model_failure_is_visible(monkeypatch):
    try:
        import sklearn.decomposition  # noqa: F401
    except ImportError:
        pytest.skip("scikit-learn not available")

    class Boom:
        def __init__(self, *args, **kwargs):
            pass

        def fit_transform(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr("sklearn.decomposition.NMF", Boom)
    res = vta.run_text_analytics("topics", ["alpha beta gamma delta epsilon zeta eta theta " * 2])
    assert res["status"] == "ok"
    err = res.get("result", {}).get("error", "")
    assert "topic_model_failed" in err and "boom" in err


def test_topics_non_numeric_n_topics_is_topic_model_failed():
    try:
        import sklearn.decomposition  # noqa: F401
    except ImportError:
        pytest.skip("scikit-learn not available")

    text = "one two three four five six seven eight nine ten"
    res1 = vta._extract_topics(text, n_topics="auto")
    assert res1["topics"] == []
    assert res1.get("error", "").startswith("topic_model_failed:")

    res2 = vta._extract_topics(text, n_topics=["no"])
    assert res2["topics"] == []
    assert res2.get("error", "").startswith("topic_model_failed:")



def test_get_writer_sections_emits_heading_once(monkeypatch):
    monkeypatch.setattr("plugin.doc.doc_type.is_writer", lambda doc: True)
    monkeypatch.setattr(
        "plugin.doc.text_helpers.build_heading_tree",
        lambda doc: {"children": [{"text": "Introduction", "para_index": 0, "children": []}]},
    )

    class FakeEl:
        def __init__(self, text, is_para=True):
            self.text = text
            self._is_para = is_para

        def supportsService(self, name):
            return self._is_para and name == "com.sun.star.text.Paragraph"

    monkeypatch.setattr(
        "plugin.doc.text_helpers.get_string_without_tracked_deletions",
        lambda el: getattr(el, "text", ""),
    )

    class FakeEnum:
        def __init__(self, elements):
            self.elements = list(elements)
            self.idx = 0

        def hasMoreElements(self):
            return self.idx < len(self.elements)

        def nextElement(self):
            el = self.elements[self.idx]
            self.idx += 1
            return el

    class FakeDoc:
        def __init__(self, elements):
            self.elements = elements

        def getText(self):
            return self

        def createEnumeration(self):
            return FakeEnum(self.elements)

    doc1 = FakeDoc([
        FakeEl("Introduction"),
        FakeEl("The body has enough words to pass the length filter easily."),
    ])
    secs1 = ta._get_writer_sections(doc1)
    expected1 = "Introduction\nThe body has enough words to pass the length filter easily."
    assert secs1 == [expected1]
    assert "Introduction\nIntroduction" not in secs1[0]

    # Second case: non-paragraph (table) before heading, para_index 1
    monkeypatch.setattr(
        "plugin.doc.text_helpers.build_heading_tree",
        lambda doc: {"children": [{"text": "Introduction", "para_index": 1, "children": []}]},
    )
    doc2 = FakeDoc([
        FakeEl("Table content", is_para=False),
        FakeEl("Introduction"),
        FakeEl("This body is definitely long enough to remain."),
    ])
    secs2 = ta._get_writer_sections(doc2)
    expected2 = "Introduction\nThis body is definitely long enough to remain."
    assert secs2 == [expected2]


def test_get_writer_sections_fallback_calls_writer_text_once(monkeypatch):
    monkeypatch.setattr("plugin.doc.doc_type.is_writer", lambda doc: True)
    monkeypatch.setattr(
        "plugin.doc.text_helpers.build_heading_tree",
        lambda doc: {"children": [{"text": "Hi", "para_index": 0, "children": []}]},
    )

    class FakeEl:
        def __init__(self, text):
            self.text = text

        def supportsService(self, name):
            return name == "com.sun.star.text.Paragraph"

    monkeypatch.setattr(
        "plugin.doc.text_helpers.get_string_without_tracked_deletions",
        lambda el: getattr(el, "text", ""),
    )

    class FakeDoc:
        def getText(self):
            return self

        def createEnumeration(self):
            class Enum:
                def __init__(self):
                    self.items = [FakeEl("Hi"), FakeEl("x")]
                    self.i = 0

                def hasMoreElements(self):
                    return self.i < len(self.items)

                def nextElement(self):
                    it = self.items[self.i]
                    self.i += 1
                    return it

            return Enum()

    call_count = 0

    def fake_get_writer_text(doc):
        nonlocal call_count
        call_count += 1
        return "full document text that is long enough"

    monkeypatch.setattr("plugin.scripting.text_analytics._get_writer_text", fake_get_writer_text)

    res = ta._get_writer_sections(FakeDoc())
    assert call_count == 1
    assert res == ["full document text that is long enough"]


def test_format_topics_missing_package_reads_inner_payload(monkeypatch):
    monkeypatch.setattr(
        vta,
        "_extract_topics",
        lambda text, n_topics=4: {
            "topics": [],
            "error": "MISSING_PACKAGE",
            "install_hint": "scikit-learn (part of the Data Analysis / EDA stack)",
            "install": "uv pip install scikit-learn",
        },
    )
    res = vta.run_text_analytics("topics", "alpha " * 40)
    assert res["status"] == "error"
    out = format_text_analytics_result_for_display("topics", res)
    expected = (
        "Topics: MISSING_PACKAGE\n\n"
        "scikit-learn (part of the Data Analysis / EDA stack)\n\n"
        "Suggested install: uv pip install scikit-learn"
    )
    assert out == expected
    assert "scikit-learn from the analysis stack" not in out
    assert "{'" not in out


def test_format_topic_model_failed_has_no_install_hint():
    fake = {
        "status": "ok",
        "result": {
            "topics": [],
            "error": "topic_model_failed: boom",
        },
    }
    out = format_text_analytics_result_for_display("topics", fake)
    assert out == "Topics: topic_model_failed: boom"


def test_format_topics_insufficient_terms_has_no_install_hint():
    fake = {
        "status": "ok",
        "result": {
            "topics": [],
            "note": "insufficient distinct terms",
        },
    }
    out = format_text_analytics_result_for_display("topics", fake)
    assert out == "Topics: insufficient distinct terms"


def test_format_sentiment_missing_package_and_model_load():
    missing_dict = {
        "status": "error",
        "code": "TEXT_ANALYTICS_ERROR",
        "helper": "sentiment",
        "message": "MISSING_PACKAGE",
        "result": {
            "overall": {"score": 0.0, "label": "neutral"},
            "error": "MISSING_PACKAGE",
            "install_hint": "transformers (sentiment analysis)",
            "install": "uv pip install transformers torch",
            "detail": "ImportError: No module named 'transformers'",
        },
    }
    out_missing = format_text_analytics_result_for_display("sentiment", missing_dict)
    assert (
        "Sentiment: MISSING_PACKAGE (the transformers package or its dependencies could not be imported)"
        in out_missing
    )
    assert "transformers (sentiment analysis)" in out_missing
    assert "Suggested install: uv pip install transformers torch" in out_missing
    assert "Real error details from the worker process:\nImportError: No module named 'transformers'" in out_missing
    assert "{'" not in out_missing
    assert "Sentiment: neutral" not in out_missing

    load_dict = {
        "status": "error",
        "code": "TEXT_ANALYTICS_ERROR",
        "helper": "sentiment",
        "message": "MODEL_LOAD_FAILED",
        "result": {
            "overall": {"score": 0.0, "label": "neutral"},
            "error": "MODEL_LOAD_FAILED",
            "install_hint": "transformers model load failed",
            "install": "uv pip install torch",
            "detail": "OSError: model file corrupt",
        },
    }
    out_load = format_text_analytics_result_for_display("sentiment", load_dict)
    assert (
        "Sentiment: model load failed (package is present, but loading the model/tokenizer failed)"
        in out_load
    )
    assert "transformers model load failed" in out_load
    assert "Suggested install: uv pip install torch" in out_load
    assert "Real error details from the worker process:\nOSError: model file corrupt" in out_load
    assert "{'" not in out_load
    assert "Sentiment: neutral" not in out_load


def test_format_unknown_helper_is_one_line():
    err_dict = {
        "status": "error",
        "code": "UNKNOWN_HELPER",
        "helper": "nope",
        "message": "Unknown text analytics helper 'nope'",
    }
    out = format_text_analytics_result_for_display("nope", err_dict)
    assert out == "Error: Unknown text analytics helper 'nope'"


def test_format_diagnostics_inner_error_still_shows_install():
    diag = {
        "status": "ok",
        "result": {
            "status": "error",
            "message": "no python found",
            "python_used_by_worker": "/usr/bin/python3",
            "text_analytics_install": "uv pip install spacy",
        },
    }
    out = format_text_analytics_result_for_display("diagnostics", diag)
    assert "Diagnostics Error" in out
    assert "uv pip install spacy" in out


def test_format_diagnostics_reports_sklearn():
    data_installed = {
        "status": "ok",
        "result": {
            "has_sklearn": True,
            "sklearn_version": "1.2.3",
            "has_transformers": False,
            "has_torch": False,
        },
    }
    out_installed = format_text_analytics_result_for_display("diagnostics", data_installed)
    assert "scikit-learn: Installed" in out_installed
    assert "scikit-learn version: 1.2.3" in out_installed

    data_missing = {
        "status": "ok",
        "result": {
            "has_sklearn": False,
            "has_transformers": False,
            "has_torch": False,
        },
    }
    out_missing = format_text_analytics_result_for_display("diagnostics", data_missing)
    assert "scikit-learn: Missing" in out_missing
    assert "scikit-learn version" not in out_missing


def test_format_readability_bool_is_not_int():
    data = {
        "status": "ok",
        "result": {
            "readability": {"flag": True, "score": 1.5},
            "descriptive_stats": {"flag": False, "n_tokens": 3},
        },
    }
    out = format_text_analytics_result_for_display("readability", data)
    assert "flag: True" in out
    assert "score: 1.50" in out
    assert "flag: 1" not in out
    assert "flag: False" in out
    assert "n_tokens: 3" in out


def test_format_non_dict_is_no_result():
    assert format_text_analytics_result_for_display("readability", None) == "No result."
    assert format_text_analytics_result_for_display("readability", "x") == "No result."  # type: ignore[arg-type]


def test_selection_scope_does_not_fall_through_to_document(monkeypatch):
    monkeypatch.setattr("plugin.doc.doc_type.is_writer", lambda doc: True)

    class FakeDoc:
        def getCurrentController(self):
            class Controller:
                def getSelection(self):
                    class Sel:
                        def getCount(self):
                            return 0

                    return Sel()

            return Controller()

        def getText(self):
            raise AssertionError("getText should not be called for selection scope")

    def forbidden_sections(doc):
        raise AssertionError("_get_writer_sections should not be called for selection scope")

    monkeypatch.setattr("plugin.scripting.text_analytics._get_writer_sections", forbidden_sections)

    text, ctx = ta.resolve_text_analytics_document_inputs(FakeDoc(), "topics", scope="selection")
    assert text == ""


def test_selection_scope_topics_uses_selection_string(monkeypatch):
    monkeypatch.setattr("plugin.doc.doc_type.is_writer", lambda doc: True)
    monkeypatch.setattr(
        "plugin.doc.text_helpers.get_string_without_tracked_deletions",
        lambda rng: "A" * 30,
    )
    monkeypatch.setattr("plugin.scripting.text_analytics.get_doc_language", lambda doc: "de")

    class FakeDoc:
        def getCurrentController(self):
            class Controller:
                def getSelection(self):
                    class Sel:
                        def getCount(self):
                            return 1

                        def getByIndex(self, idx):
                            return "fake_range"

                    return Sel()

            return Controller()

    def forbidden_sections(doc):
        raise AssertionError("_get_writer_sections should not be called for selection scope")

    monkeypatch.setattr("plugin.scripting.text_analytics._get_writer_sections", forbidden_sections)

    text, ctx = ta.resolve_text_analytics_document_inputs(FakeDoc(), "topics", scope="selection")
    assert text == "A" * 30
    assert ctx == {"lang": "de"}
    assert "doc_lang" not in ctx


def test_whole_scope_topics_still_uses_sections(monkeypatch):
    monkeypatch.setattr("plugin.doc.doc_type.is_writer", lambda doc: True)

    class FakeDoc:
        def getCurrentController(self):
            raise AssertionError("getCurrentController should not be called for whole scope")

    call_count = 0

    def fake_sections(doc):
        nonlocal call_count
        call_count += 1
        return ["section 1", "section 2"]

    monkeypatch.setattr("plugin.scripting.text_analytics._get_writer_sections", fake_sections)

    text1, _ = ta.resolve_text_analytics_document_inputs(FakeDoc(), "topics")
    assert text1 == ["section 1", "section 2"]

    text2, _ = ta.resolve_text_analytics_document_inputs(FakeDoc(), "topics", scope="whole")
    assert text2 == ["section 1", "section 2"]

    text3, _ = ta.resolve_text_analytics_document_inputs(FakeDoc(), "topics", scope="other")
    assert text3 == ["section 1", "section 2"]

    assert call_count == 3


def test_selection_extraction_failure_is_empty_and_logged(caplog, monkeypatch):
    import logging

    monkeypatch.setattr("plugin.doc.doc_type.is_writer", lambda doc: True)

    class FakeDoc:
        def getCurrentController(self):
            class Controller:
                def getSelection(self):
                    raise RuntimeError("controller dead")

            return Controller()

    with caplog.at_level(logging.DEBUG):
        text, _ = ta.resolve_text_analytics_document_inputs(FakeDoc(), "readability", scope="selection")
    assert text == ""
    if not is_release_build():  # log.debug call site is stripped in release bundles
        assert "selection text failed" in caplog.text


def test_html_table_ignores_overall_key():
    assert ta._result_to_html_table({"overall": {"score": 0.4, "label": "positive"}}) == ""
    assert "positive" in ta._result_to_html_table({"sentiment": {"score": 0.4, "label": "positive"}})


def test_insert_empty_ok_result_does_not_dump_envelope(monkeypatch):
    monkeypatch.setattr("plugin.doc.doc_type.is_writer", lambda doc: True)
    captured_html = []

    def fake_insert(doc, ctx, html, pos):
        captured_html.append(html)

    monkeypatch.setattr("plugin.writer.format.insert_content_at_position", fake_insert)

    class FakeDoc:
        def getCurrentController(self):
            raise RuntimeError("no controller")

    doc = FakeDoc()
    ret = ta.insert_text_analytics_result_into_doc(None, doc, {"status": "ok", "result": {}, "note": "empty text"})
    assert ret == 1
    assert len(captured_html) == 1
    html = captured_html[0]
    assert "{}" in html
    assert "status" not in html
    assert "empty text" not in html


def test_writer_text_reraises_disposal_and_skips_getstring(monkeypatch):
    from plugin.framework.errors import DocumentDisposedError

    class DisposedException(Exception):
        pass

    monkeypatch.setattr("plugin.doc.doc_type.is_writer", lambda doc: True)

    class DisposedDoc:
        def getText(self):
            raise DisposedException("closed")

    with pytest.raises(DocumentDisposedError):
        ta._get_writer_text(DisposedDoc())

    get_text_calls = 0

    class RuntimeDoc:
        def getText(self):
            nonlocal get_text_calls
            get_text_calls += 1
            raise RuntimeError("other error")

    assert ta._get_writer_text(RuntimeDoc()) == ""
    assert get_text_calls == 1

    class DisposedSelectionDoc:
        def getCurrentController(self):
            class Controller:
                def getSelection(self):
                    raise DisposedException("closed")

            return Controller()

    with pytest.raises(DocumentDisposedError):
        ta._get_writer_selection_text(DisposedSelectionDoc())

    class RuntimeSelectionDoc:
        def getCurrentController(self):
            class Controller:
                def getSelection(self):
                    raise RuntimeError("other error")

            return Controller()

    assert ta._get_writer_selection_text(RuntimeSelectionDoc()) == ""

    class DummyDoc:
        pass

    def raise_disposed(d):
        raise DisposedException("closed")

    monkeypatch.setattr("plugin.doc.text_helpers.build_heading_tree", raise_disposed)
    with pytest.raises(DocumentDisposedError):
        ta._get_writer_sections(DummyDoc())

    def raise_runtime(d):
        raise RuntimeError("heading tree error")

    monkeypatch.setattr("plugin.doc.text_helpers.build_heading_tree", raise_runtime)
    monkeypatch.setattr("plugin.scripting.text_analytics._get_writer_text", lambda d: "fallback text long enough")
    assert ta._get_writer_sections(DummyDoc()) == ["fallback text long enough"]


