# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for folder_search_mode helpers in plugin.framework.constants."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.framework.constants import (
    document_research_uses_embeddings,
    document_research_uses_folder_fts,
    get_folder_search_mode,
)


def test_get_folder_search_mode_defaults_to_none():
    with patch("plugin.framework.config.get_config", return_value=None):
        assert get_folder_search_mode(MagicMock()) == "none"


def test_get_folder_search_mode_embeddings():
    with patch("plugin.framework.config.get_config", return_value="embeddings"):
        assert get_folder_search_mode(MagicMock()) == "embeddings"
        assert document_research_uses_embeddings(MagicMock()) is True
        assert document_research_uses_folder_fts(MagicMock()) is False


def test_get_folder_search_mode_fts():
    with patch("plugin.framework.config.get_config", return_value="fts"):
        assert get_folder_search_mode(MagicMock()) == "fts"
        assert document_research_uses_embeddings(MagicMock()) is False
        assert document_research_uses_folder_fts(MagicMock()) is True


def test_get_folder_search_mode_invalid_falls_back_to_none():
    with patch("plugin.framework.config.get_config", return_value="both"):
        assert get_folder_search_mode(MagicMock()) == "none"
        assert document_research_uses_embeddings(MagicMock()) is False
        assert document_research_uses_folder_fts(MagicMock()) is False


def test_get_folder_search_mode_normalizes_case():
    with patch("plugin.framework.config.get_config", return_value="Embeddings"):
        assert get_folder_search_mode(MagicMock()) == "embeddings"
