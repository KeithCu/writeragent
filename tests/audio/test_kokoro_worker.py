# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Kokoro worker job handler without a real ONNX runtime."""

from __future__ import annotations

import sys
import types

from plugin.audio import kokoro_worker


def _install_fake_kokoro(monkeypatch, inits: list[tuple[str, str]]):
    kokoro_worker._loaded = None
    kokoro_worker._loaded_key = None

    class Kokoro:
        voices = ["af_bella", "am_adam"]

        def __init__(self, model_path: str, voices_path: str) -> None:
            inits.append((model_path, voices_path))

        def create(self, text: str, voice: str, speed: float, lang: str):
            assert voice == "af_bella"
            assert lang == "en-us"
            return [0.0, float(len(text))], 24000

    mod = types.ModuleType("kokoro_onnx")
    mod.Kokoro = Kokoro
    soundfile = types.ModuleType("soundfile")

    def write(path: str, samples, rate: int) -> None:
        del samples, rate
        with open(path, "wb") as handle:
            handle.write(b"RIFF")

    soundfile.write = write
    monkeypatch.setitem(sys.modules, "kokoro_onnx", mod)
    monkeypatch.setitem(sys.modules, "soundfile", soundfile)


def test_handle_kokoro_job_loads_model_once(tmp_path, monkeypatch):
    inits: list[tuple[str, str]] = []
    _install_fake_kokoro(monkeypatch, inits)
    first = tmp_path / "a.wav"
    second = tmp_path / "b.wav"
    job = {
        "text": "Hello.",
        "voice": "af_bella",
        "speed": 1.0,
        "lang": "en-us",
        "model_path": "/models/kokoro.onnx",
        "voices_path": "/models/voices.bin",
    }
    first_result = kokoro_worker.handle_kokoro_job({**job, "out_path": str(first)})
    second_result = kokoro_worker.handle_kokoro_job({**job, "text": "Again.", "out_path": str(second)})

    assert inits == [("/models/kokoro.onnx", "/models/voices.bin")]
    assert first_result["status"] == "ok"
    assert first_result["path"] == str(first)
    assert second_result["path"] == str(second)
    assert first.read_bytes() == b"RIFF"
    assert second.read_bytes() == b"RIFF"


def test_handle_kokoro_job_substitutes_missing_voice(tmp_path, monkeypatch):
    inits: list[tuple[str, str]] = []
    _install_fake_kokoro(monkeypatch, inits)
    out = tmp_path / "c.wav"
    result = kokoro_worker.handle_kokoro_job(
        {
            "text": "Hi.",
            "voice": "not_a_voice",
            "speed": 1.0,
            "lang": "en-us",
            "model_path": "/models/kokoro.onnx",
            "voices_path": "/models/voices.bin",
            "out_path": str(out),
        }
    )
    assert result["status"] == "ok"
    assert "not_a_voice" in result["warning"]
    assert "af_bella" in result["warning"]


def test_handle_kokoro_job_rejects_missing_path():
    result = kokoro_worker.handle_kokoro_job({"text": "Hi."})
    assert result["status"] == "error"


def _install_voice_kokoro(monkeypatch, voices: list[str], seen: list[dict]):
    kokoro_worker._loaded = None
    kokoro_worker._loaded_key = None

    class Kokoro:
        def __init__(self, model_path: str, voices_path: str) -> None:
            del model_path, voices_path
            self.voices = list(voices)

        def create(self, text, voice, speed, lang, is_phonemes=False):
            del speed
            seen.append(
                {"text": text, "voice": voice, "lang": lang, "is_phonemes": is_phonemes}
            )
            return [0.0], 24000

    mod = types.ModuleType("kokoro_onnx")
    mod.Kokoro = Kokoro
    soundfile = types.ModuleType("soundfile")

    def write(path: str, samples, rate: int) -> None:
        del samples, rate
        with open(path, "wb") as handle:
            handle.write(b"RIFF")

    soundfile.write = write
    monkeypatch.setitem(sys.modules, "kokoro_onnx", mod)
    monkeypatch.setitem(sys.modules, "soundfile", soundfile)


def test_handle_kokoro_job_japanese_uses_misaki_phonemes(tmp_path, monkeypatch):
    seen: list[dict] = []
    _install_voice_kokoro(monkeypatch, ["jf_alpha", "af_bella"], seen)
    monkeypatch.setattr(
        "plugin.audio.kokoro_g2p.misaki_phonemes",
        lambda text, lang: f"PH:{lang}:{text}",
    )
    out = tmp_path / "ja.wav"
    result = kokoro_worker.handle_kokoro_job(
        {
            "text": "非常に",
            "voice": "jf_alpha",
            "speed": 1.0,
            "lang": "ja",
            "model_path": "/models/kokoro.onnx",
            "voices_path": "/models/voices.bin",
            "out_path": str(out),
        }
    )
    assert result["status"] == "ok"
    assert "warning" not in result
    assert seen == [
        {"text": "PH:ja:非常に", "voice": "jf_alpha", "lang": "ja", "is_phonemes": True}
    ]
    assert out.read_bytes() == b"RIFF"


def test_handle_kokoro_job_empty_misaki_falls_back_to_espeak(tmp_path, monkeypatch):
    seen: list[dict] = []
    _install_voice_kokoro(monkeypatch, ["jf_alpha"], seen)
    monkeypatch.setattr("plugin.audio.kokoro_g2p.misaki_phonemes", lambda text, lang: "")
    out = tmp_path / "ja.wav"
    result = kokoro_worker.handle_kokoro_job(
        {
            "text": "非常に",
            "voice": "jf_alpha",
            "speed": 1.0,
            "lang": "ja",
            "model_path": "/models/kokoro.onnx",
            "voices_path": "/models/voices.bin",
            "out_path": str(out),
        }
    )
    assert result["status"] == "ok"
    assert "fell back to espeak-ng" in result["warning"]
    assert seen == [
        {"text": "非常に", "voice": "jf_alpha", "lang": "ja", "is_phonemes": False}
    ]
