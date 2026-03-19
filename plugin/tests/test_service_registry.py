"""Tests for plugin.framework.service_registry."""

import pytest

from plugin.framework.service_registry import ServiceRegistry
from plugin.framework.service_base import ServiceBase


class DummyService(ServiceBase):
    name = "dummy"


class AnotherService(ServiceBase):
    name = "another"


class TestRegister:
    def test_register_and_get(self):
        reg = ServiceRegistry()
        svc = DummyService()
        reg.register("dummy", svc)
        assert reg.get("dummy") is svc

    def test_register_duplicate_raises(self):
        reg = ServiceRegistry()
        reg.register("dummy", DummyService())
        with pytest.raises(ValueError, match="already registered"):
            reg.register("dummy", DummyService())

    def test_register(self):
        reg = ServiceRegistry()
        obj = {"hello": "world"}
        reg.register("myobj", obj)
        assert reg.get("myobj") is obj

class TestAccess:
    def test_getattr(self):
        reg = ServiceRegistry()
        svc = DummyService()
        reg.register("dummy", svc)
        assert reg.dummy is svc

    def test_getattr_missing_raises(self):
        reg = ServiceRegistry()
        with pytest.raises(AttributeError, match="No service registered"):
            _ = reg.nonexistent

    def test_contains(self):
        reg = ServiceRegistry()
        reg.register("dummy", DummyService())
        assert "dummy" in reg
        assert "missing" not in reg

    def test_get_returns_none_for_missing(self):
        reg = ServiceRegistry()
        assert reg.get("nope") is None

    def test_service_names(self):
        reg = ServiceRegistry()
        reg.register("dummy", DummyService())
        reg.register("another", AnotherService())
        assert set(reg.service_names) == {"dummy", "another"}


class TestLifecycle:
    def test_initialize_all(self):
        reg = ServiceRegistry()
        initialized = []

        class InitService(ServiceBase):
            name = "init_svc"
            def initialize(self, ctx):
                initialized.append(ctx)

        reg.register("init_svc", InitService())
        reg.initialize_all("fake_ctx")
        assert initialized == ["fake_ctx"]

    def test_shutdown_all_swallows_errors(self):
        reg = ServiceRegistry()

        class BadShutdown(ServiceBase):
            name = "bad"
            def shutdown(self):
                raise RuntimeError("boom")

        reg.register("bad", BadShutdown())
        reg.shutdown_all()  # should not raise
