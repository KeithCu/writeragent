# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Vendored langdetect profile allowlist vs grammar registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugin.writer.locale.grammar_proofread_locale import (
    GRAMMAR_REGISTRY_LOCALE_TAGS,
    bcp47_to_langdetect_profile,
    get_grammar_detect_language_mode,
    langdetect_profiles_for_grammar_registry,
)

_CONTRIB_PROFILES = Path(__file__).resolve().parents[3] / "plugin" / "contrib" / "langdetect" / "profiles"


def test_bcp47_to_langdetect_profile_mapping() -> None:
    assert bcp47_to_langdetect_profile("en-US") == "en"
    assert bcp47_to_langdetect_profile("zh-CN") == "zh-cn"
    assert bcp47_to_langdetect_profile("zh-TW") == "zh-tw"
    assert bcp47_to_langdetect_profile("nb-NO") == "no"
    assert bcp47_to_langdetect_profile("nn-NO") == "no"


def test_on_disk_profiles_match_grammar_allowlist() -> None:
    if not _CONTRIB_PROFILES.is_dir():
        pytest.skip("plugin/contrib/langdetect not built — run make langdetect-contrib")
    allowed = langdetect_profiles_for_grammar_registry()
    on_disk = {p.name for p in _CONTRIB_PROFILES.iterdir() if p.is_file() and not p.name.startswith(".")}
    assert on_disk == allowed
    assert len(on_disk) >= 2
    for tag in GRAMMAR_REGISTRY_LOCALE_TAGS:
        prof = bcp47_to_langdetect_profile(tag)
        assert (_CONTRIB_PROFILES / prof).is_file(), f"missing profile for {tag} -> {prof}"


def test_get_grammar_detect_language_mode_legacy_bool() -> None:
    class _Cfg:
        def __init__(self, val):
            self._val = val

    from plugin.framework import config

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "get_config", lambda _ctx, _key: True)
        assert get_grammar_detect_language_mode(object()) == "llm"
        mp.setattr(config, "get_config", lambda _ctx, _key: False)
        assert get_grammar_detect_language_mode(object()) == "off"


def test_get_grammar_detect_language_mode_strings() -> None:
    from plugin.framework import config

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "get_config", lambda _ctx, _key: "langdetect")
        assert get_grammar_detect_language_mode(object()) == "langdetect"
        mp.setattr(config, "get_config", lambda _ctx, _key: "off")
        assert get_grammar_detect_language_mode(object()) == "off"


@pytest.mark.skipif(not _CONTRIB_PROFILES.is_dir(), reason="contrib langdetect missing")
def test_langdetect_smoke_french() -> None:
    from plugin.contrib.langdetect import detect_langs

    langs = detect_langs("Bonjour le monde.")
    assert langs
    assert langs[0].lang == "fr"
