import uno
from plugin.testing_runner import native_test
from plugin.modules.writer.grammar_proofread_engine import (
    cache_get_sentence,
    cache_put_sentence,
    clear_sentence_cache,
    make_sentence_key,
    split_into_sentences,
    normalize_errors_for_text,
)

@native_test
def test_split_basic_two_sentences(uno_context=None):
    if uno_context is None: return
    result = split_into_sentences(uno_context, "en-US", "Hello world. This is fine.")
    assert len(result) == 2, result
    assert result[0] == (0, "Hello world.")
    assert result[1][1] == "This is fine."

@native_test
def test_split_single_sentence(uno_context=None):
    if uno_context is None: return
    result = split_into_sentences(uno_context, "en-US", "Just one.")
    assert len(result) == 1
    assert result[0] == (0, "Just one.")

@native_test
def test_split_three_sentences(uno_context=None):
    if uno_context is None: return
    text = "First. Second. Third."
    result = split_into_sentences(uno_context, "en-US", text)
    assert len(result) == 3
    assert result[0][1] == "First."
    assert result[1][1] == "Second."
    assert result[2][1] == "Third."
    # Verify offsets are correct
    for offset, sent in result:
        assert text[offset : offset + len(sent)] == sent

@native_test
def test_split_multilingual_terminators(uno_context=None):
    if uno_context is None: return
    result = split_into_sentences(uno_context, "ja-JP", "これは文です。 次の文。")
    assert len(result) == 2

@native_test
def test_split_question_and_exclamation(uno_context=None):
    if uno_context is None: return
    result = split_into_sentences(uno_context, "en-US", "Really? Yes! Okay.")
    assert len(result) == 3

@native_test
def test_split_empty_and_whitespace(uno_context=None):
    if uno_context is None: return
    assert split_into_sentences(uno_context, "en-US", "") == []
    assert split_into_sentences(uno_context, "en-US", "   ") == []

@native_test
def test_split_no_terminator(uno_context=None):
    if uno_context is None: return
    """Text without sentence-ending punctuation stays as one segment."""
    result = split_into_sentences(uno_context, "en-US", "hello world without punctuation")
    assert len(result) == 1
    assert result[0] == (0, "hello world without punctuation")

@native_test
def test_split_preserves_offsets(uno_context=None):
    if uno_context is None: return
    """Offsets should correctly index back into the original text."""
    text = "Alpha bravo. Charlie delta. Echo foxtrot."
    result = split_into_sentences(uno_context, "en-US", text)
    for offset, sent in result:
        assert text[offset : offset + len(sent)] == sent

# --- Hybrid / Edge Cases ---

@native_test
def test_split_thai_spaces(uno_context=None):
    if uno_context is None: return
    # Thai heuristic splits on spaces
    text = "สวัสดีครับ ผมชื่อสมชาย ยินดีที่ได้รู้จัก"
    result = split_into_sentences(uno_context, "th-TH", text)
    assert len(result) == 3
    assert result[0][1] == "สวัสดีครับ"
    assert result[1][1] == "ผมชื่อสมชาย"
    assert result[2][1] == "ยินดีที่ได้รู้จัก"

@native_test
def test_split_quotes(uno_context=None):
    if uno_context is None: return
    # LO BreakIterator handles quotes properly where simple regex fails
    text = "He said 'Hello.' Then he left."
    result = split_into_sentences(uno_context, "en-US", text)
    assert len(result) == 2
    assert result[0][1] == "He said 'Hello.'"
    assert result[1][1] == "Then he left."

@native_test
def test_split_abbreviation_heuristic(uno_context=None):
    if uno_context is None: return
    text = "Mr. Smith went to Washington. Dr. Jones is happy."
    result = split_into_sentences(uno_context, "en-US", text)
    assert len(result) == 2, result
    assert result[0][1] == "Mr. Smith went to Washington."
    assert result[1][1] == "Dr. Jones is happy."

# --- Trailing whitespace normalization tests ---

@native_test
def test_whitespace_normalization_cache_key(uno_context=None):
    if uno_context is None: return
    """'Hello.' and 'Hello. ' and 'Hello.\\n' should produce the same cache key."""
    key1 = make_sentence_key("en-US", "Hello.")
    key2 = make_sentence_key("en-US", "Hello. ")
    key3 = make_sentence_key("en-US", "Hello.\n")
    assert key1 == key2 == key3

@native_test
def test_cache_hit_with_trailing_whitespace(uno_context=None):
    if uno_context is None: return
    """Putting 'Hello.' and getting 'Hello. ' should be a cache hit."""
    clear_sentence_cache()
    cache_put_sentence("en-US", "Hello.", [{"n_error_start": 0, "n_error_length": 5}])
    result = cache_get_sentence("en-US", "Hello. ")
    assert result is not None
    assert len(result) == 1
    assert result[0]["n_error_start"] == 0

@native_test
def test_cache_roundtrip_per_sentence(uno_context=None):
    if uno_context is None: return
    """Simulate the per-sentence cache flow: store per sentence, retrieve per sentence."""
    clear_sentence_cache()
    cache_put_sentence("en-US", "This has a eror.", [
        {"n_error_start": 11, "n_error_length": 4, "suggestions": ("error",),
         "short_comment": "(spelling) typo", "full_comment": "typo",
         "rule_identifier": "wa_grammar_0_abc"},
    ])
    cache_put_sentence("en-US", "Second sent.", [])

    sentences = split_into_sentences(uno_context, "en-US", "This has a eror. Second sent. Third.")
    assert len(sentences) == 3

    assert cache_get_sentence("en-US", "Third.") is None


@native_test
def test_overlap_thai_native(uno_context=None):
    if uno_context is None: return
    # test that the BreakIterator successfully splits Thai characters for overlap detection
    full = "ผมไปที่ร้านค้า"  # "I went to the store"
    # LLM flagged "ไป" (went) but correct was "ไปที่" (went to) which overlaps with "ที่ร้านค้า" (to the store)
    items = [{"wrong": "ไป", "correct": "ไปที่", "type": "grammar"}]
    
    # First, test without context (regex fallback). It should FAIL to expand the range.
    norms_fallback = normalize_errors_for_text(full, 0, len(full), items, ctx=None, loc_key=None)
    assert len(norms_fallback) == 1
    err1 = norms_fallback[0]
    expanded_wrong_fallback = full[err1.n_error_start : err1.n_error_start + err1.n_error_length]
    # Without BreakIterator, it doesn't know where words start/end, so it stays as just "ไป"
    assert expanded_wrong_fallback == "ไป"

    # Now, test with context (BreakIterator). It should correctly expand the range!
    norms_native = normalize_errors_for_text(full, 0, len(full), items, ctx=uno_context, loc_key="th-TH")
    assert len(norms_native) == 1
    err2 = norms_native[0]
    expanded_wrong_native = full[err2.n_error_start : err2.n_error_start + err2.n_error_length]
    assert expanded_wrong_native == "ไปที่", f"Expected 'ไปที่' but got '{expanded_wrong_native}'"
