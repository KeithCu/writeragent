# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted_action_registry wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.trusted_action_registry import TrustedActionWiring, get_trusted_action_wiring


def test_get_trusted_action_wiring_known_domains() -> None:
    analysis = get_trusted_action_wiring("analysis")
    assert analysis is not None
    assert analysis.handler.endswith("trusted_dispatch:dispatch_analysis")

    embeddings = get_trusted_action_wiring("embeddings_index")
    assert embeddings is not None
    assert embeddings.supports_heartbeat is True

    languagetool = get_trusted_action_wiring("languagetool")
    assert languagetool is not None
    assert languagetool.handler.endswith("trusted_dispatch:dispatch_languagetool")


def test_all_trusted_action_wirings_resolve_and_match_heartbeat() -> None:
    import importlib
    import inspect
    from plugin.scripting.trusted_action_registry import _TRUSTED_ACTION_WIRING

    for wiring in _TRUSTED_ACTION_WIRING:
        mod_name, attr_name = wiring.handler.rsplit(":", 1)
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, attr_name)
        assert callable(fn), f"{wiring.handler} is not callable"
        sig = inspect.signature(fn)
        has_heartbeat_param = "heartbeat_fn" in sig.parameters
        assert has_heartbeat_param == wiring.supports_heartbeat, (
            f"{wiring.domain} ({wiring.handler}) supports_heartbeat={wiring.supports_heartbeat} "
            f"but heartbeat_fn in signature={has_heartbeat_param}"
        )


def test_get_trusted_action_wiring_unknown_domain() -> None:
    assert get_trusted_action_wiring("not_a_domain") is None


def test_dispatch_rejects_handler_without_colon() -> None:
    wiring = TrustedActionWiring("t", "plugin.scripting.venv.trusted_dispatch")
    with pytest.raises(ValueError, match="module:attr"):
        wiring.dispatch({})


def test_dispatch_passes_heartbeat_only_when_supported() -> None:
    def no_heartbeat(data: dict[str, object]) -> str:
        return f"no:{data['k']}"

    def with_heartbeat(data: dict[str, object], *, heartbeat_fn: object = None) -> str:
        return f"yes:{heartbeat_fn is sentinel}"

    sentinel = object()
    mod = MagicMock()
    mod.no_heartbeat = no_heartbeat
    mod.with_heartbeat = with_heartbeat
    plain = TrustedActionWiring("t", "pkg:no_heartbeat", supports_heartbeat=False)
    beating = TrustedActionWiring("t", "pkg:with_heartbeat", supports_heartbeat=True)
    with patch("importlib.import_module", return_value=mod):
        assert plain.dispatch({"k": 1}, heartbeat_fn=sentinel) == "no:1"
        assert beating.dispatch({"k": 1}, heartbeat_fn=sentinel) == "yes:True"
