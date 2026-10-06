# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Send/Record relabel keeps the shared button width and the VCL mnemonic."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from plugin.chatbot.panel import SendButtonListener, _relabel_button
from plugin.chatbot.send_state import UpdateUIEffect


class _Peer:
    def __init__(self, label: str) -> None:
        self.label = label

    def getProperty(self, name: str) -> Any:
        assert name == "Label"
        return self.label

    def setProperty(self, name: str, value: Any) -> None:
        assert name == "Label"
        self.label = value


class _Button:
    """Model label syncs to the peer without a mnemonic, as VCLXButton does."""

    def __init__(self, label: str, shown: str, rect: tuple[int, int, int, int], grow_on_relabel: int = 0) -> None:
        self.peer = _Peer(shown)
        self.rect = rect
        self.grow = grow_on_relabel
        self.calls: list[tuple[int, int, int, int]] = []
        button = self

        class _Model:
            Enabled = True

            def __init__(self) -> None:
                self._label = label

            @property
            def Label(self) -> str:
                return self._label

            @Label.setter
            def Label(self, value: str) -> None:
                self._label = value
                button.peer.label = value
                x, y, w, h = button.rect
                button.rect = (x, y, w + button.grow, h)

        self.model = _Model()

    def getModel(self) -> Any:
        return self.model

    def getPeer(self) -> Any:
        return self.peer

    def getPosSize(self) -> Any:
        x, y, w, h = self.rect
        return SimpleNamespace(X=x, Y=y, Width=w, Height=h)

    def setPosSize(self, x: int, y: int, w: int, h: int, flags: int) -> None:
        self.calls.append((x, y, w, h))
        self.rect = (x, y, w, h)


def test_relabel_keeps_the_layout_rect():
    btn = _Button("Record", "~Record", (16, 700, 148, 24), grow_on_relabel=12)
    _relabel_button(btn, "Send", {})
    assert btn.rect == (16, 700, 148, 24)
    assert btn.model.Label == "Send"


def test_relabel_does_not_move_a_button_that_kept_its_size():
    btn = _Button("Record", "~Record", (16, 700, 148, 24))
    _relabel_button(btn, "Send", {})
    assert btn.calls == []


def test_mnemonic_comes_back_with_its_label():
    btn = _Button("Record", "~Record", (16, 700, 148, 24))
    cache: dict[str, str] = {}
    _relabel_button(btn, "Send", cache)
    assert btn.peer.label == "Send"
    _relabel_button(btn, "Record", cache)
    assert btn.peer.label == "~Record"
    # Click handlers compare the model label; it stays plain.
    assert btn.model.Label == "Record"


def test_same_label_is_a_no_op():
    btn = _Button("Record", "~Record", (16, 700, 148, 24), grow_on_relabel=12)
    _relabel_button(btn, "Record", {})
    assert btn.peer.label == "~Record"
    assert btn.calls == [] and btn.rect == (16, 700, 148, 24)


def test_end_of_turn_effect_keeps_the_shared_width():
    """The effect used to pin Send/Record to the XDL-measured width (89 vs 79 at 1x).

    That pinned width (``_fixed_send_width``) is gone; the relabel keeps the
    rect the layout gave the button.
    """
    send = SendButtonListener.__new__(SendButtonListener)
    send.send_control = _Button("Send", "Send", (16, 700, 79, 24))
    send.send_control.peer.label = "Send"
    send.stop_control = None
    send._interpret_effect(UpdateUIEffect(send_enabled=True, stop_enabled=False, send_label="Record", status_text=None))
    assert send.send_control.model.Label == "Record"
    assert send.send_control.rect == (16, 700, 79, 24)
