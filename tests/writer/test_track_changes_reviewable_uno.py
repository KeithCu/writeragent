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
# flag, and the apply_document_content tool wiring end to end (incl. never block-waiting on the
# main thread). The EditReviewSession itself is covered in test_edit_review_uno.py; the inline
# review UI helpers in test_inline_review_uno.py.
import contextlib

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.writer.edit_review import WriterStreamedRewriteSession, WriterStreamedAppendSession
from plugin.writer.content import ApplyDocumentContent
import plugin.writer.edit_review as _content
from plugin.writer.edit_review import EditReviewSession, get_agent_edit_review_mode
from plugin.framework.config import set_config, get_config
import plugin.writer.format as fmt
import plugin.writer.html_import as html_import

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
        _accept_all(doc, ctx)


@contextlib.contextmanager
def _recording(doc):
    """Record the wrapped edit as tracked changes (the primitive the session builds on)."""
    doc.setPropertyValue("RecordChanges", True)
    try:
        yield
    finally:
        doc.setPropertyValue("RecordChanges", False)


def _redlines(doc):
    out = []
    e = doc.getRedlines().createEnumeration()
    while e.hasMoreElements():
        rl = e.nextElement()
        entry = {"type": str(rl.getPropertyValue("RedlineType"))}
        for prop in ("RedlineComment", "RedlineAuthor"):
            try:
                entry[prop] = str(rl.getPropertyValue(prop))
            except Exception:
                entry[prop] = ""
        out.append(entry)
    return out


def _redline_types(doc):
    return [r["type"] for r in _redlines(doc)]


def _accept_all(doc, ctx):
    helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    frame = doc.getCurrentController().getFrame()
    helper.executeDispatch(frame, ".uno:AcceptAllTrackedChanges", "", 0, ())


def _reject_all(doc, ctx):
    helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    frame = doc.getCurrentController().getFrame()
    helper.executeDispatch(frame, ".uno:RejectAllTrackedChanges", "", 0, ())


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


def _tool_ctx(doc, ctx):
    return TestingFactory.create_context(doc=doc, ctx=ctx, env="native")


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
        fmt.replace_preserving_format(doc, _para_range(doc), "This clause is critically important.", ctx)
    rl = _redline_types(doc)
    assert "Insert" in rl and "Delete" in rl, "plain replace under recording must yield Insert+Delete, got %r" % rl
    _reject_all(doc, ctx)
    assert _para_text(doc) == "This clause is important.", "reject must restore the original exactly, got %r" % _para_text(doc)


@native_test
@with_native_doc("writer")
def test_search_replace_plain_text_accept_keeps_only_new_uno(ctx, doc):
    """Accept must leave ONLY the new text -- the old must be a real tracked deletion that gets removed."""
    _reset(doc, ctx, "This clause is important.")
    with _recording(doc):
        fmt.replace_preserving_format(doc, _para_range(doc), "This clause is critically important.", ctx)
    _accept_all(doc, ctx)
    assert _para_text(doc) == "This clause is critically important.", "accept must keep only the new text, got %r" % _para_text(doc)


@native_test
@with_native_doc("writer")
def test_search_replace_inline_markup_no_format_redline_uno(ctx, doc):
    """Inline markup replace must record a Delete (not a Format) for the old text. A Format redline
    survives Accept, so the doc would keep BOTH the old and the new text."""
    _reset(doc, ctx, "This clause is important.")
    with _recording(doc):
        fmt.replace_single_range_with_content(
            doc, _para_range(doc), "<span>This clause is critically important.</span>", ctx, None)
    rl = _redline_types(doc)
    assert "Format" not in rl, "inline replace must not leave a Format redline (it keeps old text on accept), got %r" % rl
    assert "Delete" in rl and "Insert" in rl, "inline replace must be a clean Delete+Insert, got %r" % rl
    _accept_all(doc, ctx)
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
            doc, _para_range(doc), "<span>Powertrain selection</span>", ctx, None)
    _accept_all(doc, ctx)
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
    rl = _redline_types(doc)
    assert "Insert" in rl and "Delete" in rl, "full replace under recording must yield Insert+Delete redlines, got %r" % rl
    _reject_all(doc, ctx)
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
    assert _redline_types(doc), "flag on must collapse the streamed edit into a redline"
    assert doc.getPropertyValue("RecordChanges") is False, "recording restored to OFF"
    _reject_all(doc, ctx)
    assert _para_text(doc) == "Sentence to rewrite.", "reject restores the original sentence"


@native_test
@with_native_doc("writer")
def test_session_flag_off_user_off_no_redline_uno(ctx, doc):
    _session_run(doc, track_reviewable=False, prior_recording=False)
    assert _redline_types(doc) == [], "flag off + user off must not create a redline"
    assert _para_text(doc) == "Rewritten sentence.", "edit applied directly"


@native_test
@with_native_doc("writer")
def test_session_prior_recording_on_preserved_uno(ctx, doc):
    _session_run(doc, track_reviewable=False, prior_recording=True)
    assert _redline_types(doc), "user-on tracking must still collapse to a redline"
    assert doc.getPropertyValue("RecordChanges") is True, "prior ON state preserved"
    doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_streamed_edit_tagged_as_agent_change_uno(ctx, doc):
    """The streamed rewrite collapses to one tracked change; it must also be TAGGED with a
    session token so the review tooling recognizes it as an agent change."""
    _session_run(doc, track_reviewable=True, prior_recording=False)
    comments = [r["RedlineComment"] for r in _redlines(doc)]
    assert comments and all(c.startswith("wa-review:") for c in comments), \
        "streamed edit redlines must carry a session token, got %r" % comments
    assert len(set(comments)) == 1, "the streamed edit is ONE agent change (one token), got %r" % comments
    authors = {r["RedlineAuthor"] for r in _redlines(doc)}
    assert authors == {"WriterAgent"}, "the streamed change is authored as the agent, got %r" % authors
    _reject_all(doc, ctx)


# --- attribution: insertions vs deletions get different authors (-> 2 by-author colors) ---

@native_test
@with_native_doc("writer")
def test_split_authors_insert_vs_delete_uno(ctx, doc):
    """A replace records its Insert and Delete under DIFFERENT authors, so LibreOffice's
    by-author redline coloring shows new vs removed text in two distinct colors."""
    _reset(doc, ctx, "Old clause body here.")
    prev = get_config(_FLAG)
    set_config(_FLAG, "record")
    try:
        res = ApplyDocumentContent().execute(
            _tool_ctx(doc, ctx), target="search", old_content="Old clause body here.", content=["New clause body here."])
        assert res.get("status") == "ok", res
        by_type = {r["type"]: r["RedlineAuthor"] for r in _redlines(doc)}
        assert by_type.get("Insert") == "WriterAgent", "insertions authored WriterAgent: %r" % by_type
        assert by_type.get("Delete") == "WriterAgent (deletions)", "deletions authored distinctly: %r" % by_type
    finally:
        set_config(_FLAG, prev)
        _reject_all(doc, ctx)


@native_test
@with_native_doc("writer")
def test_split_authors_whole_block_on_two_colors_uno(ctx, doc):
    """The consistency fix: a WHOLE-BLOCK agent edit also authors its Delete distinctly (two colors),
    not just the surgical path. Before, whole-block was authored as one (one color). Threshold 0.0
    forces the whole-block path; accept must still reconstruct exactly the new text (atomicity)."""
    _reset(doc, ctx, "Old clause body here.")
    prev = get_config(_FLAG)
    prev_split, prev_thresh = _content._SPLIT_AUTHOR_COLORS, _content._WORD_DIFF_THRESHOLD
    set_config(_FLAG, "record")
    _content._SPLIT_AUTHOR_COLORS = True
    _content._WORD_DIFF_THRESHOLD = 0.0  # any change -> ONE whole block (not surgical)
    try:
        res = ApplyDocumentContent().execute(
            _tool_ctx(doc, ctx), target="search", old_content="Old clause body here.", content=["New clause body here."])
        assert res.get("status") == "ok", res
        by_type = {r["type"]: r["RedlineAuthor"] for r in _redlines(doc)}
        assert by_type.get("Insert") == "WriterAgent", "insert authored WriterAgent: %r" % by_type
        assert by_type.get("Delete") == "WriterAgent (deletions)", \
            "whole-block deletion must be authored distinctly (two colors): %r" % by_type
        _accept_all(doc, ctx)
        assert _para_text(doc) == "New clause body here.", \
            "accept must reconstruct exactly the new text (whole-block atomicity), got %r" % _para_text(doc)
    finally:
        set_config(_FLAG, prev)
        _content._SPLIT_AUTHOR_COLORS, _content._WORD_DIFF_THRESHOLD = prev_split, prev_thresh
        if len(doc.getRedlines()):
            _reject_all(doc, ctx)


@native_test
@with_native_doc("writer")
def test_split_authors_whole_block_off_one_color_uno(ctx, doc):
    """Toggle OFF: a whole-block edit's Delete and Insert share ONE author (one color), and the edit
    stays atomic -- accept reconstructs exactly the new text."""
    _reset(doc, ctx, "Old clause body here.")
    prev = get_config(_FLAG)
    prev_split, prev_thresh = _content._SPLIT_AUTHOR_COLORS, _content._WORD_DIFF_THRESHOLD
    set_config(_FLAG, "record")
    _content._SPLIT_AUTHOR_COLORS = False
    _content._WORD_DIFF_THRESHOLD = 0.0  # force whole-block
    try:
        res = ApplyDocumentContent().execute(
            _tool_ctx(doc, ctx), target="search", old_content="Old clause body here.", content=["New clause body here."])
        assert res.get("status") == "ok", res
        authors = {r["RedlineAuthor"] for r in _redlines(doc)}
        assert authors == {"WriterAgent"}, "toggle off -> one author / one color, got %r" % authors
        _accept_all(doc, ctx)
        assert _para_text(doc) == "New clause body here.", \
            "accept must reconstruct exactly the new text, got %r" % _para_text(doc)
    finally:
        set_config(_FLAG, prev)
        _content._SPLIT_AUTHOR_COLORS, _content._WORD_DIFF_THRESHOLD = prev_split, prev_thresh
        if len(doc.getRedlines()):
            _reject_all(doc, ctx)


@native_test
@with_native_doc("writer")
def test_html_import_failure_rolls_back_in_review_mode_uno(ctx, doc):
    """Atomicity for the HTML/import path: in record mode replace_full_document does setString('')
    then imports HTML. If the import throws AFTER the delete, the whole edit is rolled back -- the
    document keeps its original text with no stranded tracked deletion, and no agent change lands."""
    _reset(doc, ctx, "Original body text to keep.")
    prev = get_config(_FLAG)
    set_config(_FLAG, "record")
    real = html_import._insert_mixed_or_plain_html
    real_fmt = fmt._insert_mixed_or_plain_html

    def _boom(*a, **k):
        raise RuntimeError("simulated HTML import failure")

    html_import._insert_mixed_or_plain_html = _boom
    fmt._insert_mixed_or_plain_html = _boom
    try:
        res = None
        try:
            res = ApplyDocumentContent().execute(_tool_ctx(doc, ctx), target="full_document", content=["<p>New body.</p>"])
        except Exception:
            res = {"status": "error", "raised": True}
        assert res.get("status") == "error", "the failed import must surface as an error: %r" % res
        assert _para_text(doc) == "Original body text to keep.", \
            "document must be restored (no stranded deletion), got %r" % _para_text(doc)
        assert _redline_types(doc) == [], \
            "no tracked change may survive the rolled-back edit, got %r" % _redline_types(doc)
    finally:
        html_import._insert_mixed_or_plain_html = real
        fmt._insert_mixed_or_plain_html = real_fmt
        set_config(_FLAG, prev)
        if len(doc.getRedlines()):
            _reject_all(doc, ctx)


@native_test
@with_native_doc("writer")
def test_split_authors_surgical_off_one_color_uno(ctx, doc):
    """Toggle OFF collapses the SURGICAL path to one author too (consistency both ways)."""
    _reset(doc, ctx, "Old clause body here.")
    prev = get_config(_FLAG)
    prev_split = _content._SPLIT_AUTHOR_COLORS
    set_config(_FLAG, "record")
    _content._SPLIT_AUTHOR_COLORS = False  # default threshold -> small change takes the surgical path
    try:
        res = ApplyDocumentContent().execute(
            _tool_ctx(doc, ctx), target="search", old_content="Old clause body here.", content=["New clause body here."])
        assert res.get("status") == "ok", res
        authors = {r["RedlineAuthor"] for r in _redlines(doc)}
        assert authors == {"WriterAgent"}, "surgical toggle off -> one author, got %r" % authors
        _accept_all(doc, ctx)
        assert _para_text(doc) == "New clause body here.", "accept must yield the new text, got %r" % _para_text(doc)
    finally:
        set_config(_FLAG, prev)
        _content._SPLIT_AUTHOR_COLORS = prev_split
        if len(doc.getRedlines()):
            _reject_all(doc, ctx)


# --- the apply_document_content tool end to end (config read + session wiring) -----------

@native_test
@with_native_doc("writer")
def test_apply_document_content_tool_tracks_when_config_on_uno(ctx, doc):
    """Real tool path: content.py reads doc.agent_edit_review_mode=record and records the
    edit as reviewable redlines; reject restores. (Exercises the config read, which the
    primitive-level tests above do not.)"""
    _reset(doc, ctx, "Old tool body.")
    prev = get_config(_FLAG)
    set_config(_FLAG, "record")
    try:
        assert get_agent_edit_review_mode(ctx) == "record", "mode should read record (test-infra sanity)"
        res = ApplyDocumentContent().execute(_tool_ctx(doc, ctx), target="full_document", content=["<p>Tool new body.</p>"])
        assert res.get("status") == "ok", res
        rl = _redline_types(doc)
        assert "Insert" in rl and "Delete" in rl, "tool path with flag on must create redlines, got %r" % rl
        _reject_all(doc, ctx)
        assert "Old tool body." in _para_text(doc), "reject must restore the original, got %r" % _para_text(doc)
    finally:
        set_config(_FLAG, prev)  # restore the dev's prior value, not a hardcoded False


@native_test
@with_native_doc("writer")
def test_apply_document_content_tool_untracked_when_config_off_uno(ctx, doc):
    """Mode off (the default): the tool applies directly, no redline."""
    _reset(doc, ctx, "Old tool body.")
    prev = get_config(_FLAG)
    set_config(_FLAG, "off")
    try:
        assert get_agent_edit_review_mode(ctx) == "off"
        res = ApplyDocumentContent().execute(_tool_ctx(doc, ctx), target="full_document", content=["<p>Tool new body.</p>"])
        assert res.get("status") == "ok", res
        assert _redline_types(doc) == [], "flag off must not create redlines"
        assert "Tool new body." in _para_text(doc)
    finally:
        set_config(_FLAG, prev)


@native_test
@with_native_doc("writer")
def test_apply_document_content_tool_tags_changes_with_session_tokens_uno(ctx, doc):
    """Every redline of a flag-on edit carries a wa-review:<session>:<n> token (so completion
    and outcome detection key on this session only), and a replace-all yields one tagged change
    PER MATCH."""
    _reset(doc, ctx, "Tag alpha here. Tag alpha there.")
    prev = get_config(_FLAG)
    set_config(_FLAG, "record")
    try:
        res = ApplyDocumentContent().execute(
            _tool_ctx(doc, ctx), target="search", old_content="Tag alpha", content=["Tag beta"], all_matches=True)
        assert res.get("status") == "ok", res
        assert res.get("replaced_count") == 2, res
        comments = [r["RedlineComment"] for r in _redlines(doc)]
        assert comments and all(c.startswith("wa-review:") for c in comments), \
            "all redlines must carry session tokens, got %r" % comments
        assert len(set(comments)) == 2, "two matches -> two distinct per-change tokens, got %r" % comments
    finally:
        set_config(_FLAG, prev)
        _reject_all(doc, ctx)


@native_test
@with_native_doc("writer")
def test_tool_never_blocks_on_main_thread_even_with_wait_flag_uno(ctx, doc):
    """doc.agent_edit_review_mode=wait: from the MAIN thread the tool must NOT block-wait (the
    user could never click accept/reject if the UI thread were parked) -- it edits, records
    (wait implies recording), and returns without a review payload. The blocking wait only
    happens on a background MCP/chat thread."""
    _reset(doc, ctx, "Guard body text.")
    prev_mode = get_config(_FLAG)
    set_config(_FLAG, "wait")
    try:
        res = ApplyDocumentContent().execute(_tool_ctx(doc, ctx), target="end", content=["<p>Guard addition.</p>"])
        assert res.get("status") == "ok", res
        assert "review" not in res, "main-thread call must not block-wait: %r" % res
        assert _redline_types(doc), "wait mode must imply recording (redlines expected)"
    finally:
        set_config(_FLAG, prev_mode)
        _reject_all(doc, ctx)


# --- style changes are not redline-trackable; the tool says so under review mode ---------

@native_test
@with_native_doc("writer")
def test_apply_style_flags_unreviewed_when_review_on_uno(ctx, doc):
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "Heading target text.")
    prev = get_config(_FLAG)
    set_config(_FLAG, "record")
    try:
        res = ApplyStyle().execute(_tool_ctx(doc, ctx), style="Heading 2", family="ParagraphStyles", target="full_document")
        assert res.get("status") == "ok", res
        assert res.get("style_unreviewed") is True, "style edit must flag unreviewed under review mode: %r" % res
        assert _redline_types(doc) == [], "a paragraph-style change creates no redline"
    finally:
        set_config(_FLAG, prev)


@native_test
@with_native_doc("writer")
def test_apply_style_no_unreviewed_flag_when_review_off_uno(ctx, doc):
    from plugin.writer.styles import ApplyStyle

    _reset(doc, ctx, "Heading target text.")
    prev = get_config(_FLAG)
    set_config(_FLAG, "off")
    try:
        res = ApplyStyle().execute(_tool_ctx(doc, ctx), style="Heading 2", family="ParagraphStyles", target="full_document")
        assert res.get("status") == "ok", res
        assert "style_unreviewed" not in res, "no unreviewed flag when review mode off: %r" % res
    finally:
        set_config(_FLAG, prev)


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
    rls = _redlines(doc)
    assert any(r["type"] == "Insert" for r in rls), "insert must create an Insert redline, got %r" % rls
    assert all(r["RedlineComment"].startswith("wa-review:") for r in rls), "tagged: %r" % rls
    assert doc.getPropertyValue("RecordChanges") is False, "recording restored to OFF"
    _reject_all(doc, ctx)


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
    rls = _redlines(doc)
    assert rls, "review mode must collapse the appended continuation into a redline"
    assert all(r["type"] == "Insert" for r in rls), \
        "extend appends -> Insert only, the original keeps no Delete redline, got %r" % [r["type"] for r in rls]
    comments = [r["RedlineComment"] for r in rls]
    assert comments and all(c.startswith("wa-review:") for c in comments), \
        "appended redline must carry a session token, got %r" % comments
    assert len(set(comments)) == 1, "the append is ONE agent change (one token), got %r" % comments
    assert doc.getPropertyValue("ShowChanges") is True, "review mode forces markup visible"
    assert doc.getPropertyValue("RecordChanges") is False, "recording restored to OFF"
    _reject_all(doc, ctx)
    assert _para_text(doc) == "Original sentence.", "reject removes only the appended continuation"


@native_test
@with_native_doc("writer")
def test_streamed_append_flag_off_no_redline_uno(ctx, doc):
    """Flag off + user not recording: extend appends directly, no redline (today's behavior)."""
    _append_run(doc, ctx, track_reviewable=False, prior_recording=False)
    assert _redline_types(doc) == [], "flag off + user off must not create a redline"
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
    set_config(_FLAG, "record")
    try:
        res = StyleUpdate().execute(
            _tool_ctx(doc, ctx), style="Standard", family="ParagraphStyles",
            property_updates={"CharWeight": 150.0})  # 150.0 == BOLD
        assert res.get("status") == "ok", res
        assert res.get("style_unreviewed") is True, "update_style must flag unreviewed under review mode: %r" % res
        assert _redline_types(doc) == [], "a style change creates no redline"
    finally:
        set_config(_FLAG, prev)


@native_test
@with_native_doc("writer")
def test_is_async_true_on_background_thread_when_review_toggled_off_uno(ctx, doc):
    """If review is toggled OFF after the async snapshot dispatched the tool to a worker thread,
    is_async() must still report True there so execute_safe's main-thread guard won't reject it
    (execute() then marshals the wait-free edit to the main thread)."""
    import threading

    prev = get_config(_FLAG)
    set_config(_FLAG, "off")
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
        set_config(_FLAG, prev)


@native_test
@with_native_doc("writer")
def test_bookmarks_cleaned_when_edit_raises_midway_uno(ctx, doc):
    """#4: if _execute_edit raises AFTER a change was anchored (a replace-all that fails on a later
    match, having anchored the first), execute() must still release the session's wa_review_*
    anchor bookmarks via the session_sink -- none may leak in the document."""
    from plugin.writer.content import ApplyDocumentContent
    from plugin.writer.edit_review import EditReviewSession

    _reset(doc, ctx, "Anchor target paragraph.")
    prev = get_config(_FLAG)
    set_config(_FLAG, "record")

    class _BoomTool(ApplyDocumentContent):
        def _execute_edit(self, tctx, session_sink=None, **kwargs):
            session = EditReviewSession(tctx.doc, tctx.ctx, enabled=True)
            if session_sink is not None:
                session_sink.append(session)
            with session:
                session.record_mutation(
                    lambda: fmt.insert_content_at_position(tctx.doc, tctx.ctx, "<p>Inserted.</p>", "end"))
            raise RuntimeError("boom after anchoring the first change")

    try:
        raised = False
        try:
            _BoomTool().execute(_tool_ctx(doc, ctx))
        except RuntimeError:
            raised = True
        assert raised, "the mid-edit failure must propagate to the caller"
        leaked = [n for n in doc.getBookmarks().getElementNames() if n.startswith("wa_review_")]
        assert leaked == [], "anchor bookmarks must be cleaned up on a mid-edit failure, leaked: %r" % leaked
    finally:
        set_config(_FLAG, prev)
        if len(doc.getRedlines()):
            _reject_all(doc, ctx)


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
    set_config(_FLAG, "wait")
    set_config("doc.edit_review_timeout", 0)

    try:
        tool = ApplyDocumentContent()
        res = {}
        def _run_tool():
            # Must run on a background thread so the tool is considered async and enters wait path
            tool_ctx = _tool_ctx(doc, ctx)
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
        set_config(_FLAG, prev_mode)
        set_config("doc.edit_review_timeout", prev_timeout)
        _reject_all(doc, ctx)
