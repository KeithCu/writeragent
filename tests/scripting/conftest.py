"""Shared fixtures for scripting unit tests."""

from __future__ import annotations

import sys

import pytest


@pytest.fixture
def alias_import_state():
    """Save and restore sys.meta_path and writeragent.* sys.modules entries."""
    prev_meta_path = list(sys.meta_path)
    prev_wa = {
        key: mod
        for key, mod in sys.modules.items()
        if key == "writeragent" or key.startswith("writeragent.")
    }
    try:
        yield prev_wa
    finally:
        sys.meta_path[:] = prev_meta_path
        for key in list(sys.modules):
            if key == "writeragent" or key.startswith("writeragent."):
                if key in prev_wa:
                    sys.modules[key] = prev_wa[key]
                else:
                    del sys.modules[key]
