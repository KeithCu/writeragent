# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
import pytest


@pytest.fixture
def review_registry(monkeypatch):
    """A fresh, empty review-view registry for one test; returns the live dict production code reads."""
    from plugin.writer import review_view_attachment as rva
    registry = {}
    monkeypatch.setattr(rva, "_attachments", registry)
    monkeypatch.setattr(rva, "_doc_listener", None)
    monkeypatch.setattr(rva, "_pending_refresh", set())
    return registry
