import pytest
from unittest.mock import MagicMock, patch

from plugin.calc.python.function import (
    _spill_registry_doc_key,
    load_spill_registry_for_doc,
    save_spill_registry_for_doc,
    SPILL_REGISTRY,
    LOADED_DOCUMENTS,
    _SPILL_REGISTRY_LOCK
)

def test_spill_registry_doc_key():
    mock_doc = MagicMock()
    mock_doc.getPropertyValue.return_value = "uid-123"
    assert _spill_registry_doc_key(mock_doc) == "uid-123"

def test_spill_save_as_lifecycle_consistency():
    # If a document changes URL (e.g., Save As), its spill registry should still be
    # correctly accessed because it uses the lifecycle key (RuntimeUID).

    mock_doc = MagicMock()
    mock_doc.getPropertyValue.return_value = "uid-123"
    mock_doc.getURL.return_value = "file:///tmp/old.ods"

    SPILL_REGISTRY.clear()
    LOADED_DOCUMENTS.clear()

    doc_key = _spill_registry_doc_key(mock_doc)
    SPILL_REGISTRY[(doc_key, "Sheet1", 0, 0)] = [(1, 0), (2, 0)]
    LOADED_DOCUMENTS.add(doc_key)

    # Save As simulates URL change, but RuntimeUID remains.
    mock_doc.getURL.return_value = "file:///tmp/new.ods"

    # Check if the registry still works with the new URL / same doc
    new_doc_key = _spill_registry_doc_key(mock_doc)
    assert new_doc_key == doc_key
    assert (new_doc_key, "Sheet1", 0, 0) in SPILL_REGISTRY

def test_spill_registry_migration():
    mock_doc = MagicMock()
    mock_doc.getPropertyValue.return_value = "uid-123"
    # Important: we must bind getURL to a real function returning a string,
    # otherwise MagicMock's method is returned by getattr without () evaluation.
    mock_doc.getURL = lambda: "file:///tmp/old.ods"

    SPILL_REGISTRY.clear()
    LOADED_DOCUMENTS.clear()

    # Simulate old state where keys were stored under URL
    SPILL_REGISTRY[("file:///tmp/old.ods", "Sheet1", 0, 0)] = [(1, 0), (2, 0)]
    LOADED_DOCUMENTS.add("file:///tmp/old.ods")

    # When _spill_registry_doc_key is called, it should migrate the keys
    # to the new identity (uid-123)
    doc_key = _spill_registry_doc_key(mock_doc)
    assert doc_key == "uid-123"

    assert ("uid-123", "Sheet1", 0, 0) in SPILL_REGISTRY
    assert ("file:///tmp/old.ods", "Sheet1", 0, 0) not in SPILL_REGISTRY
    assert "uid-123" in LOADED_DOCUMENTS
    assert "file:///tmp/old.ods" not in LOADED_DOCUMENTS
