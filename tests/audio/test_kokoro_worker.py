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
from typing import Any

import pytest

from plugin.audio import kokoro_worker


@pytest.fixture(autouse=True)
def _reset_kokoro_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kokoro_worker, "_loaded", None)


def _job(out_path, **overrides: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "text": "Hi.",
        "voice": "af_sky",
        "speed": 1.0,
        "lang": "en-us",
        "model_path": "/models/kokoro.onnx",
        "voices_path": "/models/voices.bin",
        "out_path": str(out_path),
    }
    job.update(overrides)
    return job


def _install_fake_kokoro(
    monkeypatch: pytest.MonkeyPatch,
    voices: Any,
    *,
    seen: list[dict] | None = None,
    inits: list[tuple[str, str]] | None = None,
    speeds: list[float] | None = None,
    samples: list[float] | None = None,
) -> None:
    class Kokoro:
        def __init__(self, model_path: str, voices_path: str) -> None:
            if inits is not None:
                inits.append((model_path, voices_path))
            self.voices = voices

        def create(self, text: str, voice: str, speed: float, lang: str, is_phonemes: bool = False):
            if seen is not None:
                seen.append({"text": text, "voice": voice, "lang": lang, "is_phonemes": is_phonemes})
            if speeds is not None:
                speeds.append(speed)
            return ([0.0] if samples is None else samples), 24000

    mod: Any = types.ModuleType("kokoro_onnx")
    mod.Kokoro = Kokoro
    soundfile: Any = types.ModuleType("soundfile")

    def write(path: str, data, rate: int) -> None:
        del data, rate
        with open(path, "wb") as handle:
            handle.write(b"RIFF")

    soundfile.write = write
    monkeypatch.setitem(sys.modules, "kokoro_onnx", mod)
    monkeypatch.setitem(sys.modules, "soundfile", soundfile)


def test_handle_kokoro_job_loads_model_once(tmp_path, monkeypatch):
    inits: list[tuple[str, str]] = []
    seen: list[dict] = []
    _install_fake_kokoro(monkeypatch, ["af_bella", "am_adam"], seen=seen, inits=inits)
    first = tmp_path / "a.wav"
    second = tmp_path / "b.wav"
    first_result = kokoro_worker.handle_kokoro_job(_job(first, text="Hello.", voice="af_bella"))
    second_result = kokoro_worker.handle_kokoro_job(_job(second, text="Again.", voice="af_bella"))

    assert inits == [("/models/kokoro.onnx", "/models/voices.bin")]
    assert [s["voice"] for s in seen] == ["af_bella", "af_bella"]
    assert [s["lang"] for s in seen] == ["en-us", "en-us"]
    assert first_result["status"] == "ok"
    assert first_result["path"] == str(first)
    assert first_result["g2p"] == "espeak"
    assert "warning" not in first_result
    assert second_result["path"] == str(second)
    assert second_result["g2p"] == "espeak"
    assert "warning" not in second_result
    assert first.read_bytes() == b"RIFF"
    assert second.read_bytes() == b"RIFF"


@pytest.mark.parametrize(
    ("voices", "text", "voice", "lang", "misaki_prefix", "expected_call", "warning_substrings"),
    [
        # Bella is listed first so a first-voice fallback would pick it. Sky is
        # the preferred substitute when the requested id is missing from the pack.
        pytest.param(
            ["af_bella", "af_sky", "am_adam"],
            "Hi.",
            "not_a_voice",
            "en-us",
            None,
            {"text": "Hi.", "voice": "af_sky", "lang": "en-us", "is_phonemes": False},
            ("not_a_voice", "af_sky"),
            id="substitutes_missing_voice",
        ),
        pytest.param(
            ["af_bella", "af_sky"],
            "非常に",
            "jf_alpha",
            "ja",
            "WRONG_JA",
            {"text": "非常に", "voice": "af_sky", "lang": "en-us", "is_phonemes": False},
            (),
            id="cross_language_recomputes_lang",
        ),
        pytest.param(
            ["af_sky", "jf_gong"],
            "非常に",
            "jf_alpha",
            "ja",
            "PH",
            {"text": "PH:ja:非常に", "voice": "jf_gong", "lang": "ja", "is_phonemes": True},
            (),
            id="prefers_same_family",
        ),
        pytest.param(
            ["ef_dora"],
            "Bonjour",
            "ff_siwis",
            "fr-fr",
            "PH",
            {"text": "PH:es:Bonjour", "voice": "ef_dora", "lang": "es", "is_phonemes": True},
            (),
            id="first_archive_voice_recomputes_lang",
        ),
        pytest.param(
            ["ef_dora", "fm_siwis"],
            "Bonjour",
            "ff_siwis",
            "fr-fr",
            "PH",
            {"text": "PH:fr-fr:Bonjour", "voice": "fm_siwis", "lang": "fr-fr", "is_phonemes": True},
            (),
            id="french_same_family",
        ),
    ],
)
def test_handle_kokoro_job_missing_voice(
    tmp_path,
    monkeypatch,
    voices: list[str],
    text: str,
    voice: str,
    lang: str,
    misaki_prefix: str | None,
    expected_call: dict,
    warning_substrings: tuple[str, ...],
):
    seen: list[dict] = []
    _install_fake_kokoro(monkeypatch, voices, seen=seen)
    if misaki_prefix is not None:
        monkeypatch.setattr(
            "plugin.audio.kokoro_g2p._get_misaki_g2p",
            lambda g2p_lang: (lambda src: (f"{misaki_prefix}:{g2p_lang}:{src}", None)),
        )
    out = tmp_path / "out.wav"
    result = kokoro_worker.handle_kokoro_job(_job(out, text=text, voice=voice, lang=lang))
    assert result["status"] == "ok"
    for substr in warning_substrings:
        assert substr in result["warning"]
    assert seen[0] == expected_call


def test_handle_kokoro_job_rejects_empty_samples(tmp_path, monkeypatch):
    _install_fake_kokoro(monkeypatch, ["af_sky"], samples=[])
    out = tmp_path / "empty.wav"
    result = kokoro_worker.handle_kokoro_job(_job(out))
    assert result["status"] == "error"
    assert "no audio samples" in result["error"]
    assert not out.exists()


def test_handle_kokoro_job_cached_import_failure(monkeypatch, tmp_path):
    call_count = 0
    orig_import = __import__

    def fake_import(name, *args, **kwargs):
        nonlocal call_count
        if name in ("kokoro_onnx", "soundfile"):
            call_count += 1
            raise ModuleNotFoundError(f"No module named {name!r}")
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    out = tmp_path / "fail.wav"
    job = _job(out)
    res1 = kokoro_worker.handle_kokoro_job(job)
    res2 = kokoro_worker.handle_kokoro_job(job)
    from plugin.audio.kokoro_g2p import KOKORO_PIP_INSTALL

    assert res1["status"] == "error"
    assert "Kokoro dependency missing" in res1["error"]
    assert KOKORO_PIP_INSTALL in res1["error"]
    assert res1 == res2
    assert call_count == 2


def test_handle_kokoro_job_rejects_missing_path():
    result = kokoro_worker.handle_kokoro_job({"text": "Hi."})
    assert result["status"] == "error"


def test_handle_kokoro_job_japanese_uses_misaki(tmp_path, monkeypatch):
    seen: list[dict] = []
    _install_fake_kokoro(monkeypatch, ["jf_alpha", "af_bella"], seen=seen)
    monkeypatch.setattr(
        "plugin.audio.kokoro_g2p._get_misaki_g2p",
        lambda lang: (lambda text: (f"PH:{lang}:{text}", None)),
    )
    out = tmp_path / "ja.wav"
    result = kokoro_worker.handle_kokoro_job(_job(out, text="非常に", voice="jf_alpha", lang="ja"))
    assert result["status"] == "ok"
    assert result["g2p"] == "misaki"
    assert "warning" not in result
    assert seen == [
        {"text": "PH:ja:非常に", "voice": "jf_alpha", "lang": "ja", "is_phonemes": True}
    ]
    assert out.read_bytes() == b"RIFF"


def test_handle_kokoro_job_empty_misaki_falls_back_to_espeak(tmp_path, monkeypatch):
    seen: list[dict] = []
    _install_fake_kokoro(monkeypatch, ["jf_alpha"], seen=seen)
    monkeypatch.setattr("plugin.audio.kokoro_g2p._get_misaki_g2p", lambda lang: (lambda text: ("", None)))
    out = tmp_path / "ja.wav"
    result = kokoro_worker.handle_kokoro_job(_job(out, text="非常に", voice="jf_alpha", lang="ja"))
    assert result["status"] == "ok"
    assert result["g2p"] == "espeak"
    assert "fell back to espeak-ng" in result["warning"]
    assert seen == [
        {"text": "非常に", "voice": "jf_alpha", "lang": "ja", "is_phonemes": False}
    ]


def test_handle_kokoro_job_misaki_exception_falls_back_to_espeak(tmp_path, monkeypatch):
    seen: list[dict] = []
    _install_fake_kokoro(monkeypatch, ["jf_alpha"], seen=seen)

    def raise_err(lang):
        raise RuntimeError("boom")

    monkeypatch.setattr("plugin.audio.kokoro_g2p._get_misaki_g2p", raise_err)
    out = tmp_path / "ja_err.wav"
    result = kokoro_worker.handle_kokoro_job(_job(out, text="非常に", voice="jf_alpha", lang="ja"))
    assert result["status"] == "ok"
    assert result["g2p"] == "espeak"
    from plugin.audio.kokoro_g2p import G2P_FAILED_MARK

    assert G2P_FAILED_MARK in result["warning"]
    assert seen == [
        {"text": "非常に", "voice": "jf_alpha", "lang": "ja", "is_phonemes": False}
    ]


def test_kokoro_worker_once_subprocess(tmp_path):
    import json
    import subprocess

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    worker_script = os.path.join(repo_root, "plugin", "audio", "kokoro_worker.py")

    fake_onnx = tmp_path / "kokoro_onnx.py"
    fake_onnx.write_text(
        "class Kokoro:\n"
        "    voices = ['af_sky']\n"
        "    def __init__(self, model_path, voices_path):\n"
        "        pass\n"
        "    def create(self, text, voice='af_sky', speed=1.0, lang='en-us', is_phonemes=False):\n"
        "        return [0.0], 24000\n"
    )
    fake_sf = tmp_path / "soundfile.py"
    fake_sf.write_text(
        "def write(path, samples, rate):\n"
        "    with open(path, 'wb') as f:\n"
        "        f.write(b'RIFFdata')\n"
    )
    out_wav = tmp_path / "out.wav"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(tmp_path), repo_root, env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)

    cmd = [
        sys.executable,
        worker_script,
        "--once",
        "-",
        "af_sky",
        "1.0",
        str(out_wav),
        "m.onnx",
        "v.bin",
        "en-us",
    ]
    proc = subprocess.run(
        cmd,
        input="Hello",
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert proc.returncode == 0
    assert out_wav.read_bytes().startswith(b"RIFF")
    res = json.loads(proc.stdout)
    assert res["status"] == "ok"
    assert res["g2p"] == "espeak"


def test_fallback_voice_npz_without_af_sky(tmp_path, monkeypatch):
    import numpy as np

    npz_path = tmp_path / "voices.npz"
    np.savez(npz_path, af_bella=np.zeros(10, dtype=np.float32))
    archive = np.load(npz_path)

    seen: list[dict] = []
    _install_fake_kokoro(monkeypatch, archive, seen=seen)

    out = tmp_path / "fallback.wav"
    result = kokoro_worker.handle_kokoro_job(_job(out, voice="not_a_voice"))
    assert result["status"] == "ok"
    assert "af_bella" in result["warning"]
    assert seen[0]["voice"] == "af_bella"


def test_fallback_voice_empty_container(tmp_path, monkeypatch):
    seen: list[dict] = []
    _install_fake_kokoro(monkeypatch, [], seen=seen)
    out = tmp_path / "empty.wav"
    result = kokoro_worker.handle_kokoro_job(_job(out, voice="not_a_voice"))
    assert result["status"] == "ok"
    assert "af_sky" in result["warning"]
    assert seen[0]["voice"] == "af_sky"


def test_speed_zero_is_kept(tmp_path, monkeypatch):
    recorded_speed: list[float] = []
    _install_fake_kokoro(monkeypatch, ["af_sky"], speeds=recorded_speed)

    out = tmp_path / "zero.wav"
    result = kokoro_worker.handle_kokoro_job(_job(out, speed=0.0))
    assert result["status"] == "ok"
    assert recorded_speed == [0.0]


def test_stdio_loop_exits_on_bad_length_prefix(tmp_path):
    from plugin.framework.ipc import pack_pickle_frame, read_pickle_frame

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
    from plugin.framework.ipc import pack_pickle_frame, read_pickle_frame

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
    from plugin.framework.ipc import pack_pickle_frame, read_pickle_frame

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
