# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.app state accessors."""
from __future__ import annotations

import pytest

import plugin.app as app


def test_services_or_none_does_not_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """services_or_none returns None or current registry without starting bootstrap."""
    def _fail_bootstrap(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("bootstrap must not be called by services_or_none")

    monkeypatch.setattr(app, "_services", None)
    monkeypatch.setattr(app, "bootstrap", _fail_bootstrap)

    assert app.services_or_none() is None

    sentinel = object()
    monkeypatch.setattr(app, "_services", sentinel)
    assert app.services_or_none() is sentinel
