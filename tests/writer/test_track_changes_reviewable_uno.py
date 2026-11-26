# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Reviewable agent edits — recording coverage. With doc.agent_edit_review_mode set to record or
# wait, agent edits land as native tracked changes (redlines) the user can accept/reject, tagged
# per-change session tokens; the user's prior RecordChanges state is restored afterward. Flag
# off = today's behavior, byte for byte. Covers: the replace primitives staying Track-Changes-
# safe under recording (a clean Delete+Insert per change, never a per-character mess or a
# Format redline that keeps old text on Accept), the streamed rewrite session, attribution
# (insertions and deletions authored distinctly for by-author coloring), the style_unreviewed
# flag, and the apply_content tool wiring end to end (incl. never block-waiting on the
# main thread). The EditReviewSession itself is covered in test_edit_review_uno.py; the inline
# review UI helpers in test_inline_review_uno.py.
import contextlib

from plugin.doc.compound_undo import undo_titles
from plugin.doc.text_helpers import normalize_linebreaks
from plugin.framework.uno_context import enum_has_more
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import (
    native_writer_tool_context,
    with_native_doc,
)
from plugin.writer.edit_review import (
    TRACKED_SELECTION_MESSAGE,
    TrackedChangesInSelection,
    WriterStreamedAppendSession,
    WriterStreamedRewriteSession,
)
from plugin.writer.apply_document_content import ApplyDocumentContent
from plugin.writer.edit_review import EditReviewSession, get_agent_edit_review_mode
from plugin.framework.config import save, get_config
from plugin.writer import html_import
fmt = html_import
from plugin.writer.edit_review import _string_skipping_redline
from plugin.writer.inline_review import _resolve_all_agent_changes
from tests.writer.doc_builders import (
    accept_all_changes,
    body_paragraphs,
    body_shape,
    body_texts,
    list_redline_types,
    list_redlines,
    reject_all_changes,
)

_FLAG = "doc.agent_edit_review_mode"


def _reset(doc, ctx, text_str="Original body text."):
    text = doc.getText()
    doc.setPropertyValue("RecordChanges", False)
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, text_str, False)
    # Clear any redlines accumulated by a prior test (accept-all leaves a clean doc).
    if len(doc.getRedlines()):
        accept_all_changes(doc, ctx)


@contextlib.contextmanager
def _recording(doc):
    """Record the wrapped edit as tracked changes (the primitive the session builds on)."""
    doc.setPropertyValue("RecordChanges", True)
    try:
        yield
    finally:
        doc.setPropertyValue("RecordChanges", False)


def _para_text(doc):
    cur = doc.getText().createTextCursor()
    cur.gotoStart(False)
    cur.gotoEndOfParagraph(True)
    return cur.getString()


def _para_range(doc):
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEndOfParagraph(True)
    return cur


# --- replace primitives must be Track-Changes-safe: a clean Delete+Insert, not a char-by-char
# --- mess (plain text) nor a Format redline that keeps the old text on Accept (inline markup) ---

@native_test
@with_native_doc("writer")
def test_search_replace_plain_text_clean_redline_uno(ctx, doc):
    """Plain-text replace_preserving_format under recording must NOT diff char-by-char (which
    records a redline per changed character -> a scrambled, un-reviewable redline). It must be a
    single clean Delete+Insert, so reject restores the original exactly."""
    _reset(doc, ctx, "This clause is important.")
    with _recording(doc):
        fmt.replace_preserving_format(doc, _para_range(doc), "This clause is critically important.")
    rl = list_redline_types(doc)
    assert "Insert" in rl and "Delete" in rl, "plain replace under recording must yield Insert+Delete, got %r" % rl
    reject_all_changes(doc, ctx)
    assert _para_text(doc) == "This clause is important.", "reject must restore the original exactly, got %r" % _para_text(doc)


@native_test
@with_native_doc("writer")
def test_search_replace_plain_text_accept_keeps_only_new_uno(ctx, doc):
    """Accept must leave ONLY the new text -- the old must be a real tracked deletion that gets removed."""
    _reset(doc, ctx, "This clause is important.")
    with _recording(doc):
        fmt.replace_preserving_format(doc, _para_range(doc), "This clause is critically important.")
    accept_all_changes(doc, ctx)
    assert _para_text(doc) == "This clause is critically important.", "accept must keep only the new text, got %r" % _para_text(doc)


@native_test
@with_native_doc("writer")
def test_search_replace_inline_markup_no_format_redline_uno(ctx, doc):
    """Inline markup replace must record a Delete (not a Format) for the old text. A Format redline
    survives Accept, so the doc would keep BOTH the old and the new text."""
    _reset(doc, ctx, "This clause is important.")
    with _recording(doc):
        fmt.replace_single_range_with_content(
            doc, _para_range(doc), "<span>This clause is critically important.</span>", ctx)
    rl = list_redline_types(doc)
    assert "Format" not in rl, "inline replace must not leave a Format redline (it keeps old text on accept), got %r" % rl
    assert "Delete" in rl and "Insert" in rl, "inline replace must be a clean Delete+Insert, got %r" % rl
    accept_all_changes(doc, ctx)
    assert _para_text(doc) == "This clause is critically important.", "accept must keep ONLY the new text, got %r" % _para_text(doc)


@native_test
@with_native_doc("writer")
def test_search_replace_inline_markup_heading_style_preserved_uno(ctx, doc):
    """Skipping the paragraph-style restore while recording must NOT demote a heading: the inline
    HTML import keeps the style, and the edit stays a clean reviewable Delete+Insert."""
    _reset(doc, ctx, "Engine selection")
    _para_range(doc).setPropertyValue("ParaStyleName", "Heading 3")
    with _recording(doc):
        fmt.replace_single_range_with_content(
            doc, _para_range(doc), "<span>Powertrain selection</span>", ctx)
    accept_all_changes(doc, ctx)
    assert _para_text(doc) == "Powertrain selection", "accept must keep only the new heading text, got %r" % _para_text(doc)
    assert _para_range(doc).getPropertyValue("ParaStyleName") == "Heading 3", \
        "heading style must be preserved, got %r" % _para_range(doc).getPropertyValue("ParaStyleName")


@native_test
@with_native_doc("writer")
def test_full_replace_tracked_and_reject_restores_uno(ctx, doc):
    """replace_full_document under recording -> reviewable redlines; reject restores the body."""
    _reset(doc, ctx, "Old document body.")
    with _recording(doc):
        fmt.replace_full_document(doc, ctx, "<p>Brand new body.</p>")
    rl = list_redline_types(doc)
    assert "Insert" in rl and "Delete" in rl, "full replace under recording must yield Insert+Delete redlines, got %r" % rl
    reject_all_changes(doc, ctx)
    assert "Old document body." in _para_text(doc), "reject must restore the original body, got %r" % _para_text(doc)


# --- streamed rewrite session (edit-selection path) --------------------------------------

def _session_run(doc, track_reviewable, prior_recording):
    _reset(doc, None, "Sentence to rewrite.")
    doc.setPropertyValue("RecordChanges", prior_recording)
    text = doc.getText()
    rng = text.createTextCursor()
    rng.gotoStart(False)
    rng.gotoEndOfParagraph(True)
    session = WriterStreamedRewriteSession(doc, rng, "Sentence to rewrite.", track_reviewable=track_reviewable)
    session.append_chunk("Rewritten sentence.")
    session.finish()


@native_test
@with_native_doc("writer")
def test_session_flag_on_user_off_creates_redline_restores_off_uno(ctx, doc):
    _session_run(doc, track_reviewable=True, prior_recording=False)
    assert list_redline_types(doc), "flag on must collapse the streamed edit into a redline"
    assert doc.getPropertyValue("RecordChanges") is False, "recording restored to OFF"
    reject_all_changes(doc, ctx)
    assert _para_text(doc) == "Sentence to rewrite.", "reject restores the original sentence"


@native_test
@with_native_doc("writer")
def test_session_flag_off_user_off_no_redline_uno(ctx, doc):
    _session_run(doc, track_reviewable=False, prior_recording=False)
    assert list_redline_types(doc) == [], "flag off + user off must not create a redline"
    assert _para_text(doc) == "Rewritten sentence.", "edit applied directly"


@native_test
@with_native_doc("writer")
def test_session_prior_recording_on_preserved_uno(ctx, doc):
    _session_run(doc, track_reviewable=False, prior_recording=True)
    assert list_redline_types(doc), "user-on tracking must still collapse to a redline"
    assert doc.getPropertyValue("RecordChanges") is True, "prior ON state preserved"
    doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_streamed_edit_tagged_as_agent_change_uno(ctx, doc):
    """The streamed rewrite collapses to one tracked change; it must also be TAGGED with a
    session token so the review tooling recognizes it as an agent change."""
    _session_run(doc, track_reviewable=True, prior_recording=False)
    comments = [r["RedlineComment"] for r in list_redlines(doc)]
    assert comments and all(c.startswith("wa-review:") for c in comments), \
        "streamed edit redlines must carry a session token, got %r" % comments
    assert len(set(comments)) == 1, "the streamed edit is ONE agent change (one token), got %r" % comments
    authors = {r["RedlineAuthor"] for r in list_redlines(doc)}
    assert authors == {"WriterAgent"}, "the streamed change is authored as the agent, got %r" % authors
    reject_all_changes(doc, ctx)


# --- attribution: insertions vs deletions get different authors (-> 2 by-author colors) ---

@native_test
@with_native_doc("writer")
def test_split_authors_insert_vs_delete_uno(ctx, doc):
    """A replace records its Insert and Delete under DIFFERENT authors, so LibreOffice's
    by-author redline coloring shows new vs removed text in two distinct colors."""
    _reset(doc, ctx, "Old clause body here.")
    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    try:
        res = ApplyDocumentContent().execute(
            native_writer_tool_context(doc, ctx), target="search", old_content="Old clause body here.", content=["New clause body here."])
        assert res.get("status") == "ok", res
        by_type = {r["type"]: r["RedlineAuthor"] for r in list_redlines(doc)}
        assert by_type.get("Insert") == "WriterAgent", "insertions authored WriterAgent: %r" % by_type
        assert by_type.get("Delete") == "WriterAgent (deletions)", "deletions authored distinctly: %r" % by_type
    finally:
        save({_FLAG: prev})
        reject_all_changes(doc, ctx)


@contextlib.contextmanager
def _saved_config(doc, ctx, overrides):
    prev = {key: get_config(key) for key in overrides}
    save(overrides)
    try:
        yield
    finally:
        save(prev)
        if len(doc.getRedlines()):
            reject_all_changes(doc, ctx)


@native_test
@with_native_doc("writer")
def test_split_authors_whole_block_on_two_colors_uno(ctx, doc):
    """A whole-block agent edit authors its Delete distinctly (two colors), matching the
    surgical path. Threshold 0.0 forces the whole-block path, and accept reconstructs
    the exact new text."""
    _reset(doc, ctx, "Old clause body here.")
    with _saved_config(doc, ctx, {
        _FLAG: "record",
        "doc.agent_edit_split_author_colors": True,
        "doc.agent_edit_diff_threshold": 0.0,
    }):
        res = ApplyDocumentContent().execute(
            native_writer_tool_context(doc, ctx), target="search", old_content="Old clause body here.", content=["New clause body here."])
        assert res.get("status") == "ok", res
        by_type = {r["type"]: r["RedlineAuthor"] for r in list_redlines(doc)}
        assert by_type.get("Insert") == "WriterAgent", "insert authored WriterAgent: %r" % by_type
        assert by_type.get("Delete") == "WriterAgent (deletions)", \
            "whole-block deletion must be authored distinctly (two colors): %r" % by_type
        accept_all_changes(doc, ctx)
        assert _para_text(doc) == "New clause body here.", \
            "accept must reconstruct exactly the new text (whole-block atomicity), got %r" % _para_text(doc)


@native_test
@with_native_doc("writer")
def test_split_authors_whole_block_off_one_color_uno(ctx, doc):
    """Toggle OFF: a whole-block edit's Delete and Insert share ONE author (one color), and the edit
    stays atomic -- accept reconstructs exactly the new text."""
    _reset(doc, ctx, "Old clause body here.")
    with _saved_config(doc, ctx, {
        _FLAG: "record",
        "doc.agent_edit_split_author_colors": False,
        "doc.agent_edit_diff_threshold": 0.0,
    }):
        res = ApplyDocumentContent().execute(
            native_writer_tool_context(doc, ctx), target="search", old_content="Old clause body here.", content=["New clause body here."])
        assert res.get("status") == "ok", res
        authors = {r["RedlineAuthor"] for r in list_redlines(doc)}
        assert authors == {"WriterAgent"}, "toggle off -> one author / one color, got %r" % authors
        accept_all_changes(doc, ctx)
        assert _para_text(doc) == "New clause body here.", \
            "accept must reconstruct exactly the new text, got %r" % _para_text(doc)


@native_test
@with_native_doc("writer")
def test_html_import_failure_rolls_back_in_review_mode_uno(ctx, doc):
    """Atomicity for the HTML/import path: in record mode replace_full_document does setString('')
    then imports HTML. If the import throws AFTER the delete, the whole edit is rolled back -- the
    document keeps its original text with no stranded tracked deletion, and no agent change lands."""
    _reset(doc, ctx, "Original body text to keep.")
    real = html_import._insert_mixed_or_plain_html

    def _boom(*a, **k):
        raise RuntimeError("simulated HTML import failure")

    with _saved_config(doc, ctx, {_FLAG: "record"}):
        html_import._insert_mixed_or_plain_html = _boom
        try:
            res = None
            try:
                res = ApplyDocumentContent().execute(native_writer_tool_context(doc, ctx), target="full_document", content=["<p>New body.</p>"])
            except Exception:
                res = {"status": "error", "raised": True}
            assert res.get("status") == "error", "the failed import must surface as an error: %r" % res
            assert _para_text(doc) == "Original body text to keep.", \
                "document must be restored (no stranded deletion), got %r" % _para_text(doc)
            assert list_redline_types(doc) == [], \
                "no tracked change may survive the rolled-back edit, got %r" % list_redline_types(doc)
        finally:
            html_import._insert_mixed_or_plain_html = real


@native_test
@with_native_doc("writer")
def test_split_authors_surgical_off_one_color_uno(ctx, doc):
    """Toggle OFF collapses the SURGICAL path to one author too (consistency both ways)."""
    _reset(doc, ctx, "Old clause body here.")
    with _saved_config(doc, ctx, {
        _FLAG: "record",
        "doc.agent_edit_split_author_colors": False,
    }):
        res = ApplyDocumentContent().execute(
            native_writer_tool_context(doc, ctx), target="search", old_content="Old clause body here.", content=["New clause body here."])
        assert res.get("status") == "ok", res
        authors = {r["RedlineAuthor"] for r in list_redlines(doc)}
        assert authors == {"WriterAgent"}, "surgical toggle off -> one author, got %r" % authors
        accept_all_changes(doc, ctx)
        assert _para_text(doc) == "New clause body here.", "accept must yield the new text, got %r" % _para_text(doc)


# --- the apply_content tool end to end (config read + session wiring) -----------

@native_test
@with_native_doc("writer")
def test_apply_document_content_tool_tracks_when_config_on_uno(ctx, doc):
    """Real tool path: apply_document_content.py reads doc.agent_edit_review_mode=record and records the
    edit as reviewable redlines; reject restores. (Exercises the config read, which the
    primitive-level tests above do not.)"""
    _reset(doc, ctx, "Old tool body.")
    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    try:
        assert get_agent_edit_review_mode() == "record", "mode should read record (test-infra sanity)"
        res = ApplyDocumentContent().execute(native_writer_tool_context(doc, ctx), target="full_document", content=["<p>Tool new body.</p>"])
        assert res.get("status") == "ok", res
        rl = list_redline_types(doc)
        assert "Insert" in rl and "Delete" in rl, "tool path with flag on must create redlines, got %r" % rl
        reject_all_changes(doc, ctx)
        assert "Old tool body." in _para_text(doc), "reject must restore the original, got %r" % _para_text(doc)
    finally:
        save({_FLAG: prev})  # restore the dev's prior value, not a hardcoded False


@native_test
@with_native_doc("writer")
def test_apply_document_content_tool_untracked_when_config_off_uno(ctx, doc):
    """Mode off (the default): the tool applies directly, no redline."""
    _reset(doc, ctx, "Old tool body.")
    prev = get_config(_FLAG)
    save({_FLAG: "off"})
    try:
        assert get_agent_edit_review_mode() == "off"
        res = ApplyDocumentContent().execute(native_writer_tool_context(doc, ctx), target="full_document", content=["<p>Tool new body.</p>"])
        assert res.get("status") == "ok", res
        assert list_redline_types(doc) == [], "flag off must not create redlines"
        assert "Tool new body." in _para_text(doc)
    finally:
        save({_FLAG: prev})


def _alice_changes(doc, ctx):
    from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK
    from plugin.writer import review_authors

    _reset(doc, ctx, "Clause one keep. Alice deletes this.")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    text.insertControlCharacter(cur, PARAGRAPH_BREAK, False)
    text.insertString(cur, "Clause two. Tail.", False)

    prior = review_authors.begin(ctx)
    try:
        review_authors.set_office_author(ctx, "Alice")
        with _recording(doc):
            del_cur = text.createTextCursor()
            del_cur.gotoStart(False)
            del_cur.goRight(len("Clause one keep. "), False)
            del_cur.goRight(len("Alice deletes this."), True)
            del_cur.setString("")

            ins_cur = text.createTextCursor()
            ins_cur.gotoEnd(False)
            text.insertString(ins_cur, " Alice inserted.", False)
    finally:
        review_authors.end(ctx, prior)


def _redline_snapshot(doc):
    from plugin.framework.uno_context import enum_has_more

    out = []
    e = doc.getRedlines().createEnumeration()
    while enum_has_more(e):
        rl = e.nextElement()
        start = rl.getPropertyValue("RedlineStart")
        end = rl.getPropertyValue("RedlineEnd")
        cur = end.getText().createTextCursorByRange(start)
        cur.gotoRange(end, True)
        succ = rl.getPropertyValue("RedlineSuccessorData")
        succ_dict = {pv.Name: pv.Value for pv in succ} if succ else {}
        out.append(
            (str(rl.getPropertyValue("RedlineType")), str(rl.getPropertyValue("RedlineAuthor")), cur.getString(), succ_dict)
        )
    return out


@native_test
@with_native_doc("writer")
def test_full_document_untracked_refuses_over_pending_changes_uno(ctx, doc):
    """full_document with Track Changes off refuses over pending Insert/Delete redlines by any author."""
    _alice_changes(doc, ctx)
    with _saved_config(doc, ctx, {_FLAG: "off"}):
        before_text = doc.getText().getString()
        before_redlines = _redline_snapshot(doc)
        before_undo = undo_titles(doc)

        res = ApplyDocumentContent().execute(
            native_writer_tool_context(doc, ctx), target="full_document", content=["<p>New body.</p>"]
        )
        assert res.get("status") == "error", res
        assert res.get("code") == "PENDING_TRACKED_CHANGES", res
        assert doc.getText().getString() == before_text
        assert _redline_snapshot(doc) == before_redlines
        assert undo_titles(doc) == before_undo

    # Also refuses over a lone agent-authored pending Insert left from an earlier record-mode turn.
    _reset(doc, ctx, "Plain start.")
    with _saved_config(doc, ctx, {_FLAG: "record"}):
        seed = ApplyDocumentContent().execute(
            native_writer_tool_context(doc, ctx), target="end", content=["<p>Agent added.</p>"]
        )
        assert seed.get("status") == "ok", seed
        save({_FLAG: "off"})
        res2 = ApplyDocumentContent().execute(
            native_writer_tool_context(doc, ctx), target="full_document", content=["<p>Wipe all.</p>"]
        )
        assert res2.get("status") == "error", res2
        assert res2.get("code") == "PENDING_TRACKED_CHANGES", res2


@native_test
@with_native_doc("writer")
def test_full_document_recorded_keeps_other_authors_changes_uno(ctx, doc):
    """Recorded full_document keeps Alice's deletion and stacks on Alice's insertion."""
    from plugin.writer import review_authors

    for mode, user_rec in (("record", False), ("off", True)):
        _alice_changes(doc, ctx)
        with _saved_config(doc, ctx, {_FLAG: mode}):
            doc.setPropertyValue("RecordChanges", user_rec)
            try:
                res = ApplyDocumentContent().execute(
                    native_writer_tool_context(doc, ctx), target="full_document", content=["<p>New body.</p>"]
                )
                assert res.get("status") == "ok", res
                snap = _redline_snapshot(doc)
                assert any(
                    rtype == "Delete" and author == "Alice" and rtext == "Alice deletes this."
                    for rtype, author, rtext, _succ in snap
                ), snap

                assert any(
                    rtext == " Alice inserted."
                    and succ.get("RedlineType") == "Insert"
                    and succ.get("RedlineAuthor") == "Alice"
                    for _rtype, _author, rtext, succ in snap
                ), snap

                expected_authors = (
                    {review_authors.INSERT_AUTHOR, review_authors.DELETE_AUTHOR}
                    if mode == "record"
                    else {review_authors.office_author(ctx)}
                )
                for _rtype, author, rtext, _succ in snap:
                    if rtext == "Alice deletes this.":
                        assert author == "Alice"
                    else:
                        assert author in expected_authors, (mode, author, rtext)

                accept_all_changes(doc, ctx)
                assert doc.getText().getString().rstrip("\r\n") == "New body."
            finally:
                doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_apply_document_content_tool_tags_changes_with_session_tokens_uno(ctx, doc):
    """Every redline of a flag-on edit carries a wa-review:<session>:<n> token (so completion
    and outcome detection key on this session only), and a replace-all yields one tagged change
    PER MATCH."""
    _reset(doc, ctx, "Tag alpha here. Tag alpha there.")
    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    try:
        res = ApplyDocumentContent().execute(
            native_writer_tool_context(doc, ctx), target="search", old_content="Tag alpha", content=["Tag beta"], all_matches=True)
        assert res.get("status") == "ok", res
        assert res.get("replaced_count") == 2, res
        comments = [r["RedlineComment"] for r in list_redlines(doc)]
        assert comments and all(c.startswith("wa-review:") for c in comments), \
            "all redlines must carry session tokens, got %r" % comments
        assert len(set(comments)) == 2, "two matches -> two distinct per-change tokens, got %r" % comments
    finally:
        save({_FLAG: prev})
        reject_all_changes(doc, ctx)


@native_test
@with_native_doc("writer")
def test_tool_never_blocks_on_main_thread_even_with_wait_flag_uno(ctx, doc):
    """doc.agent_edit_review_mode=wait: from the MAIN thread the tool must NOT block-wait (the
    user could never click accept/reject if the UI thread were parked) -- it edits, records
    (wait implies recording), and returns without a review payload. The blocking wait only
    happens on a background MCP/chat thread."""
    _reset(doc, ctx, "Guard body text.")
    prev_mode = get_config(_FLAG)
    save({_FLAG: "wait"})
    try:
        res = ApplyDocumentContent().execute(native_writer_tool_context(doc, ctx), target="end", content=["<p>Guard addition.</p>"])
        assert res.get("status") == "ok", res
        assert "review" not in res, "main-thread call must not block-wait: %r" % res
        assert list_redline_types(doc), "wait mode must imply recording (redlines expected)"
    finally:
        save({_FLAG: prev_mode})
        reject_all_changes(doc, ctx)


# --- paragraph apply_style records ParagraphFormat redlines in review mode ---------------

@native_test
@with_native_doc("writer")
def test_apply_style_records_in_review_mode_on_non_recording_doc_uno(ctx, doc):
    from plugin.writer import review_authors
    from plugin.writer.styles import ApplyStyle

    prev = get_config(_FLAG)
    try:
        for mode in ("record", "wait"):
            _reset(doc, ctx, "First bold.")
            doc.setPropertyValue("RecordChanges", False)
            para = doc.getText().createEnumeration().nextElement()
            para.setPropertyValue("ParaLeftMargin", 500)
            save({_FLAG: mode})
            res = ApplyStyle().execute(
                native_writer_tool_context(doc, ctx),
                style="Heading 2",
                family="ParagraphStyles",
                target="full_document",
                clear_direct="none",
            )
            assert res.get("status") == "ok", res
            assert res.get("tracked") is True, res
            assert "style_unreviewed" not in res, res
            rls = list_redlines(doc)
            assert [r["type"] for r in rls] == ["ParagraphFormat"], rls
            assert rls[0]["RedlineAuthor"] == review_authors.INSERT_AUTHOR, rls
            assert doc.getPropertyValue("RecordChanges") is False
            reject_all_changes(doc, ctx)
            para = doc.getText().createEnumeration().nextElement()
            assert para.getPropertyValue("ParaStyleName") == "Standard"
            assert abs(para.getPropertyValue("ParaLeftMargin") - 500) <= 2
    finally:
        save({_FLAG: prev})
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_whole_paragraph_char_change_over_para_redline_is_flagged_uno(ctx, doc):
    from plugin.writer.styles import ApplyStyle, StyleFormatText

    def _setup(text_str: str, *, bold_first_word: bool = False):
        _reset(doc, ctx, text_str)
        doc.setPropertyValue("RecordChanges", False)
        para = doc.getText().createEnumeration().nextElement()
        para.setPropertyValue("ParaLeftMargin", 500)
        if bold_first_word:
            c = doc.getText().createTextCursor()
            c.gotoStart(False)
            c.goRight(len(text_str.split()[0]), True)
            c.setPropertyValue("CharWeight", 150.0)
        doc.setPropertyValue("RecordChanges", True)
        return doc.getText().createTextCursor()

    def _select_all(cur):
        cur.gotoStart(False)
        cur.gotoEnd(True)
        doc.getCurrentController().select(cur)

    def _select_first_word(cur, word_len: int):
        cur.gotoStart(False)
        cur.goRight(word_len, True)
        doc.getCurrentController().select(cur)

    def _margin() -> int:
        return doc.getText().createEnumeration().nextElement().getPropertyValue("ParaLeftMargin")

    try:
        # 1. Para then whole Char: flagged, margin lost on Reject All (LibreOffice ResetAttrs).
        cur = _setup("Alpha beta.")
        _select_all(cur)
        r1 = ApplyStyle().execute(native_writer_tool_context(doc, ctx), style="Heading 2", clear_direct="none")
        assert r1.get("status") == "ok", r1
        r2 = StyleFormatText().execute(native_writer_tool_context(doc, ctx), properties={"CharWeight": 150})
        assert r2.get("status") == "ok", r2
        assert r2.get("style_unreviewed") is True, r2
        assert "direct paragraph formatting" in r2.get("warning", ""), r2
        reject_all_changes(doc, ctx)
        assert _margin() <= 2

        # 2. Para then character apply_style: flagged.
        cur = _setup("Alpha beta.")
        _select_all(cur)
        ApplyStyle().execute(native_writer_tool_context(doc, ctx), style="Heading 2", clear_direct="none")
        r_char = ApplyStyle().execute(native_writer_tool_context(doc, ctx), style="Emphasis", family="CharacterStyles")
        assert r_char.get("status") == "ok", r_char
        assert r_char.get("style_unreviewed") is True, r_char
        assert "direct paragraph formatting" in r_char.get("warning", ""), r_char
        reject_all_changes(doc, ctx)
        assert _margin() <= 2

        # 3. Char then Para: not flagged, margin kept.
        cur = _setup("Alpha beta.")
        _select_all(cur)
        StyleFormatText().execute(native_writer_tool_context(doc, ctx), properties={"CharWeight": 150})
        r_para = ApplyStyle().execute(native_writer_tool_context(doc, ctx), style="Heading 2", clear_direct="none")
        assert r_para.get("status") == "ok", r_para
        assert "style_unreviewed" not in r_para, r_para
        reject_all_changes(doc, ctx)
        assert abs(_margin() - 500) <= 2

        # 4. Para then one word: not flagged, margin kept.
        cur = _setup("Alpha beta.")
        _select_all(cur)
        ApplyStyle().execute(native_writer_tool_context(doc, ctx), style="Heading 2", clear_direct="none")
        _select_first_word(cur, 5)
        r_word = StyleFormatText().execute(native_writer_tool_context(doc, ctx), properties={"CharWeight": 150})
        assert r_word.get("status") == "ok", r_word
        assert "style_unreviewed" not in r_word, r_word
        reject_all_changes(doc, ctx)
        assert abs(_margin() - 500) <= 2

        # 5. Para then whole Char on a paragraph with one bold word: not flagged, margin kept.
        cur = _setup("Alpha beta.", bold_first_word=True)
        _select_all(cur)
        ApplyStyle().execute(native_writer_tool_context(doc, ctx), style="Heading 2", clear_direct="none")
        r_two = StyleFormatText().execute(native_writer_tool_context(doc, ctx), properties={"CharPosture": 2})
        assert r_two.get("status") == "ok", r_two
        assert "style_unreviewed" not in r_two, r_two
        reject_all_changes(doc, ctx)
        assert abs(_margin() - 500) <= 2
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_apply_style_no_unreviewed_flag_when_review_off_uno(ctx, doc):
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "Heading target text.")
    prev = get_config(_FLAG)
    save({_FLAG: "off"})
    try:
        res = ApplyStyle().execute(native_writer_tool_context(doc, ctx), style="Heading 2", family="ParagraphStyles", target="full_document")
        assert res.get("status") == "ok", res
        assert "style_unreviewed" not in res, "no unreviewed flag when review mode off: %r" % res
    finally:
        save({_FLAG: prev})


# --- script/vision result insertions record through the session too ----------------------

@native_test
@with_native_doc("writer")
def test_insert_content_at_position_recorded_by_session_uno(ctx, doc):
    """The mechanism the script/vision result insertions rely on: insert_content_at_position
    inside an enabled EditReviewSession lands as a tagged Insert redline, and the prior
    recording state is restored."""
    _reset(doc, ctx, "Existing body.")
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(lambda: fmt.insert_content_at_position(doc, ctx, "<p>Inserted result.</p>", "end"))
    session.cleanup()
    rls = list_redlines(doc)
    assert any(r["type"] == "Insert" for r in rls), "insert must create an Insert redline, got %r" % rls
    assert all(r["RedlineComment"].startswith("wa-review:") for r in rls), "tagged: %r" % rls
    assert doc.getPropertyValue("RecordChanges") is False, "recording restored to OFF"
    reject_all_changes(doc, ctx)


@native_test
@with_native_doc("writer")
def test_domain_result_inserts_recorded_in_review_mode_uno(ctx, doc):
    """Units, text analytics, and symbolic math inserts record tagged redlines in Record/Wait."""
    from plugin.scripting.symbolic import insert_symbolic_result_into_writer
    from plugin.scripting.text_analytics import insert_text_analytics_result_into_doc
    from plugin.scripting.units import insert_units_result_into_writer

    prev = get_config(_FLAG)
    text = doc.getText()

    def _seed_two():
        _reset(doc, ctx, "")
        cur = text.createTextCursor()
        cur.gotoStart(False)
        cur.setPropertyValue("ParaStyleName", "Standard")
        text.insertString(cur, "Alpha", False)
        text.insertControlCharacter(cur, 0, False)
        cur.setPropertyValue("ParaStyleName", "Heading 3")
        text.insertString(cur, "Omega", False)
        cur.gotoStart(False)
        cur.goRight(2, False)
        doc.getCurrentController().select(cur)
        return [("Standard", "Alpha"), ("Heading 3", "Omega")]

    units_res = {"status": "ok", "helper": "convert_quantity", "formatted": "36 km/h"}
    ta_res = {"status": "ok", "result": {"readability": {"flesch_reading_ease": 60}}}
    sym_res = {"status": "ok", "latex": "x^2", "helper": "solve"}

    cases = [
        ("units", lambda: insert_units_result_into_writer(ctx, doc, units_res), "text", "36 km/h"),
        ("text_analytics", lambda: insert_text_analytics_result_into_doc(ctx, doc, ta_res), "text", "Text Analytics"),
        ("symbolic_inline", lambda: insert_symbolic_result_into_writer(ctx, doc, sym_res, display_block=False), "embedded", None),
        ("symbolic_block", lambda: insert_symbolic_result_into_writer(ctx, doc, sym_res, display_block=True), "embedded", None),
    ]

    try:
        for mode in ("record", "wait"):
            save({_FLAG: mode})
            for name, fn, check_kind, expected_substr in cases:
                expected_seed = _seed_two()
                fn()
                rls = list_redlines(doc)
                assert any(r["type"] == "Insert" for r in rls), (mode, name, rls)
                assert all(r["RedlineComment"].startswith("wa-review:") for r in rls), (mode, name, rls)
                assert doc.getPropertyValue("RecordChanges") is False, (mode, name)
                assert not any(
                    b.startswith("wa_review_") for b in doc.getBookmarks().getElementNames()
                ), (mode, name)
                if check_kind == "text":
                    assert expected_substr in text.getString(), (mode, name)
                else:
                    assert doc.getEmbeddedObjects().getCount() == 1, (mode, name)
                reject_all_changes(doc, ctx)
                assert body_shape(text) == expected_seed, (mode, name)
                assert doc.getEmbeddedObjects().getCount() == 0, (mode, name)
                assert doc.getTextTables().getCount() == 0, (mode, name)

        save({_FLAG: "record"})
        _seed_two()
        insert_units_result_into_writer(ctx, doc, units_res)
        accept_all_changes(doc, ctx)
        assert _para_text(doc) == "Al36 km/hpha"

        save({_FLAG: "off"})
        _seed_two()
        insert_units_result_into_writer(ctx, doc, units_res)
        assert list_redlines(doc) == []
        assert _para_text(doc) == "Al36 km/hpha"
    finally:
        save({_FLAG: prev})
        if len(doc.getRedlines()):
            reject_all_changes(doc, ctx)


# --- extend-selection: append the continuation as ONE tracked INSERTION (the original is never
# --- struck), tagged as an agent change -- via WriterStreamedAppendSession --------------------

def _append_run(doc, ctx, track_reviewable, prior_recording):
    _reset(doc, ctx, "Original sentence.")
    doc.setPropertyValue("RecordChanges", prior_recording)
    rng = doc.getText().createTextCursor()
    rng.gotoStart(False)
    rng.gotoEndOfParagraph(True)
    session = WriterStreamedAppendSession(doc, rng, "Original sentence.", track_reviewable=track_reviewable)
    session.append_chunk(" Added continuation.")
    session.finish()


@native_test
@with_native_doc("writer")
def test_streamed_append_tracks_only_appended_text_uno(ctx, doc):
    """Extend collapses to ONE tracked INSERTION of just the appended text -- the original keeps
    no Delete redline -- tagged as an agent change. Reject removes only the appended text."""
    doc.setPropertyValue("ShowChanges", False)
    _append_run(doc, ctx, track_reviewable=True, prior_recording=False)
    rls = list_redlines(doc)
    assert rls, "review mode must collapse the appended continuation into a redline"
    assert all(r["type"] == "Insert" for r in rls), \
        "extend appends -> Insert only, the original keeps no Delete redline, got %r" % [r["type"] for r in rls]
    comments = [r["RedlineComment"] for r in rls]
    assert comments and all(c.startswith("wa-review:") for c in comments), \
        "appended redline must carry a session token, got %r" % comments
    assert len(set(comments)) == 1, "the append is ONE agent change (one token), got %r" % comments
    assert doc.getPropertyValue("ShowChanges") is True, "review mode forces markup visible"
    assert doc.getPropertyValue("RecordChanges") is False, "recording restored to OFF"
    reject_all_changes(doc, ctx)
    assert _para_text(doc) == "Original sentence.", "reject removes only the appended continuation"


@native_test
@with_native_doc("writer")
def test_streamed_append_flag_off_no_redline_uno(ctx, doc):
    """Flag off + user not recording: extend appends directly, no redline (today's behavior)."""
    _append_run(doc, ctx, track_reviewable=False, prior_recording=False)
    assert list_redline_types(doc) == [], "flag off + user off must not create a redline"
    assert _para_text(doc) == "Original sentence. Added continuation.", "the continuation is appended in full"


@native_test
@with_native_doc("writer")
def test_streamed_append_no_output_restores_prior_recording_uno(ctx, doc):
    """A no-op streamed extend still restores the user's RecordChanges state.

    WriterStreamedAppendSession turns recording off while chunks stream, so an empty model result
    must not leave the user's own tracking disabled just because there was no edit to collapse.
    """
    _reset(doc, ctx, "Original sentence.")
    doc.setPropertyValue("RecordChanges", True)
    rng = doc.getText().createTextCursor()
    rng.gotoStart(False)
    rng.gotoEndOfParagraph(True)
    session = WriterStreamedAppendSession(doc, rng, "Original sentence.", track_reviewable=False)
    warning = session.finish()
    assert warning is None
    assert doc.getPropertyValue("RecordChanges") is True, "no-output append must restore the user's RecordChanges ON state"
    doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_update_style_flags_unreviewed_when_review_on_uno(ctx, doc):
    """Like apply_style, update_style is a style mutation -> not reviewable -> must flag the agent."""
    from plugin.writer.styles import StyleUpdate

    _reset(doc, ctx, "Body text for style update.")
    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    try:
        res = StyleUpdate().execute(
            native_writer_tool_context(doc, ctx), style="Standard", family="ParagraphStyles",
            property_updates={"CharWeight": 150.0})  # 150.0 == BOLD
        assert res.get("status") == "ok", res
        assert res.get("style_unreviewed") is True, "update_style must flag unreviewed under review mode: %r" % res
        assert list_redline_types(doc) == [], "a style change creates no redline"
    finally:
        save({_FLAG: prev})


@native_test
@with_native_doc("writer")
def test_is_async_true_on_background_thread_when_review_toggled_off_uno(ctx, doc):
    """If review is toggled OFF after the async snapshot dispatched the tool to a worker thread,
    is_async() must still report True there so execute_safe's main-thread guard won't reject it
    (execute() then marshals the wait-free edit to the main thread)."""
    import threading

    prev = get_config(_FLAG)
    save({_FLAG: "off"})
    try:
        tool = ApplyDocumentContent()
        assert tool.is_async() is False, "main thread + review off -> synchronous (no spurious async)"
        seen = {}

        def _check():
            seen["v"] = tool.is_async()

        t = threading.Thread(target=_check)
        t.start()
        t.join()
        assert seen.get("v") is True, "on a worker thread is_async must stay True so execute_safe won't reject"
    finally:
        save({_FLAG: prev})


@native_test
@with_native_doc("writer")
def test_bookmarks_cleaned_when_edit_raises_midway_uno(ctx, doc):
    """#4: if the edit path raises AFTER a change was anchored (e.g. end insert recorded),
    execute() must still release the session's wa_review_* anchor bookmarks -- none may leak."""

    _reset(doc, ctx, "Anchor target paragraph.")
    prev = get_config(_FLAG)
    save({_FLAG: "record"})

    class _BoomTool(ApplyDocumentContent):
        def _edit_insert_at(self, tctx, session, params):
            from plugin.writer import html_import as fmt

            with session:
                session.record_mutation(
                    lambda: fmt.insert_content_at_position(
                        tctx.doc, tctx.ctx, "<p>Inserted.</p>", params.target),
                    proposed_preview=params.plain)
            raise RuntimeError("boom after anchoring the first change")

    try:
        raised = False
        try:
            _BoomTool().execute(native_writer_tool_context(doc, ctx), target="end", content="<p>Inserted.</p>")
        except RuntimeError:
            raised = True
        assert raised, "the mid-edit failure must propagate to the caller"
        leaked = [n for n in doc.getBookmarks().getElementNames() if n.startswith("wa_review_")]
        assert leaked == [], "anchor bookmarks must be cleaned up on a mid-edit failure, leaked: %r" % leaked
    finally:
        save({_FLAG: prev})
        if len(doc.getRedlines()):
            reject_all_changes(doc, ctx)


@native_test
@with_native_doc("writer")
def test_review_authors_failed_begin_leaves_split_authoring_disarmed_uno(ctx, doc):
    """A failed begin() (office-author access unavailable) must NOT arm the thread-local; otherwise
    deletion_author() would stay armed for a later, unrelated edit on this thread. The streamed
    sessions skip end() on a None return, so the safety has to live in begin() itself."""
    from plugin.writer import review_authors

    class _BadCtx:
        def getServiceManager(self):  # _author_access() calls this; raising makes begin() fail
            raise RuntimeError("no service manager")

    prior = review_authors.begin(_BadCtx())
    try:
        assert prior is None, "begin() on a broken ctx must return None"
        assert getattr(review_authors._state, "ctx", None) is None, \
            "a failed begin() must leave split authoring disarmed (deletion_author stays inert)"
    finally:
        review_authors.end(ctx, None)  # keep the thread-local clean for later tests


@native_test
@with_native_doc("writer")
def test_apply_document_content_wait_timeout_zero_returns_pending_uno(ctx, doc):
    """Wait mode with timeout=0 on a background thread: executes edit and returns immediately
    with complete=False and pending changes."""
    import threading
    _reset(doc, ctx, "Initial text.")
    prev_mode = get_config(_FLAG)
    prev_timeout = get_config("doc.edit_review_timeout")
    save({_FLAG: "wait"})
    save({"doc.edit_review_timeout": 0})

    try:
        tool = ApplyDocumentContent()
        res = {}
        def _run_tool():
            # Must run on a background thread so the tool is considered async and enters wait path
            tool_ctx = native_writer_tool_context(doc, ctx)
            res["val"] = tool.execute(tool_ctx, target="full_document", content=["New document body."])

        t = threading.Thread(target=_run_tool)
        t.start()
        t.join()

        outcome = res.get("val", {})
        assert outcome.get("status") == "ok", outcome
        review = outcome.get("review", {})
        assert review.get("complete") is False, "complete should be False on immediate timeout"
        assert review.get("timed_out") is True, "timed_out should be True"
        changes = review.get("changes", [])
        assert len(changes) == 1, changes
        assert changes[0]["outcome"] == "pending", changes[0]["outcome"]
    finally:
        save({_FLAG: prev_mode})
        save({"doc.edit_review_timeout": prev_timeout})
        reject_all_changes(doc, ctx)


@native_test
@with_native_doc("writer")
def test_streamed_rewrite_refuses_tracked_deletion_uno(ctx, doc):
    """A streamed rewrite over a tracked deletion raises and leaves that redline alone.

    setString on a tracked-deletion range accepts the deletion, so the session must
    raise TrackedChangesInSelection in __init__—before the undo context, before
    RecordChanges changes, and before setString—so abort_and_restore can still
    restore the redline.
    """
    from plugin.doc.text_helpers import get_string_without_tracked_deletions

    _reset(doc, ctx, "Original text with some words to delete.")

    # "Original text " is 14 characters; the next 10 are the tracked deletion "with some ".
    doc.setPropertyValue("RecordChanges", True)
    rng = doc.getText().createTextCursor()
    rng.gotoStart(False)
    rng.goRight(14, False)
    rng.goRight(10, True)
    rng.setString("")
    doc.setPropertyValue("RecordChanges", False)

    # getString() keeps the deleted characters. The accept-view used by Edit Selection does not.
    raw = _para_text(doc)
    assert raw == "Original text with some words to delete.", raw
    before_redlines = list_redlines(doc)
    assert len(before_redlines) == 1, before_redlines
    assert before_redlines[0]["type"] == "Delete", before_redlines
    before_recording = bool(doc.getPropertyValue("RecordChanges"))
    assert before_recording is False

    rng.gotoStart(False)
    rng.gotoEndOfParagraph(True)
    visible = get_string_without_tracked_deletions(rng)
    assert visible == "Original text words to delete.", visible

    undo = doc.getUndoManager()
    assert undo.isUndoPossible()
    before_titles = undo_titles(doc)

    try:
        WriterStreamedRewriteSession(doc, rng, visible, track_reviewable=True)
    except TrackedChangesInSelection as exc:
        assert str(exc) == TRACKED_SELECTION_MESSAGE
    else:
        raise AssertionError("rewrite over a tracked deletion must refuse before writing")

    assert _para_text(doc) == raw, "the refused rewrite must not accept or clear the deletion"
    assert list_redlines(doc) == before_redlines, "the existing Delete redline must be untouched"
    assert bool(doc.getPropertyValue("RecordChanges")) is before_recording
    assert get_string_without_tracked_deletions(rng) == visible
    assert undo.isUndoPossible(), "refuse must not leave an undo context open"
    assert undo_titles(doc) == before_titles


def _two_para_body(doc, ctx, p1="Alpha initiative", p2="project Beta"):
    text = doc.getText()
    doc.setPropertyValue("RecordChanges", False)
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    text.insertString(cur, p1, False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, p2, False)
    if len(doc.getRedlines()):
        accept_all_changes(doc, ctx)


@native_test
@with_native_doc("writer")
def test_full_replace_multi_paragraph_review_mode_uno(ctx, doc):
    """Two paragraphs, review mode on, replace_full_document with uppercased body:
    no paragraph getString() concatenates boundary words ('initiativeproject'),
    skip-Delete equals new body, skip-Insert equals old body."""
    _two_para_body(doc, ctx, "Alpha initiative", "project Beta")
    prev = get_config(_FLAG)
    save({_FLAG: "wait"})
    try:
        tool_ctx = native_writer_tool_context(doc, ctx)
        tool = ApplyDocumentContent()
        res = tool.execute(tool_ctx, target="full_document", content=["<p>ALPHA INITIATIVE</p><p>PROJECT BETA</p>"])
        assert res.get("status") == "ok", res

        paras = body_texts(doc.getText())
        for p in paras:
            assert "initiativeproject" not in p.lower(), "paragraph mark was deleted, joining words: %r" % p

        accepted = _string_skipping_redline(doc.getText(), "Delete")
        rejected = _string_skipping_redline(doc.getText(), "Insert")
        assert rejected == "Alpha initiative\nproject Beta", "skip-Insert text must match old body: %r" % rejected
        accept_all_changes(doc, ctx)
        assert normalize_linebreaks(doc.getText().getString()) == accepted
    finally:
        save({_FLAG: prev})
        if len(doc.getRedlines()):
            accept_all_changes(doc, ctx)


@native_test
@with_native_doc("writer")
def test_reject_all_and_undo_recovers_edit_uno(ctx, doc):
    """resolve_all_agent_changes(accept=False) then undo(): process stays alive,
    body equals original, redlines back as clean Delete+Insert, second undo removes edit."""
    _two_para_body(doc, ctx, "Alpha initiative", "project Beta")
    prev = get_config(_FLAG)
    save({_FLAG: "wait"})
    try:
        tool_ctx = native_writer_tool_context(doc, ctx)
        tool = ApplyDocumentContent()
        res = tool.execute(tool_ctx, target="full_document", content=["<p>ALPHA INITIATIVE</p><p>PROJECT BETA</p>"])
        assert res.get("status") == "ok", res
        accepted_before = _string_skipping_redline(doc.getText(), "Delete")

        resolve_res = _resolve_all_agent_changes(doc, ctx, accept=False).resolved
        assert resolve_res >= 1, "at least one agent change resolved: %r" % resolve_res

        um = doc.getUndoManager()
        assert um.isUndoPossible(), "undo must be possible after reject all"
        um.undo()  # Undo of RejectAllTrackedChanges - must not crash soffice!

        # Redlines are back as clean Delete + Insert
        rl_types = list_redline_types(doc)
        assert "Delete" in rl_types and "Insert" in rl_types, "redlines must be restored: %r" % rl_types
        assert _string_skipping_redline(doc.getText(), "Insert") == "Alpha initiative\nproject Beta"
        assert _string_skipping_redline(doc.getText(), "Delete") == accepted_before

        # A second undo removes that edit in one step
        assert um.isUndoPossible(), "second undo must be possible"
        um.undo()
        assert len(doc.getRedlines()) == 0, "second undo must remove edit redlines"
        assert _string_skipping_redline(doc.getText(), "Insert") == "Alpha initiative\nproject Beta"
    finally:
        save({_FLAG: prev})
        if len(doc.getRedlines()):
            accept_all_changes(doc, ctx)


@native_test
@with_native_doc("writer")
def test_plain_two_para_track_changes_reject_and_undo_uno(ctx, doc):
    """The same Reject + Undo on a two-paragraph change made only with RecordChanges and setString."""
    _two_para_body(doc, ctx, "Alpha initiative", "project Beta")
    doc.setPropertyValue("RecordChanges", True)
    try:
        cur = doc.getText().createTextCursor()
        cur.gotoStart(False)
        cur.gotoEnd(True)
        cur.setString("ALPHA INITIATIVE\nPROJECT BETA")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    reject_all_changes(doc, ctx)
    um = doc.getUndoManager()
    assert um.isUndoPossible()
    um.undo()  # Undo of RejectAllTrackedChanges
    assert _string_skipping_redline(doc.getText(), "Insert") == "Alpha initiative\nproject Beta"
    assert _string_skipping_redline(doc.getText(), "Delete") == "ALPHA INITIATIVE\nPROJECT BETA"
    rl_types = list_redline_types(doc)
    assert len(rl_types) > 0, "redlines restored on undo"

    assert um.isUndoPossible()
    um.undo()
    assert len(doc.getRedlines()) == 0
    assert _string_skipping_redline(doc.getText(), "Insert") == "Alpha initiative\nproject Beta"


@native_test
@with_native_doc("writer")
def test_recorded_replace_keeps_bold_uno(ctx, doc):
    """Recorded replace keeps bold on bold targets and plain between bold runs, in user-recording and review mode."""
    text = doc.getText()
    prev = get_config(_FLAG)

    def _seed_bold_mid():
        doc.setPropertyValue("RecordChanges", False)
        text.setString("")
        cur = text.createTextCursor()
        cur.setPropertyValue("CharWeight", 100.0)
        text.insertString(cur, "aaa ", False)
        cur.setPropertyValue("CharWeight", 150.0)
        text.insertString(cur, "Replace", False)
        cur.setPropertyValue("CharWeight", 100.0)
        text.insertString(cur, " bbb", False)
        if len(doc.getRedlines()):
            accept_all_changes(doc, ctx)

    def _seed_surgical():
        doc.setPropertyValue("RecordChanges", False)
        text.setString("")
        cur = text.createTextCursor()
        cur.setPropertyValue("CharWeight", 100.0)
        text.insertString(cur, "The ", False)
        cur.setPropertyValue("CharWeight", 150.0)
        text.insertString(cur, "quick", False)
        cur.setPropertyValue("CharWeight", 100.0)
        text.insertString(cur, " fox ran far away", False)
        if len(doc.getRedlines()):
            accept_all_changes(doc, ctx)

    def _seed_plain_between_bold():
        doc.setPropertyValue("RecordChanges", False)
        text.setString("")
        cur = text.createTextCursor()
        cur.setPropertyValue("CharWeight", 150.0)
        text.insertString(cur, "aaa ", False)
        cur.setPropertyValue("CharWeight", 100.0)
        text.insertString(cur, "Replace", False)
        cur.setPropertyValue("CharWeight", 150.0)
        text.insertString(cur, " bbb", False)
        if len(doc.getRedlines()):
            accept_all_changes(doc, ctx)

    def _runs():
        para = text.createEnumeration().nextElement()
        pe = para.createEnumeration()
        out = []
        while enum_has_more(pe):
            p = pe.nextElement()
            if p.getPropertyValue("TextPortionType") == "Text" and p.getString():
                out.append((p.getString(), float(p.getPropertyValue("CharWeight"))))
        return out

    tool = ApplyDocumentContent()
    try:
        for mode_label in ("user_recording", "review_record"):
            if mode_label == "user_recording":
                save({_FLAG: "off"})
                def mode_ctx():
                    return _recording(doc)
            else:
                save({_FLAG: "record"})
                mode_ctx = contextlib.nullcontext

            # aaa **Replace** bbb -> Swap (Accept)
            _seed_bold_mid()
            with mode_ctx():
                res = tool.execute(native_writer_tool_context(doc, ctx), target="search", old_content="Replace", content="Swap")
                assert res.get("status") == "ok", res
                assert sorted(list_redline_types(doc)) == ["Delete", "Insert"], list_redline_types(doc)
            accept_all_changes(doc, ctx)
            assert _runs() == [("aaa ", 100.0), ("Swap", 150.0), (" bbb", 100.0)], (mode_label, _runs())

            # aaa **Replace** bbb -> Swap (Reject)
            _seed_bold_mid()
            with mode_ctx():
                res = tool.execute(native_writer_tool_context(doc, ctx), target="search", old_content="Replace", content="Swap")
                assert res.get("status") == "ok", res
            reject_all_changes(doc, ctx)
            assert _runs() == [("aaa ", 100.0), ("Replace", 150.0), (" bbb", 100.0)], (mode_label, _runs())

            # Surgical The **quick** fox ran far away -> slow (review mode enables surgical split)
            if mode_label == "review_record":
                _seed_surgical()
                with mode_ctx():
                    res = tool.execute(
                        native_writer_tool_context(doc, ctx),
                        target="search",
                        old_content="The quick fox ran far away",
                        content="The slow fox ran far away",
                    )
                    assert res.get("status") == "ok", res
                accept_all_changes(doc, ctx)
                assert _runs() == [("The ", 100.0), ("slow", 150.0), (" fox ran far away", 100.0)], (mode_label, _runs())

            # Plain word between bold runs stays plain after Accept
            _seed_plain_between_bold()
            with mode_ctx():
                res = tool.execute(native_writer_tool_context(doc, ctx), target="search", old_content="Replace", content="Swap")
                assert res.get("status") == "ok", res
            accept_all_changes(doc, ctx)
            assert _runs() == [("aaa ", 150.0), ("Swap", 100.0), (" bbb", 150.0)], (mode_label, _runs())
    finally:
        save({_FLAG: prev})
        doc.setPropertyValue("RecordChanges", False)
        if len(doc.getRedlines()):
            accept_all_changes(doc, ctx)


@native_test
@with_native_doc("writer", reuse=False)
def test_recording_doc_with_review_off_records_styles_and_flags_header_uno(ctx, doc):
    from plugin.writer.page import PageSetHeaderFooterText
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "Styled text.")
    prev = get_config(_FLAG)
    save({_FLAG: "off"})
    doc.setPropertyValue("RecordChanges", True)
    try:
        tool_ctx = native_writer_tool_context(doc, ctx)
        assert len(doc.getRedlines()) == 0
        char_res = ApplyStyle().execute(
            tool_ctx, style="Emphasis", family="CharacterStyles", target="full_document"
        )
        assert char_res["status"] == "ok", char_res
        assert "style_unreviewed" not in char_res
        assert len(doc.getRedlines()) > 0

        before_hdr = len(doc.getRedlines())
        hdr_res = PageSetHeaderFooterText().execute(
            tool_ctx, style="Standard", region="header", content="<p>Header note</p>"
        )
        assert hdr_res["status"] == "ok", hdr_res
        assert hdr_res.get("style_unreviewed") is True  # first write turned header on / auto_height
        assert len(doc.getRedlines()) > before_hdr

        hdr_res2 = PageSetHeaderFooterText().execute(
            tool_ctx, style="Standard", region="header", content="<p>Header note 2</p>"
        )
        assert hdr_res2["status"] == "ok", hdr_res2
        assert "style_unreviewed" not in hdr_res2

        para_res = ApplyStyle().execute(
            tool_ctx, style="Heading 1", family="ParagraphStyles", target="full_document", clear_direct="none"
        )
        assert para_res["status"] == "ok", para_res
        assert para_res.get("tracked") is True
        assert "style_unreviewed" not in para_res
        assert "ParagraphFormat" in list_redline_types(doc)
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})
        if len(doc.getRedlines()):
            accept_all_changes(doc, ctx)


@native_test
@with_native_doc("writer")
def test_streamed_append_abort_and_restore_uno(ctx, doc):
    """abort_and_restore on WriterStreamedAppendSession collapses partial text when recording and stays plain when off."""

    _reset(doc, ctx, "Orig.")
    doc.setPropertyValue("RecordChanges", True)
    try:
        cur = doc.getText().createTextCursor()
        cur.gotoStart(False)
        cur.gotoEnd(True)
        sess = WriterStreamedAppendSession(doc, cur, "Orig.")
        sess.append_chunk(" More")
        sess.abort_and_restore()
        assert doc.getPropertyValue("RecordChanges") is True
        assert len(doc.getRedlines()) == 1
        reject_all_changes(doc, ctx)
        assert doc.getText().getString() == "Orig."

        cur2 = doc.getText().createTextCursor()
        cur2.gotoStart(False)
        cur2.gotoEnd(True)
        sess2 = WriterStreamedAppendSession(doc, cur2, "Orig.")
        sess2.append_chunk(" Kept")
        sess2.abort_and_restore()
        accept_all_changes(doc, ctx)
        assert doc.getText().getString() == "Orig. Kept"
    finally:
        doc.setPropertyValue("RecordChanges", False)

    _reset(doc, ctx, "Orig.")
    cur3 = doc.getText().createTextCursor()
    cur3.gotoStart(False)
    cur3.gotoEnd(True)
    sess3 = WriterStreamedAppendSession(doc, cur3, "Orig.")
    sess3.append_chunk(" Plain")
    sess3.abort_and_restore()
    assert len(doc.getRedlines()) == 0
    assert doc.getText().getString() == "Orig. Plain"


@native_test
@with_native_doc("writer")
def test_format_of_agent_insertion_rejects_to_original_uno(ctx, doc):
    """Formatting an agent insertion keeps its Insert redline and wa-review token so Reject All restores original text."""
    from plugin.writer.styles import ApplyStyle, StyleFormatText

    def _assert_agent_insert_redline() -> None:
        redlines = doc.getRedlines()
        # Hold every SwXRedline wrapper until reads finish: ~SwXRedline removes its
        # SvtListener without SolarMutex (unoredline.cxx, listener.cxx), so dropping
        # one over URP while fetching the next can race.
        reds = [redlines.getByIndex(i) for i in range(redlines.getCount())]
        assert any(
            r.getPropertyValue("RedlineType") == "Insert"
            and r.getPropertyValue("RedlineAuthor") == "WriterAgent"
            and str(r.getPropertyValue("RedlineComment")).startswith("wa-review:")
            for r in reds
        )

    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    try:
        _reset(doc, ctx, "First words here.")
        tool_ctx = native_writer_tool_context(doc, ctx)

        ins = ApplyDocumentContent().execute(
            tool_ctx,
            target="search",
            old_content="words",
            content="quick brown words",
            preserve_format=True,
        )
        assert ins["status"] == "ok", ins

        fmt_res = StyleFormatText().execute(
            tool_ctx,
            target="search",
            old_content="quick brown",
            properties={"CharWeight": 150.0},
        )
        assert fmt_res["status"] == "ok", fmt_res

        _assert_agent_insert_redline()

        reject_all_changes(doc, ctx)
        assert doc.getText().getString() == "First words here."

        # Second case: character ApplyStyle on a recording document.
        _reset(doc, ctx, "First words here.")
        ins2 = ApplyDocumentContent().execute(
            tool_ctx,
            target="search",
            old_content="words",
            content="quick brown words",
            preserve_format=True,
        )
        assert ins2["status"] == "ok", ins2
        doc.setPropertyValue("RecordChanges", True)
        try:
            sty_res = ApplyStyle().execute(
                tool_ctx,
                style="Strong Emphasis",
                family="CharacterStyles",
                target="search",
                old_content="quick brown",
            )
            assert sty_res["status"] == "ok", sty_res
        finally:
            doc.setPropertyValue("RecordChanges", False)

        _assert_agent_insert_redline()

        reject_all_changes(doc, ctx)
        assert doc.getText().getString() == "First words here."
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})


@native_test
@with_native_doc("writer")
def test_format_of_user_insertion_keeps_it_rejectable_uno(ctx, doc):
    """Formatting a user's pending insertion keeps the office author so Reject All removes the user's text."""
    from plugin.writer.styles import StyleFormatText

    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    try:
        _reset(doc, ctx, "aaa bbb ccc")
        doc.setPropertyValue("RecordChanges", True)
        cur = doc.getText().createTextCursor()
        cur.gotoStart(False)
        cur.goRight(4, False)
        doc.getText().insertString(cur, "NEWTEXT ", False)

        res = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx),
            target="search",
            old_content="NEWTEXT",
            properties={"CharWeight": 150.0},
        )
        assert res["status"] == "ok", res

        reject_all_changes(doc, ctx)
        assert doc.getText().getString() == "aaa bbb ccc"
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})


@native_test
@with_native_doc("writer")
def test_format_redline_author_uno(ctx, doc):
    """Format redlines are authored WriterAgent in record mode, and office author when review is off."""
    from com.sun.star.beans import NamedValue
    from plugin.writer.styles import StyleFormatText

    def _read_office_author():
        provider = ctx.getServiceManager().createInstanceWithContext(
            "com.sun.star.configuration.ConfigurationProvider", ctx
        )
        node = NamedValue(Name="nodepath", Value="/org.openoffice.UserProfile/Data")
        access = provider.createInstanceWithArguments(
            "com.sun.star.configuration.ConfigurationAccess", (node,)
        )
        return str(access.getPropertyValue("givenname")), str(access.getPropertyValue("sn"))

    prev = get_config(_FLAG)
    prior_given, prior_sn = _read_office_author()
    try:
        _reset(doc, ctx, "Plain text.")
        save({_FLAG: "record"})
        res_rec = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx),
            target="search",
            old_content="Plain",
            properties={"CharWeight": 150.0},
        )
        assert res_rec["status"] == "ok", res_rec
        assert doc.getRedlines().getCount() == 1
        assert doc.getRedlines().getByIndex(0).getPropertyValue("RedlineAuthor") == "WriterAgent"

        assert _read_office_author() == (prior_given, prior_sn)

        _reset(doc, ctx, "Plain text.")
        save({_FLAG: "off"})
        doc.setPropertyValue("RecordChanges", True)
        res_off = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx),
            target="search",
            old_content="Plain",
            properties={"CharWeight": 150.0},
        )
        assert res_off["status"] == "ok", res_off
        assert doc.getRedlines().getCount() == 1
        assert doc.getRedlines().getByIndex(0).getPropertyValue("RedlineAuthor") != "WriterAgent"
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})


@native_test
@with_native_doc("writer")
def test_recorded_mixed_ranges_longest_run_and_emoji_uno(ctx, doc):
    from com.sun.star.awt.FontWeight import BOLD, NORMAL

    def _runs():
        out = []
        para = doc.getText().createEnumeration().nextElement()
        portions = para.createEnumeration()
        while enum_has_more(portions):
            p = portions.nextElement()
            s = str(p.getString() or "")
            if s:
                out.append((s, float(p.getPropertyValue("CharWeight"))))
        return out

    # Bold longest first -> <D><I><D>, Accept bold, Reject original runs
    _reset(doc, ctx, "Alpha b")
    cur = doc.getText().createTextCursor()
    cur.gotoStart(False)
    cur.goRight(6, True)
    cur.setPropertyValue("CharWeight", BOLD)
    with _recording(doc):
        rng = doc.getText().createTextCursor()
        rng.gotoStart(False)
        rng.gotoEnd(True)
        fmt.replace_preserving_format(doc, rng, "Swap")
    assert [r["type"] for r in list_redlines(doc)] == ["Delete", "Insert", "Delete"]
    accept_all_changes(doc, ctx)
    assert _runs() == [("Swap", float(BOLD))]

    _reset(doc, ctx, "Alpha b")
    cur = doc.getText().createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setPropertyValue("CharWeight", NORMAL)
    cur.gotoStart(False)
    cur.goRight(6, True)
    cur.setPropertyValue("CharWeight", BOLD)
    with _recording(doc):
        rng = doc.getText().createTextCursor()
        rng.gotoStart(False)
        rng.gotoEnd(True)
        fmt.replace_preserving_format(doc, rng, "Swap")
    reject_all_changes(doc, ctx)
    assert _runs() == [("Alpha ", float(BOLD)), ("b", float(NORMAL))], _runs()

    # Plain longest last -> Accept plain
    _reset(doc, ctx, "Note: blah blah")
    cur = doc.getText().createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setPropertyValue("CharWeight", NORMAL)
    cur.gotoStart(False)
    cur.goRight(5, True)
    cur.setPropertyValue("CharWeight", BOLD)
    with _recording(doc):
        rng = doc.getText().createTextCursor()
        rng.gotoStart(False)
        rng.gotoEnd(True)
        fmt.replace_preserving_format(doc, rng, "Updated")
    accept_all_changes(doc, ctx)
    assert _runs() == [("Updated", float(NORMAL))], _runs()

    # Emoji prefix and run -> Accept plain, Reject restores emoji
    _reset(doc, ctx, "😀 plain")
    cur = doc.getText().createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setPropertyValue("CharWeight", NORMAL)
    cur.gotoStart(False)
    cur.goRight(len("😀 "), True)
    cur.setPropertyValue("CharWeight", BOLD)
    with _recording(doc):
        rng = doc.getText().createTextCursor()
        rng.gotoStart(False)
        rng.gotoEnd(True)
        fmt.replace_preserving_format(doc, rng, "New")
    accept_all_changes(doc, ctx)
    assert _runs() == [("New", float(NORMAL))], _runs()

    _reset(doc, ctx, "😀 plain")
    cur = doc.getText().createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setPropertyValue("CharWeight", NORMAL)
    cur.gotoStart(False)
    cur.goRight(len("😀 "), True)
    cur.setPropertyValue("CharWeight", BOLD)
    with _recording(doc):
        rng = doc.getText().createTextCursor()
        rng.gotoStart(False)
        rng.gotoEnd(True)
        fmt.replace_preserving_format(doc, rng, "New")
    reject_all_changes(doc, ctx)
    assert _runs() == [("😀 ", float(BOLD)), ("plain", float(NORMAL))], _runs()


@native_test
@with_native_doc("writer", reuse=False)
def test_apply_para_style_recording_reject_and_accept_uno(ctx, doc):
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "First bold rest.\rSecond plain.")
    paras = body_paragraphs(doc.getText())
    p0_cur = doc.getText().createTextCursorByRange(paras[0].getStart())
    p0_cur.gotoEndOfParagraph(True)
    p0_cur.setPropertyValue("ParaLeftMargin", 500)
    p0_cur.setPropertyValue("ParaAdjust", 3)  # CENTER
    bold_cur = doc.getText().createTextCursorByRange(paras[0].getStart())
    bold_cur.goRight(5, True)
    bold_cur.setPropertyValue("CharWeight", 150.0)

    prev = get_config(_FLAG)
    save({_FLAG: "off"})
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Heading 1", family="ParagraphStyles",
            target="full_document", clear_direct="none",
        )
        assert res["status"] == "ok", res
        assert res["tracked"] is True
        assert "style_unreviewed" not in res
        assert list_redline_types(doc) == ["ParagraphFormat", "ParagraphFormat"]

        reject_all_changes(doc, ctx)
        assert len(doc.getRedlines()) == 0
        paras = body_paragraphs(doc.getText())
        assert paras[0].getPropertyValue("ParaStyleName") == "Standard"
        assert paras[1].getPropertyValue("ParaStyleName") == "Standard"
        assert abs(paras[0].getPropertyValue("ParaLeftMargin") - 500) <= 2
        assert paras[0].getPropertyValue("ParaAdjust") == 3
        runs = []
        pe = paras[0].createEnumeration()
        while enum_has_more(pe):
            p = pe.nextElement()
            if p.getPropertyValue("TextPortionType") == "Text":
                runs.append((p.getString(), p.getPropertyValue("CharWeight")))
        assert runs == [("First", 150.0), (" bold rest.", 100.0)]

        # Accept All keeps the new style and leaves no redlines.
        res2 = ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Heading 1", family="ParagraphStyles",
            target="full_document", clear_direct="none",
        )
        assert res2["tracked"] is True
        accept_all_changes(doc, ctx)
        assert len(doc.getRedlines()) == 0
        assert body_paragraphs(doc.getText())[0].getPropertyValue("ParaStyleName") == "Heading 1"
        assert body_paragraphs(doc.getText())[1].getPropertyValue("ParaStyleName") == "Heading 1"
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})
        if len(doc.getRedlines()):
            accept_all_changes(doc, ctx)


@native_test
@with_native_doc("writer", reuse=False)
def test_apply_para_style_recording_twice_and_all_matches_uno(ctx, doc):
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "Alpha token and token end.")
    p0 = body_paragraphs(doc.getText())[0]
    p0.setPropertyValue("ParaLeftMargin", 400)

    prev = get_config(_FLAG)
    save({_FLAG: "off"})
    doc.setPropertyValue("RecordChanges", True)
    try:
        tool = ApplyStyle()
        tctx = native_writer_tool_context(doc, ctx)
        res1 = tool.execute(tctx, style="Heading 1", target="full_document", clear_direct="none")
        res2 = tool.execute(tctx, style="Heading 2", target="full_document", clear_direct="none")
        assert res1["tracked"] is True and res2["tracked"] is True
        assert list_redline_types(doc) == ["ParagraphFormat"]

        reject_all_changes(doc, ctx)
        p0 = body_paragraphs(doc.getText())[0]
        assert p0.getPropertyValue("ParaStyleName") == "Standard"
        assert p0.getPropertyValue("ParaLeftMargin") == 400

        # Two search matches in the same paragraph produce only one ParagraphFormat redline.
        res3 = tool.execute(
            tctx, style="Heading 1", target="search", old_content="token",
            all_matches=True, clear_direct="none",
        )
        assert res3["applied_count"] == 2
        assert list_redline_types(doc) == ["ParagraphFormat"]
        reject_all_changes(doc, ctx)
        assert body_paragraphs(doc.getText())[0].getPropertyValue("ParaStyleName") == "Standard"
        assert body_paragraphs(doc.getText())[0].getPropertyValue("ParaLeftMargin") == 400
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})
        if len(doc.getRedlines()):
            accept_all_changes(doc, ctx)


@native_test
@with_native_doc("writer", reuse=False)
def test_apply_para_style_recording_table_cell_and_body_uno(ctx, doc):
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "Body para.")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(1, 1)
    text.insertTextContent(cur, table, False)
    cell = table.getCellByName("A1")
    cell.setString("Cell text.")

    prev = get_config(_FLAG)
    save({_FLAG: "off"})
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Heading 1", target="full_document", clear_direct="none"
        )
        assert res["tracked"] is True
        reject_all_changes(doc, ctx)
        assert body_paragraphs(doc.getText())[0].getPropertyValue("ParaStyleName") == "Standard"
        ce = cell.createEnumeration()
        assert ce.nextElement().getPropertyValue("ParaStyleName") == "Table Contents"
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})
        if len(doc.getRedlines()):
            accept_all_changes(doc, ctx)


@native_test
@with_native_doc("writer", reuse=False)
def test_apply_para_style_recording_over_insert_and_delete_redlines_uno(ctx, doc):
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "Keep drop end.")
    p0 = body_paragraphs(doc.getText())[0]
    p0.setPropertyValue("ParaLeftMargin", 600)

    prev = get_config(_FLAG)
    save({_FLAG: "off"})
    doc.setPropertyValue("RecordChanges", True)
    try:
        cur = doc.getText().createTextCursor()
        cur.gotoStart(False)
        cur.goRight(5, False)
        cur.goRight(5, True)
        cur.setString("")  # tracked Delete of "drop "
        cur.gotoEnd(False)
        doc.getText().insertString(cur, " added", False)  # tracked Insert

        res = ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Heading 1", target="full_document", clear_direct="none"
        )
        assert res["tracked"] is True
        assert "ParagraphFormat" in list_redline_types(doc)

        reject_all_changes(doc, ctx)
        assert doc.getText().getString() == "Keep drop end."
        assert body_paragraphs(doc.getText())[0].getPropertyValue("ParaStyleName") == "Standard"
        assert body_paragraphs(doc.getText())[0].getPropertyValue("ParaLeftMargin") == 600
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})
        if len(doc.getRedlines()):
            accept_all_changes(doc, ctx)


@native_test
@with_native_doc("writer", reuse=False)
def test_apply_para_style_recording_numbered_paragraph_uno(ctx, doc):
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "Numbered item.")
    p0 = body_paragraphs(doc.getText())[0]
    p0.setPropertyValue("NumberingStyleName", "Numbering 123")
    assert p0.getPropertyValue("ListLabelString") == "1."

    prev = get_config(_FLAG)
    save({_FLAG: "off"})
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Heading 1", target="full_document", clear_direct="none"
        )
        assert res["tracked"] is True
        assert res.get("style_unreviewed") is True
        assert "list" in res["warning"].lower()

        reject_all_changes(doc, ctx)
        p0 = body_paragraphs(doc.getText())[0]
        assert p0.getPropertyValue("ParaStyleName") == "Standard"
        assert p0.getPropertyValue("ListLabelString") == "1."
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})
        if len(doc.getRedlines()):
            accept_all_changes(doc, ctx)


@native_test
@with_native_doc("writer", reuse=False)
def test_apply_para_style_recording_fill_tabstop_border_survive_reject_uno(ctx, doc):
    import uno
    from com.sun.star.style import TabStop
    from com.sun.star.table import BorderLine2
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "Decorated paragraph.")
    p0 = body_paragraphs(doc.getText())[0]
    solid = uno.Enum("com.sun.star.drawing.FillStyle", "SOLID")
    p0.setPropertyValue("FillStyle", solid)
    p0.setPropertyValue("FillColor", 0xDDEEFF)
    ts = TabStop()
    ts.Position = 2000
    uno.invoke(p0, "setPropertyValue", ("ParaTabStops", uno.Any("[]com.sun.star.style.TabStop", (ts,))))
    border = BorderLine2()
    border.OuterLineWidth = 50
    border.Color = 0x112233
    p0.setPropertyValue("LeftBorder", border)

    prev = get_config(_FLAG)
    save({_FLAG: "off"})
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Heading 1", target="full_document", clear_direct="none"
        )
        assert res["tracked"] is True
        reject_all_changes(doc, ctx)
        p0 = body_paragraphs(doc.getText())[0]
        assert p0.getPropertyValue("FillStyle") == solid
        assert p0.getPropertyValue("FillColor") == 0xDDEEFF
        assert len(p0.getPropertyValue("ParaTabStops")) == 1
        assert p0.getPropertyValue("ParaTabStops")[0].Position == 2000
        left_border = p0.getPropertyValue("LeftBorder")
        assert abs(left_border.OuterLineWidth - 50) <= 2
        assert left_border.Color == 0x112233
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})
        if len(doc.getRedlines()):
            accept_all_changes(doc, ctx)


@native_test
@with_native_doc("writer", reuse=False)
def test_apply_para_style_recording_clear_direct_warns_without_format_redline_uno(ctx, doc):
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "Big font text.")
    cur = _para_range(doc)
    cur.setPropertyValue("CharHeight", 20.0)

    prev = get_config(_FLAG)
    save({_FLAG: "off"})
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Heading 1", target="full_document"
        )
        assert res["status"] == "ok", res
        assert res["tracked"] is True
        assert res.get("style_unreviewed") is True
        assert "warning" in res
        assert list_redline_types(doc) == ["ParagraphFormat"]
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})
        if len(doc.getRedlines()):
            accept_all_changes(doc, ctx)


@native_test
@with_native_doc("writer", reuse=False)
def test_apply_para_style_recording_uses_office_author_uno(ctx, doc):
    from plugin.writer import review_authors
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "Author test.")
    prev = get_config(_FLAG)
    save({_FLAG: "off"})
    prior = review_authors.begin(ctx)
    doc.setPropertyValue("RecordChanges", True)
    try:
        assert review_authors.set_office_author(ctx, "Test User") is True
        cur = doc.getText().createTextCursor()
        cur.gotoEnd(False)
        doc.getText().insertString(cur, " typed", False)
        res = ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Heading 1", target="full_document", clear_direct="none"
        )
        assert res["tracked"] is True
        authors = {r["RedlineAuthor"] for r in list_redlines(doc)}
        assert authors == {"Test User"}
    finally:
        doc.setPropertyValue("RecordChanges", False)
        review_authors.end(ctx, prior)
        save({_FLAG: prev})
        if len(doc.getRedlines()):
            accept_all_changes(doc, ctx)


@native_test
@with_native_doc("writer", reuse=False)
def test_apply_para_style_recording_empty_paragraph_uno(ctx, doc):
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "")
    prev = get_config(_FLAG)
    save({_FLAG: "off"})
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Heading 1", target="selection", clear_direct="none"
        )
        assert res["status"] == "ok", res
        assert res["tracked"] is False
        assert res.get("style_unreviewed") is True
        assert body_paragraphs(doc.getText())[0].getPropertyValue("ParaStyleName") == "Heading 1"
        assert doc.getPropertyValue("RecordChanges") is True
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_apply_para_style_recording_docx_roundtrip_uno(ctx, doc):
    import os
    import tempfile
    from com.sun.star.beans import PropertyValue
    import uno
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "Roundtrip body.")
    body_paragraphs(doc.getText())[0].setPropertyValue("ParaLeftMargin", 500)

    prev = get_config(_FLAG)
    save({_FLAG: "off"})
    doc.setPropertyValue("RecordChanges", True)
    reloaded = None
    try:
        res = ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Heading 1", target="full_document", clear_direct="none"
        )
        assert res["tracked"] is True
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "para_style.docx")
            url = uno.systemPathToFileUrl(path)
            fv = PropertyValue()
            fv.Name = "FilterName"
            fv.Value = "Office Open XML Text"
            doc.storeToURL(url, (fv,))
            desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
            hv = PropertyValue()
            hv.Name = "Hidden"
            hv.Value = True
            reloaded = desktop.loadComponentFromURL(url, "_blank", 0, (hv,))
            assert "ParagraphFormat" in list_redline_types(reloaded)
            reject_all_changes(reloaded, ctx)
            p0 = body_paragraphs(reloaded.getText())[0]
            assert p0.getPropertyValue("ParaStyleName") == "Standard"
            assert abs(p0.getPropertyValue("ParaLeftMargin") - 500) <= 2
    finally:
        if reloaded is not None:
            reloaded.close(True)
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})
        if len(doc.getRedlines()):
            accept_all_changes(doc, ctx)


@native_test
@with_native_doc("writer", reuse=False)
def test_style_format_text_para_and_char_reject_and_accept_uno(ctx, doc):
    from plugin.writer.styles import StyleFormatText

    _reset(doc, ctx, "First para.")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "Second para.", False)
    paras = body_paragraphs(doc.getText())
    paras[0].setPropertyValue("ParaLeftMargin", 500)
    paras[1].setPropertyValue("ParaLeftMargin", 700)
    orig_adjust = [p.getPropertyValue("ParaAdjust") for p in paras]
    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    try:
        res = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx),
            properties={"ParaLeftMargin": 1200, "ParaAdjust": "center", "CharWeight": 150.0},
            target="full_document",
        )
        assert res["status"] == "ok", res
        assert res["tracked"] is True
        assert "style_unreviewed" not in res
        assert list_redline_types(doc).count("ParagraphFormat") == 2

        reject_all_changes(doc, ctx)
        paras = body_paragraphs(doc.getText())
        assert abs(paras[0].getPropertyValue("ParaLeftMargin") - 500) <= 2
        assert abs(paras[1].getPropertyValue("ParaLeftMargin") - 700) <= 2
        for idx, p in enumerate(paras):
            assert p.getPropertyValue("ParaAdjust") == orig_adjust[idx]
            assert p.createEnumeration().nextElement().getPropertyValue("CharWeight") == 100.0

        res2 = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx),
            properties={"ParaLeftMargin": 1200, "ParaAdjust": "center", "CharWeight": 150.0},
            target="full_document",
        )
        assert res2["status"] == "ok", res2
        accept_all_changes(doc, ctx)
        for p in body_paragraphs(doc.getText()):
            assert abs(p.getPropertyValue("ParaLeftMargin") - 1200) <= 2
            assert int(p.getPropertyValue("ParaAdjust")) == 3  # ParagraphAdjust.CENTER
            assert p.createEnumeration().nextElement().getPropertyValue("CharWeight") == 150.0
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_style_format_text_numbering_style_set_and_reject_uno(ctx, doc):
    from plugin.writer.styles import StyleFormatText

    _reset(doc, ctx, "Unnumbered.")
    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    try:
        res = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx),
            properties={"NumberingStyleName": "List 1"},
            target="full_document",
        )
        assert res["status"] == "ok", res
        assert res["tracked"] is True
        assert "ParagraphFormat" in list_redline_types(doc)

        reject_all_changes(doc, ctx)
        p0 = body_paragraphs(doc.getText())[0]
        assert not p0.getPropertyValue("NumberingStyleName")
        assert p0.getPropertyValue("ListLabelString") == ""
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_style_format_text_skips_list_item_and_heading_para_redline_uno(ctx, doc):
    from plugin.writer.styles import StyleFormatText

    _reset(doc, ctx, "")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.setPropertyValue("ParaStyleName", "Heading 2")
    text.insertString(cur, "Heading two", False)
    text.insertControlCharacter(cur.getEnd(), 0, False)
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Plain body", False)
    text.insertControlCharacter(cur.getEnd(), 0, False)
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "List item", False)
    cur.gotoStartOfParagraph(False)
    cur.gotoEndOfParagraph(True)
    cur.setPropertyValue("NumberingStyleName", "List 1")
    cur.setPropertyValue("NumberingLevel", 1)

    paras = body_paragraphs(doc.getText())
    assert paras[0].getPropertyValue("NumberingStyleName") != "List 1"
    assert paras[0].getPropertyValue("ListLabelString") == ""
    orig_h2_label = paras[0].getPropertyValue("ListLabelString")
    orig_list_label = paras[2].getPropertyValue("ListLabelString")
    orig_list_level = paras[2].getPropertyValue("NumberingLevel")

    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    try:
        res_h2 = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx),
            properties={"ParaLeftMargin": 900},
            target="search",
            old_content="Heading two",
        )
        assert res_h2["tracked"] is False
        assert res_h2.get("style_unreviewed") is True
        assert "ParagraphFormat" not in list_redline_types(doc)

        res_list = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx),
            properties={"ParaLeftMargin": 900},
            target="search",
            old_content="List item",
        )
        assert res_list["tracked"] is False
        assert res_list.get("style_unreviewed") is True
        assert "ParagraphFormat" not in list_redline_types(doc)

        res_plain = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx),
            properties={"ParaLeftMargin": 900},
            target="search",
            old_content="Plain body",
        )
        assert res_plain["tracked"] is True
        assert "ParagraphFormat" in list_redline_types(doc)

        reject_all_changes(doc, ctx)
        paras = body_paragraphs(doc.getText())
        assert paras[0].getPropertyValue("ListLabelString") == orig_h2_label
        assert paras[2].getPropertyValue("ListLabelString") == orig_list_label
        assert paras[2].getPropertyValue("NumberingLevel") == orig_list_level
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_apply_para_style_recording_uses_agent_author_in_review_mode_uno(ctx, doc):
    from plugin.writer import review_authors
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "Author test.")
    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    prior = review_authors.begin(ctx)
    doc.setPropertyValue("RecordChanges", True)
    try:
        assert review_authors.set_office_author(ctx, "Alice") is True
        cur = doc.getText().createTextCursor()
        cur.gotoEnd(False)
        doc.getText().insertString(cur, " typed", False)
        res = ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Heading 1", target="full_document", clear_direct="none"
        )
        assert res["tracked"] is True
        para_authors = {
            r["RedlineAuthor"] for r in list_redlines(doc) if r["type"] == "ParagraphFormat"
        }
        assert para_authors == {review_authors.INSERT_AUTHOR}
        reject_all_changes(doc, ctx)
        assert doc.getText().getString() == "Author test."
        assert body_paragraphs(doc.getText())[0].getPropertyValue("ParaStyleName") == "Standard"
    finally:
        doc.setPropertyValue("RecordChanges", False)
        review_authors.end(ctx, prior)
        save({_FLAG: prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_apply_para_style_undo_then_reapply_stale_redline_limitation_uno(ctx, doc):
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "Stale redline.")
    body_paragraphs(doc.getText())[0].setPropertyValue("ParaLeftMargin", 500)
    prev = get_config(_FLAG)
    save({_FLAG: "off"})
    doc.setPropertyValue("RecordChanges", True)
    try:
        ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Heading 1", target="full_document", clear_direct="none"
        )
        doc.getUndoManager().undo()
        body_paragraphs(doc.getText())[0].setPropertyValue("ParaLeftMargin", 1300)
        ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Heading 1", target="full_document", clear_direct="none"
        )
        reject_all_changes(doc, ctx)
        assert abs(body_paragraphs(doc.getText())[0].getPropertyValue("ParaLeftMargin") - 500) <= 2
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_apply_char_style_records_in_review_mode_on_non_recording_doc_uno(ctx, doc):
    from plugin.writer import review_authors
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "Char style target.")
    cur = doc.getText().createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setPropertyValue("CharStyleName", "Emphasis")

    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    doc.setPropertyValue("RecordChanges", False)
    try:
        res = ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Source Text", family="CharacterStyles", target="full_document"
        )
        assert res["status"] == "ok", res
        assert res["tracked"] is True
        assert "style_unreviewed" not in res
        assert doc.getPropertyValue("RecordChanges") is False
        fmt = [r for r in list_redlines(doc) if r["type"] == "Format"]
        assert fmt and fmt[0]["RedlineAuthor"] == review_authors.INSERT_AUTHOR

        reject_all_changes(doc, ctx)
        portion = body_paragraphs(doc.getText())[0].createEnumeration().nextElement()
        assert portion.getPropertyValue("CharStyleName") == "Emphasis"
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})


def _create_test_png(width: int = 8, height: int = 8) -> bytes:
    import struct
    import zlib

    raw = b"".join(b"\x00" + b"\x33\x66\x99" * width for _ in range(height))
    compressed = zlib.compress(raw)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", compressed) + _chunk(b"IEND", b"")


@native_test
@with_native_doc("writer", reuse=False)
def test_para_format_reject_restores_wide_direct_properties_uno(ctx, doc):
    import uno
    from plugin.writer.styles import ApplyStyle, StyleFormatText

    fill_solid = uno.Enum("com.sun.star.drawing.FillStyle", "SOLID")
    para_block = uno.Enum("com.sun.star.style.ParagraphAdjust", "BLOCK")

    def _configure_para(para):
        para.setPropertyValue("ParaIsHyphenation", True)
        para.setPropertyValue("ParaHyphenationMaxLeadingChars", 4)
        para.setPropertyValue("ParaHyphenationMaxHyphens", 3)
        para.setPropertyValue("ParaRegisterModeActive", True)
        para.setPropertyValue("ParaLineNumberStartValue", 7)
        para.setPropertyValue("ParaIsHangingPunctuation", False)
        para.setPropertyValue("SnapToGrid", False)
        para.setPropertyValue("ParaAdjust", para_block)
        para.setPropertyValue("ParaLastLineAdjust", 2)
        para.setPropertyValue("ParaExpandSingleWord", True)
        dc = para.getPropertyValue("DropCapFormat")
        dc.Lines = 3
        dc.Count = 1
        para.setPropertyValue("DropCapFormat", dc)
        para.setPropertyValue("DropCapWholeWord", True)
        para.setPropertyValue("FillStyle", fill_solid)
        para.setPropertyValue("FillColor", 0x112233)
        para.setPropertyValue("FillTransparence", 40)
        if para.getPropertySetInfo().hasPropertyByName("WritingModeAutomatic"):
            para.setPropertyValue("WritingModeAutomatic", False)

    def _snapshot(para):
        snap = {
            "ParaIsHyphenation": para.getPropertyValue("ParaIsHyphenation"),
            "ParaHyphenationMaxLeadingChars": para.getPropertyValue("ParaHyphenationMaxLeadingChars"),
            "ParaHyphenationMaxHyphens": para.getPropertyValue("ParaHyphenationMaxHyphens"),
            "ParaRegisterModeActive": para.getPropertyValue("ParaRegisterModeActive"),
            "ParaLineNumberStartValue": para.getPropertyValue("ParaLineNumberStartValue"),
            "ParaIsHangingPunctuation": para.getPropertyValue("ParaIsHangingPunctuation"),
            "SnapToGrid": para.getPropertyValue("SnapToGrid"),
            "ParaAdjust": para.getPropertyValue("ParaAdjust"),
            "ParaLastLineAdjust": para.getPropertyValue("ParaLastLineAdjust"),
            "ParaExpandSingleWord": para.getPropertyValue("ParaExpandSingleWord"),
            "DropCapWholeWord": para.getPropertyValue("DropCapWholeWord"),
            "DropCapLines": para.getPropertyValue("DropCapFormat").Lines,
            "FillStyle": para.getPropertyValue("FillStyle"),
            "FillColor": para.getPropertyValue("FillColor"),
            "FillTransparence": para.getPropertyValue("FillTransparence"),
        }
        if para.getPropertySetInfo().hasPropertyByName("WritingModeAutomatic"):
            snap["WritingModeAutomatic"] = para.getPropertyValue("WritingModeAutomatic")
        return snap

    _reset(doc, ctx, "Wide direct formatting paragraph.")
    p0 = body_paragraphs(doc.getText())[0]
    has_wma = p0.getPropertySetInfo().hasPropertyByName("WritingModeAutomatic")
    _configure_para(p0)
    before = _snapshot(body_paragraphs(doc.getText())[0])
    assert before["ParaHyphenationMaxLeadingChars"] == 4
    assert before["ParaLastLineAdjust"] == 2
    assert before["FillTransparence"] == 40
    if has_wma:
        assert before["WritingModeAutomatic"] is False

    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    try:
        res_style = ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Heading 1", target="full_document", clear_direct="none"
        )
        assert res_style["status"] == "ok", res_style
        assert res_style["tracked"] is True
        assert "style_unreviewed" not in res_style
        reject_all_changes(doc, ctx)
        assert _snapshot(body_paragraphs(doc.getText())[0]) == before

        res_fmt = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx), properties={"ParaBottomMargin": 500}, target="full_document"
        )
        assert res_fmt["status"] == "ok", res_fmt
        assert res_fmt["tracked"] is True
        assert "style_unreviewed" not in res_fmt
        reject_all_changes(doc, ctx)
        assert _snapshot(body_paragraphs(doc.getText())[0]) == before
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_para_format_reject_restores_bitmap_fill_uno(ctx, doc):
    import os
    import tempfile
    import uno
    from com.sun.star.beans import PropertyValue
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "Bitmap fill paragraph.")
    fill_bitmap = uno.Enum("com.sun.star.drawing.FillStyle", "BITMAP")

    with tempfile.TemporaryDirectory() as tmpdir:
        png_path = os.path.join(tmpdir, "fill.png")
        with open(png_path, "wb") as f:
            f.write(_create_test_png())
        gp = ctx.getServiceManager().createInstanceWithContext("com.sun.star.graphic.GraphicProvider", ctx)
        pv = PropertyValue()
        pv.Name = "URL"
        pv.Value = uno.systemPathToFileUrl(png_path)
        graphic = gp.queryGraphic((pv,))

    p0 = body_paragraphs(doc.getText())[0]
    # Do not set FillBitmapMode: setting it or passing it to makeRedline crashes LibreOffice
    # (out-of-pool OWN_ATTR_FILLBMP_MODE id).
    p0.setPropertyValue("FillStyle", fill_bitmap)
    p0.setPropertyValue("FillBitmap", graphic)
    p0.setPropertyValue("FillBitmapStretch", False)

    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    try:
        res = ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Heading 1", target="full_document", clear_direct="none"
        )
        assert res["status"] == "ok", res
        assert res["tracked"] is True
        assert "style_unreviewed" not in res
        reject_all_changes(doc, ctx)
        p0 = body_paragraphs(doc.getText())[0]
        assert p0.getPropertyValue("FillStyle") == fill_bitmap
        assert p0.getPropertyValue("FillBitmap") is not None
        assert p0.getPropertyValue("FillBitmapStretch") is False
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_para_format_names_unrestorable_property_uno(ctx, doc):
    from plugin.writer.styles import ApplyStyle, StyleFormatText

    _reset(doc, ctx, "Unrestorable property paragraph.")
    p0 = body_paragraphs(doc.getText())[0]
    dc = p0.getPropertyValue("DropCapFormat")
    dc.Lines = 3
    dc.Count = 1
    p0.setPropertyValue("DropCapFormat", dc)
    p0.setPropertyValue("DropCapCharStyleName", "Emphasis")

    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    try:
        res_style = ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Heading 1", target="full_document", clear_direct="none"
        )
        assert res_style.get("style_unreviewed") is True
        assert "DropCapCharStyleName" in res_style.get("warning", "")
        reject_all_changes(doc, ctx)

        p0 = body_paragraphs(doc.getText())[0]
        dc = p0.getPropertyValue("DropCapFormat")
        dc.Lines = 3
        dc.Count = 1
        p0.setPropertyValue("DropCapFormat", dc)
        p0.setPropertyValue("DropCapCharStyleName", "Emphasis")

        res_fmt = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx), properties={"ParaBottomMargin": 400}, target="full_document"
        )
        assert res_fmt.get("style_unreviewed") is True
        assert "DropCapCharStyleName" in res_fmt.get("warning", "")
        reject_all_changes(doc, ctx)

        _reset(doc, ctx, "Restorable only paragraph.")
        body_paragraphs(doc.getText())[0].setPropertyValue("ParaLeftMargin", 500)
        res_clean = ApplyStyle().execute(
            native_writer_tool_context(doc, ctx), style="Heading 1", target="full_document", clear_direct="none"
        )
        assert "style_unreviewed" not in res_clean
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_para_format_character_unit_indent_is_tracked_uno(ctx, doc):
    import os
    import tempfile
    import zipfile
    import uno
    from com.sun.star.beans import PropertyValue
    from plugin.writer.styles import ApplyStyle

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        '</Relationships>'
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:pPr>'
        '<w:ind w:leftChars="200" w:left="480" w:firstLineChars="100" w:firstLine="240"/>'
        '</w:pPr><w:r><w:t>Char unit indent.</w:t></w:r></w:p></w:body></w:document>'
    )

    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    reloaded = None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            docx_path = os.path.join(tmpdir, "char_indent.docx")
            with zipfile.ZipFile(docx_path, "w") as zf:
                zf.writestr("[Content_Types].xml", content_types)
                zf.writestr("_rels/.rels", rels)
                zf.writestr("word/document.xml", document_xml)
            desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
            hv = PropertyValue()
            hv.Name = "Hidden"
            hv.Value = True
            reloaded = desktop.loadComponentFromURL(uno.systemPathToFileUrl(docx_path), "_blank", 0, (hv,))
            p0 = body_paragraphs(reloaded.getText())[0]
            info = p0.getPropertySetInfo()
            orig_left = p0.getPropertyValue("ParaLeftMargin")
            has_left_unit = info.hasPropertyByName("ParaLeftMarginUnit")
            has_first_unit = info.hasPropertyByName("ParaFirstLineIndentUnit")
            orig_left_unit = p0.getPropertyValue("ParaLeftMarginUnit") if has_left_unit else None
            orig_first_unit = p0.getPropertyValue("ParaFirstLineIndentUnit") if has_first_unit else None
            if has_left_unit:
                assert orig_left is None
                assert orig_left_unit is not None

            res = ApplyStyle().execute(
                native_writer_tool_context(reloaded, ctx), style="Heading 1", target="full_document", clear_direct="none"
            )
            assert res["status"] == "ok", res
            assert res["tracked"] is True

            reject_all_changes(reloaded, ctx)
            p0 = body_paragraphs(reloaded.getText())[0]
            assert p0.getPropertyValue("ParaLeftMargin") == orig_left
            if has_left_unit:
                assert p0.getPropertyValue("ParaLeftMarginUnit") == orig_left_unit
            if has_first_unit:
                assert p0.getPropertyValue("ParaFirstLineIndentUnit") == orig_first_unit
    finally:
        if reloaded is not None:
            reloaded.close(True)
        save({_FLAG: prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_style_format_text_register_mode_tracked_writing_mode_flagged_uno(ctx, doc):
    from plugin.writer.styles import StyleFormatText

    _reset(doc, ctx, "Register mode and writing mode.")
    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    try:
        res_reg = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx), properties={"ParaRegisterModeActive": True}, target="full_document"
        )
        assert res_reg["status"] == "ok", res_reg
        assert res_reg["tracked"] is True
        assert "style_unreviewed" not in res_reg
        reject_all_changes(doc, ctx)
        assert body_paragraphs(doc.getText())[0].getPropertyValue("ParaRegisterModeActive") is False

        res_wm = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx), properties={"WritingMode": 1}, target="full_document"
        )
        assert res_wm["status"] == "ok", res_wm
        assert res_wm["tracked"] is False
        assert res_wm.get("style_unreviewed") is True
        assert "WritingMode" in res_wm.get("warning", "")
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_style_format_text_para_back_color_tracked_reject_and_accept_uno(ctx, doc):
    import uno
    from plugin.writer.styles import StyleFormatText

    _reset(doc, ctx, "Background tracked.")
    p0 = body_paragraphs(doc.getText())[0]
    p0.setPropertyValue("FillColor", 0x112233)
    p0.setPropertyValue("FillStyle", uno.Enum("com.sun.star.drawing.FillStyle", "SOLID"))
    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    try:
        res = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx), properties={"ParaBackColor": "#FFE4C4"}, target="full_document"
        )
        assert res["status"] == "ok", res
        assert res["tracked"] is True
        assert "style_unreviewed" not in res
        assert int(body_paragraphs(doc.getText())[0].getPropertyValue("ParaBackColor")) == 0xFFE4C4

        reject_all_changes(doc, ctx)
        assert int(body_paragraphs(doc.getText())[0].getPropertyValue("ParaBackColor")) == 0x112233

        res2 = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx), properties={"ParaBackColor": "#FFE4C4"}, target="full_document"
        )
        assert res2["status"] == "ok", res2
        accept_all_changes(doc, ctx)
        assert int(body_paragraphs(doc.getText())[0].getPropertyValue("ParaBackColor")) == 0xFFE4C4
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_style_format_text_writing_mode_automatic_tracked_and_rejectable_uno(ctx, doc):
    from plugin.tests.testing_utils import libreoffice_version
    from plugin.writer.styles import StyleFormatText

    _reset(doc, ctx, "WritingModeAutomatic check.")
    p0 = body_paragraphs(doc.getText())[0]
    if not p0.getPropertySetInfo().hasPropertyByName("WritingModeAutomatic"):
        return
    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    try:
        res = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx), properties={"WritingModeAutomatic": False}, target="full_document"
        )
        assert res["status"] == "ok", res
        assert res["tracked"] is True
        assert "style_unreviewed" not in res
        assert body_paragraphs(doc.getText())[0].getPropertyValue("WritingModeAutomatic") is False

        reject_all_changes(doc, ctx)
        # Before LibreOffice 26.8, lcl_RstAttr (sw/source/core/doc/docfmt.cxx) preserves
        # RES_PARATR_AUTOFRAMEDIR when false across ResetAttrs.
        if libreoffice_version(ctx) >= (26, 8):
            p_after = body_paragraphs(doc.getText())[0]
            assert p_after.getPropertyValue("WritingModeAutomatic") is True
            assert p_after.getPropertyState("WritingModeAutomatic").value == "DEFAULT_VALUE"
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})


@native_test
@with_native_doc("writer")
def test_string_skipping_redline_inside_content_control_uno(ctx, doc):
    _reset(doc, ctx, "xx CTRL yy")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.goRight(3, False)
    cur.goRight(4, True)
    ctrl = doc.createInstance("com.sun.star.text.ContentControl")
    text.insertTextContent(cur, ctrl, True)

    doc.setPropertyValue("RecordChanges", True)
    try:
        edit_cur = text.createTextCursor()
        edit_cur.gotoStart(False)
        edit_cur.goRight(4, False)  # past "xx " + control start mark
        edit_cur.goRight(4, True)
        edit_cur.setString("NEW")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    rng = text.createTextCursor()
    rng.gotoStart(False)
    rng.gotoEnd(True)
    accepted = _string_skipping_redline(rng, "Delete")
    rejected = _string_skipping_redline(rng, "Insert")
    assert accepted != rejected
    assert "NEW" in accepted
    assert "CTRL" in rejected


@native_test
@with_native_doc("writer")
def test_style_format_text_caret_records_nothing_uno(ctx, doc):
    from plugin.writer.styles import StyleFormatText

    _reset(doc, ctx, "Caret format check.")
    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    try:
        res_caret = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx), properties={"CharWeight": 150}, target="end"
        )
        assert res_caret["status"] == "ok", res_caret
        assert res_caret["tracked"] is False
        assert "pending_review" not in res_caret
        assert list_redlines(doc) == []

        res_reset_caret = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx), reset=["CharWeight"], target="end"
        )
        assert res_reset_caret["status"] == "ok", res_reset_caret
        assert res_reset_caret["tracked"] is False
        assert "pending_review" not in res_reset_caret
        assert list_redlines(doc) == []

        res_search = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx),
            properties={"CharWeight": 150},
            target="search",
            old_content="format",
        )
        assert res_search["status"] == "ok", res_search
        assert res_search["pending_review"] is True
        assert [r["type"] for r in list_redlines(doc)] == ["Format"]
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})


@native_test
@with_native_doc("writer")
def test_user_recording_with_review_off_reports_pending_uno(ctx, doc):
    from plugin.writer.styles import ApplyStyle, StyleFormatText

    _reset(doc, ctx, "User recording check.")
    prev = get_config(_FLAG)
    save({_FLAG: "off"})
    doc.setPropertyValue("RecordChanges", True)
    try:
        res_fmt = StyleFormatText().execute(
            native_writer_tool_context(doc, ctx),
            properties={"CharWeight": 150},
            target="search",
            old_content="recording",
        )
        assert res_fmt["status"] == "ok", res_fmt
        assert res_fmt["pending_review"] is True
        assert "review_mode" not in res_fmt
        assert [r["type"] for r in list_redlines(doc)] == ["Format"]

        res_para = ApplyStyle().execute(
            native_writer_tool_context(doc, ctx),
            style="Heading 1",
            family="ParagraphStyles",
            target="search",
            old_content="User",
        )
        assert res_para["status"] == "ok", res_para
        assert res_para["pending_review"] is True
        assert "review_mode" not in res_para
        assert sorted(list_redline_types(doc)) == ["Format", "ParagraphFormat"]
    finally:
        doc.setPropertyValue("RecordChanges", False)
        save({_FLAG: prev})
