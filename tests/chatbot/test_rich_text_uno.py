# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Live Writer checks for plugin.chatbot.rich_text.append_rich_text."""

from __future__ import annotations

from typing import Any

from plugin.testing_runner import native_test


def _hidden_writer(ctx: Any) -> Any:
    import uno

    from plugin.framework.uno_context import get_desktop

    hidden = uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True)
    return get_desktop(ctx).loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden,))


def _paragraph_runs(doc: Any) -> list[list[tuple[str, int]]]:
    """(text, CharColor) runs per paragraph."""
    out: list[list[tuple[str, int]]] = []
    paras = doc.getText().createEnumeration()
    while paras.hasMoreElements():
        para = paras.nextElement()
        if not hasattr(para, "createEnumeration"):
            continue
        runs: list[tuple[str, int]] = []
        portions = para.createEnumeration()
        while portions.hasMoreElements():
            portion = portions.nextElement()
            txt = portion.getString()
            if txt:
                runs.append((txt, int(portion.CharColor)))
        out.append(runs)
    return out


@native_test
def test_user_row_color_does_not_bleed_into_previous_answer(ctx):
    """Restored history drew the tail of 'done' in the You color (Scrolly QA).

    The body range was positioned with doc.CharacterCount, which skips
    paragraph breaks, so it started inside the previous answer.
    """
    from plugin.chatbot.rich_text import ChatTheme, append_rich_text

    doc = _hidden_writer(ctx)
    try:
        assert append_rich_text(doc, "first question", role="user")
        assert append_rich_text(doc, "done", role="assistant")
        assert append_rich_text(doc, "second question", role="user")
        theme = ChatTheme.resolve(doc)
        # Rows may share one paragraph (line breaks), so judge runs, not paragraphs.
        runs = [run for para in _paragraph_runs(doc) for run in para]
        text = "".join(t for t, _c in runs)
        assert "done" in text, "answer row missing: %r" % runs
        start = text.index("Assistant: ")
        end = text.index("You: ", start)
        pos = 0
        for txt, color in runs:
            run_start, pos = pos, pos + len(txt)
            if pos <= start or run_start >= end:
                continue
            if not txt.strip():
                continue
            assert color != theme.user_color, "You color bled into the answer: %r" % runs
        assert runs[-1][0].endswith("second question"), runs
        assert runs[-1][1] == theme.user_color, "user row lost its color: %r" % runs
    finally:
        doc.close(True)


@native_test
def test_html_body_range_covers_the_imported_list(ctx):
    """The body range now starts at a position taken before the HTML import.

    If that position moved to the end of the insert, list tightening would get
    an empty range and sidebar lists would keep the wide default indent.
    """
    from unittest.mock import patch

    import plugin.chatbot.rich_text as rich_text

    seen: list[str] = []
    real_tighten = rich_text._tighten_list_indent

    def record(body_range: Any) -> None:
        seen.append(body_range.getString())
        real_tighten(body_range)

    doc = _hidden_writer(ctx)
    try:
        assert rich_text.append_rich_text(doc, "first question", role="user")
        with patch.object(rich_text, "_tighten_list_indent", record):
            assert rich_text.append_rich_text(doc, "<ul><li>alpha</li><li>beta</li></ul>", role="assistant")
        assert seen, "list tightening did not run"
        assert "alpha" in seen[-1] and "beta" in seen[-1], "body range missed the list: %r" % seen
        assert "first question" not in seen[-1], "body range reached the previous row: %r" % seen
    finally:
        doc.close(True)


def _list_labels(doc: Any) -> list[tuple[str, str]]:
    """(text, ListLabelString) for each non-empty paragraph."""
    out: list[tuple[str, str]] = []
    paras = doc.getText().createEnumeration()
    while paras.hasMoreElements():
        para = paras.nextElement()
        text = para.getString().strip()
        if text:
            out.append((text, str(para.getPropertyValue("ListLabelString") or "")))
    return out


@native_test
def test_leading_list_numbers_every_item(ctx):
    """A reply that opens with <ol> drew its first item without a number (release QA area 5)."""
    from plugin.chatbot import rich_text

    doc = _hidden_writer(ctx)
    try:
        assert rich_text.append_rich_text(doc, "first question", role="user")
        assert rich_text.append_rich_text(doc, "<ol><li>alpha</li><li>beta</li></ol>", role="assistant")
        assert rich_text.append_rich_text(doc, '<ol start="11"><li>gamma</li><li>delta</li></ol>', role="assistant")
        labels = _list_labels(doc)
        assert ("alpha", "1.") in labels and ("beta", "2.") in labels, labels
        assert ("gamma", "11.") in labels and ("delta", "12.") in labels, labels
        assert "\u200b" not in doc.getText().getString()
    finally:
        doc.close(True)
