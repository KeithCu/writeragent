# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for TTS service playback, process management, and sentence pipeline."""

import os
import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

from plugin.audio.kokoro_g2p import KOKORO_ONNX_SCRIPT
from plugin.audio.tts_service import (
    clean_text_for_speech,
    is_speaking,
    sentence_speak_enabled,
    sentences_for_speech,
    speak_text_async,
    stop_speech,
    uses_sentence_by_sentence,
)


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
                assert isinstance(mock_sys.call_args.kwargs["generation"], int)
                assert completed is True


def test_stop_speech_terminates_proc():
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with patch("plugin.audio.tts_service._play_proc", mock_proc):
        assert is_speaking() is True
        stop_speech()
        for _ in range(10):
            if mock_proc.terminate.call_count > 0:
                break
            time.sleep(0.01)
        mock_proc.terminate.assert_called_once()


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
         patch("plugin.audio.tts_service._speak_kokoro_local") as mock_kokoro, \
         patch("plugin.audio.tts_service._speak_piper_local") as mock_piper:
        speak_text_async("Hello Kokoro")
        assert mock_kokoro.call_args.args == ("Hello Kokoro",)
        assert mock_kokoro.call_args.kwargs["voice"] == "af_bella"
        assert mock_kokoro.call_args.kwargs["speed"] == 1.1
        assert mock_kokoro.call_args.kwargs["on_status"] is None
        assert isinstance(mock_kokoro.call_args.kwargs["generation"], int)
        mock_piper.assert_not_called()

        cfg["audio.tts_provider"] = "piper"
        cfg["audio.tts_voice_piper"] = "en_US-lessac-medium"
        speak_text_async("Hello Piper")
        assert mock_piper.call_args.args == ("Hello Piper",)
        assert mock_piper.call_args.kwargs["voice"] == "en_US-lessac-medium"
        assert mock_piper.call_args.kwargs["speed"] == 1.1
        assert mock_piper.call_args.kwargs["on_status"] is None


def test_speak_text_async_uses_dialog_overrides_when_config_disabled():
    with patch("plugin.framework.config.get_config", return_value=False), \
         patch("plugin.audio.tts_service.run_in_background", side_effect=lambda fn, **kw: fn()), \
         patch("plugin.audio.tts_service._speak_kokoro_local") as mock_kokoro:
        speak_text_async(
            "Bonjour",
            provider="Kokoro (Local Neural, ONNX CPU)",
            voice="jf_alpha (Kokoro JP Female - Alpha)",
            speed=1.25,
            enabled=True,
        )
        mock_kokoro.assert_called_once()
        assert mock_kokoro.call_args.args == ("Bonjour",)
        assert mock_kokoro.call_args.kwargs["voice"] == "jf_alpha"
        assert mock_kokoro.call_args.kwargs["speed"] == 1.25
        assert mock_kokoro.call_args.kwargs["on_status"] is None
        assert isinstance(mock_kokoro.call_args.kwargs["generation"], int)


def test_speak_kokoro_local_uses_misaki_script_for_non_english(tmp_path):
    import plugin.audio.tts_service as tts
    from plugin.audio.tts_service import _speak_kokoro_local

    tts._speech_cancelled.clear()
    tts._speech_active = False

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
        with patch("plugin.audio.tts_service.get_config_str", return_value=str(tmp_path)), \
             patch("plugin.audio.tts_service._venv_python", return_value=str(py)), \
             patch("plugin.audio.tts_models._resolve_kokoro_model_files", return_value=(str(model), str(voices))), \
             patch("plugin.audio.tts_service.ensure_kokoro_misaki", ensure), \
             patch("plugin.audio.tts_service.subprocess.Popen", side_effect=fake_popen), \
             patch("plugin.audio.tts_service._play_audio_file") as play:
            _speak_kokoro_local(text, voice=voice, speed=1.0)
        assert play.called
        assert len(launched) == 1
        return launched[0], ensure

    ja_cmd, ja_ensure = _launch("こんにちは", "jf_alpha")
    assert ja_cmd[1] == "-c"
    assert ja_cmd[2] == KOKORO_ONNX_SCRIPT
    assert ja_cmd[3] == "-"
    assert ja_cmd[4] == "jf_alpha"
    assert ja_cmd[9] == "ja"
    assert "is_phonemes=True" in ja_cmd[2]


def test_speak_kokoro_local_skips_synthesis_when_phonemizer_install_cancelled(tmp_path):
    import plugin.audio.tts_service as tts
    from plugin.audio.tts_service import _speak_kokoro_local

    tts._speech_cancelled.clear()
    tts._speech_active = False

    model = tmp_path / "kokoro-v1.0.onnx"
    voices = tmp_path / "voices-v1.0.bin"
    model.write_bytes(b"m")
    voices.write_bytes(b"v")

    with patch("plugin.audio.tts_service.get_config_str", return_value=str(tmp_path)), \
         patch("plugin.audio.tts_service._venv_python", return_value=str(tmp_path / "python")), \
         patch("plugin.audio.tts_models._resolve_kokoro_model_files", return_value=(str(model), str(voices))), \
         patch("plugin.audio.tts_service.ensure_kokoro_misaki", return_value=None), \
         patch("plugin.audio.tts_service.subprocess.Popen") as popen, \
         patch("plugin.audio.tts_service._speak_system") as system:
        _speak_kokoro_local("こんにちは", voice="jf_alpha", speed=1.0)

    popen.assert_not_called()
    system.assert_not_called()


def test_sentence_pipeline_installs_misaki_once_per_reply(tmp_path):
    import plugin.audio.tts_service as tts

    ensure = MagicMock(return_value=True)
    synthesized: list[str] = []

    def synth(sentence, provider, voice, speed, on_status, generation):
        del provider, voice, speed, on_status, generation
        synthesized.append(sentence)
        path = tmp_path / f"{len(synthesized)}.wav"
        path.write_bytes(b"RIFF")
        return tts._ReadyClip(str(path), sentence, 4, False)

    generation = tts._begin_utterance()
    try:
        with patch.object(tts, "ensure_kokoro_misaki", ensure), \
             patch.object(tts, "get_config_str", return_value="/venv"), \
             patch.object(tts, "_venv_python", return_value="/venv/bin/python"), \
             patch.object(tts, "_synthesize_sentence_clip", side_effect=synth), \
             patch.object(tts, "_play_audio_file"), \
             patch.object(tts, "_speak_system") as system:
            tts._run_sentence_pipeline(
                ["一。", "二。", "三。"],
                "kokoro",
                "jf_alpha",
                1.0,
                None,
                generation,
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

    generation = tts._begin_utterance()
    try:
        with patch.object(tts, "ensure_kokoro_misaki", return_value=None), \
             patch.object(tts, "get_config_str", return_value="/venv"), \
             patch.object(tts, "_venv_python", return_value="/venv/bin/python"), \
             patch.object(tts, "_synthesize_sentence_clip") as synth, \
             patch.object(tts, "_speak_system") as system:
            tts._run_sentence_pipeline(
                ["こんにちは。", "次の文。"],
                "kokoro",
                "jf_alpha",
                1.0,
                None,
                generation,
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
        "plugin.writer.locale.grammar_proofread_text.filter_sentence_spans_for_thresholds",
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


def test_module_yaml_sentence_mode_defaults_on():
    import yaml

    with open(os.path.join(os.path.dirname(__file__), "..", "..", "plugin", "audio", "module.yaml"), encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle)
    field = manifest["config"]["tts_sentence_mode"]
    assert field["type"] == "boolean"
    assert field["widget"] == "checkbox"
    assert field["default"] is True
    assert str(field["label"]).endswith(" (Local)")


def test_module_yaml_short_answers_defaults_on():
    import yaml

    with open(os.path.join(os.path.dirname(__file__), "..", "..", "plugin", "audio", "module.yaml"), encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle)
    field = manifest["config"]["tts_short_answers"]
    assert field["type"] == "boolean"
    assert field["widget"] == "checkbox"
    assert field["default"] is True
    assert field["label"] == "Keep replies brief"
    assert "speech output (TTS) is on" in field["helper"]


def test_prefetch_keeps_synthesizing_during_playback(tmp_path):
    import plugin.audio.tts_service as tts

    short = "Hi."
    longer = "This sentence is much longer and should already be synthesizing."
    long_started = threading.Event()
    paths = []

    def synth(sentence, provider, voice, speed, on_status, generation):
        del provider, voice, speed, on_status, generation
        if sentence == longer:
            long_started.set()
        path = tmp_path / f"{len(paths)}.wav"
        path.write_bytes(b"RIFF")
        paths.append(str(path))
        return tts._ReadyClip(str(path), sentence, 4, False)

    def play(path, generation=None):
        del path
        if generation is not None and not long_started.is_set():
            assert long_started.wait(2), "next sentence was not synthesizing while the short one played"

    generation = tts._begin_utterance()
    try:
        with patch.object(tts, "_synthesize_sentence_clip", side_effect=synth), patch.object(
            tts, "_play_audio_file", side_effect=play
        ):
            tts._run_sentence_pipeline(
                [short, longer],
                "kokoro",
                "af_bella",
                1.0,
                None,
                generation,
            )
    finally:
        tts.stop_speech()
    assert long_started.is_set()


def test_ready_queue_backpressure_stops_past_the_cap():
    import plugin.audio.tts_service as tts

    sentences = ["A.", "B.", "C.", "D.", "E."]
    started: list[str] = []
    release_play = threading.Event()

    def synth(sentence, provider, voice, speed, on_status, generation):
        del provider, voice, speed, on_status, generation
        started.append(sentence)
        return tts._ReadyClip(None, sentence, 0, True)

    def play(path, generation=None):
        del path, generation

    def speak_system(text, speed=1.0, generation=None):
        del text, speed, generation
        release_play.wait(30)

    generation = tts._begin_utterance()
    finished = threading.Event()

    def run() -> None:
        try:
            with patch.object(tts, "_synthesize_sentence_clip", side_effect=synth), patch.object(
                tts, "_play_audio_file", side_effect=play
            ), patch.object(tts, "_speak_system", side_effect=speak_system):
                tts._run_sentence_pipeline(
                    sentences,
                    "kokoro",
                    "af_bella",
                    1.0,
                    None,
                    generation,
                    max_clips=2,
                    max_bytes=10**9,
                )
        finally:
            finished.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(started) < 4:
            time.sleep(0.02)
        time.sleep(0.2)
        assert started == sentences[:4]
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

    def synth(sentence, provider, voice, speed, on_status, generation):
        del provider, voice, speed, on_status, generation
        if sentence.startswith("Second"):
            hold_second.wait(2)
        path = tmp_path / f"{sentence[:1]}.wav"
        path.write_bytes(b"wav")
        return tts._ReadyClip(str(path), sentence, 3, False)

    def play(path, generation=None):
        played.append(path)
        first_playing.set()
        while generation is not None and not tts._playback_blocked(generation):
            threading.Event().wait(0.01)

    generation = tts._begin_utterance()
    finished = threading.Event()

    def run() -> None:
        try:
            with patch.object(tts, "_synthesize_sentence_clip", side_effect=synth), patch.object(
                tts, "_play_audio_file", side_effect=play
            ):
                tts._run_sentence_pipeline(
                    ["First sentence.", "Second sentence."],
                    "kokoro",
                    "af_bella",
                    1.0,
                    None,
                    generation,
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
    finally:
        hold_second.set()
        tts.stop_speech()
        worker.join(2)
    assert len(played) == 1
    assert not late_path.exists()


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


def test_sentence_speak_enabled_unset_is_on_saved_false_stays_off():
    def _lookup(cfg):
        return lambda key, default=None: cfg.get(key, default)

    with patch("plugin.framework.config.get_config", side_effect=_lookup({})):
        assert sentence_speak_enabled() is True
    with patch(
        "plugin.framework.config.get_config",
        side_effect=_lookup({"audio.tts_sentence_mode": False}),
    ):
        assert sentence_speak_enabled() is False
    with patch(
        "plugin.framework.config.get_config",
        side_effect=_lookup({"audio.tts_sentence_mode": True}),
    ):
        assert sentence_speak_enabled() is True


def _speak_sentence_case(cfg, *, provider=None, model=None):
    import plugin.audio.tts_service as tts

    captured: dict[str, object] = {}
    oneshot: dict[str, str] = {}
    real_split = tts.sentences_for_speech

    def fake_pipeline(sentences, prov, voice, speed, on_status, generation, **kwargs):
        del voice, speed, on_status, generation, kwargs
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

    with patch("plugin.framework.config.get_config", side_effect=lambda key, default=None: cfg.get(key, default)), patch(
        "plugin.audio.tts_service.run_in_background", side_effect=lambda fn, **kwargs: fn()
    ), patch(
        "plugin.writer.locale.grammar_proofread_text.get_break_iterator_and_locale",
        return_value=(_FakeSentenceBI(), "en-US"),
    ), patch("plugin.audio.tts_service.sentences_for_speech", side_effect=spy_split), patch(
        "plugin.audio.tts_service._run_sentence_pipeline", side_effect=fake_pipeline
    ), patch("plugin.audio.tts_service._speak_system", side_effect=remember("system")), patch(
        "plugin.audio.tts_service._speak_kokoro_local", side_effect=remember("kokoro")
    ), patch("plugin.audio.tts_service._speak_piper_local", side_effect=remember("piper")), patch(
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

    # macOS uses -f <temp_file>
    with patch("sys.platform", "darwin"):
        _speak_system(text, speed=1.0)
        assert mock_popen.called
        cmd = mock_popen.call_args.args[0]
        assert cmd[0] == "/usr/bin/say"
        assert "-r" in cmd
        assert "-f" in cmd

    mock_popen.reset_mock()

    # Linux spd-say chunks argv
    with patch("sys.platform", "linux"):
        mock_which.side_effect = lambda x: "/usr/bin/spd-say" if x == "spd-say" else None
        _speak_system(text, speed=1.0)
        mock_popen.assert_called_with(["spd-say", "-r", "0", "-w", "--", text], None, slot="play")

    mock_popen.reset_mock()

    # Linux espeak uses --stdin
    with patch("sys.platform", "linux"):
        mock_which.side_effect = lambda x: "/usr/bin/espeak" if x == "espeak" else None
        _speak_system(text, speed=1.0)
        cmd = mock_popen.call_args.args[0]
        assert cmd == ["espeak", "-s", "160", "--stdin"]
        assert mock_popen.call_args.kwargs["stdin"] == subprocess.PIPE

    mock_popen.reset_mock()

    # Windows passes file path in WA_SAPI_FILE env
    with patch("sys.platform", "win32"):
        _speak_system(text, speed=1.0)
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
        return tts._ReadyClip(str(path), sentence, 4, False)

    generation = tts._begin_utterance()
    try:
        with patch.object(tts, "_synthesize_sentence_clip", side_effect=synth), \
             patch.object(tts, "_play_audio_file") as play, \
             patch.object(tts, "_speak_system") as system:
            tts._run_sentence_pipeline(
                ["One.", "Two.", "Three."], "kokoro", "af_sky", 1.0, None, generation,
            )
    finally:
        tts.stop_speech()

    assert play.call_count == 1
    system.assert_called_once()
    assert system.call_args.args[0] == "Two. Three."


@patch("plugin.audio.tts_service._popen_for_speech", return_value=None)
def test_play_audio_file_windows_path_not_in_script(mock_popen):
    from plugin.audio.tts_service import _play_audio_file

    path = 'C:\\Users\\o"neil $x\\clip.wav'
    with patch("sys.platform", "win32"):
        _play_audio_file(path)
    cmd = mock_popen.call_args.args[0]
    assert path not in " ".join(cmd)
    assert "$env:WA_AUDIO_PATH" in cmd[-1]
    assert mock_popen.call_args.kwargs["env"]["WA_AUDIO_PATH"] == path


def test_piper_timeout_scaling_and_reap(tmp_path):
    """Bug 2: Piper timeout scales with text length; killed process is reaped."""
    from plugin.audio.tts_service import _piper_audio_file

    mock_proc = MagicMock()
    mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd=["piper"], timeout=30)
    mock_proc.returncode = None

    with patch("plugin.audio.tts_service._venv_python", return_value="/venv/bin/python"), \
         patch("plugin.audio.tts_models._resolve_piper_model_file", return_value="/models/v.onnx"), \
         patch("os.path.isfile", return_value=True), \
         patch("plugin.audio.tts_service._popen_for_speech", return_value=mock_proc):
        # Long text
        long_text = "Word " * 200
        try:
            _piper_audio_file(long_text, "voice", 1.0, None, None)
        except Exception:
            pass

        # Verify communicate received scaled timeout
        assert mock_proc.communicate.called
        timeout_used = mock_proc.communicate.call_args.kwargs["timeout"]
        assert timeout_used > 30.0

        # Verify process was killed and reaped (wait called)
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_called_once()


def test_play_audio_file_player_order_and_fallback(tmp_path):
    """Bug 3: Player chosen by suffix, exit code checked, fallback on failure."""
    from plugin.audio.tts_service import _play_audio_file

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
        _play_audio_file(wav_file)
        assert proc1.wait.called
        assert proc2.wait.called
