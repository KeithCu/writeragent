# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LOBackend pins: fallback ctx on start, caller document at each queued call. No soffice."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
_REPO = Path(__file__).resolve().parents[2]
for _p in (_PO, _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import plugin.framework.uno_context as uno_context  # noqa: E402
import tools_lo as tl  # noqa: E402
from plugin.framework.thread_guard import get_designated_main_thread, set_designated_main_thread  # noqa: E402
from plugin.framework.uno_context import get_ctx, set_fallback_ctx  # noqa: E402


def _boot(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    """Start LOBackend against a fake pipe. Returns (remote ctx, desktop)."""
    if tl._lo_thread is not None:
        tl._lo_docs.clear()
        tl.LOBackend.stop()
    mock_ctx = MagicMock(name="remote_ctx")
    mock_desktop = MagicMock(name="desktop")
    mock_ctx.getServiceManager.return_value.createInstanceWithContext.return_value = mock_desktop
    monkeypatch.setattr(tl, "_bootstrap_headless", lambda: mock_ctx)
    monkeypatch.setattr("plugin.main.bootstrap", lambda ctx: None)
    tl.LOBackend.start()
    return mock_ctx, mock_desktop


def _shutdown(prev_designated: threading.Thread | None, prev_harness: str | None) -> None:
    tl._lo_docs.clear()
    tl._lo_kinds.clear()
    if tl._lo_thread is not None:
        tl.LOBackend.stop()
    set_designated_main_thread(prev_designated)
    if prev_harness is None:
        os.environ.pop("WRITERAGENT_EVAL_HARNESS", None)
    else:
        os.environ["WRITERAGENT_EVAL_HARNESS"] = prev_harness


@pytest.fixture
def lo_env():
    prev_designated = get_designated_main_thread()
    prev_harness = os.environ.get("WRITERAGENT_EVAL_HARNESS")
    prev_fallback = uno_context._fallback_ctx
    try:
        yield
    finally:
        _shutdown(prev_designated, prev_harness)
        # stop() restores the fallback captured at start. If start never ran,
        # put back whatever this test found.
        if tl._lo_saved_fallback is tl._FALLBACK_UNSET and uno_context._fallback_ctx is not prev_fallback:
            set_fallback_ctx(prev_fallback)


def test_start_sets_fallback_ctx_and_stop_restores_it(monkeypatch: pytest.MonkeyPatch, lo_env: None) -> None:
    del lo_env
    sentinel = MagicMock(name="previous_fallback")
    set_fallback_ctx(sentinel)
    mock_ctx, _desktop = _boot(monkeypatch)
    try:
        assert uno_context._fallback_ctx is mock_ctx
        assert tl.LOBackend.call(get_ctx) is mock_ctx
    finally:
        tl._lo_docs.clear()
        tl.LOBackend.stop()
    assert uno_context._fallback_ctx is sentinel

    set_fallback_ctx(None)
    tl.LOBackend.start()
    try:
        assert uno_context._fallback_ctx is mock_ctx
    finally:
        tl._lo_docs.clear()
        tl.LOBackend.stop()
    assert uno_context._fallback_ctx is None


def test_call_skips_pin_when_caller_has_no_document(monkeypatch: pytest.MonkeyPatch, lo_env: None) -> None:
    del lo_env
    _mock_ctx, desktop = _boot(monkeypatch)
    assert tl.LOBackend.call(lambda: "ok") == "ok"
    desktop.setActiveFrame.assert_not_called()


def test_call_activates_caller_document_before_the_body(monkeypatch: pytest.MonkeyPatch, lo_env: None) -> None:
    del lo_env
    _mock_ctx, desktop = _boot(monkeypatch)
    events: list[tuple[str, object]] = []
    frame = MagicMock(name="frame")
    doc = MagicMock(name="doc")
    doc.getCurrentController.return_value.getFrame.return_value = frame
    desktop.setActiveFrame.side_effect = lambda active: events.append(("pin", active))
    tl._lo_docs[threading.get_ident()] = doc

    def _body() -> str:
        events.append(("body",))
        return "ran"

    assert tl.LOBackend.call(_body) == "ran"
    assert events == [("pin", frame), ("body",)]


def test_each_caller_pins_its_own_document(monkeypatch: pytest.MonkeyPatch, lo_env: None) -> None:
    del lo_env
    _mock_ctx, _desktop = _boot(monkeypatch)
    seen: list[object] = []
    lock = threading.Lock()

    def _worker() -> None:
        frame = MagicMock(name="frame")
        doc = MagicMock(name="doc")
        doc.getCurrentController.return_value.getFrame.return_value = frame
        tl._lo_docs[threading.get_ident()] = doc

        def _body() -> None:
            pinned = tl._lo_desktop.setActiveFrame.call_args[0][0]
            with lock:
                seen.append(pinned)
            assert pinned is frame

        tl.LOBackend.call(_body)

    threads = [threading.Thread(target=_worker), threading.Thread(target=_worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    assert len(seen) == 2
    assert seen[0] is not seen[1]
