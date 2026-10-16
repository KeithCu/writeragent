# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Kokoro worker job handler without a real ONNX runtime."""

from __future__ import annotations

import os
import struct
import subprocess
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
    # Bella is listed first so a first-voice fallback would pick it. Sky is
    # the preferred substitute when the requested id is missing from the pack.
    seen: list[dict] = []
    _install_voice_kokoro(monkeypatch, ["af_bella", "af_sky", "am_adam"], seen)
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
    assert "af_sky" in result["warning"]
    assert seen[0]["voice"] == "af_sky"


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


def test_fallback_voice_npz_without_af_sky(tmp_path, monkeypatch):
    import numpy as np

    npz_path = tmp_path / "voices.npz"
    np.savez(npz_path, af_bella=np.zeros(10, dtype=np.float32))
    archive = np.load(npz_path)

    seen: list[dict] = []
    kokoro_worker._loaded = None
    kokoro_worker._loaded_key = None

    class Kokoro:
        def __init__(self, model_path: str, voices_path: str) -> None:
            del model_path, voices_path
            self.voices = archive

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

    out = tmp_path / "fallback.wav"
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
    assert "af_bella" in result["warning"]
    assert seen[0]["voice"] == "af_bella"


def test_fallback_voice_empty_container(tmp_path, monkeypatch):
    seen: list[dict] = []
    _install_voice_kokoro(monkeypatch, [], seen)
    out = tmp_path / "empty.wav"
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
    assert "af_sky" in result["warning"]
    assert seen[0]["voice"] == "af_sky"


def test_speed_zero_is_kept(tmp_path, monkeypatch):
    kokoro_worker._loaded = None
    kokoro_worker._loaded_key = None
    recorded_speed = []

    class Kokoro:
        voices = ["af_sky"]

        def __init__(self, model_path: str, voices_path: str) -> None:
            del model_path, voices_path

        def create(self, text: str, voice: str, speed: float, lang: str):
            recorded_speed.append(speed)
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

    out = tmp_path / "zero.wav"
    result = kokoro_worker.handle_kokoro_job(
        {
            "text": "Hi.",
            "voice": "af_sky",
            "speed": 0.0,
            "lang": "en-us",
            "model_path": "/models/kokoro.onnx",
            "voices_path": "/models/voices.bin",
            "out_path": str(out),
        }
    )
    assert result["status"] == "ok"
    assert recorded_speed == [0.0]


def test_stdio_loop_exits_on_bad_length_prefix(tmp_path):
    from plugin.scripting.ipc import pack_pickle_frame, read_pickle_frame

    marker_file = tmp_path / "called.txt"
    script = tmp_path / "worker.py"
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    script.write_text(
        f"import sys, pathlib\nsys.path.insert(0, {root!r})\n"
        "from plugin.audio.kokoro_worker import run_kokoro_stdio_loop\n"
        f"marker = pathlib.Path({str(marker_file)!r})\n"
        "def handler(req):\n"
        "    marker.write_text('called')\n"
        "    return {'status': 'ok'}\n"
        "raise SystemExit(run_kokoro_stdio_loop(handler))\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    assert proc.stdin is not None
    ready = read_pickle_frame(proc.stdout)
    assert isinstance(ready, dict) and ready.get("status") == "ready"
    proc.stdin.write(struct.pack("!I", 0xFFFFFFFF))
    proc.stdin.write(pack_pickle_frame({"text": "test"}))
    proc.stdin.flush()
    proc.stdin.close()
    proc.wait(timeout=5)
    assert proc.returncode == 0
    assert not marker_file.exists()


def test_stdio_loop_continues_after_handler_error(tmp_path):
    from plugin.scripting.ipc import pack_pickle_frame, read_pickle_frame

    script = tmp_path / "worker_err.py"
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    script.write_text(
        f"import sys\nsys.path.insert(0, {root!r})\n"
        "from plugin.audio.kokoro_worker import run_kokoro_stdio_loop\n"
        "calls = 0\n"
        "def handler(req):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    if calls == 1:\n"
        "        raise RuntimeError('boom')\n"
        "    return {'status': 'ok', 'calls': calls}\n"
        "raise SystemExit(run_kokoro_stdio_loop(handler))\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    assert proc.stdin is not None
    ready = read_pickle_frame(proc.stdout)
    assert isinstance(ready, dict) and ready.get("status") == "ready"
    proc.stdin.write(pack_pickle_frame({"first": 1}))
    proc.stdin.flush()
    res1 = read_pickle_frame(proc.stdout)
    assert isinstance(res1, dict) and res1.get("status") == "error"
    assert "Unhandled error" in str(res1.get("error", ""))

    proc.stdin.write(pack_pickle_frame({"second": 2}))
    proc.stdin.flush()
    res2 = read_pickle_frame(proc.stdout)
    assert isinstance(res2, dict) and res2.get("status") == "ok"
    assert res2.get("calls") == 2

    proc.stdin.close()
    proc.wait(timeout=5)


def test_stdio_loop_claim_keeps_fd1_off_the_frame(tmp_path):
    from plugin.scripting.ipc import pack_pickle_frame, read_pickle_frame

    script = tmp_path / "worker_noise.py"
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    script.write_text(
        f"import os, sys\nsys.path.insert(0, {root!r})\n"
        "from plugin.audio.kokoro_worker import run_kokoro_stdio_loop\n"
        "def handler(req):\n"
        "    os.write(1, b'NOISE')\n"
        "    return {'status': 'ok', 'echo': req.get('msg')}\n"
        "raise SystemExit(run_kokoro_stdio_loop(handler))\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    assert proc.stdin is not None
    ready = read_pickle_frame(proc.stdout)
    assert isinstance(ready, dict) and ready.get("status") == "ready"
    proc.stdin.write(pack_pickle_frame({"msg": "hello"}))
    proc.stdin.flush()
    res = read_pickle_frame(proc.stdout)
    assert isinstance(res, dict) and res.get("status") == "ok"
    assert res.get("echo") == "hello"

    # communicate() closes stdin itself; closing it first makes its flush raise ValueError.
    _out, stderr_bytes = proc.communicate(timeout=5)
    assert b"NOISE" in stderr_bytes
