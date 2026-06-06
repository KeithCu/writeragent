# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.vision_common."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting.vision_common import merge_vision_params


def test_merge_vision_params_applies_config_defaults():
    ctx = MagicMock()
    with patch("plugin.framework.config.get_config") as mock_get:
        mock_get.side_effect = lambda _ctx, key: {
            "vision.lang": "de",
            "vision.images_scale": 2.0,
            "vision.table_mode": "fast",
        }.get(key)

        merged = merge_vision_params(ctx, None)

    assert merged["lang"] == "de"
    assert merged["images_scale"] == 2.0
    assert merged["table_mode"] == "fast"


def test_merge_vision_params_template_overrides_config():
    ctx = MagicMock()
    with patch("plugin.framework.config.get_config") as mock_get:
        mock_get.side_effect = lambda _ctx, key: {"vision.lang": "de"}.get(key)

        merged = merge_vision_params(ctx, {"lang": "en", "ocr_backend": "rapidocr_paddle"})

    assert merged["lang"] == "en"
    assert merged["ocr_backend"] == "rapidocr_paddle"


def test_merge_vision_params_skips_empty_config_values():
    ctx = MagicMock()
    with patch("plugin.framework.config.get_config", return_value=""):
        merged = merge_vision_params(ctx, {"lang": "en"})

    assert merged == {"lang": "en"}
