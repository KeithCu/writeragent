# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""WriterModule.initialize must register every tool class the package defines."""

import importlib
import pkgutil
from unittest.mock import MagicMock

from plugin.framework.service import ServiceRegistry, iter_named_concrete_subclasses
from plugin.framework.tool import ToolBase, ToolBaseDummy, ToolRegistry
from plugin.writer import WriterModule


def _initialized():
    services = ServiceRegistry()
    for name in ("config", "document", "events"):
        services.register(name, MagicMock())
    tools = ToolRegistry(services)
    services.register("tools", tools)
    WriterModule().initialize(services)
    return services, tools


def _writer_tool_names():
    import plugin.writer as writer_pkg

    names = set()
    for info in pkgutil.walk_packages(writer_pkg.__path__, prefix="plugin.writer."):
        module = importlib.import_module(info.name)
        for cls in iter_named_concrete_subclasses(module, ToolBase, ToolBaseDummy):
            names.add(cls.name)
    return names


def test_every_writer_tool_class_is_registered():
    _unused, tools = _initialized()
    expected, registered = _writer_tool_names(), set(tools._tools)
    assert expected == registered, (
        "Writer discovery is incomplete: missing=%s extra=%s"
        % (sorted(expected - registered), sorted(registered - expected))
    )


def test_writer_services_are_registered():
    services, _unused = _initialized()
    assert {"writer_bookmarks", "writer_tree", "writer_proximity", "writer_index"} <= set(services._services)
