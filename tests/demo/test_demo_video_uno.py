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
CALC = "Sales_Data.ods"
CHAT_MODEL = "openai/gpt-oss-120b:nitro"  # fast; dead time kills demos
FLAG_MODEL = "google/gemini-3.8-flash"  # Draw flag needs a stronger model
TYPE_CPS = 28.0  # chars/second for the typewriter; ~human, readable on video
BREATH = 1.5  # seconds to hold on a finished result so the viewer can read it

# Margin sentence is spelled and structured correctly but self-contradictory:
# only an LLM checker flags it (harper-ls does not).
LOGIC_SENTENCE = "Revenue rose 12% while costs fell faster, so our margins shrank."

_saved: dict[str, Any] = {}


def _say(msg: str) -> None:
    """Timestamped progress on the runner's stdout, so a stalled step is visible."""
    print("[demo %s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


@setup
def _setup(ctx):
    if os.environ.get("WRITERAGENT_DEMO") != "1":
        raise unittest.SkipTest("demo scenes only run via make demo-video")
    from plugin.framework.config import get_config, init_config

    init_config(ctx)
    from plugin.chatbot.chat_sidebar_mode import mark_librarian_invoked

    mark_librarian_invoked()  # start in Chat mode, not the Librarian onboarding
    import plugin.chatbot.sidebar_test_hooks  # noqa: F401  (debug hooks)

    for key in ("text_model", "doc.grammar_proofreader_enabled", "doc.grammar_proofreader_model", "audio.tts_enabled"):
        _saved[key] = get_config(key)
    # Keep the basic (Harper) tier off camera: the video shows the LLM checker only.
    from plugin.framework.config import save

    save({"doc.grammar_proofreader_enabled": "llm", "doc.grammar_proofreader_model": ""})
    # Open every document now, before any chat deck is shown. Loading a component
    # over URP after a deck exists can block the bridge (see open_calc_document in
    # sidebar_test_hooks); scenes then only switch windows.
    for name in (CALC, REPORT):
        _open(ctx, name)
    _draw(ctx)


@teardown
def _teardown(ctx):
    # Put the user's real settings back; documents stay open for re-takes.
    from plugin.framework.config import save

    for key, val in _saved.items():
        save({key: val})


def _open(ctx, name: str):
    """Bring ``build/demo/<name>`` to the front (setup loads it once) and return the model."""
    import uno

    from plugin.chatbot.sidebar_test_hooks import desktop_from_ctx, iter_desktop_components

    url = uno.systemPathToFileUrl(str(DEMO_DIR / name))
    for comp in iter_desktop_components(ctx):
        if getattr(comp, "URL", "") == url:
            _front(comp)
            return comp
    doc = desktop_from_ctx(ctx).loadComponentFromURL(url, "_blank", 0, ())
    _front(doc)
    time.sleep(1.5)
    return doc


def _draw(ctx):
    """The blank Draw document for the flag scene (created in setup)."""
    from plugin.chatbot.sidebar_test_hooks import desktop_from_ctx, iter_desktop_components

    for comp in iter_desktop_components(ctx):
        if comp.supportsService("com.sun.star.drawing.DrawingDocument") and not getattr(comp, "URL", ""):
            _front(comp)
            return comp
    doc = desktop_from_ctx(ctx).loadComponentFromURL("private:factory/sdraw", "_blank", 0, ())
    _front(doc)
    return doc


def _front(doc) -> None:
    """Raise *doc*'s window, maximized, so the recording shows the right app."""
    frame = doc.getCurrentController().getFrame()
    _maximize(doc)
    frame.getContainerWindow().toFront()
    frame.activate()
    time.sleep(0.5)


def _maximize(doc) -> None:
    """Fill the screen for recording.

    Windows opened via ``loadComponentFromURL`` over URP come up at LibreOffice's
    small default size, not the size saved in the user's profile.
    ``XTopWindow2.IsMaximized`` is the toolkit's maximize toggle.
    """
    win = doc.getCurrentController().getFrame().getContainerWindow()
    win.IsMaximized = True


def _chat(ctx, doc, doc_type: str = "writer") -> dict[str, Any]:
    """Show the chat deck for *doc* in Chat mode; return its controls.

    This module runs out-of-process over URP, so the in-soffice
    ``SendButtonListener`` is unreachable (``send_listener_for_doc`` returns
    None and ``press_send`` raises). Drive the real widgets instead, like the
    controls fallback in ``tests/chatbot/test_mock_llm_sidebar_uno.py``.
    """
    from plugin.chatbot.sidebar_test_hooks import ensure_sidebar_chat_mode, wait_for_chat_dialog_controls

    controls = wait_for_chat_dialog_controls(ctx, timeout=30.0, doc=doc)
    assert controls, "chat sidebar did not appear"
    ensure_sidebar_chat_mode(controls, doc_type=doc_type)
    return controls


def _transcript(controls: dict[str, Any]) -> str:
    from plugin.chatbot.dialogs import get_control_text

    for name in ("response_rich", "response"):
        if name in controls:
            return get_control_text(controls[name]) or ""
    return ""


def _type_into_query(controls: dict[str, Any], text: str) -> None:
    """Typewriter effect in the query box; the final call enables Send."""
    from plugin.chatbot.dialogs import set_control_text
    from plugin.chatbot.sidebar_test_hooks import set_query_text_via_controls

    _say("typing %d chars" % len(text))
    for i in range(1, len(text) + 1):
        set_control_text(controls["query"], text[:i])
        time.sleep(1.0 / TYPE_CPS)
    _say("typed; enabling Send")
    set_query_text_via_controls(controls, text)
    _say("Send enabled")


def _ask(ctx, doc, prompt: str, *, doc_type: str = "writer", timeout: float = 180.0) -> dict[str, Any]:
    """Type *prompt* on camera, click Send, wait for the agent to finish, then hold."""
    from plugin.chatbot.sidebar_test_hooks import uno_click, wait_controls_send_finished

    controls = _chat(ctx, doc, doc_type)
    time.sleep(0.8)  # beat before typing so the viewer's eye finds the box
    _type_into_query(controls, prompt)
    time.sleep(0.4)
    before = _transcript(controls)
    _say("clicking Send")
    uno_click(controls["send"])
    _say("waiting for the agent")
    done = wait_controls_send_finished(controls, timeout=timeout, transcript_fn=lambda: _transcript(controls), before=before)
    assert done, "agent did not finish: %r" % prompt
    _say("agent finished")
    time.sleep(BREATH)
    return controls


def _wait_for_text(controls: dict[str, Any], needle: str, timeout: float) -> bool:
    """Poll the transcript for *needle* (e.g. a peer reply that arrives later)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in _transcript(controls):
            return True
        time.sleep(0.5)
    return False


def _wait_idle(controls: dict[str, Any], timeout: float) -> bool:
    """Wait for a turn the driver did not start (voice, peer follow-up) to finish.

    Stop is enabled while a turn runs. Give the turn a few seconds to start,
    then wait for Stop to go disabled.
    """
    from plugin.chatbot.sidebar_test_hooks import control_enabled

    stop = controls.get("stop")
    deadline = time.monotonic() + timeout
    start_by = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        busy = control_enabled(stop) is True
        if not busy and time.monotonic() > start_by:
            return True
        if busy:
            start_by = 0.0
        time.sleep(0.3)
    return False


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
    from plugin.framework.config import save

    save({"text_model": CHAT_MODEL})
    doc = _open(ctx, REPORT)
    time.sleep(2.0)  # hold on the "before" for the viewer
    _ask(ctx, doc, "Rewrite the Executive Summary so it is crisp and executive-ready. Keep the bold revenue figure and the formatting.")


@native_test
def test_scene2_grammar(ctx):
    """Local/online LLM grammar checker catches a logic error no rule engine can."""
    from plugin.framework.config import save

    # "llm" uses grammar_proofreader_model, or the chat model when empty.
    save({"doc.grammar_proofreader_enabled": "llm"})
    save({"doc.grammar_proofreader_model": ""})
    doc = _open(ctx, REPORT)
    time.sleep(3.0)  # let the checker underline the subject-verb slip already in the summary
    _type_into_document(doc, LOGIC_SENTENCE)
    time.sleep(12.0)  # LLM round trip + squiggle; hover/click it on camera


@native_test
def test_scene3_chat_edit(ctx):
    """Chat edit: shorten the rambling paragraph, keep the table."""
    from plugin.framework.config import save

    save({"text_model": CHAT_MODEL})
    doc = _open(ctx, REPORT)
    _ask(ctx, doc, "Make the Product Categories paragraph about half as long. Keep the table.")


@native_test
def test_scene4_voice(ctx):
    """Talk to your document (live mic) and hear the answer read back (TTS)."""
    from plugin.framework.config import save

    # Speech output reads the reply aloud; uses your configured TTS provider/voice.
    save({"text_model": CHAT_MODEL, "audio.tts_enabled": True})
    doc = _open(ctx, REPORT)
    controls = _chat(ctx, doc)
    before = _transcript(controls)
    _say("YOUR TURN: click Record and say: 'Which region grew fastest, and by how much?'")
    _say("then click Stop Rec and Send. Waiting up to 4 minutes for the reply.")
    deadline = time.monotonic() + 240.0
    while time.monotonic() < deadline and _transcript(controls) == before:
        time.sleep(0.5)
    assert _wait_idle(controls, timeout=240.0), "voice turn did not finish"
    _say("reply done; holding while it is read aloud")
    time.sleep(10.0)


@native_test
def test_scene5_web_research(ctx):
    """Web research: the agent searches, reads pages, and cites sources in the doc."""
    from plugin.framework.config import save

    save({"text_model": CHAT_MODEL})
    doc = _open(ctx, REPORT)
    _ask(
        ctx,
        doc,
        "Search the web and add two sentences of market context on office electronics demand to the Outlook section, with sources.",
        timeout=360.0,
    )


@native_test
def test_scene6_document_research(ctx):
    """Document research: answers from the sibling files in the report's folder."""
    from plugin.framework.config import save

    save({"text_model": CHAT_MODEL})
    doc = _open(ctx, REPORT)
    _ask(ctx, doc, "Which of my other documents covers the supplier renewal, and how much notice do we have to give?", timeout=240.0)


@native_test
def test_scene7_latex(ctx):
    """LaTeX in, an editable LibreOffice formula out."""
    from plugin.framework.config import save

    save({"text_model": CHAT_MODEL})
    doc = _open(ctx, REPORT)
    _ask(ctx, doc, "Add a short 'Growth Math' section at the end with the compound annual growth rate formula in LaTeX and one sentence explaining it.")


@native_test
def test_scene8_peer_chat(ctx):
    """Agents talking: the Writer agent asks the open spreadsheet's agent for numbers."""
    from plugin.framework.config import save

    save({"text_model": CHAT_MODEL})
    calc = _open(ctx, CALC)
    _chat(ctx, calc, "calc")  # the peer needs a live sidebar to receive the request
    time.sleep(1.0)
    doc = _open(ctx, REPORT)
    controls = _ask(
        ctx,
        doc,
        "Ask the open Sales_Data spreadsheet for total revenue by customer type, then add a sentence with the top segment to the Outlook section.",
        timeout=240.0,
    )
    # The reply is a follow-up turn once Calc's agent finishes (docs/chat/peer-messaging.md).
    _say("waiting for the spreadsheet agent's reply")
    assert _wait_for_text(controls, "[Peer result from:", timeout=300.0), "no peer reply arrived"
    assert _wait_idle(controls, timeout=240.0), "Writer follow-up turn did not finish"
    _say("peer round trip finished")
    time.sleep(BREATH)


@native_test
def test_scene9_python_formula(ctx):
    """=PY(): real Python (pandas/NumPy) in a spreadsheet cell, written by the agent."""
    from plugin.framework.config import save

    save({"text_model": CHAT_MODEL})
    calc = _open(ctx, CALC)
    _ask(
        ctx,
        calc,
        "On the Sales_Analytics sheet, in the first empty row below the metrics, add a labeled =PY() formula that returns total revenue by region, largest first.",
        doc_type="calc",
        timeout=240.0,
    )


@native_test
def test_scene10_languages(ctx):
    """Ask in Japanese, then Arabic; the agent answers in kind (34 UI languages)."""
    from plugin.framework.config import save

    save({"text_model": CHAT_MODEL})
    doc = _open(ctx, REPORT)
    _ask(ctx, doc, "このレポートで最も売上が大きい地域はどこですか？一文で答えてください。")
    _ask(ctx, doc, "ما هي فئة المنتجات الأعلى إيرادًا؟ أجب بجملة واحدة.")


@native_test
def test_scene11_draw_flag(ctx):
    """Draw: the American flag built from shapes by a stronger model."""
    from plugin.framework.config import save

    save({"text_model": FLAG_MODEL})
    draw = _draw(ctx)
    _ask(ctx, draw, "Draw the American flag with shapes.", doc_type="draw", timeout=300.0)


@native_test
def test_scene12_close(ctx):
    """Closing shot: back to the finished report for the voice-over."""
    _open(ctx, REPORT)
    _say("closing shot: hold for narration")
    time.sleep(8.0)
