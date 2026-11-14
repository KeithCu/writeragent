# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared fixtures for chatbot tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from plugin.framework.tool import ToolBase


class _ApplyDocumentContentStub(ToolBase):
    name = "apply_document_content"
    description = "stub"
    tier = "core"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self):
        super().__init__()
        self.execute_safe = MagicMock(return_value={"status": "ok", "message": "applied"})

    def execute(self, ctx, **kwargs):
        return self.execute_safe(ctx, **kwargs)


@pytest.fixture
def apply_document_content_stub():
    return _ApplyDocumentContentStub()
