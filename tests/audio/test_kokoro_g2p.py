"""Misaki G2P selection for local Kokoro. The venv script is exec'd with fake modules."""

from __future__ import annotations

import re
import subprocess
import sys
import types
from typing import Any
from unittest.mock import patch

import pytest

from plugin.audio.kokoro_g2p import (
    KOKORO_VOICE_PREFIX_TO_LANG,
    _kokoro_lang_for_voice,
    clear_kokoro_misaki_probe_cache,
    ensure_kokoro_misaki,
    kokoro_g2p_lang,
    kokoro_lang_uses_misaki,
    kokoro_misaki_install_hint,
    kokoro_misaki_probe_code,
    misaki_frontend,
)
from plugin.audio.voice_catalog import KOKORO_CATALOG_ITEMS
from plugin.framework.send_cancellation import SendCancellation


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    clear_kokoro_misaki_probe_cache()
    try:
        yield
    finally:
        clear_kokoro_misaki_probe_cache()


@pytest.mark.parametrize(
    ("lang", "uses_misaki"),
    [
        ("ja", True),
        ("zh", True),
        ("es", True),
        ("fr-fr", True),
        ("it", True),
        ("hi", True),
        ("pt-br", True),
        ("en-us", False),
        ("en-gb", False),
    ],
)
def test_misaki_lang_properties(lang: str, uses_misaki: bool):
    assert kokoro_lang_uses_misaki(lang) is uses_misaki

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
                return ("ok", None)

        fake_ja: Any = types.ModuleType("misaki.ja")
        fake_ja.JAG2P = FakeG2P
        fake_zh: Any = types.ModuleType("misaki.zh")
        fake_zh.ZHG2P = FakeG2P
        fake_espeak: Any = types.ModuleType("misaki.espeak")
        fake_espeak.EspeakG2P = FakeG2P
        with patch.dict(
            sys.modules,
            {"misaki.ja": fake_ja, "misaki.zh": fake_zh, "misaki.espeak": fake_espeak},
        ):
            with pytest.raises(SystemExit) as excinfo:
                exec(compile(code, "<probe>", "exec"), {})
            assert excinfo.value.code == 0
        assert len(calls) == 1


def test_misaki_probe_code_empty_phonemes_fails():
    code = kokoro_misaki_probe_code("ja")
    assert code

    class EmptyFakeG2P:
        def __init__(self, **kwargs):
            pass

        def __call__(self, text):
            return ("", None)

    fake_ja: Any = types.ModuleType("misaki.ja")
    fake_ja.JAG2P = EmptyFakeG2P
    with patch.dict(sys.modules, {"misaki.ja": fake_ja}):
        with pytest.raises(SystemExit) as excinfo:
            exec(compile(code, "<probe>", "exec"), {})
        assert excinfo.value.code == 1

    class NoneFakeG2P:
        def __init__(self, **kwargs):
            pass

        def __call__(self, text):
            return (None, None)

    fake_ja.JAG2P = NoneFakeG2P
    with patch.dict(sys.modules, {"misaki.ja": fake_ja}):
        with pytest.raises(SystemExit) as excinfo:
            exec(compile(code, "<probe>", "exec"), {})
        assert excinfo.value.code == 1


def test_kokoro_unknown_voice_prefix_logs_once(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="plugin.audio.kokoro_g2p")
    assert kokoro_g2p_lang("hi", "qf_nobody") == "en-us"
    records = [
        r for r in caplog.records
        if r.name == "plugin.audio.kokoro_g2p" and "no G2P prefix" in r.getMessage()
    ]
    assert len(records) == 1
    assert "qf_nobody" in records[0].getMessage()

    assert kokoro_g2p_lang("hi", "qf_nobody") == "en-us"
    records = [
        r for r in caplog.records
        if r.name == "plugin.audio.kokoro_g2p" and "no G2P prefix" in r.getMessage()
    ]
    assert len(records) == 1


def test_kokoro_catalog_prefixes_match_g2p():
    pattern = re.compile(r"^[abefhijpz][fm]_")
    lang_map = {
        "en": {"en-us", "en-gb"},
        "es": {"es"},
        "fr": {"fr-fr"},
        "it": {"it"},
        "ja": {"ja"},
        "zh": {"zh"},
        "hi": {"hi"},
        "pt": {"pt-br"},
    }
    for item in KOKORO_CATALOG_ITEMS:
        voice_id = item["value"]
        prefix = voice_id[:1]
        assert prefix in KOKORO_VOICE_PREFIX_TO_LANG
        assert pattern.match(voice_id)
        mapped = KOKORO_VOICE_PREFIX_TO_LANG[prefix]
        assert mapped in lang_map[item["lang"]]
        assert mapped in ("en-us", "en-gb") or kokoro_lang_uses_misaki(mapped)


def test_ensure_kokoro_misaki_probe_cache_success():
    calls: list[list[str]] = []

    def fake_run(cmd, timeout):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert ensure_kokoro_misaki("/venv/bin/python", "ja", run_cmd=fake_run) is True
    assert len(calls) == 1

    assert ensure_kokoro_misaki("/venv/bin/python", "ja", run_cmd=fake_run) is True
    assert len(calls) == 1


def test_ensure_kokoro_misaki_probe_cache_failed_not_remembered():
    calls: list[list[str]] = []

    def fake_run(cmd, timeout):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 1, "", "error")

    assert ensure_kokoro_misaki("/venv/bin/python", "es", run_cmd=fake_run) is False
    c_invocations_1 = sum(1 for c in calls if "-c" in c)
    assert c_invocations_1 == 1

    assert ensure_kokoro_misaki("/venv/bin/python", "es", run_cmd=fake_run) is False
    c_invocations_2 = sum(1 for c in calls if "-c" in c)
    assert c_invocations_2 == 2


def test_ensure_kokoro_misaki_probe_cache_different_venvs():
    calls: list[list[str]] = []

    def fake_run(cmd, timeout):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert ensure_kokoro_misaki("/venv/bin/python", "ja", run_cmd=fake_run) is True
    assert ensure_kokoro_misaki("/other/bin/python", "ja", run_cmd=fake_run) is True
    assert len(calls) == 2


def test_ensure_kokoro_misaki_cached_success_cancelled():
    calls: list[list[str]] = []

    def fake_run(cmd, timeout):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert ensure_kokoro_misaki("/venv/bin/python", "ja", run_cmd=fake_run) is True
    assert len(calls) == 1

    cancel = SendCancellation()
    cancel.cancel()
    res = ensure_kokoro_misaki(
        "/venv/bin/python", "ja", cancel=cancel, run_cmd=fake_run
    )
    assert res is None
    assert len(calls) == 1


def test_kokoro_misaki_install_hints():
    assert kokoro_misaki_install_hint("ja") == "uv pip install 'misaki[ja]'"
    assert kokoro_misaki_install_hint("zh") == "uv pip install 'misaki[zh]'"
    assert kokoro_misaki_install_hint("fr-fr") == "uv pip install misaki phonemizer-fork espeakng-loader"


def test_misaki_frontend_helper_uses_the_same_snippets(monkeypatch):
    """The warm worker calls this helper; it must match the one-shot script."""
    from plugin.audio.kokoro_g2p import _g2p_cache, misaki_frontend

    g2p_calls: list[tuple[str, str]] = []

    class FakeJAG2P:
        def __call__(self, text: str):
            g2p_calls.append(("ja", text))
            return "JA:" + text, None

    misaki_mod: Any = types.ModuleType("misaki")
    misaki_mod.__path__ = []  # type: ignore[attr-defined]
    ja_mod: Any = types.ModuleType("misaki.ja")
    ja_mod.JAG2P = FakeJAG2P

    _g2p_cache.clear()
    try:
        monkeypatch.setitem(sys.modules, "misaki", misaki_mod)
        monkeypatch.setitem(sys.modules, "misaki.ja", ja_mod)

        assert misaki_frontend("非常に強力", "ja") == ("JA:非常に強力", "")
        assert misaki_frontend("Hello", "en-us") == ("", "")
        assert g2p_calls == [("ja", "非常に強力")]
    finally:
        _g2p_cache.clear()


def test_misaki_frontend_caches_g2p_per_lang(monkeypatch):
    from plugin.audio.kokoro_g2p import _g2p_cache, misaki_frontend

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

        misaki: Any = types.ModuleType("misaki")
        ja: Any = types.ModuleType("misaki.ja")
        ja.JAG2P = FakeJAG2P
        zh: Any = types.ModuleType("misaki.zh")
        zh.ZHG2P = FakeZHG2P
        espeak: Any = types.ModuleType("misaki.espeak")
        espeak.EspeakG2P = FakeEspeakG2P

        monkeypatch.setitem(sys.modules, "misaki", misaki)
        monkeypatch.setitem(sys.modules, "misaki.ja", ja)
        monkeypatch.setitem(sys.modules, "misaki.zh", zh)
        monkeypatch.setitem(sys.modules, "misaki.espeak", espeak)

        assert misaki_frontend("text_ja1", "ja") == ("P:text_ja1", "")
        assert misaki_frontend("text_ja2", "ja") == ("P:text_ja2", "")
        assert misaki_frontend("text_zh1", "zh") == ("P:text_zh1", "")
        assert misaki_frontend("text_zh2", "zh") == ("P:text_zh2", "")
        assert misaki_frontend("text_es1", "es") == ("P:text_es1", "")
        assert misaki_frontend("text_es2", "es") == ("P:text_es2", "")
        assert misaki_frontend("text_en", "en-us") == ("", "")

        assert len(ja_inits) == 1
        assert zh_inits == [None]
        assert espeak_inits == ["es"]
        assert "en-us" not in _g2p_cache
    finally:
        _g2p_cache.clear()


def test_get_misaki_g2p_does_not_cache_a_failed_construct(monkeypatch):
    from plugin.audio.kokoro_g2p import _g2p_cache, _get_misaki_g2p

    _g2p_cache.clear()
    try:
        class BrokenJAG2P:
            def __init__(self) -> None:
                raise RuntimeError("no dict")

        misaki: Any = types.ModuleType("misaki")
        ja: Any = types.ModuleType("misaki.ja")
        ja.JAG2P = BrokenJAG2P
        monkeypatch.setitem(sys.modules, "misaki", misaki)
        monkeypatch.setitem(sys.modules, "misaki.ja", ja)

        with pytest.raises(RuntimeError, match="no dict"):
            _get_misaki_g2p("ja")
        assert "ja" not in _g2p_cache

        with pytest.raises(RuntimeError, match="no dict"):
            _get_misaki_g2p("ja")
        assert "ja" not in _g2p_cache
    finally:
        _g2p_cache.clear()


def test_ensure_kokoro_misaki_installs_ja_when_probe_fails():
    calls: list[list[str]] = []
    probes = {"n": 0}

    def fake_run(cmd, timeout):
        calls.append(list(cmd))
        if "-c" in cmd:
            probes["n"] += 1
            code = 1 if probes["n"] == 1 else 0
            err = "ModuleNotFoundError" if code else ""
            return subprocess.CompletedProcess(cmd, code, "", err)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    messages: list[str] = []
    with patch("plugin.audio.kokoro_g2p.shutil.which", return_value=None):
        ready = ensure_kokoro_misaki(
            "/venv/bin/python", "ja", on_status=messages.append, run_cmd=fake_run
        )

    assert ready is True
    assert calls[1] == ["/venv/bin/python", "-m", "pip", "install", "misaki[ja]"]
    assert not any("unidic" in part for cmd in calls for part in cmd)
    assert messages == ["Installing Kokoro phonemizer…"]


def test_ensure_kokoro_misaki_downloads_unidic_when_ja_still_unusable():
    calls: list[list[str]] = []
    probes = {"n": 0}

    def fake_run(cmd, timeout):
        calls.append(list(cmd))
        if "-c" in cmd:
            probes["n"] += 1
            code = 0 if probes["n"] >= 3 else 1
            return subprocess.CompletedProcess(cmd, code, "", "no dictionary" if code else "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    messages: list[str] = []
    with patch("plugin.audio.kokoro_g2p.shutil.which", return_value="/usr/bin/uv"):
        ready = ensure_kokoro_misaki(
            "/venv/bin/python", "ja", on_status=messages.append, run_cmd=fake_run
        )

    assert ready is True
    assert calls[1] == [
        "/usr/bin/uv", "pip", "install", "--python", "/venv/bin/python", "misaki[ja]",
    ]
    assert calls[3] == ["/venv/bin/python", "-m", "unidic", "download"]
    assert len(messages) >= 2


def test_ensure_kokoro_misaki_espeak_langs_skip_torch_and_unidic():
    calls: list[list[str]] = []
    probes = {"n": 0}

    def fake_run(cmd, timeout):
        calls.append(list(cmd))
        if "-c" in cmd:
            probes["n"] += 1
            code = 1 if probes["n"] == 1 else 0
            return subprocess.CompletedProcess(cmd, code, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("plugin.audio.kokoro_g2p.shutil.which", return_value=None):
        ready = ensure_kokoro_misaki("/venv/bin/python", "fr-fr", run_cmd=fake_run)

    assert ready is True
    assert calls[1] == [
        "/venv/bin/python", "-m", "pip", "install", "misaki", "phonemizer-fork", "espeakng-loader",
    ]


def test_ensure_kokoro_misaki_reports_install_failure_and_does_not_raise():
    def fake_run(cmd, timeout):
        if "-c" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "ModuleNotFoundError: misaki")
        return subprocess.CompletedProcess(cmd, 1, "", "no network")

    messages: list[str] = []
    with patch("plugin.audio.kokoro_g2p.shutil.which", return_value=None):
        ready = ensure_kokoro_misaki(
            "/venv/bin/python", "es", on_status=messages.append, run_cmd=fake_run
        )

    assert ready is False
    assert len(messages) == 2


def test_ensure_kokoro_misaki_english_does_not_install():
    calls: list[list[str]] = []

    def fake_run(cmd, timeout):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    ready = ensure_kokoro_misaki("/venv/bin/python", "en-us", run_cmd=fake_run)
    assert ready is True
    assert calls == []


def test_ensure_kokoro_misaki_cancelled_skips_install():
    calls: list[list[str]] = []

    def fake_run(cmd, timeout):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    cancel = SendCancellation()
    cancel.cancel()
    ready = ensure_kokoro_misaki(
        "/venv/bin/python", "ja", cancel=cancel, run_cmd=fake_run
    )
    assert ready is None
    assert calls == []


def test_kokoro_g2p_lang_english_on_non_english_voice():
    english = "Hello, I'm your LibreOffice WriterAgent."
    assert kokoro_g2p_lang(english, "jf_alpha") == "en-us"
    assert kokoro_g2p_lang(english, "zf_xiaobei") == "en-us"
    assert kokoro_g2p_lang(english, "hf_alpha") == "en-us"
    assert kokoro_g2p_lang("hello", "af_bella") == "en-us"
    assert kokoro_g2p_lang(english, "bf_emma") == "en-gb"
    assert kokoro_g2p_lang("Café au lait", "ff_siwis") == "fr-fr"
    assert kokoro_g2p_lang("Bonjour, je suis WriterAgent.", "ff_siwis") == "fr-fr"
    assert kokoro_g2p_lang("Hola, ¿cómo estás?", "ef_dora") == "es"
    assert kokoro_g2p_lang("Ciao, come stai?", "if_sara") == "it"
    assert kokoro_g2p_lang("Olá, tudo bem?", "pf_dora") == "pt-br"
    assert kokoro_g2p_lang("こんにちは、WriterAgent です。", "jf_alpha") == "ja"
    assert kokoro_g2p_lang("你好", "zf_xiaobei") == "zh"
    assert kokoro_g2p_lang("नमस्ते", "hf_alpha") == "hi"


def test_kokoro_lang_for_voice():
    assert _kokoro_lang_for_voice("af_bella") == "en-us"
    assert _kokoro_lang_for_voice("am_adam") == "en-us"
    assert _kokoro_lang_for_voice("bf_emma") == "en-gb"
    assert _kokoro_lang_for_voice("bm_george") == "en-gb"
    assert _kokoro_lang_for_voice("ef_dora") == "es"
    assert _kokoro_lang_for_voice("em_alex") == "es"
    assert _kokoro_lang_for_voice("ff_siwis") == "fr-fr"
    assert _kokoro_lang_for_voice("if_sara") == "it"
    assert _kokoro_lang_for_voice("jf_alpha") == "ja"
    assert _kokoro_lang_for_voice("zf_xiaobei") == "zh"
    assert _kokoro_lang_for_voice("hf_alpha") == "hi"
    assert _kokoro_lang_for_voice("pf_dora") == "pt-br"


def test_misaki_frontend_english_and_non_misaki():
    assert misaki_frontend("Hello", "en-us") == ("", "")
    assert misaki_frontend("Hello", "en-gb") == ("", "")
    assert misaki_frontend("Hello", "") == ("", "")
    assert misaki_frontend("Hello", "unknown") == ("", "")


def test_misaki_frontend_success():
    with patch("plugin.audio.kokoro_g2p._get_misaki_g2p", return_value=lambda text: ("k o n n i c h i w a", None)):
        phonemes, warning = misaki_frontend("こんにちは", "ja")
    assert phonemes == "k o n n i c h i w a"
    assert warning == ""


def test_misaki_frontend_empty_phonemes_warning():
    with patch("plugin.audio.kokoro_g2p._get_misaki_g2p", return_value=lambda text: ("   ", None)):
        phonemes, warning = misaki_frontend("こんにちは", "ja")
    assert phonemes == ""
    assert "ja" in warning


def test_misaki_frontend_exception_warning():
    with patch("plugin.audio.kokoro_g2p._get_misaki_g2p", side_effect=RuntimeError("G2P crash")):
        phonemes, warning = misaki_frontend("Bonjour", "fr-fr")
    assert phonemes == ""
    assert "fr-fr" in warning
    assert "G2P crash" in warning
