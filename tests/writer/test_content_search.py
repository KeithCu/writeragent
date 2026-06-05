# Unit tests for apply_document_content search helpers (no LibreOffice required).
from plugin.writer.content import (
    _all_start_indices,
    _normalize_search_string_for_find,
    _SPACE_NORMALIZE_MAP,
)


def test_all_start_indices_non_overlapping():
    assert _all_start_indices("abababa", "aba") == [0, 4]
    assert _all_start_indices("", "x") == []
    assert _all_start_indices("abc", "") == []


def test_normalize_search_string_collapses_nbsp():
    nbsp = "\u00a0"
    em_space = "\u2003"
    cjk_space = "\u3000"
    assert _normalize_search_string_for_find("foo" + nbsp + nbsp + "bar") == "foo bar"
    assert _normalize_search_string_for_find("foo" + em_space + cjk_space + "bar") == "foo bar"
    assert _normalize_search_string_for_find("line1\nline2") == "line1\nline2"


def test_space_normalize_map_is_one_to_one_ascii():
    for cp, replacement in _SPACE_NORMALIZE_MAP.items():
        assert len(chr(cp)) == 1
        assert replacement == " "

