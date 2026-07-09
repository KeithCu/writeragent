# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted_action_registry wiring."""

from __future__ import annotations

from plugin.scripting.trusted_action_registry import get_trusted_action_wiring


def test_get_trusted_action_wiring_known_domains() -> None:
    analysis = get_trusted_action_wiring("analysis")
    assert analysis is not None
    assert analysis.handler.endswith("trusted_dispatch:dispatch_trusted")

    embeddings = get_trusted_action_wiring("embeddings_index")
    assert embeddings is not None
    assert embeddings.supports_heartbeat is True


def test_get_trusted_action_wiring_unknown_domain() -> None:
    assert get_trusted_action_wiring("not_a_domain") is None
