# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for faster-whisper transcription invocation."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

from plugin.audio.whisper_transcribe import transcribe_wav


def test_transcribe_wav_options_and_joining(monkeypatch: Any) -> None:
    recorded_init: list[tuple[str, str, str]] = []
    recorded_calls: list[tuple[str, dict[str, Any]]] = []

    class FakeWhisperModel:
        def __init__(self, model_name: str, device: str = "cpu", compute_type: str = "int8"):
            recorded_init.append((model_name, device, compute_type))

        def transcribe(self, wav_path: str, **kwargs: Any) -> tuple[Any, None]:
            recorded_calls.append((wav_path, kwargs))
            segments = iter([SimpleNamespace(text=" hello"), SimpleNamespace(text=" world ")])
            return segments, None

    fake_module = ModuleType("faster_whisper")
    setattr(fake_module, "WhisperModel", FakeWhisperModel)
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    text = transcribe_wav("a.wav", "base")
    assert text == "hello world"
    assert recorded_init == [("base", "cpu", "int8")]
    assert len(recorded_calls) == 1
    call_path, kwargs = recorded_calls[0]
    assert call_path == "a.wav"
    assert kwargs == {"vad_filter": True, "condition_on_previous_text": False}


def test_transcribe_wav_skips_empty_or_missing_text(monkeypatch: Any) -> None:
    class FakeWhisperModel:
        def __init__(self, model_name: str, device: str = "cpu", compute_type: str = "int8"):
            pass

        def transcribe(self, wav_path: str, **kwargs: Any) -> tuple[Any, None]:
            segments = iter(
                [
                    SimpleNamespace(text="first"),
                    SimpleNamespace(text=""),
                    SimpleNamespace(),
                    SimpleNamespace(text=" last "),
                ]
            )
            return segments, None

    fake_module = ModuleType("faster_whisper")
    setattr(fake_module, "WhisperModel", FakeWhisperModel)
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    text = transcribe_wav("a.wav", "base")
    assert text == "first last"
