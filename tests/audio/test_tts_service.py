# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for TTS service playback, process management, and sentence pipeline."""

import os
import subprocess
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import plugin.audio.tts_service as tts
from plugin.audio.tts_service import (
    clean_text_for_speech,
    is_speaking,
    sentence_speak_enabled,
    sentences_for_speech,
    speak_text_async,
    stop_speech,
    uses_sentence_by_sentence,
)
from plugin.framework.send_cancellation import SendCancellation, wait_for_approval


def test_clean_text_for_speech_markdown():
    raw = (
        "# Heading\n\n"
        "Here is some **bold** and *italic* text with a [link](https://example.com).\n\n"
        "```python\ndef foo():\n    return 42\n```\n\n"
        "And some `inline code` here."
    )
    cleaned = clean_text_for_speech(raw)
    assert "#" not in cleaned
    assert "**" not in cleaned
    assert "*" not in cleaned
    assert "https://example.com" not in cleaned
    assert "link" in cleaned
    assert "inline code" in cleaned
    assert "def foo" not in cleaned
    assert "[code block omitted]" in cleaned


def test_clean_text_for_speech_empty():
    assert clean_text_for_speech("") == ""
    assert clean_text_for_speech("   ") == ""


def test_clean_text_for_speech_identifiers():
    assert clean_text_for_speech("my_var_name") == "my var name"
    assert clean_text_for_speech("**my_var_name**") == "my var name"
    assert clean_text_for_speech("__bold__ and _it_") == "bold and it"
    assert clean_text_for_speech("see https://x.org/a_b now") == "see link now"


def test_clean_text_for_speech_math_preserved():
    """Bug 8: Mathematical comparisons with < and > must not be stripped as HTML tags."""
    math_text = "if x<5 and y>3: return True"
    assert clean_text_for_speech(math_text) == "if x<5 and y>3: return True"

    mixed = "<p>Result: if a < 10 and b > 20 then alert('ok')</p>"
    cleaned = clean_text_for_speech(mixed)
    assert "<p>" not in cleaned
    assert "</p>" not in cleaned
    assert "a < 10 and b > 20" in cleaned

    # The old any-letter tag pattern treated "a<b and c>d" as a <b> tag, and
    # the old star pattern treated "2 * 3 * 4" as italic.
    assert clean_text_for_speech("a<b and c>d") == "a<b and c>d"
    assert clean_text_for_speech("2 * 3 * 4") == "2 * 3 * 4"
    assert clean_text_for_speech("2*3*4") == "2*3*4"
    assert clean_text_for_speech("<b>bold</b>") == "bold"
    assert clean_text_for_speech('<a href="https://example.com">t</a>') == "t"
    spoken = clean_text_for_speech(
        "- first item\n"
        "* second item\n"
        "> a quote\n"
        "| a | b |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
    )
    assert spoken == "first item second item a quote a b 1 2"
    assert "|" not in spoken
    assert "---" not in spoken


def test_speak_text_async_disabled_by_default():
    with patch("plugin.framework.config.get_config", return_value=False):
        with patch("plugin.audio.tts_service.run_in_background") as mock_bg:
            speak_text_async("Hello world")
            mock_bg.assert_not_called()


def test_speak_text_async_enabled_system():
    def _cfg(key, default=None):
        if key == "audio.tts_enabled":
            return True
        if key == "audio.tts_provider":
            return "system"
        if key == "audio.tts_speed":
            return 1.0
        return default

    with patch("plugin.framework.config.get_config", side_effect=_cfg):
        with patch("plugin.audio.tts_service._speak_system") as mock_sys:
            with patch("plugin.audio.tts_service.run_in_background", side_effect=lambda fn, **kw: fn()):
                completed = False

                def _done():
                    nonlocal completed
                    completed = True

                speak_text_async("Testing system speech", on_complete=_done)
                mock_sys.assert_called_once()
                assert mock_sys.call_args.args == ("Testing system speech",)
                assert mock_sys.call_args.kwargs["speed"] == 1.0
                assert isinstance(mock_sys.call_args.kwargs["cancel"], SendCancellation)
                assert completed is True


def test_stop_speech_terminates_proc():
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with patch.object(tts, "_procs", [mock_proc]):
        assert is_speaking() is True
        stop_speech()
        mock_proc.kill.assert_called_once()
        mock_proc.terminate.assert_not_called()


def test_is_speaking_lifecycle():
    assert is_speaking() is False
    with patch("plugin.audio.tts_service._speech_active", True):
        assert is_speaking() is True
    assert is_speaking() is False


def test_get_tts_model_sanitizes_placeholder():
    from plugin.framework.client.model_fetcher import get_tts_model

    with patch("plugin.framework.client.model_fetcher.get_config", return_value="(Default for current endpoint)"):
        with patch("plugin.framework.client.model_fetcher.get_current_endpoint", return_value="https://openrouter.ai/api"):
            model = get_tts_model()
            assert model == "hexgrad/Kokoro-82M"
            assert "(Default for current endpoint)" not in model


def test_speak_text_async_routing_local():
    cfg = {
        "audio.tts_enabled": True,
        "audio.tts_provider": "kokoro",
        "audio.tts_speed": 1.1,
        "audio.tts_voice_kokoro": "af_bella",
    }
    with patch("plugin.framework.config.get_config", side_effect=lambda k, d=None: cfg.get(k, d)), \
         patch("plugin.audio.tts_service.run_in_background", side_effect=lambda fn, **kw: fn()), \
         patch("plugin.audio.tts_service._speak_local") as mock_local:
        speak_text_async("Hello Kokoro")
        assert mock_local.call_args.args == ("kokoro", "Hello Kokoro")
        assert mock_local.call_args.kwargs["voice"] == "af_bella"
        assert mock_local.call_args.kwargs["speed"] == 1.1
        assert mock_local.call_args.kwargs["on_status"] is None
        assert isinstance(mock_local.call_args.kwargs["utt"], tts.Utterance)

        cfg["audio.tts_provider"] = "piper"
        cfg["audio.tts_voice_piper"] = "en_US-lessac-medium"
        speak_text_async("Hello Piper")
        assert mock_local.call_args.args == ("piper", "Hello Piper")
        assert mock_local.call_args.kwargs["voice"] == "en_US-lessac-medium"
        assert mock_local.call_args.kwargs["speed"] == 1.1
        assert mock_local.call_args.kwargs["on_status"] is None


def test_speak_text_async_uses_dialog_overrides_when_config_disabled():
    with patch("plugin.framework.config.get_config", return_value=False), \
         patch("plugin.audio.tts_service.run_in_background", side_effect=lambda fn, **kw: fn()), \
         patch("plugin.audio.tts_service._speak_local") as mock_local:
        speak_text_async(
            "Bonjour",
            provider="Kokoro (Local Neural, ONNX CPU)",
            voice="jf_alpha (Kokoro JP Female - Alpha)",
            speed=1.25,
            enabled=True,
        )
        mock_local.assert_called_once()
        assert mock_local.call_args.args == ("kokoro", "Bonjour")
        assert mock_local.call_args.kwargs["voice"] == "jf_alpha"
        assert mock_local.call_args.kwargs["speed"] == 1.25
        assert mock_local.call_args.kwargs["on_status"] is None
        assert isinstance(mock_local.call_args.kwargs["utt"], tts.Utterance)


def test_speak_kokoro_local_uses_misaki_script_for_non_english(tmp_path):
    import plugin.audio.tts_service as tts
    from plugin.audio.tts_service import _speak_local

    utt = tts._begin_utterance()


    model = tmp_path / "kokoro-v1.0.onnx"
    voices = tmp_path / "voices-v1.0.bin"
    model.write_bytes(b"m")
    voices.write_bytes(b"v")
    py = tmp_path / "python"
    py.write_text("")

    def _launch(text: str, voice: str) -> tuple[list[str], object]:
        launched: list[list[str]] = []

        def fake_popen(cmd, **kwargs):
            launched.append(list(cmd))
            with open(cmd[6], "wb") as handle:
                handle.write(b"RIFF")

            class _Proc:
                returncode = 0

                def communicate(self, input=None, timeout=None):
                    return "", ""

                def poll(self):
                    return 0

            return _Proc()

        ensure = MagicMock(return_value=True)
        with patch("plugin.audio.tts_service.resolve_kokoro_python", return_value=str(py)), \
             patch("plugin.audio.tts_service.resolve_kokoro_model_files", return_value=(str(model), str(voices))), \
             patch("plugin.audio.tts_service.ensure_kokoro_misaki", ensure), \
             patch("plugin.framework.managed_subprocess.subprocess.Popen", side_effect=fake_popen), \
             patch("plugin.audio.tts_service.get_kokoro_pool", return_value=None), \
             patch("plugin.audio.tts_service._play_audio_file") as play:
            _speak_local("kokoro", text, voice=voice, speed=1.0, utt=utt)
        assert play.called
        assert len(launched) == 1
        return launched[0], ensure

    ja_cmd, ja_ensure = _launch("こんにちは", "jf_alpha")
    assert ja_cmd[1].endswith("kokoro_worker.py")
    assert ja_cmd[2] == "--once"
    assert ja_cmd[3] == "-"
    assert ja_cmd[4] == "jf_alpha"
    assert ja_cmd[9] == "ja"


def test_speak_kokoro_local_skips_synthesis_when_phonemizer_install_cancelled(tmp_path):
    import plugin.audio.tts_service as tts
    from plugin.audio.tts_service import _speak_local

    utt = tts._begin_utterance()


    model = tmp_path / "kokoro-v1.0.onnx"
    voices = tmp_path / "voices-v1.0.bin"
    model.write_bytes(b"m")
    voices.write_bytes(b"v")

    with patch("plugin.audio.tts_service.resolve_kokoro_python", return_value=str(tmp_path / "python")), \
         patch("plugin.audio.tts_service.resolve_kokoro_model_files", return_value=(str(model), str(voices))), \
         patch("plugin.audio.tts_service.ensure_kokoro_misaki", return_value=None), \
         patch("plugin.framework.managed_subprocess.subprocess.Popen") as popen, \
         patch("plugin.audio.tts_service._speak_system") as system:
        _speak_local("kokoro", "こんにちは", voice="jf_alpha", speed=1.0, utt=utt)

    popen.assert_not_called()
    system.assert_not_called()


def test_sentence_pipeline_installs_misaki_once_per_reply(tmp_path):
    import plugin.audio.tts_service as tts

    ensure = MagicMock(return_value=True)
    synthesized: list[str] = []

    def synth(sentence, provider, voice, speed, on_status, cancel):
        del provider, voice, speed, on_status, cancel
        synthesized.append(sentence)
        path = tmp_path / f"{len(synthesized)}.wav"
        path.write_bytes(b"RIFF")
        return tts._ReadyClip(str(path), sentence, 4)

    utt = tts._begin_utterance()

    try:
        with patch.object(tts, "ensure_kokoro_misaki", ensure), \
             patch.object(tts, "resolve_kokoro_python", return_value="/venv/bin/python"), \
             patch.object(tts, "_synthesize_sentence_clip", side_effect=synth), \
             patch.object(tts, "_play_audio_file"), \
             patch.object(tts, "_speak_system") as system:
            tts._run_sentence_pipeline(
                ["一。", "二。", "三。"],
                "kokoro",
                "jf_alpha",
                1.0,
                None,
                utt,
            )
    finally:
        tts.stop_speech()

    assert synthesized == ["一。", "二。", "三。"]
    ensure.assert_called_once()
    assert ensure.call_args.args[0] == "/venv/bin/python"
    assert ensure.call_args.args[1] == "ja"
    system.assert_not_called()


def test_sentence_pipeline_stop_during_misaki_install_skips_os_speech():
    import plugin.audio.tts_service as tts

    utt = tts._begin_utterance()

    try:
        with patch.object(tts, "ensure_kokoro_misaki", return_value=None), \
             patch.object(tts, "resolve_kokoro_python", return_value="/venv/bin/python"), \
             patch.object(tts, "_synthesize_sentence_clip") as synth, \
             patch.object(tts, "_speak_system") as system:
            tts._run_sentence_pipeline(
                ["こんにちは。", "次の文。"],
                "kokoro",
                "jf_alpha",
                1.0,
                None,
                utt,
            )
    finally:
        tts.stop_speech()

    synth.assert_not_called()
    system.assert_not_called()


class _FakeSentenceBI:
    def endOfSentence(self, text, pos, locale):
        import re

        match = re.search(r"[.!?]", text[pos:])
        if match:
            return pos + match.end()
        return len(text)


def test_sentences_for_speech_uses_grammar_splitter_and_keeps_short_fragments():
    with patch(
        "plugin.writer.locale.grammar_proofread_text.get_break_iterator_and_locale",
        return_value=(_FakeSentenceBI(), "en-US"),
    ), patch(
        "plugin.writer.locale.grammar_proofread_locale.filter_sentence_spans_for_thresholds",
        side_effect=AssertionError("TTS must not apply the grammar churn filter"),
    ):
        spoken = sentences_for_speech("Hi. No", object())

    assert spoken == ["Hi.", "No"]


def test_sentences_for_speech_merges_dialogue():
    with patch(
        "plugin.writer.locale.grammar_proofread_text.get_break_iterator_and_locale",
        return_value=(_FakeSentenceBI(), "en-US"),
    ):
        spoken = sentences_for_speech('"Fire! Fire!" he yelled.', object())

    assert spoken == ['"Fire! Fire!" he yelled.']


def test_prefetch_keeps_synthesizing_during_playback(tmp_path):
    import plugin.audio.tts_service as tts

    short = "Hi."
    longer = "This sentence is much longer and should already be synthesizing."
    long_started = threading.Event()
    paths = []

    def synth(sentence, provider, voice, speed, on_status, cancel):
        del provider, voice, speed, on_status, cancel
        if sentence == longer:
            long_started.set()
        path = tmp_path / f"{len(paths)}.wav"
        path.write_bytes(b"RIFF")
        paths.append(str(path))
        return tts._ReadyClip(str(path), sentence, 4)

    def play(path, cancel=None):
        del path
        if cancel is not None and not long_started.is_set():
            assert long_started.wait(2), "next sentence was not synthesizing while the short one played"
        return True

    utt = tts._begin_utterance()

    try:
        with patch.object(tts, "_prepare_kokoro_misaki", return_value=True), patch.object(
            tts, "_synthesize_sentence_clip", side_effect=synth
        ), patch.object(
            tts, "_play_audio_file", side_effect=play
        ):
            tts._run_sentence_pipeline(
                [short, longer],
                "kokoro",
                "af_bella",
                1.0,
                None,
                utt,
            )
    finally:
        tts.stop_speech()
    assert long_started.is_set()


@pytest.mark.parametrize("max_clips", [2, None])
def test_ready_queue_backpressure_stops_past_the_cap(max_clips):
    """Prefetch stops one clip past the cap: one playing, max_clips queued, one blocked in put()."""
    import plugin.audio.tts_service as tts

    cap = tts.SPEECH_READY_MAX_CLIPS if max_clips is None else max_clips
    sentences = ["A.", "B.", "C.", "D.", "E.", "F.", "G.", "H.", "I."]
    started: list[str] = []
    release_play = threading.Event()

    def synth(sentence, provider, voice, speed, on_status, cancel):
        del provider, voice, speed, on_status, cancel
        started.append(sentence)
        return tts._ReadyClip(None, sentence, 0)

    def play(path, cancel=None):
        del path, cancel

    def speak_system(text, speed=1.0, *, cancel=None):
        del text, speed, cancel
        release_play.wait(30)

    utt = tts._begin_utterance()

    finished = threading.Event()

    def run() -> None:
        try:
            with patch.object(tts, "SPEECH_READY_MAX_CLIPS", cap), patch.object(
                tts, "_synthesize_sentence_clip", side_effect=synth
            ), patch.object(tts, "_play_audio_file", side_effect=play), patch.object(
                tts, "_speak_system", side_effect=speak_system
            ):
                tts._run_sentence_pipeline(
                    sentences,
                    "kokoro",
                    "af_bella",
                    1.0,
                    None,
                    utt,
                )
        finally:
            finished.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(started) < cap + 2:
            time.sleep(0.02)
        time.sleep(0.2)
        assert started == sentences[: cap + 2]
        release_play.set()
        assert finished.wait(2)
    finally:
        release_play.set()
        tts.stop_speech()
        worker.join(2)
    assert started == sentences


def test_stop_drops_ready_clips_and_ignores_late_synth(tmp_path):
    import plugin.audio.tts_service as tts

    played: list[str] = []
    hold_second = threading.Event()
    first_playing = threading.Event()
    late_path = tmp_path / "S.wav"

    def synth(sentence, provider, voice, speed, on_status, cancel):
        del provider, voice, speed, on_status, cancel
        if sentence.startswith("Second"):
            hold_second.wait(2)
        path = tmp_path / f"{sentence[:1]}.wav"
        path.write_bytes(b"wav")
        return tts._ReadyClip(str(path), sentence, 3)

    def play(path, cancel=None):
        played.append(path)
        first_playing.set()
        while cancel is not None and not cancel.is_cancelled():
            threading.Event().wait(0.01)

    utt = tts._begin_utterance()

    finished = threading.Event()
    system_mock: list[Any] = []

    def run() -> None:
        try:
            with patch.object(tts, "resolve_kokoro_python", return_value="/venv/bin/python"), patch.object(
                tts, "_prepare_kokoro_misaki", return_value=True
            ), patch.object(tts, "_synthesize_sentence_clip", side_effect=synth), patch.object(
                tts, "_play_audio_file", side_effect=play
            ), patch.object(tts, "_speak_system") as mock_system:
                system_mock.append(mock_system)
                tts._run_sentence_pipeline(
                    ["First sentence.", "Second sentence."],
                    "kokoro",
                    "af_bella",
                    1.0,
                    None,
                    utt,
                )
        finally:
            finished.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert first_playing.wait(2)
        tts.stop_speech()
        hold_second.set()
        assert finished.wait(2)
        assert system_mock and system_mock[0].called is False
    finally:
        hold_second.set()
        tts.stop_speech()
        worker.join(2)
    assert len(played) == 1
    assert not late_path.exists()


def _wait_until(deadline: float, predicate, *, interval: float = 0.02) -> bool:
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_stop_during_synthesis_plays_nothing(tmp_path):
    """Stop while synthesis is blocked must not play or fall back to system speech."""
    import plugin.audio.tts_service as tts

    synth_entered = threading.Event()
    release_synth = threading.Event()
    prefetch_done = threading.Event()
    temp_path: list[str] = []
    play_calls: list[str] = []
    system_calls: list[str] = []
    real_bg = tts.run_in_background

    def track_bg(fn, **kwargs):
        if kwargs.get("name") != "tts-prefetch":
            return real_bg(fn, **kwargs)

        def wrapped() -> None:
            try:
                fn()
            finally:
                prefetch_done.set()

        return real_bg(wrapped, **kwargs)

    def blocking_synth(sentence, provider, voice, speed, on_status, utt):
        del sentence, provider, voice, speed, on_status
        cancel = utt.scope
        path = tts._new_speech_temp(".wav", cancel)
        assert path is not None
        temp_path.append(path)
        with open(path, "wb") as handle:
            handle.write(b"wav")
        synth_entered.set()
        if not wait_for_approval(release_synth, cancel, timeout=0.05):
            tts._release_temp(path)
            return None
        return tts._ReadyClip(path, "Hello.", 3)

    def track_play(path, cancel=None):
        del cancel
        play_calls.append(path)
        return True

    def track_system(text, speed=1.0, *, cancel=None):
        del speed, cancel
        system_calls.append(text)

    utt = tts._begin_utterance()
    scope = utt.scope
    finished = threading.Event()

    def run() -> None:
        try:
            with patch.object(tts, "run_in_background", side_effect=track_bg), patch.object(
                tts, "resolve_kokoro_python", return_value="/venv/bin/python"
            ), patch.object(tts, "_prepare_kokoro_misaki", return_value=True), patch.object(
                tts, "_synthesize_sentence_clip", side_effect=blocking_synth
            ), patch.object(tts, "_play_audio_file", side_effect=track_play), patch.object(
                tts, "_speak_system", side_effect=track_system
            ):
                tts._run_sentence_pipeline(
                    ["Hello."],
                    "kokoro",
                    "af_bella",
                    1.0,
                    None,
                    utt,
                )
        finally:
            finished.set()

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 30.0
    try:
        assert _wait_until(deadline, synth_entered.is_set), "synthesis did not reach blocked state"
        tts.stop_speech()
        assert scope.is_cancelled()
        assert _wait_until(deadline, finished.is_set), "sentence pipeline did not finish after stop"
        assert _wait_until(deadline, prefetch_done.is_set), "prefetch thread did not finish after stop"
        assert play_calls == []
        assert system_calls == []
        tracked = temp_path[0] if temp_path else ""
        assert tracked
        assert _wait_until(deadline, lambda: not os.path.exists(tracked)), (
            f"temp file still present after stop: {tracked}"
        )
    finally:
        release_synth.set()
        tts.stop_speech()
        worker.join(5)


def test_stop_before_start_spawns_nothing():
    """stop_speech before the pipeline runs must not spawn synth or playback."""
    import plugin.audio.tts_service as tts

    utt = tts._begin_utterance()
    tts.stop_speech()

    with patch.object(tts, "resolve_kokoro_python", return_value="/venv/bin/python"), patch.object(
        tts, "_prepare_kokoro_misaki", return_value=True
    ), patch.object(tts, "_popen_for_speech") as mock_popen, patch.object(
        tts, "_synthesize_sentence_clip"
    ) as mock_synth, patch.object(tts, "_play_audio_file") as mock_play:
        tts._run_sentence_pipeline(
            ["Hello."],
            "kokoro",
            "af_bella",
            1.0,
            None,
            utt,
        )
        mock_popen.assert_not_called()
        mock_synth.assert_not_called()
        mock_play.assert_not_called()


def test_stop_speech_twice_is_idempotent(tmp_path):
    """Repeated stop_speech is safe while cleanup runs on a background thread."""
    import plugin.audio.tts_service as tts

    hook_calls: list[bool] = []
    temp = tmp_path / "tracked.wav"
    temp.write_bytes(b"x")

    def hook() -> None:
        hook_calls.append(True)

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    utt = tts._begin_utterance()
    utt.scope.register_on_cancel(hook)
    with tts._speech_lock:
        utt.temps.add(str(temp))
        tts._procs.append(mock_proc)

    tts.stop_speech()
    tts.stop_speech()

    assert len(hook_calls) == 1
    mock_proc.kill.assert_called_once()
    assert is_speaking() is False
    deadline = time.monotonic() + 2.0
    while temp.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not temp.exists()


def test_uses_sentence_by_sentence_only_kokoro_and_piper():
    assert uses_sentence_by_sentence("kokoro") is True
    assert uses_sentence_by_sentence("piper") is True
    assert uses_sentence_by_sentence("Kokoro (Local Neural, ONNX CPU)") is True
    assert uses_sentence_by_sentence("Piper (Local Fast Neural, CPU)") is True
    assert uses_sentence_by_sentence("system") is False
    assert uses_sentence_by_sentence("OS Native (say / SAPI / spd-say)") is False
    assert uses_sentence_by_sentence("endpoint") is False
    assert uses_sentence_by_sentence("LLM Endpoint") is False
    assert uses_sentence_by_sentence("Current Chat Endpoint") is False
    assert uses_sentence_by_sentence("openrouter") is False
    assert uses_sentence_by_sentence("together") is False


def test_sentence_speak_enabled_uses_schema_default_on():
    from plugin.framework.config_schema import resolve_default

    assert resolve_default("audio.tts_sentence_mode") is True
    with patch(
        "plugin.audio.tts_service.config.get_config",
        side_effect=lambda key: resolve_default(key),
    ):
        assert sentence_speak_enabled() is True


def _speak_sentence_case(cfg, *, provider=None, model=None):
    import plugin.audio.tts_service as tts

    captured: dict[str, object] = {}
    oneshot: dict[str, str] = {}
    real_split = tts.sentences_for_speech

    def fake_pipeline(sentences, prov, voice, speed, on_status, utt, **kwargs):
        del voice, speed, on_status, utt, kwargs
        captured["sentences"] = list(sentences)
        captured["provider"] = prov

    def spy_split(text, ctx):
        captured["split"] = True
        return real_split(text, ctx)

    def remember(name):
        def _call(*args, **kwargs):
            del kwargs
            oneshot[name] = args[0]
        return _call

    def remember_local(*args, **kwargs):
        del kwargs
        oneshot[args[0]] = args[1]

    def _get_config(key, default=None):
        if key == "audio.tts_sentence_mode" and key not in cfg:
            return True
        return cfg.get(key, default)

    with patch("plugin.framework.config.get_config", side_effect=_get_config), patch(
        "plugin.audio.tts_service.run_in_background", side_effect=lambda fn, **kwargs: fn()
    ), patch(
        "plugin.writer.locale.grammar_proofread_text.get_break_iterator_and_locale",
        return_value=(_FakeSentenceBI(), "en-US"),
    ), patch("plugin.audio.tts_service.sentences_for_speech", side_effect=spy_split), patch(
        "plugin.audio.tts_service._run_sentence_pipeline", side_effect=fake_pipeline
    ), patch("plugin.audio.tts_service._speak_system", side_effect=remember("system")), patch(
        "plugin.audio.tts_service._speak_local", side_effect=remember_local
    ), patch(
        "plugin.audio.tts_service._speak_endpoint", side_effect=remember("endpoint")
    ), patch(
        "plugin.framework.config.get_current_endpoint", return_value="https://openrouter.ai/api/v1"
    ), patch("plugin.framework.config.get_api_key_for_endpoint", return_value="test-key"):
        tts.speak_text_async(
            "Hi. Not done yet",
            ctx=object(),
            provider=provider,
            model=model,
            voice="af_sky",
        )
    return captured, oneshot


def test_kokoro_and_piper_honor_sentence_mode():
    for prov in ("kokoro", "piper"):
        cfg = {
            "audio.tts_enabled": True,
            "audio.tts_provider": prov,
            "audio.tts_speed": 1.0,
            "audio.tts_sentence_mode": True,
        }
        captured, oneshot = _speak_sentence_case(cfg)
        assert captured["split"] is True
        assert captured["sentences"] == ["Hi.", "Not done yet"]
        assert captured["provider"] == prov
        assert oneshot == {}

        cfg["audio.tts_sentence_mode"] = False
        captured, oneshot = _speak_sentence_case(cfg)
        assert "split" not in captured
        assert "sentences" not in captured
        assert oneshot == {prov: "Hi. Not done yet"}


def test_unset_sentence_mode_splits_kokoro():
    cfg = {
        "audio.tts_enabled": True,
        "audio.tts_provider": "kokoro",
        "audio.tts_speed": 1.0,
    }
    captured, oneshot = _speak_sentence_case(cfg)
    assert captured["sentences"] == ["Hi.", "Not done yet"]
    assert oneshot == {}


def test_endpoint_and_native_do_not_split():
    endpoint_cfg = {
        "audio.tts_enabled": True,
        "audio.tts_provider": "endpoint",
        "audio.tts_speed": 1.0,
        "audio.tts_sentence_mode": True,
    }
    captured, oneshot = _speak_sentence_case(endpoint_cfg, model="openai/gpt-4o-audio-preview")
    assert "split" not in captured
    assert "sentences" not in captured
    assert oneshot == {"endpoint": "Hi. Not done yet"}

    system_cfg = {
        "audio.tts_enabled": True,
        "audio.tts_provider": "system",
        "audio.tts_speed": 1.0,
        "audio.tts_sentence_mode": True,
    }
    captured, oneshot = _speak_sentence_case(system_cfg)
    assert "split" not in captured
    assert "sentences" not in captured
    assert oneshot == {"system": "Hi. Not done yet"}


@patch("plugin.audio.tts_service._popen_for_speech")
@patch("plugin.audio.tts_service.shutil.which")
def test_speak_system_security(mock_which, mock_popen):
    """Bug 5: Text fed via temp file/stdin or chunked to avoid OS argv limits."""
    from plugin.audio.tts_service import _speak_system

    text = "don't `rm -rf` --version"
    cancel = SendCancellation()

    # macOS uses -f <temp_file>
    with patch("sys.platform", "darwin"):
        _speak_system(text, speed=1.0, cancel=cancel)
        assert mock_popen.called
        cmd = mock_popen.call_args.args[0]
        assert cmd[0] == "/usr/bin/say"
        assert "-r" in cmd
        assert "-f" in cmd

    mock_popen.reset_mock()

    # Linux spd-say chunks argv
    with patch("sys.platform", "linux"):
        mock_which.side_effect = lambda x: "/usr/bin/spd-say" if x == "spd-say" else None
        _speak_system(text, speed=1.0, cancel=cancel)
        mock_popen.assert_called_with(["spd-say", "-r", "0", "-w", "--", text], cancel)

    mock_popen.reset_mock()

    # Linux espeak uses --stdin
    with patch("sys.platform", "linux"):
        mock_which.side_effect = lambda x: "/usr/bin/espeak" if x == "espeak" else None
        _speak_system(text, speed=1.0, cancel=cancel)
        cmd = mock_popen.call_args.args[0]
        assert cmd == ["espeak", "-s", "160", "--stdin"]
        assert mock_popen.call_args.kwargs["stdin"] == subprocess.PIPE

    mock_popen.reset_mock()

    # Windows passes file path in WA_SAPI_FILE env
    with patch("sys.platform", "win32"):
        _speak_system(text, speed=1.0, cancel=cancel)
        cmd = mock_popen.call_args.args[0]
        assert text not in " ".join(cmd)
        assert "$env:WA_SAPI_FILE" in cmd[-1]
        assert "WA_SAPI_FILE" in mock_popen.call_args.kwargs["env"]


def test_stop_speech_stops_spd_only_for_own_spd_say():
    from plugin.audio import tts_service

    spd = MagicMock()
    spd.args = ["spd-say", "-w", "--", "hi"]
    spd.poll.return_value = None
    other = MagicMock()
    other.args = ["paplay", "/tmp/x.wav"]
    other.poll.return_value = None
    assert tts_service._is_spd_say(spd) is True
    assert tts_service._is_spd_say(other) is False
    assert tts_service._is_spd_say(None) is False


def test_sentence_pipeline_producer_crash_speaks_only_remaining(tmp_path):
    import plugin.audio.tts_service as tts

    calls = []

    def synth(sentence, *args):
        del args
        calls.append(sentence)
        if sentence == "Two.":
            raise RuntimeError("boom")
        path = tmp_path / f"{len(calls)}.wav"
        path.write_bytes(b"RIFF")
        return tts._ReadyClip(str(path), sentence, 4)

    utt = tts._begin_utterance()

    try:
        with patch.object(tts, "_synthesize_sentence_clip", side_effect=synth), \
             patch.object(tts, "_play_audio_file") as play, \
             patch.object(tts, "_speak_system") as system:
            tts._run_sentence_pipeline(
                ["One.", "Two.", "Three."], "kokoro", "af_sky", 1.0, None, utt,
            )
    finally:
        tts.stop_speech()

    assert play.call_count == 1
    system.assert_called_once()
    assert system.call_args.args[0] == "Two. Three."


@patch("plugin.audio.tts_service._popen_for_speech", return_value=None)
def test_play_audio_file_windows_path_not_in_script(mock_popen):
    import plugin.audio.tts_service as tts_service
    from plugin.audio.tts_service import _play_audio_file

    cancel = SendCancellation()
    path = "C:\\Users\\o'neil $x\\clip.wav"
    with patch("sys.platform", "win32"):
        _play_audio_file(path, cancel)
    cmd = mock_popen.call_args.args[0]
    assert path not in " ".join(cmd)
    assert "$env:WA_AUDIO_PATH" in cmd[-1]
    assert cmd[-1] == tts_service._WIN_WAV_PLAYER
    assert mock_popen.call_args.kwargs["env"]["WA_AUDIO_PATH"] == path

    mock_popen.reset_mock()
    mp3 = "C:\\Users\\o'neil $x\\clip.mp3"
    with patch("sys.platform", "win32"):
        _play_audio_file(mp3, cancel)
    mp3_cmd = mock_popen.call_args.args[0]
    assert mp3 not in " ".join(mp3_cmd)
    assert "$env:WA_AUDIO_PATH" in mp3_cmd[-1]
    assert mp3_cmd[-1] == tts_service._WIN_MP3_PLAYER
    assert '"' not in mp3_cmd[-1]
    assert mock_popen.call_args.kwargs["env"]["WA_AUDIO_PATH"] == mp3


def test_windows_player_nonzero_exit_triggers_os_fallback():
    """A non-zero exit from the Windows MP3 player falls back to OS speech."""
    import plugin.audio.tts_service as tts
    from plugin.audio.tts_endpoint import _EndpointClip

    utt = tts._begin_utterance()
    mock_proc = MagicMock()
    mock_proc.wait.return_value = 1

    try:
        with patch("sys.platform", "win32"), \
             patch("plugin.audio.tts_service.download_endpoint_speech", return_value=_EndpointClip(b"ID3fake", ".mp3")), \
             patch("plugin.audio.tts_service._popen_for_speech", return_value=mock_proc), \
             patch("plugin.audio.tts_service._speak_system") as mock_system:
            tts._speak_endpoint("Hello world.", "https://example.com/api", "key", cancel=utt.scope)
        mock_system.assert_called_once()
        assert mock_system.call_args.args[0] == "Hello world."
    finally:
        tts.stop_speech()


def test_piper_timeout_scaling_and_reap(tmp_path):
    """Bug 2: Piper timeout scales with text length; killed process is reaped."""
    from plugin.audio.tts_service import _piper_audio_file

    utt = tts._begin_utterance()
    mock_proc = MagicMock()
    mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd=["piper"], timeout=30)
    mock_proc.returncode = None

    with patch("plugin.audio.tts_service.resolve_kokoro_python", return_value="/venv/bin/python"), \
         patch("plugin.audio.tts_service.resolve_piper_model_file", return_value="/models/v.onnx"), \
         patch("os.path.isfile", return_value=True), \
         patch("plugin.audio.tts_service._popen_for_speech", return_value=mock_proc):
        # Long text
        long_text = "Word " * 200
        try:
            _piper_audio_file(long_text, "voice", 1.0, None, utt)
        except Exception:
            pass

        # Verify communicate received scaled timeout
        assert mock_proc.communicate.called
        timeout_used = mock_proc.communicate.call_args.kwargs["timeout"]
        assert timeout_used > 30.0

        # Verify process was killed and reaped (wait called)
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_called_once()


def test_piper_rejects_nonzero_exit_even_when_wav_is_nonempty(tmp_path):
    from plugin.audio.tts_service import _piper_audio_file

    utt = tts._begin_utterance()
    wav = tmp_path / "out.wav"
    wav.write_bytes(b"RIFF")
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = ("", "boom")
    mock_proc.returncode = 2

    with patch("plugin.audio.tts_service.resolve_kokoro_python", return_value="/venv/bin/python"), \
         patch("plugin.audio.tts_service.resolve_piper_model_file", return_value="/models/v.onnx"), \
         patch("os.path.isfile", return_value=True), \
         patch("plugin.audio.tts_service._new_speech_temp", return_value=str(wav)), \
         patch("plugin.audio.tts_service._popen_for_speech", return_value=mock_proc), \
         patch("plugin.audio.tts_service._release_temp") as release:
        assert _piper_audio_file("hello", "voice", 1.0, None, utt) is None
    release.assert_called_with(str(wav))


def test_run_synth_to_temp_returns_a_path_only_for_a_nonempty_file(tmp_path):
    from plugin.audio.tts_service import _run_synth_to_temp

    cancel = SendCancellation()
    wav = tmp_path / "out.wav"
    with patch("plugin.audio.tts_service._run_synth", return_value=(0, "", "")), \
         patch("plugin.audio.tts_service._release_temp") as release:
        assert _run_synth_to_temp(str(wav), ["cmd"], cancel, label="T") == (None, "")
        release.assert_called_once_with(str(wav))

    wav.write_bytes(b"RIFF")
    with patch("plugin.audio.tts_service._run_synth", return_value=(2, "", "boom")), \
         patch("plugin.audio.tts_service._release_temp") as release:
        assert _run_synth_to_temp(str(wav), ["cmd"], cancel, label="T") == (None, "")
        release.assert_called_once_with(str(wav))

    with patch("plugin.audio.tts_service._run_synth", return_value=(0, "", "warn")), \
         patch("plugin.audio.tts_service._release_temp") as release:
        assert _run_synth_to_temp(str(wav), ["cmd"], cancel, label="T") == (str(wav), "")
    release.assert_not_called()

    with patch("plugin.audio.tts_service._run_synth", return_value=(0, '{"g2p": "espeak"}', "")), \
         patch("plugin.audio.tts_service._release_temp") as release:
        assert _run_synth_to_temp(
            str(wav), ["cmd"], cancel, label="T"
        ) == (str(wav), '{"g2p": "espeak"}')
    release.assert_not_called()


def test_play_audio_file_player_order_and_fallback(tmp_path):
    """Bug 3: Player chosen by suffix, exit code checked, fallback on failure."""
    from plugin.audio.tts_service import _play_audio_file

    cancel = SendCancellation()
    wav_file = str(tmp_path / "test.wav")
    mp3_file = str(tmp_path / "test.mp3")

    # For WAV: order is ffplay, mpv, pw-play, paplay, aplay (mpg123 excluded)
    with patch("sys.platform", "linux"), patch("shutil.which", return_value=True):
        from plugin.audio.tts_service import _get_player_candidates
        wav_players = [p[0] for p in _get_player_candidates(wav_file)]
        assert "mpg123" not in wav_players
        assert wav_players == ["ffplay", "mpv", "pw-play", "paplay", "aplay"]

        # For MP3: order is ffplay, mpv, mpg123 (aplay/paplay excluded)
        mp3_players = [p[0] for p in _get_player_candidates(mp3_file)]
        assert "aplay" not in mp3_players
        assert mp3_players == ["ffplay", "mpv", "mpg123"]

    # When first player exits non-zero, next candidate is tried
    proc1 = MagicMock()
    proc1.wait.return_value = 1  # Failure
    proc2 = MagicMock()
    proc2.wait.return_value = 0  # Success

    with patch("plugin.audio.tts_service._get_player_candidates", return_value=[
        ("ffplay", ["ffplay", wav_file], None),
        ("mpv", ["mpv", wav_file], None),
    ]), patch("plugin.audio.tts_service._popen_for_speech", side_effect=[proc1, proc2]):
        assert _play_audio_file(wav_file, cancel) is True
        assert proc1.wait.called
        assert proc2.wait.called

    failed = MagicMock()
    failed.wait.return_value = 1
    with patch("plugin.audio.tts_service._get_player_candidates", return_value=[
        ("ffplay", ["ffplay", wav_file], None),
    ]), patch("plugin.audio.tts_service._popen_for_speech", return_value=failed):
        assert _play_audio_file(wav_file, cancel) is False


def test_chunk_text_prefers_sentence_boundary():
    from plugin.audio.tts_service import _chunk_text

    text = "Hello world. More text here please"
    chunks = _chunk_text(text, max_chunk=20)
    assert chunks[0] == "Hello world."
    assert chunks[1].startswith("More")
    assert _chunk_text("hi") == ["hi"]


def test_ready_queue_byte_cap_blocks_below_the_clip_cap():
    """The byte budget blocks put() even when fewer than max_clips are queued.

    The first two clips are always accepted (the two-clip floor), whatever
    their size, so playback never stalls on one huge sentence.
    """
    import plugin.audio.tts_service as tts
    from plugin.audio.tts_service import _ReadyClip

    utt = tts._begin_utterance()

    cancel = utt.scope
    try:
        with patch.object(tts, "SPEECH_READY_MAX_BYTES", 100):
            floor = tts._ReadyQueue()
            assert floor.put(_ReadyClip(None, "big1", 500), cancel)
            assert floor.put(_ReadyClip(None, "big2", 500), cancel)

            ready = tts._ReadyQueue()
            for text in ("A", "B", "C"):
                assert ready.put(_ReadyClip(None, text, 10), cancel)
            assert len(ready._clips) < tts.SPEECH_READY_MAX_CLIPS

            result: list[bool] = []
            done = threading.Event()

            def put_big() -> None:
                result.append(ready.put(_ReadyClip(None, "D", 80), cancel))
                done.set()

            producer = threading.Thread(target=put_big)
            producer.start()
            # 30 + 80 bytes is over the 100-byte budget while only 3 of 6 clips are queued.
            assert not done.wait(0.3)
            clip = ready.get(cancel)
            assert clip is not None and clip.text == "A"
            # 20 + 80 fits exactly, so D is admitted by bytes (not the two-clip floor).
            assert done.wait(2)
            producer.join(1)
            assert result == [True]
            assert [c.text for c in ready._clips] == ["B", "C", "D"]
            assert ready._bytes == 100
    finally:
        tts.stop_speech()


@pytest.mark.parametrize("max_clips", [2, None])
def test_sentence_pipeline_consumer_error_unblocks_producer(tmp_path, max_clips):
    """A raise in playback must close the queue so prefetch is not stuck in put()."""
    import plugin.audio.tts_service as tts

    cap = tts.SPEECH_READY_MAX_CLIPS if max_clips is None else max_clips

    sentences = ["A.", "B.", "C.", "D.", "E.", "F.", "G.", "H.", "I."]
    started: list[str] = []
    producer_threads: list[tuple[threading.Thread, threading.Event]] = []

    def synth(sentence, *args):
        del args
        started.append(sentence)
        path = tmp_path / f"{len(started)}.wav"
        path.write_bytes(b"R")
        return tts._ReadyClip(str(path), sentence, 1)

    def play(path, cancel=None):
        del path, cancel
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(started) < cap + 2:
            time.sleep(0.02)
        time.sleep(0.15)
        raise RuntimeError("player exploded")

    real_bg = tts.run_in_background

    def track_bg(fn, **kwargs):
        if kwargs.get("name") != "tts-prefetch":
            return real_bg(fn, **kwargs)
        done = threading.Event()

        def wrapped() -> None:
            try:
                fn()
            finally:
                done.set()

        thread = threading.Thread(target=wrapped)
        producer_threads.append((thread, done))
        thread.start()
        return None

    utt = tts._begin_utterance()

    try:
        with patch.object(tts, "SPEECH_READY_MAX_CLIPS", cap), \
             patch.object(tts, "run_in_background", side_effect=track_bg), \
             patch.object(tts, "_synthesize_sentence_clip", side_effect=synth), \
             patch.object(tts, "_play_audio_file", side_effect=play):
            with pytest.raises(RuntimeError, match="player exploded"):
                tts._run_sentence_pipeline(
                    sentences,
                    "kokoro",
                    "af_sky",
                    1.0,
                    None,
                    utt,
                )
        assert producer_threads
        thread, done = producer_threads[0]
        assert done.wait(2)
        thread.join(1)
        assert not thread.is_alive()
        assert list(tmp_path.glob("*.wav")) == []
        assert tts._current is None or tts._current.ready is None
    finally:
        tts.stop_speech()


def test_kokoro_oneshot_times_out_and_reaps(tmp_path):
    import plugin.audio.tts_service as tts
    from plugin.audio.tts_service import _kokoro_oneshot_to_file

    utt = tts._begin_utterance()
    mock_proc = MagicMock()
    mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd=["py"], timeout=60)
    mock_proc.returncode = None
    wav = tmp_path / "out.wav"
    with patch("plugin.audio.tts_service._new_speech_temp", return_value=str(wav)), \
         patch("plugin.audio.tts_service._popen_for_speech", return_value=mock_proc), \
         patch("plugin.audio.tts_service._release_temp") as release:
        result = _kokoro_oneshot_to_file("/py", "hello", "af_sky", 1.0, "/m", "/v", utt)
    assert result is None
    assert mock_proc.communicate.call_args.kwargs["timeout"] >= 60
    mock_proc.kill.assert_called_once()
    mock_proc.wait.assert_called()
    release.assert_called_with(str(wav))

def test_kokoro_warm_exception_allows_fallback(tmp_path):
    import plugin.audio.tts_service as tts
    from plugin.audio.tts_service import _kokoro_warm_to_file

    utt = tts._begin_utterance()
    pool = MagicMock()
    pool.execute.side_effect = RuntimeError("spawn blew up")
    wav = tmp_path / "w.wav"
    with patch("plugin.audio.tts_service.get_kokoro_pool", return_value=pool), \
         patch("plugin.audio.tts_service._new_speech_temp", return_value=str(wav)), \
         patch("plugin.audio.tts_service._release_temp") as release:
        path, allow = _kokoro_warm_to_file("hi", "af_sky", 1.0, "/m", "/v", utt)
    assert path is None
    assert allow is True
    release.assert_called_once_with(str(wav))


def test_remember_misaki_ready_ignores_stale_generation():
    import plugin.audio.tts_service as tts

    utt = tts._begin_utterance()
    try:
        utt.remember_misaki("ja", True)
        assert utt.misaki_ready("ja") is True
        tts.stop_speech()
        utt.remember_misaki("ja", True)
        assert "ja" in utt.misaki
        utt.remember_misaki("es", True)
        assert utt.misaki_ready("es") is None
    finally:
        tts.stop_speech()


def test_prepare_kokoro_misaki_stale_utterance_does_not_touch_current():
    import plugin.audio.tts_service as tts
    from plugin.audio.tts_service import _prepare_kokoro_misaki

    utt_a = tts._begin_utterance()
    utt_b = tts._begin_utterance()
    try:
        with patch.object(tts, "resolve_kokoro_python", return_value="/venv/bin/python"), \
             patch.object(tts, "kokoro_lang_uses_misaki", return_value=True), \
             patch.object(tts, "kokoro_g2p_lang", return_value="ja"), \
             patch.object(tts, "ensure_kokoro_misaki", return_value=False):
            _prepare_kokoro_misaki("こんにちは", "jf_alpha", None, utt_a)
        assert utt_b.misaki == {}
    finally:
        tts.stop_speech()


def test_run_synth_uses_raw_spawn_and_returns_communicate_stderr() -> None:
    import plugin.audio.tts_service as tts
    import plugin.framework.managed_subprocess as ms

    tts._speech_active = False

    snapshot_instances = set(ms._instances)

    captured_kwargs: list[dict[str, Any]] = []

    class _FakeProc:
        def __init__(self) -> None:
            self.returncode = 7
            self.communicate_calls: list[tuple[Any, Any]] = []

        def communicate(self, input: Any = None, timeout: Any = None) -> tuple[str, str]:
            self.communicate_calls.append((input, timeout))
            return ("", "piper-err")

        def poll(self) -> int:
            return 7

    fake_proc = _FakeProc()

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeProc:
        captured_kwargs.append(dict(kwargs))
        return fake_proc

    cancel = SendCancellation()
    try:
        with patch("plugin.framework.managed_subprocess.subprocess.Popen", side_effect=fake_popen), \
             patch("plugin.framework.managed_subprocess.start_stderr_drain", side_effect=AssertionError("drain")):
            res = tts._run_synth(["piper", "--model", "m"], cancel, stdin_text="hello", timeout=12.5)

        assert res == (7, "", "piper-err")
        assert fake_proc.communicate_calls == [("hello", 12.5)]
        kwargs = captured_kwargs[0]
        assert kwargs["stdin"] is subprocess.PIPE
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE
        assert kwargs["text"] is True
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert kwargs["env"] is None
        assert kwargs["cwd"] is None
        assert "start_new_session" not in kwargs
        assert "creationflags" not in kwargs
        assert "bufsize" not in kwargs
        assert set(ms._instances) == snapshot_instances
    finally:
        ms._instances.clear()
        ms._instances.update(snapshot_instances)
        tts._procs.clear()


def test_popen_for_speech_raw_env_object_and_devnull() -> None:
    import plugin.audio.tts_service as tts
    import plugin.framework.managed_subprocess as ms

    tts._speech_active = False

    env = {"WA_AUDIO_PATH": "C:\\clip.wav", "PATH": "/bin"}
    captured_kwargs: list[dict[str, Any]] = []
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None

    def fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        captured_kwargs.append(dict(kwargs))
        return fake_proc

    cancel = SendCancellation()
    run_calls: list[Any] = []
    try:
        with patch("plugin.framework.managed_subprocess.subprocess.Popen", side_effect=fake_popen), \
             patch.object(ms.ManagedSubprocess, "run", side_effect=lambda *a, **k: run_calls.append((a, k))):
            res = tts._popen_for_speech(
                ["powershell", "-NoProfile", "-Command", "x"],
                cancel,
                env=env,
            )

        assert res is fake_proc
        kwargs = captured_kwargs[0]
        assert kwargs["env"] is env
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["stdin"] is None
        assert kwargs["cwd"] is None
        assert "start_new_session" not in kwargs
        assert "creationflags" not in kwargs
        assert "bufsize" not in kwargs
        assert tts._procs == [fake_proc]
        assert run_calls == []
    finally:
        tts._procs.clear()


def test_reap_proc_windows_taskkill_timeout_stays_five() -> None:
    import plugin.audio.tts_service as tts

    wait_calls: list[Any] = []

    def fake_wait(timeout: float | None = None) -> int:
        wait_calls.append(timeout)
        if len(wait_calls) == 1:
            raise subprocess.TimeoutExpired(cmd="x", timeout=0.2)
        return 0

    proc = MagicMock()
    proc.wait.side_effect = fake_wait
    proc.pid = 4242

    recorded: list[tuple[Any, dict[str, Any]]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        recorded.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    with patch.object(tts.sys, "platform", "win32"), \
         patch.object(tts.ManagedSubprocess, "run", side_effect=fake_run):
        tts._reap_proc(proc)

    assert len(recorded) == 1
    args, kwargs = recorded[0]
    assert args == ["taskkill", "/F", "/T", "/PID", "4242"]
    assert kwargs == {
        "check": False,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "timeout": 5,
    }
    assert wait_calls == [0.2, 5]


def test_stop_speech_spd_say_stop_uses_managed_run() -> None:
    import plugin.audio.tts_service as tts

    proc = MagicMock()
    proc.poll.return_value = None
    proc.args = ["spd-say", "-w", "--", "hi"]
    proc.stdin = None

    recorded: list[tuple[Any, dict[str, Any]]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        recorded.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    with patch.object(tts, "_procs", [proc]), \
         patch.object(tts, "run_in_background", side_effect=lambda fn, **_kw: fn()), \
         patch.object(tts.ManagedSubprocess, "run", side_effect=fake_run):
        tts.stop_speech()

    assert len(recorded) == 1
    args, kwargs = recorded[0]
    assert args == ["spd-say", "-S"]
    assert kwargs == {
        "check": False,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "timeout": 2,
    }

    proc.kill.assert_called_once()
    proc.terminate.assert_not_called()


def test_speak_text_async_clears_active_when_worker_fails_to_start():
    def _cfg(key, default=None):
        if key == "audio.tts_enabled":
            return True
        if key == "audio.tts_provider":
            return "system"
        if key == "audio.tts_speed":
            return 1.0
        return default

    def _fake_run_in_background(fn, **kw):
        if kw.get("name") == "tts-speak":
            raise RuntimeError("no thread")
        return None

    on_complete_called = False

    def _done():
        nonlocal on_complete_called
        on_complete_called = True

    try:
        with patch("plugin.framework.config.get_config", side_effect=_cfg), \
             patch("plugin.audio.tts_service.run_in_background", side_effect=_fake_run_in_background):
            with pytest.raises(RuntimeError, match="no thread"):
                speak_text_async("Hello", on_complete=_done)
        assert not on_complete_called
        assert is_speaking() is False
    finally:
        stop_speech()


def test_stop_speech_reaps_on_a_dedicated_thread():
    with patch("plugin.audio.tts_service.run_in_background") as mock_bg:
        stop_speech()
    mock_bg.assert_called_once()
    assert mock_bg.call_args.kwargs.get("dedicated") is True
    assert mock_bg.call_args.kwargs.get("name") == "tts-cleanup"


def test_popen_cancelled_during_spawn_reaps_on_dedicated_thread():
    import plugin.audio.tts_service as tts
    from plugin.framework.managed_subprocess import ManagedSubprocess

    utt = tts._begin_utterance()

    cancel = utt.scope
    calls: list[tuple[Any, Any]] = []

    def _fake_run_in_background(fn, **kw):
        calls.append((kw.get("name"), kw.get("dedicated")))
        return None

    def _fake_start(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        self._proc = mock_proc
        cancel.cancel()

    try:
        with patch.object(ManagedSubprocess, "start", _fake_start), \
             patch("plugin.audio.tts_service.run_in_background", side_effect=_fake_run_in_background):
            proc = tts._popen_for_speech(["true"], cancel)
            assert proc is None
            assert ("tts-reap", True) in calls
    finally:
        tts.stop_speech()


def test_kokoro_warm_passes_scaled_timeout() -> None:
    import plugin.audio.tts_service as tts
    from plugin.audio.tts_service import _kokoro_warm_to_file, _synth_timeout

    calls: list[dict[str, Any]] = []

    class MockPool:
        job_timeout_sec = 120.0

        def execute(self, payload: dict[str, Any], timeout_sec: float | None = None, cancel=None):
            calls.append({"payload": payload, "timeout_sec": timeout_sec})
            return {"status": "error", "code": "WORKER_CANCELLED"}

    short_text = "Short text."
    long_text = "A" * 400
    assert _synth_timeout(short_text, 60.0) < 120.0
    assert _synth_timeout(long_text, 60.0) > 120.0

    utt = tts._begin_utterance()
    mock_pool = MockPool()
    with patch.object(tts, "get_kokoro_pool", return_value=mock_pool), \
         patch.object(tts, "_new_speech_temp", return_value="/tmp/test.wav"), \
         patch.object(tts, "_release_temp"):
        _kokoro_warm_to_file(short_text, "af_sky", 1.0, "/models/k.onnx", "/models/v.bin", utt=utt)
        _kokoro_warm_to_file(long_text, "af_sky", 1.0, "/models/k.onnx", "/models/v.bin", utt=utt)

    assert len(calls) == 2
    assert calls[0]["timeout_sec"] == 120.0
    assert calls[1]["timeout_sec"] == _synth_timeout(long_text, 60.0)


def test_kokoro_audio_file_skips_model_download_without_venv() -> None:
    from plugin.audio.tts_service import _kokoro_audio_file

    utt = tts._begin_utterance()
    with patch("plugin.audio.tts_service.resolve_kokoro_python", return_value=None), \
         patch("plugin.audio.tts_service.resolve_kokoro_model_files") as resolve:
        res = _kokoro_audio_file("Hello", "af_sky", 1.0, None, utt)
        assert res is None
        resolve.assert_not_called()


def test_kokoro_audio_file_resolves_models_when_venv_exists() -> None:
    from plugin.audio.tts_service import _kokoro_audio_file

    utt = tts._begin_utterance()
    with patch("plugin.audio.tts_service.resolve_kokoro_python", return_value="/venv/bin/python"), \
         patch("plugin.audio.tts_service.resolve_kokoro_model_files", return_value=("/no/model.onnx", "/no/voices.bin")) as resolve:
        res = _kokoro_audio_file("Hello", "af_sky", 1.0, None, utt)
        resolve.assert_called_once()
        assert res is None


def test_sentence_kokoro_without_venv_warns_once_and_uses_os_speech(caplog: pytest.LogCaptureFixture) -> None:
    import plugin.audio.tts_service as tts

    utt = tts._begin_utterance()
    try:
        with patch.object(tts, "resolve_kokoro_python", return_value=None), \
             patch("plugin.audio.tts_service.resolve_kokoro_model_files") as resolve, \
             patch.object(tts, "_speak_system") as system:
            with caplog.at_level("WARNING", logger="plugin.audio.tts_service"):
                tts._run_sentence_pipeline(["One.", "Two."], "kokoro", "af_sky", 1.0, None, utt)
        resolve.assert_not_called()
        assert [c.args[0] for c in system.call_args_list] == ["One.", "Two."]
        tts_warnings = [r for r in caplog.records if r.name == "plugin.audio.tts_service"]
        assert len(tts_warnings) == 1
    finally:
        tts.stop_speech()


def test_spd_say_nonzero_exit_stops_remaining_chunks() -> None:
    from plugin.audio.tts_service import _speak_system

    def fake_which(cmd: str) -> str | None:
        return "/usr/bin/spd-say" if cmd == "spd-say" else None

    mock_proc = MagicMock()
    mock_proc.wait.return_value = 1
    cancel = SendCancellation()

    with patch("sys.platform", "linux"), \
         patch("shutil.which", side_effect=fake_which), \
         patch("plugin.audio.tts_service._popen_for_speech", return_value=mock_proc) as mock_popen:
        _speak_system("a" * 4001, speed=1.0, cancel=cancel)
    mock_popen.assert_called_once()


def test_spd_say_zero_exit_speaks_every_chunk() -> None:
    from plugin.audio.tts_service import _speak_system

    def fake_which(cmd: str) -> str | None:
        return "/usr/bin/spd-say" if cmd == "spd-say" else None

    mock_proc = MagicMock()
    mock_proc.wait.return_value = 0
    cancel = SendCancellation()

    with patch("sys.platform", "linux"), \
         patch("shutil.which", side_effect=fake_which), \
         patch("plugin.audio.tts_service._popen_for_speech", return_value=mock_proc) as mock_popen:
        _speak_system("a" * 4001, speed=1.0, cancel=cancel)
    assert mock_popen.call_count == 2


def test_spd_say_killed_by_stop_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    import plugin.audio.tts_service as tts

    def fake_which(cmd: str) -> str | None:
        return "/usr/bin/spd-say" if cmd == "spd-say" else None

    utt = tts._begin_utterance()

    scope = utt.scope
    try:
        mock_proc = MagicMock()

        def fake_wait() -> int:
            scope.cancel()
            return -9

        mock_proc.wait.side_effect = fake_wait

        with patch("sys.platform", "linux"), \
             patch("shutil.which", side_effect=fake_which), \
             patch.object(tts, "_popen_for_speech", return_value=mock_proc) as mock_popen:
            with caplog.at_level("WARNING", logger="plugin.audio.tts_service"):
                tts._speak_system("a" * 4001, speed=1.0, cancel=scope)
        mock_popen.assert_called_once()
        assert [r for r in caplog.records if r.name == "plugin.audio.tts_service"] == []
    finally:
        tts.stop_speech()


def test_stop_speech_reaps_inline_when_cleanup_thread_fails(tmp_path: Any) -> None:
    import plugin.audio.tts_service as tts

    utt = tts._begin_utterance()
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"RIFF")
    clip_str = str(clip)
    utt.temps.add(clip_str)
    patcher = patch.object(tts, "run_in_background", side_effect=RuntimeError("no thread"))
    patcher.start()
    try:
        tts.stop_speech()
        assert not clip.exists()
        assert clip_str not in utt.temps
    finally:
        patcher.stop()
        tts.stop_speech()


def test_notify_kokoro_g2p_fallback():
    from plugin.audio.kokoro_g2p import g2p_fallback_warning
    from plugin.audio.tts_service import (
        _begin_utterance,
        _notify_kokoro_g2p_fallback,
    )

    messages: list[str] = []
    utt = _begin_utterance()

    # warning with voice substitute, misaki_ready=False, g2p="misaki" -> list stays empty
    _notify_kokoro_g2p_fallback(
        "Kokoro voice 'jf_x' is not in /v; using af_sky",
        utt,
        "ja",
        messages.append,
        g2p="misaki",
    )
    assert messages == []

    # warning with G2P_FAILED_MARK, misaki_ready=False, g2p="espeak" -> notifies once
    warn = g2p_fallback_warning("ja", RuntimeError("boom"))
    _notify_kokoro_g2p_fallback(warn, utt, "ja", messages.append, g2p="espeak")
    assert len(messages) == 1
    espeak_msg = messages[0]

    # warning="", misaki_ready=False, g2p=None -> distinct fallback notice
    messages.clear()
    utt.remember_misaki("ja", False)
    _notify_kokoro_g2p_fallback("", utt, "ja", messages.append, g2p=None)
    assert len(messages) == 1
    assert messages[0] != espeak_msg


def test_utterance_cancel_hooks_run_outside_speech_lock():
    import plugin.audio.tts_service as tts

    events: list[object] = []

    def hook():
        events.append(("hook", tts._speech_lock.locked()))

    utt1 = tts._begin_utterance()
    utt1.scope.register_on_cancel(hook)

    try:
        tts.stop_speech()
        assert events == [("hook", False)]
        assert utt1.scope.is_cancelled()
        assert tts._current is None

        events.clear()
        utt2 = tts._begin_utterance()
        utt2.scope.register_on_cancel(hook)

        real_get_config = tts.config.get_config

        def fake_get_config(key, *args, **kwargs):
            if key == "audio.tts_enabled":
                return True
            return real_get_config(key, *args, **kwargs)

        def fake_run_in_background(fn, *args, **kwargs):
            events.append(kwargs.get("name"))
            return None

        with patch("plugin.audio.tts_service.config.get_config", side_effect=fake_get_config), \
             patch("plugin.audio.tts_service.run_in_background", side_effect=fake_run_in_background):
            tts.speak_text_async("Hello")

        assert events == [("hook", False), "tts-cleanup", "tts-speak"]
        assert utt2.scope.is_cancelled()
        assert tts._current is not None
        assert tts._current is not utt2
        assert not tts._current.scope.is_cancelled()
    finally:
        tts.stop_speech()


def test_stop_before_fetch_skips_playback_and_fallback():
    """Stop between _begin_utterance and fetch aborts without playback or system speech fallback."""
    import plugin.audio.tts_service as tts

    utt = tts._begin_utterance()

    scope = utt.scope
    tts.stop_speech()
    assert scope.is_cancelled()

    with patch("plugin.audio.tts_service.download_endpoint_speech") as mock_dl, \
         patch("plugin.audio.tts_service._play_audio_file") as mock_play, \
         patch("plugin.audio.tts_service._speak_system") as mock_fallback:
        tts._speak_endpoint("hello", "https://example.com/api", "key", cancel=scope)

        mock_dl.assert_not_called()
        mock_play.assert_not_called()
        mock_fallback.assert_not_called()


def test_stop_speech_returns_while_playback_blocked_in_wait():
    """stop_speech() returns promptly without waiting for background process reaping."""
    import plugin.audio.tts_service as tts

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    unblock_wait = threading.Event()
    wait_finished = threading.Event()

    def slow_wait(*args, **kwargs):
        unblock_wait.wait(timeout=5.0)
        wait_finished.set()
        return 0

    mock_proc.wait.side_effect = slow_wait

    try:
        with patch.object(tts, "_procs", [mock_proc]):
            tts.stop_speech()
            mock_proc.kill.assert_called_once()
            assert not wait_finished.is_set()
    finally:
        unblock_wait.set()


def test_new_speak_supersedes_old_one():
    """Calling _begin_utterance() cancels the previous utterance's scope and activates the new one."""
    import plugin.audio.tts_service as tts

    utt1 = tts._begin_utterance()
    assert not utt1.scope.is_cancelled()
    assert tts._current is utt1
    assert tts.is_speaking() is True

    utt2 = tts._begin_utterance()
    assert utt1.scope.is_cancelled()
    assert not utt2.scope.is_cancelled()
    assert tts._current is utt2
    assert tts.is_speaking() is True

    tts.stop_speech()
    assert utt2.scope.is_cancelled()
    assert tts._current is None
    assert tts.is_speaking() is False


def test_capped_ready_queue_put_wakes_on_stop():
    """A thread blocked in ready.put() wakes immediately and returns False when stop_speech() runs."""
    import plugin.audio.tts_service as tts
    from plugin.audio.tts_service import _ReadyClip, _ReadyQueue

    utt = tts._begin_utterance()
    ready = _ReadyQueue()
    with tts._speech_lock:
        utt.ready = ready

    try:
        # Fill queue to cap
        for idx in range(tts.SPEECH_READY_MAX_CLIPS):
            assert ready.put(_ReadyClip(None, f"clip{idx}", 10), utt.scope) is True

        put_result: list[bool] = []
        put_done = threading.Event()

        def block_in_put():
            res = ready.put(_ReadyClip(None, "overflow", 10), utt.scope)
            put_result.append(res)
            put_done.set()

        t = threading.Thread(target=block_in_put)
        t.start()

        # Ensure thread has started and is waiting in put
        assert not put_done.wait(0.1)

        tts.stop_speech()
        assert put_done.wait(1.0), "ready.put() did not unblock on stop_speech()"
        t.join(timeout=1.0)
        assert put_result == [False]
    finally:
        tts.stop_speech()


def test_speak_text_async_on_complete_fires_when_worker_raises():
    """on_complete still fires when the speech provider raises."""
    def _cfg(key, default=None):
        return {
            "audio.tts_enabled": True,
            "audio.tts_provider": "system",
            "audio.tts_speed": 1.0,
        }.get(key, default)

    completed = threading.Event()
    threads: list[threading.Thread] = []
    real_bg = tts.run_in_background

    def _tracking_bg(fn, *args, **kwargs):
        t = real_bg(fn, *args, **kwargs)
        if isinstance(t, threading.Thread):
            threads.append(t)
        return t

    try:
        with patch("plugin.framework.config.get_config", side_effect=_cfg), \
             patch("plugin.audio.tts_service._speak_system", side_effect=RuntimeError("boom")), \
             patch("plugin.audio.tts_service.run_in_background", side_effect=_tracking_bg):
            speak_text_async("hello", on_complete=completed.set)
            assert completed.wait(timeout=2.0), "on_complete did not fire after worker error"
            assert is_speaking() is False
    finally:
        stop_speech()
        for t in threads:
            t.join(timeout=2.0)

