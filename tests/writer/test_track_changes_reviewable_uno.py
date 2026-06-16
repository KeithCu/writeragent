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

from plugin.testing_runner import native_test, setup, teardown
from plugin.tests.testing_utils import TestingFactory
from plugin.doc.document_helpers import WriterStreamedRewriteSession, WriterStreamedAppendSession
from plugin.writer.content import ApplyDocumentContent
from plugin.writer.edit_review import EditReviewSession, get_agent_edit_review_mode
from plugin.framework.config import set_config, get_config
import plugin.writer.format as fmt

_FLAG = "doc.agent_edit_review_mode"

_doc = None
_ctx = None


@setup
def my_setup(ctx):
    global _doc, _ctx
    _ctx = ctx
    _doc = TestingFactory.create_native_doc(ctx, doc_type="writer", hidden=True)


@teardown
def my_teardown(ctx):
    global _doc
    if _doc:
        _doc.close(True)
    _doc = None


def _reset(text_str="Original body text."):
    text = _doc.getText()
    _doc.setPropertyValue("RecordChanges", False)
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, text_str, False)
    # Clear any redlines accumulated by a prior test (accept-all leaves a clean doc).
    if len(_doc.getRedlines()):
        _accept_all()


@contextlib.contextmanager
def _recording():
    """Record the wrapped edit as tracked changes (the primitive the session builds on)."""
    _doc.setPropertyValue("RecordChanges", True)
    try:
        yield
    finally:
        _doc.setPropertyValue("RecordChanges", False)


def _redlines():
    out = []
    e = _doc.getRedlines().createEnumeration()
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


def _redline_types():
    return [r["type"] for r in _redlines()]


def _accept_all():
    helper = _ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", _ctx)
    frame = _doc.getCurrentController().getFrame()
    helper.executeDispatch(frame, ".uno:AcceptAllTrackedChanges", "", 0, ())


def _reject_all():
    helper = _ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", _ctx)
    frame = _doc.getCurrentController().getFrame()
    helper.executeDispatch(frame, ".uno:RejectAllTrackedChanges", "", 0, ())


def _para_text():
    cur = _doc.getText().createTextCursor()
    cur.gotoStart(False)
    cur.gotoEndOfParagraph(True)
    return cur.getString()


def _para_range():
    text = _doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEndOfParagraph(True)
    return cur


def _tool_ctx():
    return TestingFactory.create_context(doc=_doc, ctx=_ctx, env="native")


# --- replace primitives must be Track-Changes-safe: a clean Delete+Insert, not a char-by-char
# --- mess (plain text) nor a Format redline that keeps the old text on Accept (inline markup) ---

@native_test
def test_search_replace_plain_text_clean_redline_uno():
    """Plain-text replace_preserving_format under recording must NOT diff char-by-char (which
    records a redline per changed character -> a scrambled, un-reviewable redline). It must be a
    single clean Delete+Insert, so reject restores the original exactly."""
    _reset("This clause is important.")
    with _recording():
        fmt.replace_preserving_format(_doc, _para_range(), "This clause is critically important.", _ctx)
    rl = _redline_types()
    assert "Insert" in rl and "Delete" in rl, "plain replace under recording must yield Insert+Delete, got %r" % rl
    _reject_all()
    assert _para_text() == "This clause is important.", "reject must restore the original exactly, got %r" % _para_text()


@native_test
def test_search_replace_plain_text_accept_keeps_only_new_uno():
    """Accept must leave ONLY the new text -- the old must be a real tracked deletion that gets removed."""
    _reset("This clause is important.")
    with _recording():
        fmt.replace_preserving_format(_doc, _para_range(), "This clause is critically important.", _ctx)
    _accept_all()
    assert _para_text() == "This clause is critically important.", "accept must keep only the new text, got %r" % _para_text()


@native_test
def test_search_replace_inline_markup_no_format_redline_uno():
    """Inline markup replace must record a Delete (not a Format) for the old text. A Format redline
    survives Accept, so the doc would keep BOTH the old and the new text."""
    _reset("This clause is important.")
    with _recording():
        fmt.replace_single_range_with_content(
            _doc, _para_range(), "<span>This clause is critically important.</span>", _ctx, None)
    rl = _redline_types()
    assert "Format" not in rl, "inline replace must not leave a Format redline (it keeps old text on accept), got %r" % rl
    assert "Delete" in rl and "Insert" in rl, "inline replace must be a clean Delete+Insert, got %r" % rl
    _accept_all()
    assert _para_text() == "This clause is critically important.", "accept must keep ONLY the new text, got %r" % _para_text()


@native_test
def test_search_replace_inline_markup_heading_style_preserved_uno():
    """Skipping the paragraph-style restore while recording must NOT demote a heading: the inline
    HTML import keeps the style, and the edit stays a clean reviewable Delete+Insert."""
    _reset("Engine selection")
    _para_range().setPropertyValue("ParaStyleName", "Heading 3")
    with _recording():
        fmt.replace_single_range_with_content(
            _doc, _para_range(), "<span>Powertrain selection</span>", _ctx, None)
    _accept_all()
    assert _para_text() == "Powertrain selection", "accept must keep only the new heading text, got %r" % _para_text()
    assert _para_range().getPropertyValue("ParaStyleName") == "Heading 3", \
        "heading style must be preserved, got %r" % _para_range().getPropertyValue("ParaStyleName")


@native_test
def test_full_replace_tracked_and_reject_restores_uno():
    """replace_full_document under recording -> reviewable redlines; reject restores the body."""
    _reset("Old document body.")
    with _recording():
        fmt.replace_full_document(_doc, _ctx, "<p>Brand new body.</p>")
    rl = _redline_types()
    assert "Insert" in rl and "Delete" in rl, "full replace under recording must yield Insert+Delete redlines, got %r" % rl
    _reject_all()
    assert "Old document body." in _para_text(), "reject must restore the original body, got %r" % _para_text()


# --- streamed rewrite session (edit-selection path) --------------------------------------

def _session_run(track_reviewable, prior_recording):
    _reset("Sentence to rewrite.")
    _doc.setPropertyValue("RecordChanges", prior_recording)
    text = _doc.getText()
    rng = text.createTextCursor()
    rng.gotoStart(False)
    rng.gotoEndOfParagraph(True)
    session = WriterStreamedRewriteSession(_doc, rng, "Sentence to rewrite.", track_reviewable=track_reviewable)
    session.append_chunk("Rewritten sentence.")
    session.finish()


@native_test
def test_session_flag_on_user_off_creates_redline_restores_off_uno():
    _session_run(track_reviewable=True, prior_recording=False)
    assert _redline_types(), "flag on must collapse the streamed edit into a redline"
    assert _doc.getPropertyValue("RecordChanges") is False, "recording restored to OFF"
    _reject_all()
    assert _para_text() == "Sentence to rewrite.", "reject restores the original sentence"


@native_test
def test_session_flag_off_user_off_no_redline_uno():
    _session_run(track_reviewable=False, prior_recording=False)
    assert _redline_types() == [], "flag off + user off must not create a redline"
    assert _para_text() == "Rewritten sentence.", "edit applied directly"


@native_test
def test_session_prior_recording_on_preserved_uno():
    _session_run(track_reviewable=False, prior_recording=True)
    assert _redline_types(), "user-on tracking must still collapse to a redline"
    assert _doc.getPropertyValue("RecordChanges") is True, "prior ON state preserved"
    _doc.setPropertyValue("RecordChanges", False)


@native_test
def test_streamed_edit_tagged_as_agent_change_uno():
    """The streamed rewrite collapses to one tracked change; it must also be TAGGED with a
    session token so the review tooling recognizes it as an agent change."""
    _session_run(track_reviewable=True, prior_recording=False)
    comments = [r["RedlineComment"] for r in _redlines()]
    assert comments and all(c.startswith("wa-review:") for c in comments), \
        "streamed edit redlines must carry a session token, got %r" % comments
    assert len(set(comments)) == 1, "the streamed edit is ONE agent change (one token), got %r" % comments
    authors = {r["RedlineAuthor"] for r in _redlines()}
    assert authors == {"WriterAgent"}, "the streamed change is authored as the agent, got %r" % authors
    _reject_all()


# --- attribution: insertions vs deletions get different authors (-> 2 by-author colors) ---

@native_test
def test_split_authors_insert_vs_delete_uno():
    """A replace records its Insert and Delete under DIFFERENT authors, so LibreOffice's
    by-author redline coloring shows new vs removed text in two distinct colors."""
    _reset("Old clause body here.")
    prev = get_config(_ctx, _FLAG)
    set_config(_ctx, _FLAG, "record")
    try:
        res = ApplyDocumentContent().execute(
            _tool_ctx(), target="search", old_content="Old clause body here.", content=["New clause body here."])
        assert res.get("status") == "ok", res
        by_type = {r["type"]: r["RedlineAuthor"] for r in _redlines()}
        assert by_type.get("Insert") == "WriterAgent", "insertions authored WriterAgent: %r" % by_type
        assert by_type.get("Delete") == "WriterAgent (deletions)", "deletions authored distinctly: %r" % by_type
    finally:
        set_config(_ctx, _FLAG, prev)
        _reject_all()


# --- the apply_document_content tool end to end (config read + session wiring) -----------

@native_test
def test_apply_document_content_tool_tracks_when_config_on_uno():
    """Real tool path: content.py reads doc.agent_edit_review_mode=record and records the
    edit as reviewable redlines; reject restores. (Exercises the config read, which the
    primitive-level tests above do not.)"""
    _reset("Old tool body.")
    prev = get_config(_ctx, _FLAG)
    set_config(_ctx, _FLAG, "record")
    try:
        assert get_agent_edit_review_mode(_ctx) == "record", "mode should read record (test-infra sanity)"
        res = ApplyDocumentContent().execute(_tool_ctx(), target="full_document", content=["<p>Tool new body.</p>"])
        assert res.get("status") == "ok", res
        rl = _redline_types()
        assert "Insert" in rl and "Delete" in rl, "tool path with flag on must create redlines, got %r" % rl
        _reject_all()
        assert "Old tool body." in _para_text(), "reject must restore the original, got %r" % _para_text()
    finally:
        set_config(_ctx, _FLAG, prev)  # restore the dev's prior value, not a hardcoded False


@native_test
def test_apply_document_content_tool_untracked_when_config_off_uno():
    """Mode off (the default): the tool applies directly, no redline."""
    _reset("Old tool body.")
    prev = get_config(_ctx, _FLAG)
    set_config(_ctx, _FLAG, "off")
    try:
        assert get_agent_edit_review_mode(_ctx) == "off"
        res = ApplyDocumentContent().execute(_tool_ctx(), target="full_document", content=["<p>Tool new body.</p>"])
        assert res.get("status") == "ok", res
        assert _redline_types() == [], "flag off must not create redlines"
        assert "Tool new body." in _para_text()
    finally:
        set_config(_ctx, _FLAG, prev)


@native_test
def test_apply_document_content_tool_tags_changes_with_session_tokens_uno():
    """Every redline of a flag-on edit carries a wa-review:<session>:<n> token (so completion
    and outcome detection key on this session only), and a replace-all yields one tagged change
    PER MATCH."""
    _reset("Tag alpha here. Tag alpha there.")
    prev = get_config(_ctx, _FLAG)
    set_config(_ctx, _FLAG, "record")
    try:
        res = ApplyDocumentContent().execute(
            _tool_ctx(), target="search", old_content="Tag alpha", content=["Tag beta"], all_matches=True)
        assert res.get("status") == "ok", res
        assert res.get("replaced_count") == 2, res
        comments = [r["RedlineComment"] for r in _redlines()]
        assert comments and all(c.startswith("wa-review:") for c in comments), \
            "all redlines must carry session tokens, got %r" % comments
        assert len(set(comments)) == 2, "two matches -> two distinct per-change tokens, got %r" % comments
    finally:
        set_config(_ctx, _FLAG, prev)
        _reject_all()


@native_test
def test_tool_never_blocks_on_main_thread_even_with_wait_flag_uno():
    """doc.agent_edit_review_mode=wait: from the MAIN thread the tool must NOT block-wait (the
    user could never click accept/reject if the UI thread were parked) -- it edits, records
    (wait implies recording), and returns without a review payload. The blocking wait only
    happens on a background MCP/chat thread."""
    _reset("Guard body text.")
    prev_mode = get_config(_ctx, _FLAG)
    set_config(_ctx, _FLAG, "wait")
    try:
        res = ApplyDocumentContent().execute(_tool_ctx(), target="end", content=["<p>Guard addition.</p>"])
        assert res.get("status") == "ok", res
        assert "review" not in res, "main-thread call must not block-wait: %r" % res
        assert _redline_types(), "wait mode must imply recording (redlines expected)"
    finally:
        set_config(_ctx, _FLAG, prev_mode)
        _reject_all()


# --- style changes are not redline-trackable; the tool says so under review mode ---------

@native_test
def test_apply_style_flags_unreviewed_when_review_on_uno():
    from plugin.writer.styles import ApplyStyle

    _reset("Heading target text.")
    prev = get_config(_ctx, _FLAG)
    set_config(_ctx, _FLAG, "record")
    try:
        res = ApplyStyle().execute(_tool_ctx(), style_name="Heading 2", family="ParagraphStyles", target="full_document")
        assert res.get("status") == "ok", res
        assert res.get("style_unreviewed") is True, "style edit must flag unreviewed under review mode: %r" % res
        assert _redline_types() == [], "a paragraph-style change creates no redline"
    finally:
        set_config(_ctx, _FLAG, prev)


@native_test
def test_apply_style_no_unreviewed_flag_when_review_off_uno():
    from plugin.writer.styles import ApplyStyle

    _reset("Heading target text.")
    prev = get_config(_ctx, _FLAG)
    set_config(_ctx, _FLAG, "off")
    try:
        res = ApplyStyle().execute(_tool_ctx(), style_name="Heading 2", family="ParagraphStyles", target="full_document")
        assert res.get("status") == "ok", res
        assert "style_unreviewed" not in res, "no unreviewed flag when review mode off: %r" % res
    finally:
        set_config(_ctx, _FLAG, prev)


# --- script/vision result insertions record through the session too ----------------------

@native_test
def test_insert_content_at_position_recorded_by_session_uno():
    """The mechanism the script/vision result insertions rely on: insert_content_at_position
    inside an enabled EditReviewSession lands as a tagged Insert redline, and the prior
    recording state is restored."""
    _reset("Existing body.")
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        session.record_mutation(lambda: fmt.insert_content_at_position(_doc, _ctx, "<p>Inserted result.</p>", "end"))
    session.cleanup()
    rls = _redlines()
    assert any(r["type"] == "Insert" for r in rls), "insert must create an Insert redline, got %r" % rls
    assert all(r["RedlineComment"].startswith("wa-review:") for r in rls), "tagged: %r" % rls
    assert _doc.getPropertyValue("RecordChanges") is False, "recording restored to OFF"
    _reject_all()


# --- extend-selection: append the continuation as ONE tracked INSERTION (the original is never
# --- struck), tagged as an agent change -- via WriterStreamedAppendSession --------------------

def _append_run(track_reviewable, prior_recording):
    _reset("Original sentence.")
    _doc.setPropertyValue("RecordChanges", prior_recording)
    rng = _doc.getText().createTextCursor()
    rng.gotoStart(False)
    rng.gotoEndOfParagraph(True)
    session = WriterStreamedAppendSession(_doc, rng, "Original sentence.", track_reviewable=track_reviewable)
    session.append_chunk(" Added continuation.")
    session.finish()


@native_test
def test_streamed_append_tracks_only_appended_text_uno():
    """Extend collapses to ONE tracked INSERTION of just the appended text -- the original keeps
    no Delete redline -- tagged as an agent change. Reject removes only the appended text."""
    _doc.setPropertyValue("ShowChanges", False)
    _append_run(track_reviewable=True, prior_recording=False)
    rls = _redlines()
    assert rls, "review mode must collapse the appended continuation into a redline"
    assert all(r["type"] == "Insert" for r in rls), \
        "extend appends -> Insert only, the original keeps no Delete redline, got %r" % [r["type"] for r in rls]
    comments = [r["RedlineComment"] for r in rls]
    assert comments and all(c.startswith("wa-review:") for c in comments), \
        "appended redline must carry a session token, got %r" % comments
    assert len(set(comments)) == 1, "the append is ONE agent change (one token), got %r" % comments
    assert _doc.getPropertyValue("ShowChanges") is True, "review mode forces markup visible"
    assert _doc.getPropertyValue("RecordChanges") is False, "recording restored to OFF"
    _reject_all()
    assert _para_text() == "Original sentence.", "reject removes only the appended continuation"


@native_test
def test_streamed_append_flag_off_no_redline_uno():
    """Flag off + user not recording: extend appends directly, no redline (today's behavior)."""
    _append_run(track_reviewable=False, prior_recording=False)
    assert _redline_types() == [], "flag off + user off must not create a redline"
    assert _para_text() == "Original sentence. Added continuation.", "the continuation is appended in full"


@native_test
def test_streamed_append_no_output_restores_prior_recording_uno():
    """A no-op streamed extend still restores the user's RecordChanges state.

    WriterStreamedAppendSession turns recording off while chunks stream, so an empty model result
    must not leave the user's own tracking disabled just because there was no edit to collapse.
    """
    _reset("Original sentence.")
    _doc.setPropertyValue("RecordChanges", True)
    rng = _doc.getText().createTextCursor()
    rng.gotoStart(False)
    rng.gotoEndOfParagraph(True)
    session = WriterStreamedAppendSession(_doc, rng, "Original sentence.", track_reviewable=False)
    warning = session.finish()
    assert warning is None
    assert _doc.getPropertyValue("RecordChanges") is True, "no-output append must restore the user's RecordChanges ON state"
    _doc.setPropertyValue("RecordChanges", False)


@native_test
def test_update_style_flags_unreviewed_when_review_on_uno():
    """Like apply_style, update_style is a style mutation -> not reviewable -> must flag the agent."""
    from plugin.writer.styles import UpdateStyle

    _reset("Body text for style update.")
    prev = get_config(_ctx, _FLAG)
    set_config(_ctx, _FLAG, "record")
    try:
        res = UpdateStyle().execute(
            _tool_ctx(), style_name="Standard", family="ParagraphStyles",
            property_updates={"CharWeight": 150.0})  # 150.0 == BOLD
        assert res.get("status") == "ok", res
        assert res.get("style_unreviewed") is True, "update_style must flag unreviewed under review mode: %r" % res
        assert _redline_types() == [], "a style change creates no redline"
    finally:
        set_config(_ctx, _FLAG, prev)


@native_test
def test_is_async_true_on_background_thread_when_review_toggled_off_uno():
    """If review is toggled OFF after the async snapshot dispatched the tool to a worker thread,
    is_async() must still report True there so execute_safe's main-thread guard won't reject it
    (execute() then marshals the wait-free edit to the main thread)."""
    import threading

    prev = get_config(_ctx, _FLAG)
    set_config(_ctx, _FLAG, "off")
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
        set_config(_ctx, _FLAG, prev)


@native_test
def test_bookmarks_cleaned_when_edit_raises_midway_uno():
    """#4: if _execute_edit raises AFTER a change was anchored (a replace-all that fails on a later
    match, having anchored the first), execute() must still release the session's wa_review_*
    anchor bookmarks via the session_sink -- none may leak in the document."""
    from plugin.writer.content import ApplyDocumentContent
    from plugin.writer.edit_review import EditReviewSession

    _reset("Anchor target paragraph.")
    prev = get_config(_ctx, _FLAG)
    set_config(_ctx, _FLAG, "record")

    class _BoomTool(ApplyDocumentContent):
        def _execute_edit(self, ctx, session_sink=None, **kwargs):
            # Mirror the real path: create the session, register it in the sink, anchor ONE change
            # (creating a wa_review_* bookmark), then fail before returning.
            session = EditReviewSession(ctx.doc, ctx.ctx, enabled=True)
            if session_sink is not None:
                session_sink.append(session)
            with session:
                session.record_mutation(
                    lambda: fmt.insert_content_at_position(ctx.doc, ctx.ctx, "<p>Inserted.</p>", "end"))
            raise RuntimeError("boom after anchoring the first change")

    try:
        raised = False
        try:
            _BoomTool().execute(_tool_ctx())
        except RuntimeError:
            raised = True
        assert raised, "the mid-edit failure must propagate to the caller"
        leaked = [n for n in _doc.getBookmarks().getElementNames() if n.startswith("wa_review_")]
        assert leaked == [], "anchor bookmarks must be cleaned up on a mid-edit failure, leaked: %r" % leaked
    finally:
        set_config(_ctx, _FLAG, prev)
        if len(_doc.getRedlines()):
            _reject_all()


@native_test
def test_review_authors_failed_begin_leaves_split_authoring_disarmed_uno():
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
        review_authors.end(_ctx, None)  # keep the thread-local clean for later tests


@native_test
def test_apply_document_content_wait_timeout_zero_returns_pending_uno():
    """Wait mode with timeout=0 on a background thread: executes edit and returns immediately
    with complete=False and pending changes."""
    import threading
    _reset("Initial text.")
    prev_mode = get_config(_ctx, _FLAG)
    prev_timeout = get_config(_ctx, "doc.edit_review_timeout")
    set_config(_ctx, _FLAG, "wait")
    set_config(_ctx, "doc.edit_review_timeout", 0)

    try:
        tool = ApplyDocumentContent()
        res = {}
        def _run_tool():
            # Must run on a background thread so the tool is considered async and enters wait path
            tool_ctx = _tool_ctx()
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
        set_config(_ctx, _FLAG, prev_mode)
        set_config(_ctx, "doc.edit_review_timeout", prev_timeout)
        _reject_all()
