# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for shared vision helper utilities."""

from __future__ import annotations

from plugin.scripting.vision_common import (
    css_inline_unavailable_result,
    is_css_inline_import_error,
    resolve_vision_insert_mode,
)


def test_resolve_vision_insert_mode_template_overrides_config():
    from unittest.mock import patch

    ctx = object()
    assert resolve_vision_insert_mode(ctx, {"insert_mode": "structured"}) == "structured"


def test_resolve_vision_insert_mode_reads_config():
    from unittest.mock import patch

    ctx = object()
    with patch("plugin.framework.config.get_config", return_value="structured"):
        assert resolve_vision_insert_mode(ctx, None) == "structured"


def test_is_css_inline_import_error():
    exc = ImportError("No module named 'css_inline'")
    assert is_css_inline_import_error(exc) is True
    assert is_css_inline_import_error(ImportError("docling missing")) is False


def test_css_inline_unavailable_result():
    result = css_inline_unavailable_result("extract_text")
    assert result["status"] == "error"
    assert result["code"] == "CSS_INLINE_UNAVAILABLE"
    assert result["helper"] == "extract_text"
    assert "css-inline" in result["message"]
