# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Unit tests for serialization A/B support helpers."""

from __future__ import annotations

from tests.scripting.serialization_ab_support import (
    _AB_HYPOTHESIS_EXTENSIVE,
    _AB_HYPOTHESIS_LIGHT,
    ab_hypothesis_max_examples,
    serialization_extensive,
)
from tests.vhs_budget import _SERIALIZATION_EXTENSIVE_ENV


def test_serialization_extensive_default_false(monkeypatch) -> None:
    monkeypatch.delenv(_SERIALIZATION_EXTENSIVE_ENV, raising=False)
    assert serialization_extensive() is False
    assert ab_hypothesis_max_examples() == _AB_HYPOTHESIS_LIGHT


def test_serialization_extensive_enabled(monkeypatch) -> None:
    monkeypatch.setenv(_SERIALIZATION_EXTENSIVE_ENV, "1")
    assert serialization_extensive() is True
    assert ab_hypothesis_max_examples() == _AB_HYPOTHESIS_EXTENSIVE


def test_serialization_extensive_truthy_strings(monkeypatch) -> None:
    for value in ("true", "TRUE", "yes", "Yes"):
        monkeypatch.setenv(_SERIALIZATION_EXTENSIVE_ENV, value)
        assert serialization_extensive() is True
