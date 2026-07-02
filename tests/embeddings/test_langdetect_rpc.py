# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.langdetect_rpc."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugin.embeddings.venv import langdetect_rpc as rpc_mod


def test_detect_lang_sample_empty() -> None:
    assert rpc_mod.detect_lang_sample("") is None
    assert rpc_mod.detect_lang_sample("   ") is None


def test_detect_lang_batch_alignment() -> None:
    with patch.object(rpc_mod, "detect_lang_sample", side_effect=["fr-FR", None, "de-DE"]):
        assert rpc_mod.detect_lang_batch(["a", "b", "c"]) == ["fr-FR", None, "de-DE"]


def test_detect_lang_sample_french(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_detect = MagicMock(return_value=[SimpleNamespace(lang="fr", prob=0.99)])
    mock_exc = type("LangDetectException", (Exception,), {})
    fake_ld = SimpleNamespace(detect_langs=mock_detect, lang_detect_exception=SimpleNamespace(LangDetectException=mock_exc))
    monkeypatch.setitem(__import__("sys").modules, "langdetect", fake_ld)
    monkeypatch.setitem(__import__("sys").modules, "langdetect.lang_detect_exception", fake_ld.lang_detect_exception)
    assert rpc_mod.detect_lang_sample("Bonjour le monde.") == "fr-FR"


def test_detect_lang_sample_import_error_message() -> None:
    with patch.dict("sys.modules", {"langdetect": None}):
        with pytest.raises(ImportError, match="langdetect is not installed"):
            rpc_mod.detect_lang_sample("hello")


def test_detect_lang_batch_none_input() -> None:
    assert rpc_mod.detect_lang_batch(None) == []  # type: ignore[arg-type]
