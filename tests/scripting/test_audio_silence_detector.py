"""Unit tests for RMS silence / end-of-speech detection."""

from __future__ import annotations

import os
import struct
import wave
from unittest.mock import patch

from plugin.scripting.audio_silence_detector import (
    DEFAULT_SILENCE_STOP_MS,
    MIN_SPEECH_MS,
    SilenceDetector,
    SilenceDetectorConfig,
    _resolve_silence_stop_ms,
    load_silence_detector_config,
    pcm_energy_int16,
)


def _pcm_silence(sample_count: int) -> bytes:
    return b"\x00\x00" * sample_count


def _pcm_tone(sample_count: int, *, amplitude: int = 8000) -> bytes:
    samples = [amplitude if i % 2 == 0 else -amplitude for i in range(sample_count)]
    return struct.pack(f"<{sample_count}h", *samples)


def test_pcm_energy_silence_is_near_zero():
    rms, peak = pcm_energy_int16(_pcm_silence(320))
    assert rms < 0.001
    assert peak < 0.001


def test_pcm_energy_tone_is_above_threshold():
    rms, peak = pcm_energy_int16(_pcm_tone(320))
    assert rms > 0.1
    assert peak > 0.2


def test_pcm_energy_uses_full_pcm_not_truncated():
    """A later loud sample must count; CrossHair used to slice to 8 bytes."""
    pcm = _pcm_silence(8) + _pcm_tone(1, amplitude=32000)
    rms, peak = pcm_energy_int16(pcm)
    assert peak > 0.9
    assert rms > 0.0


def test_pcm_energy_int16_dropped_from_check_all_fqns():
    """Deep check-all run 32877875221 hung on the RMS/peak float post at [42/56]."""
    from pathlib import Path

    from tests.strip_bundle import skip_if_release_build

    skip_if_release_build("scripts/ not in stripped release tree")
    from scripts.crosshair_stream import cover_fqns_for_module

    fqns = cover_fqns_for_module(
        Path("plugin/scripting/audio_silence_detector.py"), require_deal=True
    )
    assert not any(f.endswith(".pcm_energy_int16") for f in fqns)


def test_silence_detector_requires_min_speech_before_auto_stop():
    config = SilenceDetectorConfig(silence_stop_ms=100)
    detector = SilenceDetector(config, sample_rate=16000)
    frame_count = 160  # 10 ms

    for _ in range(5):
        result = detector.process_chunk(_pcm_silence(frame_count), frame_count=frame_count)
        assert not result.should_stop

    for _ in range(50):
        result = detector.process_chunk(_pcm_tone(frame_count), frame_count=frame_count)
    assert result.speech_ms >= MIN_SPEECH_MS
    assert result.heard_speech
    assert not result.should_stop

    for _ in range(15):
        result = detector.process_chunk(_pcm_silence(frame_count), frame_count=frame_count)
    assert result.should_stop


def test_brief_loud_blip_does_not_count_as_heard_speech():
    config = SilenceDetectorConfig(silence_stop_ms=100)
    detector = SilenceDetector(config, sample_rate=16000)
    frame_count = 160  # 10 ms — well under MIN_SPEECH_MS
    detector.process_chunk(_pcm_tone(frame_count, amplitude=12000), frame_count=frame_count)
    for _ in range(12):
        result = detector.process_chunk(_pcm_silence(frame_count), frame_count=frame_count)
    assert result.speech_ms < MIN_SPEECH_MS
    assert not result.heard_speech
    assert not result.should_stop


def test_silence_stop_ms_zero_disables_auto_stop():
    detector = SilenceDetector(SilenceDetectorConfig(silence_stop_ms=0), sample_rate=16000)
    for _ in range(200):
        result = detector.process_chunk(_pcm_tone(160), frame_count=160)
    assert not result.should_stop


def test_speech_at_start_still_allows_auto_stop_after_pause():
    config = SilenceDetectorConfig(silence_stop_ms=100)
    detector = SilenceDetector(config, sample_rate=16000)
    frame_count = 160
    detector.process_chunk(_pcm_tone(frame_count, amplitude=12000), frame_count=frame_count)
    for _ in range(60):
        result = detector.process_chunk(_pcm_tone(frame_count), frame_count=frame_count)
    for _ in range(12):
        result = detector.process_chunk(_pcm_silence(frame_count), frame_count=frame_count)
    assert result.heard_speech
    assert result.should_stop


def test_should_emit_silence_progress_throttles_updates():
    config = SilenceDetectorConfig(silence_stop_ms=400)
    detector = SilenceDetector(config, sample_rate=16000)
    frame_count = 1600  # 100 ms
    detector.process_chunk(_pcm_tone(frame_count), frame_count=frame_count)

    first = detector.process_chunk(_pcm_silence(frame_count), frame_count=frame_count)
    assert first.silence_ms >= 100
    assert detector.should_emit_silence_progress(first)
    assert not detector.should_emit_silence_progress(first)


def test_resolve_silence_stop_ms_prefers_chatbot_key():
    with patch("plugin.framework.config.get_config_dict", return_value={"chatbot.audio_silence_stop_ms": 2500}):
        assert _resolve_silence_stop_ms() == 2500


def test_resolve_silence_stop_ms_ignores_legacy_flat_key():
    with patch(
        "plugin.framework.config.get_config_dict",
        return_value={"audio_silence_stop_ms": 1500},
    ):
        assert _resolve_silence_stop_ms() == DEFAULT_SILENCE_STOP_MS


def test_resolve_silence_stop_ms_zero_disables():
    with patch(
        "plugin.framework.config.get_config_dict",
        return_value={"chatbot.audio_silence_stop_ms": 0},
    ):
        assert _resolve_silence_stop_ms() == 0


def test_five_second_fixture_has_long_pause_then_auto_stop_after_speech():
    """5s MP3 is leading silence + phrase (speech at the tail). Auto-stop needs
    silence *after* MIN_SPEECH — feed the fixture then trailing zeros like a
    real end-of-utterance, without a microphone."""
    wav_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "chatbot",
        "fixtures",
        "hello-writeragent-5s.wav",
    )
    wav_path = os.path.abspath(wav_path)
    if not os.path.isfile(wav_path):
        return
    detector = SilenceDetector(SilenceDetectorConfig(silence_stop_ms=3000), sample_rate=16000)
    with wave.open(wav_path, "rb") as wf:
        assert wf.getframerate() == 16000
        nframes = wf.getnframes()
        assert nframes / 16000.0 >= 5.0
        frames = 1600
        while True:
            pcm = wf.readframes(frames)
            if not pcm:
                break
            detector.process_chunk(pcm, frame_count=len(pcm) // 2)
    stopped = False
    heard = False
    silence = _pcm_silence(1600)
    for _idx in range(40):
        result = detector.process_chunk(silence, frame_count=1600)
        heard = heard or result.heard_speech
        if result.should_stop:
            stopped = True
            break
    assert heard
    assert stopped, "trailing silence after 5s fixture should auto-stop"


def test_resolve_silence_stop_ms_default_when_unset():
    with patch("plugin.framework.config.get_config_dict", return_value={}):
        assert _resolve_silence_stop_ms() == DEFAULT_SILENCE_STOP_MS


def test_load_silence_detector_config_wraps_resolve():
    with patch("plugin.scripting.audio_silence_detector._resolve_silence_stop_ms", return_value=3000):
        cfg = load_silence_detector_config()
    assert cfg.silence_stop_ms == 3000
    assert cfg.enabled is True


def test_audio_record_main_uses_silence_stop_ms_only(tmp_path):
    from unittest.mock import patch

    from plugin.scripting.venv.audio_record_main import main

    def fake_record(_output, _stop_event, *, on_stream_started=None, silence_config=None, on_ipc_emit=None):
        if on_stream_started is not None:
            on_stream_started()
        assert silence_config is not None
        assert silence_config.silence_stop_ms == 0
        assert silence_config.enabled is False
        return False

    class _NoOpThread:
        def __init__(self, target, args, daemon):
            pass

        def start(self):
            return None

    out_file = str(tmp_path / "t.wav")
    with patch("plugin.scripting.venv.audio_record_main.record_to_wav", side_effect=fake_record):
        with patch("plugin.scripting.venv.audio_record_main._emit"):
            with patch("plugin.scripting.venv.audio_record_main.threading.Thread", _NoOpThread):
                rc = main(["--output", out_file, "--silence-stop-ms", "0"])
    assert rc == 0


def test_monitor_recording_stdout_invokes_auto_stop_callback(tmp_path):
    import json
    from io import StringIO
    from unittest.mock import MagicMock

    from plugin.scripting.audio_recorder_service import monitor_recording_stdout

    voice_file = str(tmp_path / "voice.wav")
    proc = MagicMock()
    proc.poll.side_effect = [None, None, 0]
    proc.stdout = StringIO(
        json.dumps({"status": "silence_progress", "ms": 500}) + "\n"
        + json.dumps({"status": "auto_stopped", "path": voice_file}) + "\n"
    )
    seen: list[str] = []
    progress: list[int] = []

    thread = monitor_recording_stdout(
        proc,
        on_auto_stopped=seen.append,
        on_silence_progress=progress.append,
    )
    thread.join(timeout=2.0)
    proc.stdout.close()

    assert seen == [voice_file]
    assert progress == [500]


def test_stop_recording_process_uses_fallback_when_child_already_exited(tmp_path):
    from io import StringIO
    from unittest.mock import MagicMock

    from plugin.scripting.audio_recorder_service import stop_recording_process

    fallback_file = str(tmp_path / "fallback.wav")
    proc = MagicMock()
    proc.poll.return_value = 0
    proc.stdout = StringIO("")
    assert stop_recording_process(proc, fallback_path=fallback_file) == fallback_file

