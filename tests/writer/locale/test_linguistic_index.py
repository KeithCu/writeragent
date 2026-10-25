import pytest
import sys
import types
from unittest.mock import MagicMock, patch

# Mock UNO and related modules before importing the plugin modules
if "uno" not in sys.modules:
    sys.modules["uno"] = MagicMock()
if "unohelper" not in sys.modules:
    sys.modules["unohelper"] = MagicMock()
sys.modules.setdefault("snowballstemmer", MagicMock())

# Create mock for com.sun.star and its submodules if not already present
com_mock = sys.modules.get("com") or types.ModuleType("com")
sys.modules["com"] = com_mock
css = sys.modules.get("com.sun.star") or types.ModuleType("com.sun.star")
if not hasattr(com_mock, "sun"):
    setattr(com_mock, "sun", types.ModuleType("sun"))
setattr(com_mock.sun, "star", css)
sys.modules["com.sun.star"] = css

# Add submodules as needed
for sub in ["text", "beans", "container", "lang"]:
    full_name = f"com.sun.star.{sub}"
    mod = sys.modules.get(full_name) or types.ModuleType(sub)
    setattr(css, sub, mod)
    sys.modules[full_name] = mod

from plugin.framework.errors import ToolExecutionError
from plugin.writer.locale.linguistic_index import (
    _DocIndex,
    IndexService,
    _normalized_stop_words,
)

def test_doc_index_query_near_basic():
    idx = _DocIndex()
    # Para 1 has 'apple', Para 3 has 'pie'
    idx.terms = {
        'appl': {1, 10},
        'pie': {3, 15}
    }

    # Distance 2: 3-1 = 2 <= 2. Should find them.
    res = idx.query_near(['appl'], ['pie'], 2)
    assert res == {1, 3}

    # Distance 1: 3-1 = 2 > 1. Should not find them.
    res = idx.query_near(['appl'], ['pie'], 1)
    assert res == set()

def test_doc_index_query_near_multiple_stems():
    idx = _DocIndex()
    idx.terms = {
        'appl': {1},
        'orang': {5},
        'pie': {3, 7}
    }

    # (appl OR orang) NEAR/2 pie
    # appl(1) NEAR/2 pie(3) -> {1, 3}
    # orang(5) NEAR/2 pie(3) -> {3, 5}
    # orang(5) NEAR/2 pie(7) -> {5, 7}
    res = idx.query_near(['appl', 'orang'], ['pie'], 2)
    assert res == {1, 3, 5, 7}

def test_doc_index_query_near_boundary():
    idx = _DocIndex()
    idx.terms = {
        'a': {10},
        'b': {20}
    }
    # Distance 10: 20-10 = 10 <= 10. Success.
    assert idx.query_near(['a'], ['b'], 10) == {10, 20}
    # Distance 9: 20-10 = 10 > 9. Failure.
    assert idx.query_near(['a'], ['b'], 9) == set()

def test_doc_index_query_near_empty():
    idx = _DocIndex()
    idx.terms = {'a': {1}}
    # b is missing
    assert idx.query_near(['a'], ['b'], 10) == set()
    # a is missing (passed as c)
    assert idx.query_near(['c'], ['a'], 10) == set()

@pytest.fixture
def mock_services():
    services = MagicMock()
    services.document = MagicMock()
    services.document.doc_key.return_value = "doc-a"
    services.writer_tree = MagicMock()
    services.writer_bookmarks = MagicMock()
    services.events = MagicMock()
    return services


def _search_doc(items, *, key="doc-a", char_count=1, doc_locale=None):
    doc = MagicMock()
    doc.CharacterCount = char_count
    doc.getPropertyValue.return_value = types.SimpleNamespace(Language=doc_locale or "")
    doc._key = key

    element_mocks = []
    for item in items:
        p = MagicMock()
        if item is None:
            p.supportsService.return_value = False
        else:
            text, lang = item
            p.supportsService.return_value = True
            p.getString.return_value = text
            p.getPropertyValue.return_value = types.SimpleNamespace(Language=lang)
            p.createEnumeration.side_effect = RuntimeError("no enum")
        element_mocks.append(p)

    def make_enum():
        e = MagicMock()
        e.hasMoreElements.side_effect = [True] * len(element_mocks) + [False]
        e.nextElement.side_effect = element_mocks
        return e

    doc.getText.return_value.createEnumeration.side_effect = make_enum
    return doc

def test_index_service_search_boolean_near_integration(mock_services):
    # Mock stemmer to return tokens as is for simplicity
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch('plugin.writer.locale.linguistic_index.IndexService._get_stemmer', return_value=mock_stemmer):

        svc = IndexService(mock_services)

        # Mock document
        doc = MagicMock()
        mock_services.document.doc_key.return_value = "test_doc"

        # Mock paragraphs
        para0 = MagicMock()
        para0.supportsService.return_value = True
        para0.getString.return_value = "The apple is here."
        para0.getPropertyValue.return_value = MagicMock(Language="en")

        para1 = MagicMock()
        para1.supportsService.return_value = True
        para1.getString.return_value = "Some filler text."

        para2 = MagicMock()
        para2.supportsService.return_value = True
        para2.getString.return_value = "The pie is delicious."

        for p in (para0, para1, para2):
            p.createEnumeration.side_effect = RuntimeError("no enum")

        def make_enum():
            e = MagicMock()
            e.hasMoreElements.side_effect = [True, True, True, False]
            e.nextElement.side_effect = [para0, para1, para2]
            return e
        doc.getText().createEnumeration.side_effect = make_enum

        # First call builds index
        # apple is in para 0, pie is in para 2. Distance is 2.
        res = svc.search_boolean(doc, "apple NEAR/2 pie")

        assert res["total_found"] == 2
        assert res["mode"] == "near"
        # apple(0) and pie(2) are within distance 2.
        indices = [m["paragraph_index"] for m in res["matches"]]
        assert 0 in indices
        assert 2 in indices

def test_index_service_search_boolean_near_too_far(mock_services):
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch('plugin.writer.locale.linguistic_index.IndexService._get_stemmer', return_value=mock_stemmer):
        svc = IndexService(mock_services)
        doc = MagicMock()
        mock_services.document.doc_key.return_value = "test_doc_far"

        para0 = MagicMock()
        para0.supportsService.return_value = True
        para0.getString.return_value = "apple"
        para0.getPropertyValue.return_value = MagicMock(Language="en")

        # 3 paragraphs in between
        paras = [para0]
        for i in range(3):
            p = MagicMock()
            p.supportsService.return_value = True
            p.getString.return_value = "filler"
            paras.append(p)

        para4 = MagicMock()
        para4.supportsService.return_value = True
        para4.getString.return_value = "pie"
        paras.append(para4)

        for p in paras:
            p.createEnumeration.side_effect = RuntimeError("no enum")

        def make_enum_far():
            e = MagicMock()
            e.hasMoreElements.side_effect = [True] * 5 + [False]
            e.nextElement.side_effect = paras
            return e
        doc.getText().createEnumeration.side_effect = make_enum_far

        # apple(0) NEAR/2 pie(4) -> 4-0 = 4 > 2. Should find nothing.
        res = svc.search_boolean(doc, "apple NEAR/2 pie")
        assert res["total_found"] == 0


def test_index_cache_hit_reports_cached(mock_services):
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch("plugin.writer.locale.linguistic_index.IndexService._get_stemmer", return_value=mock_stemmer):
        svc = IndexService(mock_services)
        doc = _search_doc([("an apple is sweet", "en")])
        res1 = svc.search_boolean(doc, "apple")
        assert res1["index"]["cached"] is False
        res2 = svc.search_boolean(doc, "apple")
        assert res2["index"]["cached"] is True


def test_index_cache_rebuilds_when_character_count_moves(mock_services):
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch("plugin.writer.locale.linguistic_index.IndexService._get_stemmer", return_value=mock_stemmer):
        svc = IndexService(mock_services)
        doc = _search_doc([("an apple is sweet", "en")], char_count=1)
        res1 = svc.search_boolean(doc, "apple")
        assert res1["index"]["cached"] is False
        doc.CharacterCount = 2
        res2 = svc.search_boolean(doc, "apple")
        assert res2["index"]["cached"] is False


def test_index_cache_evicts_oldest_document(mock_services):
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch("plugin.writer.locale.linguistic_index.IndexService._get_stemmer", return_value=mock_stemmer):
        svc = IndexService(mock_services)
        for k in ["a", "b", "c", "d", "e"]:
            mock_services.document.doc_key.return_value = k
            doc = _search_doc([("apple", "en")])
            svc.search_boolean(doc, "apple")
        assert len(svc._cache) == 4
        assert "a" not in svc._cache


def test_index_cache_never_stores_unknown_key(mock_services):
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch("plugin.writer.locale.linguistic_index.IndexService._get_stemmer", return_value=mock_stemmer):
        svc = IndexService(mock_services)
        mock_services.document.doc_key.return_value = "unknown"
        doc = _search_doc([("apple", "en")])
        svc.search_boolean(doc, "apple")
        assert svc._cache == {}


def test_language_skips_leading_table(mock_services):
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch("plugin.writer.locale.linguistic_index.IndexService._get_stemmer", return_value=mock_stemmer):
        svc = IndexService(mock_services)
        doc = _search_doc([None, ("Hallo Welt", "de")])
        res = svc.search_boolean(doc, "Hallo")
        assert res["detected_language"] == "de"
        assert res["language"] == "german"
        assert res["stem_fallback"] is False
        entry = next(iter(svc._cache.values()))[1]
        assert entry.para_texts[0] == "[Table]"


def test_language_skips_blank_first_paragraph(mock_services):
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch("plugin.writer.locale.linguistic_index.IndexService._get_stemmer", return_value=mock_stemmer):
        svc = IndexService(mock_services)
        doc = _search_doc([("", ""), ("Hallo Welt", "de")])
        res = svc.search_boolean(doc, "Hallo")
        assert res["detected_language"] == "de"


def test_document_locale_wins(mock_services):
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch("plugin.writer.locale.linguistic_index.IndexService._get_stemmer", return_value=mock_stemmer):
        svc = IndexService(mock_services)
        doc = _search_doc([("Hello", "en")], doc_locale="fr")
        res = svc.search_boolean(doc, "Hello")
        assert res["detected_language"] == "fr"
        assert res["language"] == "french"


def test_unsupported_locale_is_reported_as_fallback(mock_services):
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch("plugin.writer.locale.linguistic_index.IndexService._get_stemmer", return_value=mock_stemmer):
        svc = IndexService(mock_services)
        doc = _search_doc([("Konnichiwa", "ja")], doc_locale="ja")
        res = svc.search_boolean(doc, "Konnichiwa")
        assert res["detected_language"] == "ja"
        assert res["language"] == "english"
        assert res["stem_fallback"] is True
        stats = svc.get_index_stats(doc)
        assert stats["detected_language"] == "ja"
        assert stats["stem_fallback"] is True


def test_zxx_locale_is_ignored(mock_services):
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch("plugin.writer.locale.linguistic_index.IndexService._get_stemmer", return_value=mock_stemmer):
        svc = IndexService(mock_services)
        doc = _search_doc([("Hallo", "de")], doc_locale="zxx")
        res = svc.search_boolean(doc, "Hallo")
        assert res["detected_language"] == "de"


def test_accented_stop_word_is_dropped(mock_services):
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch("plugin.writer.locale.linguistic_index.IndexService._get_stemmer", return_value=mock_stemmer):
        svc = IndexService(mock_services)
        doc = _search_doc([("Señaló la puerta", "es")], doc_locale="es")
        with pytest.raises(ToolExecutionError) as excinfo:
            svc.search_boolean(doc, "señaló")
        assert "senalo" in str(excinfo.value)


def test_content_word_still_searchable(mock_services):
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch("plugin.writer.locale.linguistic_index.IndexService._get_stemmer", return_value=mock_stemmer):
        svc = IndexService(mock_services)
        doc = _search_doc([("the photosynthesis report", "en")])
        res = svc.search_boolean(doc, "photosynthesis")
        assert res["total_found"] == 1


def test_stop_words_are_normalized_once():
    assert _normalized_stop_words("spanish") is _normalized_stop_words("spanish")
    assert "senalo" in _normalized_stop_words("spanish")


def test_parse_query_rejects_second_near(mock_services):
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch("plugin.writer.locale.linguistic_index.IndexService._get_stemmer", return_value=mock_stemmer):
        svc = IndexService(mock_services)
        stop_words = _normalized_stop_words("english")
        parsed = svc._parse_query("a NEAR/2 b NEAR/3 c", mock_stemmer, stop_words)
        assert parsed["error"] == "Only one NEAR/N is supported per query."


def test_parse_query_accepts_one_near(mock_services):
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch("plugin.writer.locale.linguistic_index.IndexService._get_stemmer", return_value=mock_stemmer):
        svc = IndexService(mock_services)
        stop_words = _normalized_stop_words("english")
        parsed = svc._parse_query("apple NEAR/2 pie", mock_stemmer, stop_words)
        assert parsed["error"] is None
        assert len(parsed["near"]) == 1


def test_all_stop_word_query_lists_dropped_words(mock_services):
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch("plugin.writer.locale.linguistic_index.IndexService._get_stemmer", return_value=mock_stemmer):
        svc = IndexService(mock_services)
        doc = _search_doc([("the apple and of pie", "en")])
        with pytest.raises(ToolExecutionError) as excinfo:
            svc.search_boolean(doc, "the and of")
        err_msg = str(excinfo.value)
        assert "the" in err_msg
        assert "of" in err_msg


def test_search_boolean_validates_params(mock_services):
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch("plugin.writer.locale.linguistic_index.IndexService._get_stemmer", return_value=mock_stemmer):
        svc = IndexService(mock_services)
        doc = _search_doc([("apple pie", "en")])
        for invalid in (0, -1, "5", True):
            with pytest.raises(ValueError, match="max_results"):
                svc.search_boolean(doc, "apple", max_results=invalid)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="context_paragraphs"):
            svc.search_boolean(doc, "apple", context_paragraphs=-1)
        res = svc.search_boolean(doc, "apple", max_results=1, context_paragraphs=0)
        assert res["total_found"] == 1
