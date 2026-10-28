# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for get_image tool-list gating by text model vision capability."""
from typing import Any
from unittest.mock import patch

import pytest

from plugin.framework.service import ServiceRegistry
from plugin.framework.tool import ToolBase, ToolRegistry
from tests.testing_utils import TestingFactory
from plugin.vision.image_filter import (
    filter_get_image_for_text_only_model,
)


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


def test_get_image_kept_when_vision_unknown():
    p1, p2, p3 = _patch(None)
    with p1, p2, p3:
        names = [t.name for t in filter_get_image_for_text_only_model(_tools())]
    assert "get_image" in names


def test_get_image_dropped_for_text_only_model():
    p1, p2, p3 = _patch(False)
    with p1, p2, p3:
        names = [t.name for t in filter_get_image_for_text_only_model(_tools())]
    assert "get_image" not in names
    assert "apply_document_content" in names and "search_in_document" in names


def test_exceptions_not_swallowed():
    with patch("plugin.framework.client.model_fetcher.has_native_vision", side_effect=RuntimeError("boom")), \
         patch("plugin.framework.client.model_fetcher.get_text_model", return_value="m"), \
         patch("plugin.framework.client.model_fetcher.get_current_endpoint", return_value="e"):
        with pytest.raises(RuntimeError, match="boom"):
            filter_get_image_for_text_only_model(_tools())


def test_uncatalogued_gemini_flash_keeps_get_image_without_catalog_get():
    # Chat Send is on the UI thread. An id with no static row and no stored
    # answer returns None on UI thread and keeps get_image without GET /v1/models.
    import plugin.framework.client.model_fetcher as mf

    mf._model_fetch_vision_cache.clear()
    for cache in (mf._model_fetch_cache, mf._model_fetch_image_cache):
        for key in list(cache):
            if "openrouter.ai" in key:
                cache.pop(key, None)
    with patch("plugin.framework.client.requests.sync_request") as sync, \
         patch("plugin.framework.client.model_fetcher.get_config", return_value={}), \
         patch("plugin.framework.client.model_fetcher.set_config"), \
         patch("plugin.framework.client.model_fetcher.get_api_key_for_endpoint", return_value=""), \
         patch("plugin.framework.client.model_fetcher.get_text_model", return_value="google/gemini-3.8-flash"), \
         patch("plugin.framework.client.model_fetcher.get_current_endpoint", return_value="https://openrouter.ai/api"), \
         patch("plugin.framework.client.model_fetcher.on_main_thread", return_value=True), \
         patch("plugin.framework.client.model_fetcher.run_in_background") as bg:
        names = [t.name for t in filter_get_image_for_text_only_model(_tools())]
        sync.assert_not_called()
        bg.assert_called_once()
    mf._vision_in_flight.clear()
    assert "get_image" in names


def test_catalogued_text_only_model_drops_get_image_without_fetch():
    import plugin.framework.client.model_fetcher as mf

    mf._model_fetch_vision_cache.clear()
    with patch("plugin.framework.client.model_fetcher.fetch_available_models") as fetch, \
         patch("plugin.framework.client.model_fetcher.get_config", return_value={}), \
         patch("plugin.framework.client.model_fetcher.get_text_model", return_value="openai/gpt-oss-20b"), \
         patch("plugin.framework.client.model_fetcher.get_current_endpoint", return_value="https://openrouter.ai/api"), \
         patch("plugin.framework.client.model_fetcher.on_main_thread", return_value=True), \
         patch("plugin.framework.client.model_fetcher.run_in_background"):
        names = [t.name for t in filter_get_image_for_text_only_model(_tools())]
        fetch.assert_not_called()
    mf._vision_in_flight.clear()
    assert "get_image" not in names


def test_stored_vision_false_drops_get_image_without_fetch():
    import plugin.framework.client.model_fetcher as mf

    cache = {"https://openrouter.ai/api@google/gemini-3.8-flash": False}

    def mock_get_config(key):
        if key == "vision_support_map":
            return cache
        return {}

    mf._model_fetch_vision_cache.clear()
    with patch("plugin.framework.client.model_fetcher.fetch_available_models") as fetch, \
         patch("plugin.framework.client.model_fetcher.get_config", side_effect=mock_get_config), \
         patch("plugin.framework.client.model_fetcher.get_text_model", return_value="google/gemini-3.8-flash"), \
         patch("plugin.framework.client.model_fetcher.get_current_endpoint", return_value="https://openrouter.ai/api"):
        names = [t.name for t in filter_get_image_for_text_only_model(_tools())]
        fetch.assert_not_called()
    assert "get_image" not in names


class _GetImageTool(ToolBase):
    name = "get_image"
    description = "Fetch image from document"
    parameters = {"type": "object", "properties": {}}
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return {"status": "ok"}


def test_openai_hides_get_image_but_mcp_keeps_it():
    reg = ToolRegistry(ServiceRegistry())
    reg.register(_GetImageTool())
    p1, p2, p3 = _patch(False)
    with p1, p2, p3:
        openai_schemas = reg.get_schemas("openai", doc=TestingFactory.create_doc(doc_type="writer"))
        assert not any(schema["function"]["name"] == "get_image" for schema in openai_schemas)
        mcp_schemas = reg.get_schemas("mcp", doc=TestingFactory.create_doc(doc_type="writer"))
        assert any(schema["name"] == "get_image" for schema in mcp_schemas)
