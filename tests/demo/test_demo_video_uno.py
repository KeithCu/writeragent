# WriterAgent - AI Writing Assistant for LibreOffice
# SPDX-License-Identifier: GPL-3.0-or-later
"""Scripted scenes for the WriterAgent product video (NOT a regression test).

Each ``test_sceneN_*`` drives the real sidebar in a visible soffice using your
own profile (keys, models, language), so you can screen-record it. Prompts are
"typed" at human speed, then sent. Re-run one scene at a time::

    make demo-video SCENE=scene3

Skipped unless ``WRITERAGENT_DEMO=1`` (``make demo-video`` sets it) so it never
runs inside ``make test-mock-sidebar``. Build the sample files first with
``python scripts/demo/make_demo_report.py``. Storyboard: docs/demo-video.md.
"""

from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from typing import Any

from plugin.testing_runner import native_test, setup, teardown

DEMO_DIR = Path(os.environ.get("WRITERAGENT_DEMO_DIR", "build/demo")).resolve()
REPORT = "WriterAgent_Enterprises_Q2_Review.odt"
CHAT_MODEL = "openai/gpt-oss-120b:nitro"  # fast; dead time kills demos
FLAG_MODEL = "google/gemini-3.8-flash"  # Draw flag needs a stronger model
TYPE_CPS = 28.0  # chars/second for the typewriter; ~human, readable on video
BREATH = 1.5  # seconds to hold on a finished result so the viewer can read it

# Margin sentence is spelled and structured correctly but self-contradictory:
# only an LLM checker flags it (harper-ls does not).
LOGIC_SENTENCE = "Revenue rose 12% while costs fell faster, so our margins shrank."

_saved: dict[str, Any] = {}


@setup
def _setup(ctx):
    if os.environ.get("WRITERAGENT_DEMO") != "1":
        raise unittest.SkipTest("demo scenes only run via make demo-video")
    from plugin.framework.config import get_config, init_config

    init_config(ctx)
    from plugin.chatbot.chat_sidebar_mode import mark_librarian_invoked

    mark_librarian_invoked()  # start in Chat mode, not the Librarian onboarding
    import plugin.chatbot.sidebar_test_hooks  # noqa: F401  (debug hooks)

    for key in ("text_model", "doc.grammar_proofreader_enabled", "doc.grammar_proofreader_model"):
        _saved[key] = get_config(key)


@teardown
def _teardown(ctx):
    # Put the user's real settings back; documents stay open for re-takes.
    from plugin.framework.config import set_config

    for key, val in _saved.items():
        set_config(key, val)


def _open(ctx, name: str):
    """Open ``build/demo/<name>`` visibly and return the model (reuse if already open)."""
    import uno

    from plugin.chatbot.sidebar_test_hooks import desktop_from_ctx, iter_desktop_components

    url = uno.systemPathToFileUrl(str(DEMO_DIR / name))
    for comp in iter_desktop_components(ctx):
        if getattr(comp, "URL", "") == url:
            return comp
    doc = desktop_from_ctx(ctx).loadComponentFromURL(url, "_blank", 0, ())
    time.sleep(1.5)
    return doc


def _chat(ctx, doc) -> tuple[dict[str, Any], Any]:
    """Show the chat deck for *doc*; return (controls, SendButtonListener)."""
    from plugin.chatbot.sidebar_test_hooks import (
        ensure_sidebar_chat_mode,
        send_listener_for_doc,
        wait_for_chat_dialog_controls,
    )

    controls = wait_for_chat_dialog_controls(ctx, timeout=30.0, doc=doc)
    assert controls, "chat sidebar did not appear"
    sl = send_listener_for_doc(doc)
    ensure_sidebar_chat_mode(controls, listener=sl)
    return controls, sl


def _type_into_query(controls: dict[str, Any], text: str, listener: Any) -> None:
    """Typewriter effect in the query box. Only the last update enables Send."""
    from plugin.chatbot.dialogs import set_control_text
    from plugin.chatbot.sidebar_test_hooks import set_query_text_via_controls

    for i in range(1, len(text) + 1):
        set_control_text(controls["query"], text[:i])
        time.sleep(1.0 / TYPE_CPS)
    set_query_text_via_controls(controls, text, listener=listener)


def _ask(ctx, doc, prompt: str, *, timeout: float = 120.0) -> None:
    """Type *prompt* on camera, send it, wait for the agent to finish, then hold."""
    from plugin.chatbot.sidebar_test_hooks import press_send, wait_idle

    controls, sl = _chat(ctx, doc)
    time.sleep(0.8)  # beat before typing so the viewer's eye finds the box
    _type_into_query(controls, prompt, sl)
    time.sleep(0.4)
    press_send(listener=sl)
    assert wait_idle(listener=sl, timeout=timeout), "agent did not finish: %r" % prompt
    time.sleep(BREATH)


def _type_into_document(doc, text: str) -> None:
    """Type *text* at the end of the Writer body, one char at a time (grammar scene)."""
    from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK  # type: ignore[import-not-found]

    body = doc.Text
    cur = body.createTextCursor()
    cur.gotoEnd(False)
    body.insertControlCharacter(cur, PARAGRAPH_BREAK, False)
    for ch in text:
        body.insertString(cur, ch, False)
        time.sleep(1.0 / TYPE_CPS)


@native_test
def test_scene1_hook(ctx):
    """Before/after: a rough executive summary becomes crisp, formatting intact."""
    from plugin.framework.config import set_config

    set_config("text_model", CHAT_MODEL)
    doc = _open(ctx, REPORT)
    time.sleep(2.0)  # hold on the "before" for the viewer
    _ask(ctx, doc, "Rewrite the Executive Summary so it is crisp and executive-ready. Keep the bold revenue figure and the formatting.")


@native_test
def test_scene2_grammar(ctx):
    """Local/online LLM grammar checker catches a logic error no rule engine can."""
    from plugin.framework.config import set_config

    # "llm" uses grammar_proofreader_model, or the chat model when empty.
    set_config("doc.grammar_proofreader_enabled", "llm")
    set_config("doc.grammar_proofreader_model", "")
    doc = _open(ctx, REPORT)
    time.sleep(3.0)  # let the checker underline the subject-verb slip already in the summary
    _type_into_document(doc, LOGIC_SENTENCE)
    time.sleep(12.0)  # LLM round trip + squiggle; hover/click it on camera


@native_test
def test_scene3_chat_edit(ctx):
    """Chat edit: shorten the rambling paragraph, keep the table."""
    from plugin.framework.config import set_config

    set_config("text_model", CHAT_MODEL)
    doc = _open(ctx, REPORT)
    _ask(ctx, doc, "Make the Product Categories paragraph about half as long. Keep the table.")
