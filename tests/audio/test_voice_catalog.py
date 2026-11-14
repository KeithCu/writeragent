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
