"""Misaki G2P selection for local Kokoro. The venv script is exec'd with fake modules."""

from __future__ import annotations

import subprocess
import sys
import types
from unittest.mock import patch

from plugin.audio.kokoro_g2p import (
    KOKORO_ONNX_SCRIPT,
    KOKORO_PIP_INSTALL,
    ensure_kokoro_misaki,
    kokoro_lang_uses_misaki,
    kokoro_misaki_install_hint,
    kokoro_misaki_packages,
    kokoro_misaki_probe_code,
)


def test_kokoro_script_compiles():
    compile(KOKORO_ONNX_SCRIPT, "<kokoro_onnx_script>", "exec")
    assert "is_phonemes=True" in KOKORO_ONNX_SCRIPT
    assert "JAG2P()(text)" in KOKORO_ONNX_SCRIPT
    assert "ZHG2P()(text)" in KOKORO_ONNX_SCRIPT
    assert "version=" not in KOKORO_ONNX_SCRIPT
    assert "EspeakG2P(language='fr-fr')" in KOKORO_ONNX_SCRIPT
    assert "EspeakG2P(language='es')" in KOKORO_ONNX_SCRIPT


def test_misaki_package_map():
    assert kokoro_lang_uses_misaki("ja")
    assert kokoro_lang_uses_misaki("fr-fr")
    assert kokoro_lang_uses_misaki("es")
    assert not kokoro_lang_uses_misaki("en-us")
    assert not kokoro_lang_uses_misaki("en-gb")

    assert kokoro_misaki_packages("ja") == ("misaki[ja]",)
    assert kokoro_misaki_packages("zh") == ("misaki[zh]",)
    espeak_pkgs = ("misaki", "phonemizer-fork", "espeakng-loader")
    for lang in ("es", "fr-fr", "it", "hi", "pt-br"):
        assert kokoro_misaki_packages(lang) == espeak_pkgs
        assert "torch" not in " ".join(espeak_pkgs)
    assert kokoro_misaki_packages("en-us") == ()

    assert kokoro_misaki_install_hint("ja") == "uv pip install 'misaki[ja]'"
    assert kokoro_misaki_install_hint("zh") == "uv pip install 'misaki[zh]'"
    assert kokoro_misaki_install_hint("fr-fr") == (
        "uv pip install misaki phonemizer-fork espeakng-loader"
    )
    assert "misaki[ja,zh]" in KOKORO_PIP_INSTALL
    assert "phonemizer-fork" in KOKORO_PIP_INSTALL
    assert "JAG2P" in kokoro_misaki_probe_code("ja")
    assert "ZHG2P" in kokoro_misaki_probe_code("zh")
    assert "language='es'" in kokoro_misaki_probe_code("es")
    assert kokoro_misaki_probe_code("en-us") == ""


def _exec_kokoro_script(argv: list[str], modules: dict[str, types.ModuleType]) -> None:
    saved_modules = {name: sys.modules.get(name) for name in modules}
    old_argv = sys.argv
    try:
        sys.argv = argv
        sys.modules.update(modules)
        exec(compile(KOKORO_ONNX_SCRIPT, "<kokoro_onnx_script>", "exec"), {"__name__": "__main__"})
    finally:
        sys.argv = old_argv
        for name, previous in saved_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def _fake_stack(g2p_calls: list, create_calls: list, voices: dict[str, int]):
    class JAG2P:
        def __call__(self, text: str):
            g2p_calls.append(("ja", text))
            return "JA:" + text, None

    class ZHG2P:
        def __init__(self, version=None):
            g2p_calls.append(("zh-init", version))

        def __call__(self, text: str):
            g2p_calls.append(("zh", text))
            return "ZH:" + text, None

    class EspeakG2P:
        def __init__(self, language: str):
            self.language = language

        def __call__(self, text: str):
            g2p_calls.append((self.language, text))
            return "ES:" + text, None

    class Kokoro:
        def __init__(self, model_path: str, voices_path: str):
            self.voices = voices

        def create(self, text, voice, speed, lang, is_phonemes=False):
            create_calls.append(
                {
                    "text": text,
                    "voice": voice,
                    "speed": speed,
                    "lang": lang,
                    "is_phonemes": is_phonemes,
                }
            )
            return [0.0], 24000

    misaki = types.ModuleType("misaki")
    misaki.__path__ = []  # type: ignore[attr-defined]
    ja = types.ModuleType("misaki.ja")
    ja.JAG2P = JAG2P
    zh = types.ModuleType("misaki.zh")
    zh.ZHG2P = ZHG2P
    espeak = types.ModuleType("misaki.espeak")
    espeak.EspeakG2P = EspeakG2P
    ko = types.ModuleType("kokoro_onnx")
    ko.Kokoro = Kokoro
    sf = types.ModuleType("soundfile")
    sf.write = lambda path, samples, rate: None
    return {
        "misaki": misaki,
        "misaki.ja": ja,
        "misaki.zh": zh,
        "misaki.espeak": espeak,
        "kokoro_onnx": ko,
        "soundfile": sf,
    }


def _run(lang: str, voice: str, text: str, voices: dict[str, int] | None = None):
    g2p_calls: list = []
    create_calls: list = []
    catalog = voices if voices is not None else {
        "jf_alpha": 1,
        "ff_siwis": 1,
        "ef_dora": 1,
        "zf_xiaobei": 1,
        "af_bella": 1,
    }
    modules = _fake_stack(g2p_calls, create_calls, catalog)
    _exec_kokoro_script(
        ["kokoro", text, voice, "1.0", "/tmp/out.wav", "model.onnx", "voices.bin", lang],
        modules,
    )
    assert len(create_calls) == 1
    return g2p_calls, create_calls[0]


def test_misaki_phonemes_helper_uses_the_same_snippets(monkeypatch):
    """The warm worker calls this helper; it must match the one-shot script."""
    from plugin.audio.kokoro_g2p import misaki_phonemes

    g2p_calls: list = []
    modules = _fake_stack(g2p_calls, [], {"jf_alpha": 1})
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    assert misaki_phonemes("非常に強力", "ja") == "JA:非常に強力"
    assert misaki_phonemes("Hello", "en-us") == ""
    assert g2p_calls == [("ja", "非常に強力")]


def test_script_japanese_french_spanish_use_misaki_phonemes():
    g2p, created = _run("ja", "jf_alpha", "非常に強力")
    assert g2p == [("ja", "非常に強力")]
    assert created["is_phonemes"] is True
    assert created["text"] == "JA:非常に強力"
    assert created["voice"] == "jf_alpha"
    assert created["lang"] == "ja"

    g2p, created = _run("fr-fr", "ff_siwis", "Bonjour")
    assert g2p == [("fr-fr", "Bonjour")]
    assert created["is_phonemes"] is True
    assert created["text"] == "ES:Bonjour"
    assert created["voice"] == "ff_siwis"

    g2p, created = _run("es", "ef_dora", "Hola")
    assert g2p == [("es", "Hola")]
    assert created["is_phonemes"] is True
    assert created["text"] == "ES:Hola"
    assert created["voice"] == "ef_dora"


def test_script_chinese_uses_legacy_zh_g2p():
    g2p, created = _run("zh", "zf_xiaobei", "你好")
    assert g2p == [("zh-init", None), ("zh", "你好")]
    assert created["is_phonemes"] is True
    assert created["text"] == "ZH:你好"


def test_script_english_does_not_call_misaki():
    g2p, created = _run("en-us", "af_bella", "Hello")
    assert g2p == []
    assert created["is_phonemes"] is False
    assert created["text"] == "Hello"
    assert created["voice"] == "af_bella"
    assert created["lang"] == "en-us"

    g2p, created = _run("en-gb", "bf_emma", "Hello")
    assert g2p == []
    assert created["is_phonemes"] is False
    assert created["text"] == "Hello"


def test_script_misaki_failure_falls_back_to_raw_text():
    g2p_calls: list = []
    create_calls: list = []

    class JAG2P:
        def __call__(self, text: str):
            raise RuntimeError("dictionary missing")

    modules = _fake_stack(g2p_calls, create_calls, {"jf_alpha": 1, "af_bella": 1})
    modules["misaki.ja"].JAG2P = JAG2P
    _exec_kokoro_script(
        ["kokoro", "非常に強力", "jf_alpha", "1.0", "/tmp/out.wav", "m", "v", "ja"],
        modules,
    )
    assert create_calls[0]["is_phonemes"] is False
    assert create_calls[0]["text"] == "非常に強力"
    assert create_calls[0]["lang"] == "ja"


def test_script_missing_voice_uses_af_bella():
    g2p, created = _run("ja", "jf_alpha", "あ", voices={"af_bella": 1})
    assert created["voice"] == "af_bella"
    assert created["is_phonemes"] is True
    assert g2p == [("ja", "あ")]


def test_ensure_kokoro_misaki_installs_ja_when_probe_fails():
    calls: list[list[str]] = []
    probes = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "-c" in cmd:
            probes["n"] += 1
            code = 1 if probes["n"] == 1 else 0
            err = "ModuleNotFoundError" if code else ""
            return subprocess.CompletedProcess(cmd, code, "", err)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    messages: list[str] = []
    with patch("plugin.audio.kokoro_g2p.shutil.which", return_value=None), \
         patch("plugin.audio.kokoro_g2p.subprocess.run", side_effect=fake_run):
        ready = ensure_kokoro_misaki(
            "/venv/bin/python", "ja", on_status=messages.append
        )

    assert ready is True
    assert calls[1] == ["/venv/bin/python", "-m", "pip", "install", "misaki[ja]"]
    assert not any("unidic" in part for cmd in calls for part in cmd)
    assert messages == ["Installing Kokoro phonemizer…"]


def test_ensure_kokoro_misaki_downloads_unidic_when_ja_still_unusable():
    calls: list[list[str]] = []
    probes = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "-c" in cmd:
            probes["n"] += 1
            code = 0 if probes["n"] >= 3 else 1
            return subprocess.CompletedProcess(cmd, code, "", "no dictionary" if code else "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    messages: list[str] = []
    with patch("plugin.audio.kokoro_g2p.shutil.which", return_value="/usr/bin/uv"), \
         patch("plugin.audio.kokoro_g2p.subprocess.run", side_effect=fake_run):
        ready = ensure_kokoro_misaki("/venv/bin/python", "ja", on_status=messages.append)

    assert ready is True
    assert calls[1] == [
        "/usr/bin/uv", "pip", "install", "--python", "/venv/bin/python", "misaki[ja]",
    ]
    assert calls[3] == ["/venv/bin/python", "-m", "unidic", "download"]
    assert "Downloading Japanese phonemizer dictionary…" in messages


def test_ensure_kokoro_misaki_espeak_langs_skip_torch_and_unidic():
    calls: list[list[str]] = []
    probes = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "-c" in cmd:
            probes["n"] += 1
            code = 1 if probes["n"] == 1 else 0
            return subprocess.CompletedProcess(cmd, code, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("plugin.audio.kokoro_g2p.shutil.which", return_value=None), \
         patch("plugin.audio.kokoro_g2p.subprocess.run", side_effect=fake_run):
        ready = ensure_kokoro_misaki("/venv/bin/python", "fr-fr")

    assert ready is True
    assert calls[1] == [
        "/venv/bin/python", "-m", "pip", "install", "misaki", "phonemizer-fork", "espeakng-loader",
    ]


def test_ensure_kokoro_misaki_reports_install_failure_and_does_not_raise():
    def fake_run(cmd, **kwargs):
        if "-c" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "ModuleNotFoundError: misaki")
        return subprocess.CompletedProcess(cmd, 1, "", "no network")

    messages: list[str] = []
    with patch("plugin.audio.kokoro_g2p.shutil.which", return_value=None), \
         patch("plugin.audio.kokoro_g2p.subprocess.run", side_effect=fake_run):
        ready = ensure_kokoro_misaki("/venv/bin/python", "es", on_status=messages.append)

    assert ready is False
    assert messages[0] == "Installing Kokoro phonemizer…"
    assert "uv pip install misaki phonemizer-fork espeakng-loader" in messages[1]
    assert "may be misread" in messages[1]


def test_ensure_kokoro_misaki_english_does_not_install():
    with patch("plugin.audio.kokoro_g2p.subprocess.run") as run:
        ready = ensure_kokoro_misaki("/venv/bin/python", "en-us")
    assert ready is True
    run.assert_not_called()


def test_ensure_kokoro_misaki_cancelled_skips_install():
    with patch("plugin.audio.kokoro_g2p.subprocess.run") as run:
        ready = ensure_kokoro_misaki(
            "/venv/bin/python", "ja", cancelled=lambda: True
        )
    assert ready is None
    run.assert_not_called()
