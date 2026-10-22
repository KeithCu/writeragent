# WriterAgent - AI Writing Assistant for LibreOffice
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for DelegateToSpecializedBase (domain validation and mutation lock)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from plugin.framework.tool import ToolContext
from plugin.mcp.mcp_protocol import _tool_needs_document_mutation_gate
from plugin.writer.specialized_base import DelegateToSpecializedWriter


@pytest.fixture
def writer_gateway() -> DelegateToSpecializedWriter:
    return DelegateToSpecializedWriter()


@pytest.fixture
def mock_ctx() -> ToolContext:
    ctx = MagicMock(spec=ToolContext)
    ctx.ctx = MagicMock()
    ctx.services = MagicMock()
    ctx.doc = MagicMock()
    return ctx


def test_requires_document_lock_non_str_domain(writer_gateway: DelegateToSpecializedWriter) -> None:
    """Non-string domain values must safely default to requiring document lock without TypeError."""
    # Dict domain (unhashable)
    assert writer_gateway.requires_document_lock({"domain": {"nested": "value"}}) is True

    # List domain (unhashable)
    assert writer_gateway.requires_document_lock({"domain": ["tables", "shapes"]}) is True

    # Int domain
    assert writer_gateway.requires_document_lock({"domain": 42}) is True

    # None domain
    assert writer_gateway.requires_document_lock({"domain": None}) is True

    # JSON string containing a dict domain
    assert writer_gateway.requires_document_lock('{"domain": {"nested": "value"}}') is True

    # Empty / None arguments
    assert writer_gateway.requires_document_lock(None) is True
    assert writer_gateway.requires_document_lock({}) is True


def test_requires_document_lock_string_domains(writer_gateway: DelegateToSpecializedWriter) -> None:
    """Read-only string domains skip lock; mutating string domains and vision require lock."""
    # Read-only domains do not require document lock
    assert writer_gateway.requires_document_lock({"domain": "document_research"}) is False
    assert writer_gateway.requires_document_lock({"domain": "web_research"}) is False

    # Vision requires lock even though listed in _READ_ONLY_DOMAINS
    assert writer_gateway.requires_document_lock({"domain": "vision"}) is True

    # Mutating domains require lock
    assert writer_gateway.requires_document_lock({"domain": "tables"}) is True
    assert writer_gateway.requires_document_lock({"domain": "shapes"}) is True


def test_mcp_mutation_gate_non_str_domain(writer_gateway: DelegateToSpecializedWriter) -> None:
    """_tool_needs_document_mutation_gate must not crash on unhashable dict domain arguments."""
    needs_gate = _tool_needs_document_mutation_gate(writer_gateway, {"domain": {"type": "object"}})
    assert needs_gate is True


def test_execute_non_str_domain_returns_error(
    writer_gateway: DelegateToSpecializedWriter, mock_ctx: ToolContext
) -> None:
    """Executing with a non-string domain must return the invalid-domain error instead of raising TypeError."""
    result = writer_gateway.execute(mock_ctx, domain={"foo": "bar"}, task="analyze")
    assert result["status"] == "error"
    assert "No specialized tools found for domain" in result["message"]
    assert "foo" in result["message"]

    list_result = writer_gateway.execute(mock_ctx, domain=["invalid"], task="analyze")
    assert list_result["status"] == "error"
    assert "No specialized tools found for domain" in list_result["message"]
