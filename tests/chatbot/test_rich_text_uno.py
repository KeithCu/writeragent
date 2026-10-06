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
        paras = _paragraph_runs(doc)
        answer = [runs for runs in paras if "done" in "".join(t for t, _c in runs)]
        assert answer, "answer row missing: %r" % paras
        for txt, color in answer[0]:
            assert color != theme.user_color, "You color bled into the answer: %r" % answer[0]
        question = [runs for runs in paras if "second question" in "".join(t for t, _c in runs)]
        assert question and question[0][-1][1] == theme.user_color, "user row lost its color: %r" % question
    finally:
        doc.close(True)
