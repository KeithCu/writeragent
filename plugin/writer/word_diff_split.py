# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Word-level diff splitter for Track-Changes redlines.

PROBLEM
-------
With Track Changes ON, an agent edit today replaces the whole old block with the
whole new block: one Delete + one Insert (see ``plugin/writer/format.py`` recording
branch). For a tiny tweak ("teh" -> "the") in a long paragraph that buries the real
change under a wall of struck-through/inserted text the user cannot meaningfully
review. There is an *existing* char-by-char diff in ``replace_preserving_format`` but
it is DISABLED while recording because per-character redlines render as scrambled,
interleaved garbage.

This module is the PURE-PYTHON, LibreOffice-INDEPENDENT core of the fix. It does NOT
touch UNO/LO. It decides, for a given ``(old, new)`` pair, whether the edit should be:

  * a single whole-block replacement (the change is "big" -- > ``threshold`` of the
    words changed), matching today's behaviour; OR
  * a list of small, surgical sub-edits, each one a contiguous run of WORDS to be
    replaced / inserted / deleted, with CONSECUTIVE changed words AGGLUTINATED into a
    single sub-edit.

Each surgical sub-edit carries char offsets into ``old`` plus the exact old/new spans,
so a later (separate) step in ``format.py`` can turn each sub-edit into its OWN tracked
Delete+Insert by selecting the sub-range ``old[old_start:old_end]`` of the target range.

TOKENISATION
------------
The string is split into an alternating sequence of *word* and *separator* tokens that
together reconstruct the original string EXACTLY (concatenation is lossless). A "word"
is a maximal run of non-whitespace; a "separator" is a maximal run of whitespace.
Punctuation stays attached to the word run it touches (we do not split on punctuation),
which keeps the token count -- and therefore the change fraction -- intuitive and keeps
offsets exact. Only WORD tokens participate in the diff; the separators between matched
words are preserved verbatim and the separators around a changed run are absorbed into
that run's spans so reconstruction is exact.

CHANGE FRACTION (precise definition)
------------------------------------
We run ``difflib.SequenceMatcher`` over the WORD-token sequences of old and new. From
its opcodes we count:

  * ``changed_words`` = (# old words in 'replace'/'delete' blocks)
                      + (# new words in 'replace'/'insert' blocks)
  * ``total_words``   = (# old words in 'equal' blocks)  -- i.e. unchanged words
                      + ``changed_words``

so ``total_words`` counts each unchanged word once and each changed word once on
whichever side(s) it appears. The fraction is::

    fraction_changed = changed_words / total_words      (0.0 if total_words == 0)

BOUNDARY SEMANTICS
------------------
``fraction_changed > threshold``  -> WHOLE-BLOCK replacement.
``fraction_changed <= threshold`` -> SURGICAL sub-edits.

So the threshold is the *inclusive upper bound* for staying surgical: a fraction
EXACTLY at the threshold (e.g. exactly 0.60 with the default) stays SURGICAL. Only a
fraction strictly ABOVE the threshold flips to a whole-block replacement. Rationale:
"60% or less changed" is still worth surgically redlining; only past 60% is it noisy
enough to be a single clean block edit.

Two trivial fast-paths: identical strings -> surgical mode with zero sub-edits;
``total_words == 0`` (both blank) -> surgical mode with zero sub-edits.
"""

import difflib

__all__ = [
    "Token",
    "SubEdit",
    "SplitResult",
    "tokenize",
    "split_change",
]


class Token:
    """A single token from :func:`tokenize`.

    Attributes:
        text:     the raw substring.
        start:    char offset of the token's first char in the source string.
        end:      char offset one past the token's last char (``start + len(text)``).
        is_word:  ``True`` for a non-whitespace run, ``False`` for a whitespace run.
    """

    __slots__ = ("text", "start", "end", "is_word")

    def __init__(self, text, start, end, is_word):
        self.text = text
        self.start = start
        self.end = end
        self.is_word = is_word

    def __repr__(self):
        kind = "W" if self.is_word else "S"
        return "Token(%s %r @%d:%d)" % (kind, self.text, self.start, self.end)

    def __eq__(self, other):
        return (
            isinstance(other, Token)
            and self.text == other.text
            and self.start == other.start
            and self.end == other.end
            and self.is_word == other.is_word
        )


class SubEdit:
    """One surgical edit: replace ``old[old_start:old_end]`` with ``new_text``.

    Attributes:
        op:         ``"replace"``, ``"insert"`` or ``"delete"``.
        old_start:  char offset into ``old`` where the affected span begins.
        old_end:    char offset into ``old`` where the affected span ends
                    (``old_start == old_end`` for a pure insertion).
        old_text:   ``old[old_start:old_end]`` (the text to be deleted; ``""`` for insert).
        new_text:   the replacement text (``""`` for a pure deletion).

    Applying every sub-edit's ``old[:old_start] / new_text / old[old_end:]`` in order,
    left-to-right and non-overlapping, reconstructs ``new`` exactly. See
    :func:`apply_sub_edits`.
    """

    __slots__ = ("op", "old_start", "old_end", "old_text", "new_text")

    def __init__(self, op, old_start, old_end, old_text, new_text):
        self.op = op
        self.old_start = old_start
        self.old_end = old_end
        self.old_text = old_text
        self.new_text = new_text

    def __repr__(self):
        return "SubEdit(%s old[%d:%d]=%r -> %r)" % (
            self.op, self.old_start, self.old_end, self.old_text, self.new_text,
        )

    def __eq__(self, other):
        return (
            isinstance(other, SubEdit)
            and self.op == other.op
            and self.old_start == other.old_start
            and self.old_end == other.old_end
            and self.old_text == other.old_text
            and self.new_text == other.new_text
        )


class SplitResult:
    """Outcome of :func:`split_change`.

    Attributes:
        mode:              ``"block"`` or ``"surgical"``.
        fraction_changed:  the word-count fraction described in the module docstring.
        sub_edits:         list of :class:`SubEdit`. For ``mode == "block"`` this is a
                           single whole-range edit covering all of ``old`` (its ``op`` is
                           classified the same way surgical ops are: ``insert`` if ``old``
                           is empty, ``delete`` if ``new`` is empty, else ``replace``). For
                           ``mode == "surgical"`` it is zero or more surgical sub-edits
                           (empty when ``old == new``).
    """

    __slots__ = ("mode", "fraction_changed", "sub_edits")

    def __init__(self, mode, fraction_changed, sub_edits):
        self.mode = mode
        self.fraction_changed = fraction_changed
        self.sub_edits = sub_edits

    @property
    def is_block(self):
        return self.mode == "block"

    @property
    def is_surgical(self):
        return self.mode == "surgical"

    def __repr__(self):
        return "SplitResult(mode=%s frac=%.4f, %d sub-edit(s))" % (
            self.mode, self.fraction_changed, len(self.sub_edits),
        )


def tokenize(s):
    """Split *s* into alternating word/separator :class:`Token` runs.

    Concatenating ``t.text`` for the returned tokens reproduces *s* exactly, and each
    token's ``[start:end]`` indexes back into *s*. An empty string yields ``[]``.
    """
    tokens = []
    n = len(s)
    i = 0
    while i < n:
        is_word = not s[i].isspace()
        j = i + 1
        while j < n and (not s[j].isspace()) == is_word:
            j += 1
        tokens.append(Token(s[i:j], i, j, is_word))
        i = j
    return tokens


def _word_tokens(tokens):
    """Return only the word tokens (those that take part in the diff)."""
    return [t for t in tokens if t.is_word]


def split_change(old, new, threshold=0.6):
    """Decide block-vs-surgical and compute the edits to turn *old* into *new*.

    Args:
        old:        original block text.
        new:        replacement block text.
        threshold:  inclusive upper bound (by changed-word fraction) for staying
                    surgical. ``fraction <= threshold`` -> surgical; ``> threshold``
                    -> whole-block. Default ``0.6``.

    Returns:
        :class:`SplitResult`. See the module docstring for the exact change-fraction
        definition and boundary semantics.
    """
    old = old if old is not None else ""
    new = new if new is not None else ""

    old_tokens = tokenize(old)
    new_tokens = tokenize(new)
    old_words = _word_tokens(old_tokens)
    new_words = _word_tokens(new_tokens)

    # difflib anchors on equal words. When a word repeats, it may match a different occurrence
    # than a human would, which can make a surgical redline WIDER than strictly minimal -- but never
    # wrong: apply_sub_edits() always reconstructs *new* exactly (lossless not minimal).
    sm = difflib.SequenceMatcher(
        a=[t.text for t in old_words],
        b=[t.text for t in new_words],
        autojunk=False,
    )
    opcodes = sm.get_opcodes()

    # --- change fraction (by word count) ---------------------------------------
    unchanged = 0
    changed = 0
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            unchanged += (i2 - i1)
        elif tag == "replace":
            changed += (i2 - i1) + (j2 - j1)
        elif tag == "delete":
            changed += (i2 - i1)
        elif tag == "insert":
            changed += (j2 - j1)
    total = unchanged + changed
    fraction = (changed / total) if total else 0.0

    # --- whole-block path ------------------------------------------------------
    # threshold <= 0 means "never split -- always land as ONE block": any actual change then goes
    # block, including a whitespace-only edit whose word-fraction is 0.0. old == new is still
    # a no-op (handled by the surgical path returning zero sub-edits below).
    force_block = threshold <= 0.0 and old != new
    if force_block or fraction > threshold:
        # Classify the single whole-range edit with the SAME op semantics the surgical
        # path uses, so a downstream consumer (format.py recording branch) can branch on
        # ``op`` uniformly: empty old -> insert, empty new -> delete, else replace. (When
        # both are empty we never get here: fraction is 0.0, which is never > threshold.)
        if not old:
            block_op = "insert"
        elif not new:
            block_op = "delete"
        else:
            block_op = "replace"
        block = SubEdit(block_op, 0, len(old), old, new)
        return SplitResult("block", fraction, [block])

    # --- surgical path ---------------------------------------------------------
    sub_edits = _build_surgical_edits(old, new, old_words, new_words, opcodes)
    return SplitResult("surgical", fraction, sub_edits)


def _build_surgical_edits(old, new, old_words, new_words, opcodes):
    """Build surgical :class:`SubEdit`s that reconstruct *new* from *old* EXACTLY.

    ANCHOR MODEL (provably lossless). The matched ("equal") words are byte-identical on
    both sides and act as fixed anchors. We cut both strings at every matched-word edge,
    yielding interleaved segments:

        seg, anchor-word, seg, anchor-word, ..., seg

    Anchor-word segments are identical and never edited. Each in-between SEGMENT pairs an
    old span with a new span; emitting it as ``old[old_lo:old_hi] -> new[new_lo:new_hi]``
    and concatenating all segments + anchors reproduces *new* exactly, because the cut
    offsets partition *old* and the replacements supply the corresponding *new* slices.

    TIGHTENING. A raw segment also carries the separators flanking the changed words. We
    trim the longest COMMON whitespace prefix and suffix shared by the segment's old and
    new text (whitespace only, so we never split inside a word) and shrink the span
    accordingly. Trimming a shared prefix/suffix cannot change the result, so it stays
    lossless while keeping each redline tight on what truly changed. Consecutive changed
    words sit in one segment, so they AGGLUTINATE into a single sub-edit; a matched word
    between them splits them into two.

    OP CLASSIFICATION (after trimming): empty old span -> ``insert``; empty new span ->
    ``delete``; otherwise ``replace``. Segments whose old and new spans are equal (no
    real change, e.g. anchors with identical separators) are dropped.

    Returns sub-edits sorted by ``old_start`` (non-overlapping by construction).
    """
    # Pair up matched words: list of (old_word_index, new_word_index) for every equal
    # word, in order. These are the anchors.
    anchors = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            for d in range(i2 - i1):
                anchors.append((i1 + d, j1 + d))

    # Build cut boundaries. A "segment" is the char span between one anchor's end (or
    # string start) and the next anchor's start (or string end), on each side.
    sub_edits = []

    prev_old = 0          # char offset in old where the current segment begins
    prev_new = 0          # char offset in new where the current segment begins
    for oi, nj in anchors:
        ow = old_words[oi]
        nw = new_words[nj]
        # Segment between previous anchor and this matched word.
        _emit_segment(sub_edits, old, new, prev_old, ow.start, prev_new, nw.start)
        # The matched word itself is identical -> skip it (advance past it).
        prev_old = ow.end
        prev_new = nw.end

    # Trailing segment after the last anchor (or the whole string if no anchors).
    _emit_segment(sub_edits, old, new, prev_old, len(old), prev_new, len(new))

    sub_edits.sort(key=lambda e: (e.old_start, e.old_end))
    return sub_edits


def _emit_segment(out, old, new, old_lo, old_hi, new_lo, new_hi):
    """Append a (possibly trimmed) :class:`SubEdit` for one segment, if it changed."""
    old_seg = old[old_lo:old_hi]
    new_seg = new[new_lo:new_hi]
    if old_seg == new_seg:
        return  # separators (and anything else) identical -> no edit needed

    # Trim the longest COMMON WHITESPACE prefix shared by both spans.
    p = 0
    limit = min(len(old_seg), len(new_seg))
    while p < limit and old_seg[p] == new_seg[p] and old_seg[p].isspace():
        p += 1
    # Trim the longest COMMON WHITESPACE suffix shared by both spans.
    s = 0
    while (s < (limit - p)
           and old_seg[len(old_seg) - 1 - s] == new_seg[len(new_seg) - 1 - s]
           and old_seg[len(old_seg) - 1 - s].isspace()):
        s += 1

    old_lo += p
    new_lo += p
    old_hi -= s
    new_hi -= s
    old_seg = old[old_lo:old_hi]
    new_seg = new[new_lo:new_hi]

    if not old_seg and not new_seg:
        return
    if not old_seg:
        op = "insert"
    elif not new_seg:
        op = "delete"
    else:
        op = "replace"
    out.append(SubEdit(op, old_lo, old_hi, old_seg, new_seg))


def apply_sub_edits(old, sub_edits):
    """Apply *sub_edits* (from a :class:`SplitResult`) to *old*, returning the result.

    The sub-edits are assumed non-overlapping and sorted by ``old_start`` (which is how
    :func:`split_change` emits them). This mirrors what the LO integration will do --
    splice each surgical span -- and is the property used by the reconstruction tests.
    """
    out = []
    cursor = 0
    for e in sorted(sub_edits, key=lambda x: (x.old_start, x.old_end)):
        out.append(old[cursor:e.old_start])
        out.append(e.new_text)
        cursor = e.old_end
    out.append(old[cursor:])
    return "".join(out)
