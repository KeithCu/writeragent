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


def _del_piper(raw: dict) -> None:
    del raw["piper"]


def _del_piper_onnx(raw: dict) -> None:
    del raw["piper"]["voices"][3]["onnx"]


def _dup_kokoro_id(raw: dict) -> None:
    raw["kokoro"]["voices"][1]["id"] = raw["kokoro"]["voices"][0]["id"]


def _empty_kokoro_id(raw: dict) -> None:
    raw["kokoro"]["voices"][0]["id"] = ""


def _bad_fallback(raw: dict) -> None:
    raw["kokoro"]["fallback_voice"] = "af_skyx"


def _bad_locale_default(raw: dict) -> None:
    raw["kokoro"]["locale_defaults"]["es"] = "ef_dorax"


def _bad_alias_target(raw: dict) -> None:
    raw["kokoro"]["openai_aliases"]["nova"] = "af_skyx"


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        pytest.param(_del_piper, r"^voice_catalog\.json: missing 'piper' object$", id="missing_family"),
        pytest.param(_del_piper_onnx, r"piper\.voices\[3\].*onnx", id="piper_missing_onnx"),
        pytest.param(_dup_kokoro_id, r"^voice_catalog\.json: kokoro\.voices\[1\] duplicate id '[^']+'$", id="duplicate_id"),
        pytest.param(
            _empty_kokoro_id,
            r"^voice_catalog\.json: kokoro\.voices\[0\] field 'id' must be a non-empty string$",
            id="empty_id",
        ),
        pytest.param(
            _bad_fallback,
            r"^voice_catalog\.json: kokoro\.fallback_voice -> 'af_skyx' is not a kokoro voice$",
            id="bad_fallback",
        ),
        pytest.param(
            _bad_locale_default,
            r"^voice_catalog\.json: kokoro\.locale_defaults\['es'\] -> 'ef_dorax' is not a kokoro voice$",
            id="bad_locale_default",
        ),
        pytest.param(
            _bad_alias_target,
            r"^voice_catalog\.json: kokoro\.openai_aliases\.nova -> 'af_skyx' is not a kokoro voice$",
            id="bad_alias_target",
        ),
    ],
)
def test_parse_catalog_rejects_invalid(mutator, match: str) -> None:
    mutated = copy.deepcopy(_load_catalog(CATALOG_PATH))
    mutator(mutated)
    with pytest.raises(ValueError, match=match):
        _parse_catalog(mutated)
