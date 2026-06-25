# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for cross-file rerank Settings."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.framework.constants import (
    FOLDER_RERANK_MODEL_ENGLISH_SMALL,
    FOLDER_RERANK_MODEL_MULTILINGUAL,
    folder_rerank_enabled,
    resolve_folder_rerank_model,
)
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


def test_folder_rerank_enabled_defaults_false():
    ctx = MagicMock()
    with patch("plugin.framework.config.get_config_bool", return_value=False):
        assert folder_rerank_enabled() is False


def test_folder_rerank_enabled_reads_config():
    ctx = MagicMock()
    with patch("plugin.framework.config.get_config_bool", return_value=True) as mock_bool:
        assert folder_rerank_enabled() is True
    mock_bool.assert_called_once_with("embeddings.folder_rerank_enabled")


def test_resolve_folder_rerank_model_defaults_english_small():
    ctx = MagicMock()
    with patch("plugin.framework.config.get_config", side_effect=lambda key: None):
        assert resolve_folder_rerank_model() == FOLDER_RERANK_MODEL_ENGLISH_SMALL


def test_resolve_folder_rerank_model_multilingual_id():
    ctx = MagicMock()

    def _cfg(key):
        if key == "embeddings.folder_rerank_model":
            return FOLDER_RERANK_MODEL_MULTILINGUAL
        return None

    with patch("plugin.framework.config.get_config", side_effect=_cfg):
        assert resolve_folder_rerank_model() == FOLDER_RERANK_MODEL_MULTILINGUAL


def test_resolve_folder_rerank_model_unknown_falls_back_to_default():
    ctx = MagicMock()

    def _cfg(key):
        if key == "embeddings.folder_rerank_model":
            return "english_small"
        return None

    with patch("plugin.framework.config.get_config", side_effect=_cfg):
        assert resolve_folder_rerank_model() == FOLDER_RERANK_MODEL_ENGLISH_SMALL
