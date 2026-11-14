# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for TTS voice catalog loading and validation."""

from __future__ import annotations

import copy
import re

import pytest

from plugin.audio.voice_catalog import (
    CATALOG_PATH,
    _load_catalog,
    _parse_catalog,
)


def test_parse_catalog_non_object_raw():
    with pytest.raises(ValueError, match="^voice_catalog.json: asset must be a JSON object$"):
        _parse_catalog(["not", "a", "dict"])


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        pytest.param(lambda r: r.pop("piper"), r"^voice_catalog\.json: missing 'piper' object$", id="missing_family"),
        pytest.param(lambda r: r["piper"]["voices"][3].pop("onnx"), r"piper\.voices\[3\].*onnx", id="piper_missing_onnx"),
        pytest.param(
            lambda r: r["kokoro"]["voices"][1].update(id=r["kokoro"]["voices"][0]["id"]),
            lambda r: rf"^voice_catalog\.json: kokoro\.voices\[1\] duplicate id '{re.escape(r['kokoro']['voices'][0]['id'])}'$",
            id="duplicate_id",
        ),
        pytest.param(
            lambda r: r["kokoro"]["voices"][0].update(id=""),
            r"^voice_catalog\.json: kokoro\.voices\[0\] field 'id' must be a non-empty string$",
            id="empty_id",
        ),
        pytest.param(
            lambda r: r["kokoro"].update(fallback_voice="af_skyx"),
            r"^voice_catalog\.json: kokoro\.fallback_voice -> 'af_skyx' is not a kokoro voice$",
            id="bad_fallback",
        ),
        pytest.param(
            lambda r: r["kokoro"]["locale_defaults"].update(es="ef_dorax"),
            r"^voice_catalog\.json: kokoro\.locale_defaults\['es'\] -> 'ef_dorax' is not a kokoro voice$",
            id="bad_locale_default",
        ),
        pytest.param(
            lambda r: r["kokoro"]["openai_aliases"].update(nova="af_skyx"),
            r"^voice_catalog\.json: kokoro\.openai_aliases\.nova -> 'af_skyx' is not a kokoro voice$",
            id="bad_alias_target",
        ),
    ],
)
def test_parse_catalog_rejects_invalid(mutator, match) -> None:
    mutated = copy.deepcopy(_load_catalog(CATALOG_PATH))
    mutator(mutated)
    pattern = match(mutated) if callable(match) else match
    with pytest.raises(ValueError, match=pattern):
        _parse_catalog(mutated)
