# WriterAgent - stop word coverage for Snowball languages

import snowballstemmer

from plugin.framework.grammar_rule_ids import ISO_TO_SNOWBALL
from plugin.writer.locale.stop_words import STOP_WORDS, stop_words_for_snowball


def test_stop_words_cover_iso_to_snowball_languages():
    snowball_langs = set(ISO_TO_SNOWBALL.values())
    assert snowball_langs <= set(STOP_WORDS.keys())


def test_stop_words_cover_snowballstemmer_algorithms_used_by_writer():
    # porter / dutch_porter are English variants; writer maps ISO langs only.
    writer_langs = set(ISO_TO_SNOWBALL.values())
    for algo in snowballstemmer.algorithms():
        if algo in ("porter", "dutch_porter"):
            continue
        if algo in writer_langs:
            assert algo in STOP_WORDS
            assert stop_words_for_snowball(algo)


def test_unknown_snowball_lang_returns_empty_fallback():
    assert stop_words_for_snowball("klingon") == frozenset()
