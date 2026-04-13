import pytest
import sys
import types
from unittest.mock import MagicMock, patch

# Mock UNO and related modules before importing the plugin modules
mock_uno = MagicMock()
sys.modules["uno"] = mock_uno
sys.modules["unohelper"] = mock_uno
sys.modules["snowballstemmer"] = MagicMock()

# Create mock for com.sun.star and its submodules
com_mock = types.ModuleType("com")
sys.modules["com"] = com_mock
css = types.ModuleType("com.sun.star")
setattr(com_mock, "sun", types.ModuleType("sun"))
setattr(com_mock.sun, "star", css)
sys.modules["com.sun.star"] = css

# Add submodules as needed
for sub in ["text", "beans", "container", "lang"]:
    mod = types.ModuleType(sub)
    setattr(css, sub, mod)
    sys.modules[f"com.sun.star.{sub}"] = mod

from plugin.modules.writer.index import _DocIndex, IndexService

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
    services.writer_tree = MagicMock()
    services.writer_bookmarks = MagicMock()
    services.events = MagicMock()
    return services

def test_index_service_search_boolean_near_integration(mock_services):
    # Mock stemmer to return tokens as is for simplicity
    mock_stemmer = MagicMock()
    mock_stemmer.stemWord.side_effect = lambda x: x

    with patch('plugin.modules.writer.index.IndexService._get_stemmer', return_value=mock_stemmer):

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

    with patch('plugin.modules.writer.index.IndexService._get_stemmer', return_value=mock_stemmer):
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

        def make_enum_far():
            e = MagicMock()
            e.hasMoreElements.side_effect = [True] * 5 + [False]
            e.nextElement.side_effect = paras
            return e
        doc.getText().createEnumeration.side_effect = make_enum_far

        # apple(0) NEAR/2 pie(4) -> 4-0 = 4 > 2. Should find nothing.
        res = svc.search_boolean(doc, "apple NEAR/2 pie")
        assert res["total_found"] == 0
