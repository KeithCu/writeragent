# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for TTS voice catalog loading and validation."""

from __future__ import annotations

import copy

import pytest

from plugin.audio.voice_catalog import (
    CATALOG_PATH,
    _load_catalog,
    _parse_catalog,
)

EXPECTED_NEW_KOKORO_IDS = [
    "af_alloy",
    "af_aoede",
    "af_jessica",
    "af_kore",
    "af_nicole",
    "af_nova",
    "af_river",
    "am_echo",
    "am_eric",
    "am_fenrir",
    "am_liam",
    "am_onyx",
    "am_puck",
    "am_santa",
    "bf_alice",
    "bf_lily",
    "bm_daniel",
    "bm_fable",
    "jf_nezumi",
    "jf_tebukuro",
    "zf_xiaoni",
    "zf_xiaoxiao",
    "zf_xiaoyi",
    "zm_yunxi",
    "zm_yunxia",
    "zm_yunyang",
    "pm_alex",
    "pm_santa",
]


def test_real_asset_parses():
    raw = _load_catalog(CATALOG_PATH)
    catalog = _parse_catalog(raw)

    assert len(catalog.piper) == 44
    assert len(catalog.kokoro) == 54
    assert catalog.piper_fallback == "en_US-lessac-medium"
    assert catalog.kokoro_fallback == "af_sky"
    assert catalog.kokoro_aliases["nova"] == "af_sky"

    kokoro_ids = [r.id for r in catalog.kokoro]
    for expected_id in EXPECTED_NEW_KOKORO_IDS:
        assert expected_id in kokoro_ids


def test_parse_catalog_non_object_raw():
    with pytest.raises(ValueError, match="^voice_catalog.json: asset must be a JSON object$"):
        _parse_catalog(["not", "a", "dict"])


def test_parse_catalog_missing_family():
    raw = _load_catalog(CATALOG_PATH)
    mutated = copy.deepcopy(raw)
    del mutated["piper"]
    with pytest.raises(ValueError, match="^voice_catalog.json: missing 'piper' object$"):
        _parse_catalog(mutated)


def test_parse_catalog_piper_missing_onnx():
    raw = _load_catalog(CATALOG_PATH)
    mutated = copy.deepcopy(raw)
    del mutated["piper"]["voices"][3]["onnx"]
    with pytest.raises(ValueError, match=r"piper\.voices\[3\].*onnx"):
        _parse_catalog(mutated)


def test_parse_catalog_duplicate_id():
    raw = _load_catalog(CATALOG_PATH)
    mutated = copy.deepcopy(raw)
    mutated["kokoro"]["voices"][1]["id"] = mutated["kokoro"]["voices"][0]["id"]
    dup_id = mutated["kokoro"]["voices"][0]["id"]
    with pytest.raises(ValueError, match=rf"^voice_catalog\.json: kokoro\.voices\[1\] duplicate id '{dup_id}'$"):
        _parse_catalog(mutated)


def test_parse_catalog_empty_id():
    raw = _load_catalog(CATALOG_PATH)
    mutated = copy.deepcopy(raw)
    mutated["kokoro"]["voices"][0]["id"] = ""
    with pytest.raises(ValueError, match=r"^voice_catalog\.json: kokoro\.voices\[0\] field 'id' must be a non-empty string$"):
        _parse_catalog(mutated)


def test_parse_catalog_bad_fallback():
    raw = _load_catalog(CATALOG_PATH)
    mutated = copy.deepcopy(raw)
    mutated["kokoro"]["fallback_voice"] = "af_skyx"
    with pytest.raises(ValueError, match=r"^voice_catalog\.json: kokoro\.fallback_voice -> 'af_skyx' is not a kokoro voice$"):
        _parse_catalog(mutated)


def test_parse_catalog_bad_locale_default():
    raw = _load_catalog(CATALOG_PATH)
    mutated = copy.deepcopy(raw)
    mutated["kokoro"]["locale_defaults"]["es"] = "ef_dorax"
    with pytest.raises(ValueError, match=r"^voice_catalog\.json: kokoro\.locale_defaults\['es'\] -> 'ef_dorax' is not a kokoro voice$"):
        _parse_catalog(mutated)


def test_parse_catalog_bad_alias_target():
    raw = _load_catalog(CATALOG_PATH)
    mutated = copy.deepcopy(raw)
    mutated["kokoro"]["openai_aliases"]["nova"] = "af_skyx"
    with pytest.raises(ValueError, match=r"^voice_catalog\.json: kokoro\.openai_aliases\.nova -> 'af_skyx' is not a kokoro voice$"):
        _parse_catalog(mutated)
