"""Misaki G2P selection for local Kokoro. The venv script is exec'd with fake modules."""

from __future__ import annotations

import subprocess
import sys
import types
from unittest.mock import patch

import pytest

from plugin.audio.kokoro_g2p import (
    KOKORO_ONNX_SCRIPT,
    KOKORO_PIP_INSTALL,
    TTS_INSTALL_CMD,
    ensure_kokoro_misaki,
    kokoro_lang_uses_misaki,
    kokoro_misaki_install_hint,
    kokoro_misaki_packages,
    kokoro_misaki_probe_code,
)


def test_kokoro_script_compiles():
    compile(KOKORO_ONNX_SCRIPT, "<kokoro_onnx_script>", "exec")
    assert "version=" not in KOKORO_ONNX_SCRIPT


@pytest.mark.parametrize(
    ("lang", "uses_misaki", "expected_pkgs"),
    [
        ("ja", True, ("misaki[ja]",)),
        ("zh", True, ("misaki[zh]",)),
        ("es", True, ("misaki", "phonemizer-fork", "espeakng-loader")),
        ("fr-fr", True, ("misaki", "phonemizer-fork", "espeakng-loader")),
        ("it", True, ("misaki", "phonemizer-fork", "espeakng-loader")),
        ("hi", True, ("misaki", "phonemizer-fork", "espeakng-loader")),
        ("pt-br", True, ("misaki", "phonemizer-fork", "espeakng-loader")),
        ("en-us", False, ()),
        ("en-gb", False, ()),
    ],
)
def test_misaki_lang_properties(lang: str, uses_misaki: bool, expected_pkgs: tuple[str, ...]):
    assert kokoro_lang_uses_misaki(lang) is uses_misaki
    assert kokoro_misaki_packages(lang) == expected_pkgs

    code = kokoro_misaki_probe_code(lang)
    if not uses_misaki:
        assert code == ""
    else:
        compile(code, "<probe>", "exec")
        calls = []

        class FakeG2P:
            def __init__(self, **kwargs):
                calls.append(kwargs)

            def __call__(self, text):
                pass

        fake_ja = types.ModuleType("misaki.ja")
        fake_ja.JAG2P = FakeG2P
        fake_zh = types.ModuleType("misaki.zh")
        fake_zh.ZHG2P = FakeG2P
        fake_espeak = types.ModuleType("misaki.espeak")
        fake_espeak.EspeakG2P = FakeG2P
        with patch.dict(
            sys.modules,
            {"misaki.ja": fake_ja, "misaki.zh": fake_zh, "misaki.espeak": fake_espeak},
        ):
            exec(compile(code, "<probe>", "exec"), {})
        assert len(calls) == 1


def test_kokoro_misaki_install_hints_and_requirements():
    assert kokoro_misaki_install_hint("ja") == "uv pip install 'misaki[ja]'"
    assert kokoro_misaki_install_hint("zh") == "uv pip install 'misaki[zh]'"
    assert kokoro_misaki_install_hint("fr-fr") == "uv pip install misaki phonemizer-fork espeakng-loader"

    assert "misaki[ja,zh]" in KOKORO_PIP_INSTALL
    assert "phonemizer-fork" in KOKORO_PIP_INSTALL
    assert "piper-tts" not in KOKORO_PIP_INSTALL

    assert TTS_INSTALL_CMD == KOKORO_PIP_INSTALL.replace("soundfile", "soundfile piper-tts")


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
    from plugin.audio.kokoro_g2p import _g2p_cache, misaki_phonemes

    _g2p_cache.clear()
    try:
        g2p_calls: list = []
        modules = _fake_stack(g2p_calls, [], {"jf_alpha": 1})
        for name, module in modules.items():
            monkeypatch.setitem(sys.modules, name, module)

        assert misaki_phonemes("非常に強力", "ja") == "JA:非常に強力"
        assert misaki_phonemes("Hello", "en-us") == ""
        assert g2p_calls == [("ja", "非常に強力")]
    finally:
        _g2p_cache.clear()


def test_misaki_phonemes_caches_g2p_per_lang(monkeypatch):
    from plugin.audio.kokoro_g2p import _g2p_cache, misaki_phonemes

    _g2p_cache.clear()
    try:
        ja_inits: list[FakeJAG2P] = []
        zh_inits: list[object] = []
        espeak_inits: list[str] = []

        class FakeJAG2P:
            def __init__(self) -> None:
                ja_inits.append(self)

            def __call__(self, text: str):
                return "P:" + text, None

        class FakeZHG2P:
            def __init__(self, version=None) -> None:
                zh_inits.append(version)

            def __call__(self, text: str):
                return "P:" + text, None

        class FakeEspeakG2P:
            def __init__(self, language: str) -> None:
                espeak_inits.append(language)

            def __call__(self, text: str):
                return "P:" + text, None

        misaki = types.ModuleType("misaki")
        ja = types.ModuleType("misaki.ja")
        ja.JAG2P = FakeJAG2P
        zh = types.ModuleType("misaki.zh")
        zh.ZHG2P = FakeZHG2P
        espeak = types.ModuleType("misaki.espeak")
        espeak.EspeakG2P = FakeEspeakG2P

        monkeypatch.setitem(sys.modules, "misaki", misaki)
        monkeypatch.setitem(sys.modules, "misaki.ja", ja)
        monkeypatch.setitem(sys.modules, "misaki.zh", zh)
        monkeypatch.setitem(sys.modules, "misaki.espeak", espeak)

        assert misaki_phonemes("text_ja1", "ja") == "P:text_ja1"
        assert misaki_phonemes("text_ja2", "ja") == "P:text_ja2"
        assert misaki_phonemes("text_zh1", "zh") == "P:text_zh1"
        assert misaki_phonemes("text_zh2", "zh") == "P:text_zh2"
        assert misaki_phonemes("text_es1", "es") == "P:text_es1"
        assert misaki_phonemes("text_es2", "es") == "P:text_es2"
        assert misaki_phonemes("text_en", "en-us") == ""

        assert len(ja_inits) == 1
        assert zh_inits == [None]
        assert espeak_inits == ["es"]
        assert "en-us" not in _g2p_cache
    finally:
        _g2p_cache.clear()


def test_misaki_phonemes_does_not_cache_a_failed_construct(monkeypatch):
    from plugin.audio.kokoro_g2p import _g2p_cache, misaki_phonemes

    _g2p_cache.clear()
    try:
        class BrokenJAG2P:
            def __init__(self) -> None:
                raise RuntimeError("no dict")

        misaki = types.ModuleType("misaki")
        ja = types.ModuleType("misaki.ja")
        ja.JAG2P = BrokenJAG2P
        monkeypatch.setitem(sys.modules, "misaki", misaki)
        monkeypatch.setitem(sys.modules, "misaki.ja", ja)

        with pytest.raises(RuntimeError, match="no dict"):
            misaki_phonemes("a", "ja")
        assert "ja" not in _g2p_cache

        with pytest.raises(RuntimeError, match="no dict"):
            misaki_phonemes("a", "ja")
        assert "ja" not in _g2p_cache
    finally:
        _g2p_cache.clear()


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


def test_script_missing_voice_uses_af_sky():
    # Bella is listed first so a names[0] fallback would pick it. Sky is the
    # preferred substitute when the requested id is missing from the pack.
    g2p, created = _run("ja", "jf_alpha", "あ", voices={"af_bella": 1, "af_sky": 1})
    assert created["voice"] == "af_sky"
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
