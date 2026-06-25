# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the pure-python word-level diff splitter.

No UNO / LibreOffice. Pure algorithm + reconstruction property tests.
"""

import pytest

from plugin.writer.word_diff_split import (
    SubEdit,
    apply_sub_edits,
    split_change,
    tokenize,
)


# ---------------------------------------------------------------------------
# tokenize: lossless, exact offsets
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "s",
    [
        "",
        "hello",
        "hello world",
        "  leading",
        "trailing  ",
        "  both sides  ",
        "multiple   spaces\tand\ttabs",
        "punctuation, yes! really? sure.",
        "line\nbreak\nhere",
        "café déjà naïve",
    ],
)
def test_tokenize_is_lossless(s):
    toks = tokenize(s)
    assert "".join(t.text for t in toks) == s
    # offsets index back exactly
    for t in toks:
        assert s[t.start:t.end] == t.text
    # alternating word / separator, contiguous
    for a, b in zip(toks, toks[1:]):
        assert a.end == b.start
        assert a.is_word != b.is_word


def test_tokenize_empty():
    assert tokenize("") == []


def test_tokenize_word_vs_separator_classification():
    toks = tokenize(" a  b ")
    kinds = [(t.text, t.is_word) for t in toks]
    assert kinds == [
        (" ", False),
        ("a", True),
        ("  ", False),
        ("b", True),
        (" ", False),
    ]


# ---------------------------------------------------------------------------
# split_change: mode + sub-edit shape on the required edge cases
# ---------------------------------------------------------------------------

def test_identical_strings_no_sub_edits():
    r = split_change("the quick brown fox", "the quick brown fox")
    assert r.mode == "surgical"
    assert r.sub_edits == []
    assert r.fraction_changed == 0.0


def test_both_empty_no_sub_edits():
    r = split_change("", "")
    assert r.mode == "surgical"
    assert r.sub_edits == []
    assert r.fraction_changed == 0.0


def test_single_word_changed_in_long_sentence_one_surgical_edit():
    old = "the quick brown fox jumps over the lazy dog today"
    new = "the quick brown cat jumps over the lazy dog today"
    r = split_change(old, new)
    assert r.mode == "surgical"
    assert len(r.sub_edits) == 1
    e = r.sub_edits[0]
    assert e.op == "replace"
    assert e.old_text == "fox"
    assert e.new_text == "cat"
    assert old[e.old_start:e.old_end] == "fox"


def test_two_non_adjacent_words_changed_two_surgical_edits():
    old = "the quick brown fox jumps over the lazy dog today"
    new = "the slow brown fox leaps over the lazy dog today"
    r = split_change(old, new)
    assert r.mode == "surgical"
    assert len(r.sub_edits) == 2
    assert {e.old_text for e in r.sub_edits} == {"quick", "jumps"}
    assert {e.new_text for e in r.sub_edits} == {"slow", "leaps"}


def test_two_adjacent_words_changed_agglutinated_into_one():
    old = "the quick brown fox jumps over the lazy dog today"
    new = "the speedy tan fox jumps over the lazy dog today"
    r = split_change(old, new)
    assert r.mode == "surgical"
    assert len(r.sub_edits) == 1
    e = r.sub_edits[0]
    assert e.op == "replace"
    assert e.old_text == "quick brown"
    assert e.new_text == "speedy tan"


def test_three_adjacent_words_changed_agglutinated_into_one():
    old = "k1 alpha beta gamma k2 k3 k4 k5"
    new = "k1 X Y Z k2 k3 k4 k5"
    r = split_change(old, new, threshold=0.9)
    assert r.mode == "surgical"
    assert len(r.sub_edits) == 1, "consecutive changed words must merge into ONE sub-edit"
    e = r.sub_edits[0]
    assert e.op == "replace"
    assert e.old_text == "alpha beta gamma"
    assert e.new_text == "X Y Z"
    assert apply_sub_edits(old, r.sub_edits) == new


def test_adjacent_replace_and_insert_merge_into_one():
    # A replace immediately followed by an insertion (no matched word between them) lives
    # in a single segment and must agglutinate into one sub-edit.
    old = "k1 alpha k2 k3 k4 k5"
    new = "k1 X NEWWORD k2 k3 k4 k5"
    r = split_change(old, new, threshold=0.9)
    assert r.mode == "surgical"
    assert len(r.sub_edits) == 1
    assert r.sub_edits[0].op == "replace"
    assert apply_sub_edits(old, r.sub_edits) == new


def test_pure_insertion_old_subset_of_new():
    old = "the quick fox jumps over the lazy dog today here"
    new = "the quick brown fox jumps over the lazy dog today here"
    r = split_change(old, new)
    assert r.mode == "surgical"
    assert len(r.sub_edits) == 1
    e = r.sub_edits[0]
    assert e.op == "insert"
    assert "brown" in e.new_text
    assert apply_sub_edits(old, r.sub_edits) == new


def test_pure_deletion():
    old = "the quick brown fox jumps over the lazy dog today here"
    new = "the quick fox jumps over the lazy dog today here"
    r = split_change(old, new)
    assert r.mode == "surgical"
    assert len(r.sub_edits) == 1
    e = r.sub_edits[0]
    assert e.op == "delete"
    assert "brown" in e.old_text
    assert e.new_text == ""
    assert apply_sub_edits(old, r.sub_edits) == new


def test_empty_old_is_pure_insertion():
    r = split_change("", "hello world")
    # 100% changed -> block by the > threshold rule
    assert r.mode == "block"
    # block op must be classified like surgical ops: empty old -> insert (not "replace").
    assert len(r.sub_edits) == 1
    e = r.sub_edits[0]
    assert e.op == "insert"
    assert e.old_text == ""
    assert e.old_start == e.old_end == 0
    assert e.new_text == "hello world"
    assert apply_sub_edits("", r.sub_edits) == "hello world"


def test_empty_new_is_pure_deletion():
    r = split_change("hello world", "")
    assert r.mode == "block"
    assert len(r.sub_edits) == 1
    e = r.sub_edits[0]
    assert e.op == "delete"
    assert e.new_text == ""
    assert e.old_start == 0
    assert e.old_end == len("hello world")
    assert e.old_text == "hello world"
    assert apply_sub_edits("hello world", r.sub_edits) == ""


# ---------------------------------------------------------------------------
# threshold / boundary semantics: <= threshold stays surgical, > flips to block
# ---------------------------------------------------------------------------

def test_just_below_threshold_is_surgical():
    # 6 unchanged + replace(2 old, 2 new): changed = 2+2 = 4, total = 6+4 = 10
    # -> fraction = 0.4 (< 0.6). 'replace' counts changed words on BOTH sides.
    old = "k1 k2 k3 r1 r2 k4 k5 k6"
    new = "k1 k2 k3 s1 s2 k4 k5 k6"
    r = split_change(old, new, threshold=0.6)
    assert pytest.approx(r.fraction_changed) == 0.4
    assert r.mode == "surgical"


def test_exactly_at_threshold_stays_surgical():
    # Construct fraction == exactly 0.6: 4 unchanged + 6 changed words, all replaces
    # 'replace' counts changed on BOTH sides, so use equal-size replace runs.
    # old: 5 unchanged ... actually craft: unchanged=4, changed(old=3,new=3)=6, total=10.
    old = "k1 k2 k3 k4 r1 r2 r3"          # 4 keep + 3 replace = 7 old words
    new = "k1 k2 k3 k4 s1 s2 s3"          # 4 keep + 3 replace = 7 new words
    r = split_change(old, new, threshold=0.6)
    # changed = 3 (old) + 3 (new) = 6 ; unchanged = 4 ; total = 10 ; frac = 0.6
    assert pytest.approx(r.fraction_changed) == 0.6
    assert r.mode == "surgical", "exactly-at-threshold must stay surgical (inclusive)"


def test_just_above_threshold_is_block():
    # unchanged=3, replace old=4/new=4 -> changed=8, total=11, frac ~0.727 > 0.6
    old = "k1 k2 k3 r1 r2 r3 r4"
    new = "k1 k2 k3 s1 s2 s3 s4"
    r = split_change(old, new, threshold=0.6)
    assert r.fraction_changed > 0.6
    assert r.mode == "block"
    assert len(r.sub_edits) == 1
    assert r.sub_edits[0].op == "replace"
    assert r.sub_edits[0].old_start == 0
    assert r.sub_edits[0].old_end == len(old)
    assert r.sub_edits[0].new_text == new


def test_custom_threshold_one_keeps_everything_surgical():
    # Everything changed but threshold 1.0 -> fraction (1.0) is NOT > 1.0 -> surgical
    old = "alpha beta gamma"
    new = "delta epsilon zeta"
    r = split_change(old, new, threshold=1.0)
    assert pytest.approx(r.fraction_changed) == 1.0
    assert r.mode == "surgical"
    assert apply_sub_edits(old, r.sub_edits) == new


def test_custom_threshold_zero_forces_block_on_any_change():
    old = "the quick brown fox jumps over the lazy dog"
    new = "the quick brown cat jumps over the lazy dog"
    r = split_change(old, new, threshold=0.0)
    assert r.fraction_changed > 0.0
    assert r.mode == "block"


def test_repeating_binary_fraction_at_threshold_stays_surgical():
    # frac = 2/3 (a value with no exact binary float representation). When the threshold
    # is the SAME computed value, ``frac > threshold`` must be False -> surgical. Guards
    # against a float-equality off-by-epsilon flipping the boundary.
    old = "k r1"          # 1 unchanged + 1 replace(old) ...
    new = "k s1"          # ... + 1 replace(new) -> changed=2, total=3, frac=2/3
    r = split_change(old, new, threshold=2 / 3)
    assert r.fraction_changed == 2 / 3
    assert r.mode == "surgical", "frac == threshold (2/3) must stay surgical"


def test_repeating_binary_fraction_just_below_threshold_flips_block():
    old = "k r1"
    new = "k s1"
    # threshold a hair below 2/3 -> frac (2/3) IS > threshold -> block
    r = split_change(old, new, threshold=0.666)
    assert r.fraction_changed > 0.666
    assert r.mode == "block"


def test_one_third_fraction_at_threshold_stays_surgical():
    old = "k1 k2"          # 2 unchanged
    new = "k1 k2 x"        # + 1 insert -> changed=1, total=3, frac=1/3
    r = split_change(old, new, threshold=1 / 3)
    assert r.fraction_changed == 1 / 3
    assert r.mode == "surgical"


def test_single_token_replace_is_surgical_block_or_surgical_by_threshold():
    # A single word fully changed: frac = 2/2 = 1.0. With default 0.6 -> block.
    r = split_change("hello", "goodbye")
    assert r.fraction_changed == 1.0
    assert r.mode == "block"
    assert apply_sub_edits("hello", r.sub_edits) == "goodbye"
    # ... but with threshold 1.0 it stays surgical (1.0 is not > 1.0).
    r2 = split_change("hello", "goodbye", threshold=1.0)
    assert r2.mode == "surgical"
    assert len(r2.sub_edits) == 1
    assert r2.sub_edits[0].op == "replace"
    assert apply_sub_edits("hello", r2.sub_edits) == "goodbye"


def test_block_op_is_replace_when_both_sides_nonempty():
    old = "completely different content here now"
    new = "totally other stuff appears instead"
    r = split_change(old, new, threshold=0.6)
    assert r.mode == "block"
    assert r.sub_edits[0].op == "replace"
    assert r.sub_edits[0].old_text == old
    assert r.sub_edits[0].new_text == new


# ---------------------------------------------------------------------------
# whitespace / punctuation specifics
# ---------------------------------------------------------------------------

def test_leading_trailing_whitespace_preserved_in_reconstruction():
    old = "   the quick brown fox jumps over the lazy dog   "
    new = "   the quick brown cat jumps over the lazy dog   "
    r = split_change(old, new)
    assert r.mode == "surgical"
    assert apply_sub_edits(old, r.sub_edits) == new


def test_punctuation_attached_to_word():
    # Punctuation rides with the word run, so "today," is one token. Change "you"->"we".
    old = "hello, world! how are you doing today, friend"
    new = "hello, world! how are we doing today, friend"
    r = split_change(old, new)
    assert r.mode == "surgical"
    assert len(r.sub_edits) == 1
    assert r.sub_edits[0].old_text == "you"
    assert r.sub_edits[0].new_text == "we"
    assert apply_sub_edits(old, r.sub_edits) == new


def test_punctuation_token_change_is_one_edit():
    # Changing the trailing punctuation of a word changes that whole token.
    old = "wait here now, friend and stay"
    new = "wait here now. friend and stay"
    r = split_change(old, new)
    assert r.mode == "surgical"
    assert len(r.sub_edits) == 1
    assert r.sub_edits[0].old_text == "now,"
    assert r.sub_edits[0].new_text == "now."
    assert apply_sub_edits(old, r.sub_edits) == new


def test_internal_multispace_change_reconstructs():
    # Whitespace-only change between two MATCHED words: invisible to a word diff, but
    # reconciled into a tiny 'replace' so reconstruction is exact.
    old = "alpha    beta gamma delta epsilon zeta eta"
    new = "alpha beta gamma delta epsilon zeta eta"
    r = split_change(old, new)
    assert r.mode == "surgical"
    assert apply_sub_edits(old, r.sub_edits) == new
    # The single edit touches only the separator span. Common whitespace is trimmed, so
    # "    " -> " " is recorded tightly as deleting the 3 extra spaces.
    assert len(r.sub_edits) == 1
    e = r.sub_edits[0]
    assert e.old_text == "   "
    assert e.new_text == ""
    assert e.op == "delete"


def test_leading_whitespace_only_change_reconstructs():
    old = "   hello world here"
    new = " hello world here"
    r = split_change(old, new)
    assert r.mode == "surgical"
    assert apply_sub_edits(old, r.sub_edits) == new


def test_trailing_whitespace_only_change_reconstructs():
    old = "hello world here   "
    new = "hello world here "
    r = split_change(old, new)
    assert r.mode == "surgical"
    assert apply_sub_edits(old, r.sub_edits) == new


# ---------------------------------------------------------------------------
# reconstruction property: applying sub-edits to `old` yields `new` exactly
# ---------------------------------------------------------------------------

RECONSTRUCT_CASES = [
    ("", ""),
    ("a", "a"),
    ("the quick brown fox", "the quick brown fox"),
    ("the quick brown fox jumps over", "the quick brown cat jumps over"),
    ("the quick brown fox jumps over", "the slow brown fox leaps over"),
    ("the quick brown fox jumps over", "the speedy tan fox jumps over"),
    ("the quick fox jumps over the lazy dog", "the quick brown fox jumps over the lazy dog"),
    ("the quick brown fox jumps over the lazy dog", "the quick fox jumps over the lazy dog"),
    ("hello world", "goodbye cruel world"),
    ("one two three four five", "one 2 three 4 five"),
    ("a b c d e", "a x c y e"),
    ("a b c d e", "a x y d e"),
    ("   pad both   ", "   pad now both   "),
    ("first. second. third.", "first. SECOND. third."),
    ("insert at end here", "insert at end here now"),
    ("prepend the rest", "really prepend the rest"),
    ("delete from end now", "delete from end"),
    ("delete from start now", "from start now"),
    ("tabs\tand\nnewlines stay", "tabs\tand\nnewlines remain"),
    ("café déjà vu always", "café deja vu always"),
    ("aaa bbb ccc ddd eee fff ggg", "aaa ZZZ ccc ddd YYY fff ggg"),
    ("alpha    beta gamma", "alpha beta gamma"),
    ("   leading spaces here", " leading spaces here"),
    ("trailing spaces here   ", "trailing spaces here "),
    ("tab\tbetween words", "tab between words"),
    ("a b c d e f g h i j", "a b c d e f g h i j"),
    ("word", "word changed entirely now"),
    ("word changed entirely now", "word"),
    # adjacency: three consecutive words changed should agglutinate (reconstruction here)
    ("k1 a b c k2 k3 k4 k5", "k1 X Y Z k2 k3 k4 k5"),
    # adjacent replace + insert with no anchor between
    ("k1 a k2 k3 k4 k5", "k1 X NEW k2 k3 k4 k5"),
    # two changes separated by one anchor -> two segments
    ("k1 a m b k2 k3 k4", "k1 X m Y k2 k3 k4"),
    # punctuation-only token change
    ("wait here now, friend and stay", "wait here now. friend and stay"),
    # word transposition (difflib may resolve as replace; only losslessness asserted)
    ("alpha beta gamma delta", "beta alpha gamma delta"),
    # repeated identical words around a change
    ("aaa aaa aaa bbb aaa", "aaa aaa ZZZ bbb aaa"),
    # separator grows / shrinks between matched words
    ("alpha beta gamma delta", "alpha  beta gamma delta"),
    ("alpha\tbeta gamma delta", "alpha beta gamma delta"),
    # only-whitespace strings of differing length
    ("    ", "  "),
    ("  ", "    "),
]


@pytest.mark.parametrize("old,new", RECONSTRUCT_CASES)
def test_apply_sub_edits_reconstructs_new_exactly(old, new):
    for thr in (0.0, 0.3, 0.6, 0.9, 1.0):
        r = split_change(old, new, threshold=thr)
        assert apply_sub_edits(old, r.sub_edits) == new, (
            "reconstruction failed for thr=%s mode=%s" % (thr, r.mode)
        )


@pytest.mark.parametrize("old,new", RECONSTRUCT_CASES)
def test_surgical_sub_edits_are_disjoint_and_sorted(old, new):
    r = split_change(old, new, threshold=1.0)  # force surgical where possible
    if r.mode != "surgical":
        return
    prev_end = -1
    for e in r.sub_edits:
        assert e.old_start <= e.old_end
        assert e.old_start >= prev_end, "sub-edits overlap or are unsorted"
        # old_text matches the recorded span
        assert old[e.old_start:e.old_end] == e.old_text
        prev_end = e.old_end


@pytest.mark.parametrize("old,new", RECONSTRUCT_CASES)
def test_sub_edit_op_types_are_consistent(old, new):
    r = split_change(old, new, threshold=1.0)
    if r.mode != "surgical":
        return
    for e in r.sub_edits:
        if e.op == "insert":
            assert e.old_text == ""
            assert e.old_start == e.old_end
        elif e.op == "delete":
            assert e.new_text == ""
            assert e.old_start < e.old_end
        else:
            assert e.op == "replace"


@pytest.mark.parametrize("old,new", RECONSTRUCT_CASES)
def test_op_shape_invariant_holds_in_both_modes(old, new):
    # The op label must be consistent with the spans in BOTH block and surgical mode, so
    # the format.py recording branch can branch on ``op`` uniformly without special-casing
    # the whole-block edit.
    for thr in (0.0, 0.6, 1.0):
        r = split_change(old, new, threshold=thr)
        for e in r.sub_edits:
            if e.op == "insert":
                assert e.old_text == "" and e.old_start == e.old_end
            elif e.op == "delete":
                assert e.new_text == "" and e.old_start < e.old_end
            else:
                assert e.op == "replace"
                assert e.old_text != "" and e.new_text != ""
            # op never lies about emptiness
            assert (e.op == "insert") == (e.old_text == "")
            assert (e.op == "delete") == (e.new_text == "")


# ---------------------------------------------------------------------------
# SubEdit dataclass-ish equality (used by other assertions)
# ---------------------------------------------------------------------------

def test_subedit_equality():
    a = SubEdit("replace", 0, 3, "fox", "cat")
    b = SubEdit("replace", 0, 3, "fox", "cat")
    c = SubEdit("delete", 0, 3, "fox", "")
    assert a == b
    assert a != c


# ---------------------------------------------------------------------------
# randomized property test: reconstruction + invariants hold for many inputs
# ---------------------------------------------------------------------------

def test_randomized_reconstruction_and_invariants():
    import random

    rng = random.Random(20240620)
    vocab = ["the", "quick", "brown", "fox", "jumps", "over", "lazy", "dog",
             "a", "b", "c", "x,", "y.", "z!", "café", "déjà", "naïve"]
    seps = [" ", "  ", "   ", "\t", "\n", " \t "]

    def rnd_text():
        n = rng.randint(0, 9)
        if n == 0:
            return rng.choice(["", " ", "  ", "x", "\t"])
        parts = []
        if rng.random() < 0.4:
            parts.append(rng.choice(seps))
        for i in range(n):
            parts.append(rng.choice(vocab))
            if i < n - 1:
                parts.append(rng.choice(seps))
        if rng.random() < 0.4:
            parts.append(rng.choice(seps))
        return "".join(parts)

    for _ in range(3000):
        old, new = rnd_text(), rnd_text()
        for thr in (0.0, 0.3, 0.6, 1.0):
            r = split_change(old, new, threshold=thr)
            # losslessness
            assert apply_sub_edits(old, r.sub_edits) == new, (old, new, thr, r.mode)
            # non-overlap, sorted, spans match recorded old_text
            prev_end = -1
            for e in sorted(r.sub_edits, key=lambda x: (x.old_start, x.old_end)):
                assert e.old_start >= prev_end
                assert old[e.old_start:e.old_end] == e.old_text
                prev_end = e.old_end


# --------------------------------------------------------------------------- threshold <= 0

def test_threshold_zero_forces_block_on_whitespace_only_change():
    # A whitespace-only change has a 0.0 word-fraction; at threshold 0 it must STILL land as one
    # block ("never split"), not a surgical separator redline.
    r = split_change("a b c", "a  b c", 0.0)
    assert r.is_block and len(r.sub_edits) == 1


def test_threshold_zero_forces_block_on_word_change():
    r = split_change("a b c", "a X c", 0.0)
    assert r.is_block


def test_threshold_zero_no_change_records_nothing():
    # old == new is still a no-op even at threshold 0 (no spurious block edit).
    r = split_change("a b c", "a b c", 0.0)
    assert r.is_surgical and r.sub_edits == []
