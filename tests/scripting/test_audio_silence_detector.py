"""Unit tests for RMS silence / end-of-speech detection."""

from __future__ import annotations

import struct
from unittest.mock import patch

from plugin.scripting.audio_silence_detector import (
    DEFAULT_SILENCE_STOP_MS,
    MIN_SPEECH_MS,
    SilenceDetector,
    SilenceDetectorConfig,
    _resolve_silence_stop_ms,
    load_silence_detector_config,
    peak_normalized_int16,
    rms_normalized_int16,
)


def _pcm_silence(sample_count: int) -> bytes:
    return b"\x00\x00" * sample_count


def _pcm_tone(sample_count: int, *, amplitude: int = 8000) -> bytes:
    samples = [amplitude if i % 2 == 0 else -amplitude for i in range(sample_count)]
    return struct.pack(f"<{sample_count}h", *samples)


def test_rms_silence_is_near_zero():
    assert rms_normalized_int16(_pcm_silence(320)) < 0.001


def test_rms_tone_is_above_threshold():
    assert rms_normalized_int16(_pcm_tone(320)) > 0.1


def test_peak_tone_is_detectable():
    assert peak_normalized_int16(_pcm_tone(320)) > 0.2


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


def test_heard_speech_fallback_uses_session_peak_when_classifier_misses():
    config = SilenceDetectorConfig(silence_stop_ms=100)
    detector = SilenceDetector(config, sample_rate=16000)
    frame_count = 160
    detector.process_chunk(_pcm_tone(frame_count, amplitude=12000), frame_count=frame_count)
    for _ in range(12):
        result = detector.process_chunk(_pcm_silence(frame_count), frame_count=frame_count)
    assert result.heard_speech
    assert result.should_stop


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
        assert _resolve_silence_stop_ms(None) == 2500


def test_resolve_silence_stop_ms_legacy_flat_key():
    with patch(
        "plugin.framework.config.get_config_dict",
        return_value={"audio_silence_stop_ms": 1500},
    ):
        assert _resolve_silence_stop_ms(None) == 1500


def test_resolve_silence_stop_ms_zero_disables():
    with patch(
        "plugin.framework.config.get_config_dict",
        return_value={"chatbot.audio_silence_stop_ms": 0},
    ):
        assert _resolve_silence_stop_ms(None) == 0


def test_resolve_silence_stop_ms_default_when_unset():
    with patch("plugin.framework.config.get_config_dict", return_value={}):
        assert _resolve_silence_stop_ms(None) == DEFAULT_SILENCE_STOP_MS


def test_load_silence_detector_config_wraps_resolve():
    with patch("plugin.scripting.audio_silence_detector._resolve_silence_stop_ms", return_value=3000):
        cfg = load_silence_detector_config(None)
    assert cfg.silence_stop_ms == 3000
    assert cfg.enabled is True


def test_audio_record_main_uses_silence_stop_ms_only():
    from unittest.mock import patch

    from plugin.scripting.venv.audio_record_main import main

    def fake_record(_output, _stop_event, *, on_stream_started=None, silence_config=None, on_ipc_emit=None):
        if on_stream_started is not None:
            on_stream_started()
        assert silence_config is not None
        assert silence_config.silence_stop_ms == 0
        assert silence_config.enabled is False
        return False

    with patch("plugin.scripting.venv.audio_record_main.record_to_wav", side_effect=fake_record):
        with patch("plugin.scripting.venv.audio_record_main._emit"):
            rc = main(["--output", "/tmp/t.wav", "--silence-stop-ms", "0"])
    assert rc == 0


def test_monitor_recording_stdout_invokes_auto_stop_callback():
    import json
    from io import StringIO
    from unittest.mock import MagicMock

    from plugin.scripting.audio_recorder_service import monitor_recording_stdout

    proc = MagicMock()
    proc.poll.side_effect = [None, None, 0]
    proc.stdout = StringIO(
        json.dumps({"status": "silence_progress", "ms": 500}) + "\n"
        + json.dumps({"status": "auto_stopped", "path": "/tmp/voice.wav"}) + "\n"
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

    assert seen == ["/tmp/voice.wav"]
    assert progress == [500]


def test_stop_recording_process_uses_fallback_when_child_already_exited():
    from io import StringIO
    from unittest.mock import MagicMock

    from plugin.scripting.audio_recorder_service import stop_recording_process

    proc = MagicMock()
    proc.poll.return_value = 0
    proc.stdout = StringIO("")
    assert stop_recording_process(proc, fallback_path="/tmp/fallback.wav") == "/tmp/fallback.wav"
