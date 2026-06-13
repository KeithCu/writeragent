# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for folder_search_enabled in plugin.framework.constants."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.framework.constants import folder_search_enabled


def test_folder_search_disabled_by_default():
    with patch("plugin.framework.config.get_config", return_value=None):
        assert folder_search_enabled(MagicMock()) is False


def test_folder_search_enabled_when_hybrid():
    with patch("plugin.framework.config.get_config", return_value="hybrid"):
        assert folder_search_enabled(MagicMock()) is True


def test_folder_search_disabled_for_other_values():
    with patch("plugin.framework.config.get_config", return_value="embeddings"):
        assert folder_search_enabled(MagicMock()) is False
