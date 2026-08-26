# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / CrossHair verification for appearance, event_bus, and config_service."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.framework.appearance import (
    _luminance,
    get_monaco_theme_info,
)
from plugin.framework.event_bus import EventBus
from plugin.framework.config_service import ConfigService, ConfigAccessError


@given(color=st.integers(min_value=0, max_value=0xFFFFFF))
@settings(max_examples=100)
def test_luminance_contracts(color: int) -> None:
    lum = _luminance(color)
    assert isinstance(lum, float)
    assert 0.0 <= lum <= 255.0


def test_luminance_pre_rejects_unbounded_and_bool() -> None:
    import deal
    from tests.strip_bundle import deal_pre_present

    if not deal_pre_present(_luminance):
        pytest.skip("@deal.pre stripped in release bundle")
    with pytest.raises(deal.PreContractError):
        _luminance(0x1000000)
    with pytest.raises(deal.PreContractError):
        _luminance(True)  # type: ignore[arg-type]
    assert 0.0 <= _luminance(0xFFFFFF) <= 255.0
    assert _luminance(0) == 0.0


def test_monaco_theme_info_structure() -> None:
    info = get_monaco_theme_info()
    assert isinstance(info, dict)
    assert info.get("monaco") in ("vs", "vs-dark")
    assert isinstance(info.get("is_dark"), bool)
    assert isinstance(info.get("bg"), int)


def test_event_bus_exception_isolation() -> None:
    bus = EventBus()
    called = []

    def bad_handler():
        called.append("bad")
        raise RuntimeError("Subscriber crash should be swallowed by bus")

    def good_handler():
        called.append("good")

    bus.subscribe("test:event", bad_handler)
    bus.subscribe("test:event", good_handler)

    # emit must NOT raise
    bus.emit("test:event")
    assert called == ["bad", "good"]


def test_config_service_access_control_contracts() -> None:
    service = ConfigService()
    service.set_manifest({
        "mod_a": {
            "config": {
                "secret": {"default": "a", "public": False},
                "shared": {"default": "b", "public": True},
            }
        }
    })

    # Same module reading private config is allowed
    service._check_read_access("mod_a.secret", "mod_a")

    # Other module reading public config is allowed
    service._check_read_access("mod_a.shared", "mod_b")

    # Other module reading private config raises ConfigAccessError
    with pytest.raises(ConfigAccessError):
        service._check_read_access("mod_a.secret", "mod_b")

    # Write access across module boundaries raises ConfigAccessError
    with pytest.raises(ConfigAccessError):
        service._check_write_access("mod_a.secret", "mod_b")
