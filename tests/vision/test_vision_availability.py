# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for vision tool-list gating (no LibreOffice required)."""
from unittest.mock import patch

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.vision.vision_availability import filter_get_image_for_text_only_model


class _T:
    def __init__(self, name):
        self.name = name


def _tools():
    return [_T("apply_document_content"), _T("get_image"), _T("search_in_document")]


def _patch(vision):
    return (
        patch("plugin.framework.client.model_fetcher.has_native_vision", return_value=vision),
        patch("plugin.framework.client.model_fetcher.get_text_model", return_value="m"),
        patch("plugin.framework.client.model_fetcher.get_current_endpoint", return_value="e"),
    )


def test_get_image_kept_for_vision_model():
    p1, p2, p3 = _patch(True)
    with p1, p2, p3:
        names = [t.name for t in filter_get_image_for_text_only_model(_tools())]
    assert "get_image" in names


def test_get_image_dropped_for_text_only_model():
    p1, p2, p3 = _patch(False)
    with p1, p2, p3:
        names = [t.name for t in filter_get_image_for_text_only_model(_tools())]
    assert "get_image" not in names
    assert "apply_document_content" in names and "search_in_document" in names


def test_fail_open_keeps_get_image_when_capability_unknown():
    # If vision can't be determined (error), keep the tool rather than hide a working one.
    with patch("plugin.framework.client.model_fetcher.has_native_vision", side_effect=RuntimeError("boom")), \
         patch("plugin.framework.client.model_fetcher.get_text_model", return_value="m"), \
         patch("plugin.framework.client.model_fetcher.get_current_endpoint", return_value="e"):
        names = [t.name for t in filter_get_image_for_text_only_model(_tools())]
    assert "get_image" in names
