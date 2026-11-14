# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""direct_flat and direct_discovery must expose the same capabilities.

The two MCP exposure modes reach the specialized tools by different routes: direct_flat lists
them straight from the registry, direct_discovery advertises a domain catalog that find_tools
expands. Nothing kept the two in agreement, and they drifted — extract_structure_from_image was
listed and callable in direct_flat while no catalog domain mentioned it, so a model driving
direct_discovery could not know it existed.

The invariant tested here: every tool direct_flat lists belongs to a domain the discovery catalog
offers. Not "every declared domain is catalogued" — some are hidden from both on purpose — but
that the two modes hide the same things.
"""
import pytest

from plugin.doc.specialized_catalog import get_specialized_domain_catalog
from plugin.framework.tool import ToolBase
from plugin.mcp.mcp_protocol import MCP_DIRECT_FLAT_EXCLUDE_TIERS


@pytest.fixture(autouse=True)
def _register_prompt_catalog_for_parity_tests() -> None:
    from tests.tool_registry_filters import register_prompt_catalog_for_tests

    register_prompt_catalog_for_tests()


def _all_tool_classes():
    """Every concrete tool class, however deep the base hierarchy goes."""
    seen, stack, out = set(), list(ToolBase.__subclasses__()), []
    while stack:
        cls = stack.pop()
        if id(cls) in seen:
            continue
        seen.add(id(cls))
        stack.extend(cls.__subclasses__())
        # An abstract base declares no name of its own; only registered tools have one.
        if not (isinstance(getattr(cls, "name", None), str) and cls.name):
            continue
        # Tool classes defined by other test modules are in memory too once the whole suite has
        # imported them. This is an invariant about the shipped tools, not about fixtures.
        if str(getattr(cls, "__module__", "")).startswith(("tests.", "test_")):
            continue
        out.append(cls)
    return out


def _subclasses_in_tests(base):
    """Direct subclasses of *base* defined in test modules (what the catalog walk sees)."""
    return [c for c in base.__subclasses__() if str(getattr(c, "__module__", "")).startswith(("tests.", "test_"))]


def _app_bases():
    import plugin.calc  # noqa: F401
    import plugin.draw  # noqa: F401
    import plugin.writer  # noqa: F401
    from plugin.calc.base import ToolCalcSpecialBase
    from plugin.draw.base import ToolDrawSpecialBase
    from plugin.writer.specialized_base import ToolWriterSpecialBase

    return {"Writer": ToolWriterSpecialBase, "Calc": ToolCalcSpecialBase, "Draw": ToolDrawSpecialBase}


def _sidebar_only_domains():
    from plugin.framework.prompts import (
        IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS,
        WRITER_SIDEBAR_ONLY_DOMAINS,
    )

    # Sidebar-only flows need session orchestration the direct modes do not provide,
    # so they are absent from BOTH modes by design and are not a parity gap.
    return WRITER_SIDEBAR_ONLY_DOMAINS | IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS


def _direct_flat_tools(label):
    """``{name: specialized_domain}`` for one app's specialized tools that direct_flat lists.

    Read off the classes rather than a live registry: the registry is populated by module
    discovery at start-up, and this invariant is about what the code declares, not about what one
    session happened to load.
    """
    bases = _app_bases()
    wanted = tuple(bases.values()) if label is None else (bases[label],)
    sidebar_only = _sidebar_only_domains()
    return {cls.name: getattr(cls, "specialized_domain", None)
            for cls in _all_tool_classes()
            if issubclass(cls, wanted)
            and getattr(cls, "tier", None) not in MCP_DIRECT_FLAT_EXCLUDE_TIERS
            and getattr(cls, "specialized_domain", None) not in sidebar_only}


def _concrete_tools(label):
    """``{name: specialized_domain}`` for all non-dummy concrete tools of one app."""
    from plugin.framework.tool import ToolBaseDummy

    base = _app_bases()[label]
    sidebar_only = _sidebar_only_domains()
    return {
        t.name: getattr(t, "specialized_domain", None)
        for t in _all_tool_classes()
        if issubclass(t, base)
        and not issubclass(t, ToolBaseDummy)
        and getattr(t, "specialized_domain", None) not in sidebar_only
    }


@pytest.mark.parametrize(
    "label, lister, needs_init",
    [
        pytest.param("Writer", _direct_flat_tools, False, id="Writer-direct_flat"),
        pytest.param("Calc", _direct_flat_tools, False, id="Calc-direct_flat"),
        pytest.param("Draw", _direct_flat_tools, False, id="Draw-direct_flat"),
        pytest.param(None, _direct_flat_tools, False, id="all-direct_flat"),
        pytest.param("Writer", _concrete_tools, True, id="Writer-concrete"),
        pytest.param("Calc", _concrete_tools, True, id="Calc-concrete"),
        pytest.param("Draw", _concrete_tools, True, id="Draw-concrete"),
    ],
)
def test_every_listed_specialized_tool_has_a_catalogued_domain(label, lister, needs_init, request):
    if needs_init:
        request.getfixturevalue("_initialized_app_modules")
    listed = lister(label)

    offered = {e["domain"] for e in get_specialized_domain_catalog(agent_label=label, ctx=None, for_discovery=True)}

    # Guard against the check passing because it found nothing to check.
    assert listed, "no specialized tools were found for %s" % label
    assert any(domain for domain in listed.values()), (
        "no listed tool reported a specialized_domain for %s — the domain lookup is broken, not "
        "the catalog" % label)

    stranded = sorted(name for name, domain in listed.items() if domain and domain not in offered)

    assert not stranded, (
        "%s: lists these tools but no discovery domain mentions them, so a "
        "direct_discovery client cannot find them: %s" % (label, stranded))


@pytest.mark.parametrize("label", ["Writer", "Calc"])
def test_parity_holds_when_a_domains_backend_is_not_configured(label, monkeypatch):
    """The drift that prompted this file. The catalog drops the vision domain when its venv does
    not resolve — most installs — while the MCP tool list advertised extract_structure_from_image
    anyway. Whatever the availability rule is, one exposure mode must not apply it alone."""
    import plugin.framework.prompts as prompts
    from plugin.mcp.mcp_protocol import drop_unavailable_domains

    ctx = object()  # any non-None ctx makes the catalog consult the availability gate
    monkeypatch.setattr("plugin.vision.vision_availability.vision_venv_configured",
                        lambda _ctx: False)

    offered = {e["domain"] for e in prompts.get_specialized_domain_catalog(agent_label=label, ctx=ctx, for_discovery=True)}

    # Run the app's specialized tools through the same filter the MCP tool list applies, so the
    # check exercises the production path rather than restating the rule.
    registry = _FakeRegistry(_direct_flat_tools(label))
    schemas = [{"name": name} for name in registry.domains]
    kept = drop_unavailable_domains(schemas, registry, ctx)
    listed = {s["name"]: registry.domains[s["name"]] for s in kept}

    stranded = sorted(name for name, domain in listed.items() if domain and domain not in offered)

    assert not stranded, (
        "%s: with the vision venv unconfigured, direct_flat still lists %s while the discovery "
        "catalog hides the domain" % (label, stranded))


class _FakeRegistry:
    """Answers ``get(name)`` with an object carrying just the specialized_domain."""

    def __init__(self, domains):
        self.domains = domains

    def get(self, name):
        return type("_Tool", (), {"specialized_domain": self.domains.get(name)})


def test_an_unavailable_domains_tools_are_dropped_from_the_list(monkeypatch):
    """The concrete bug: extract_structure_from_image stayed in tools/list on an install whose
    vision venv does not resolve, while the discovery catalog hid the domain."""
    from plugin.mcp.mcp_protocol import drop_unavailable_domains

    monkeypatch.setattr("plugin.vision.vision_availability.vision_venv_configured",
                        lambda _ctx: False)
    registry = _FakeRegistry({"extract_structure_from_image": "vision", "style_list": "styles"})
    schemas = [{"name": "extract_structure_from_image"}, {"name": "style_list"}]

    kept = drop_unavailable_domains(schemas, registry, object())

    assert [s["name"] for s in kept] == ["style_list"]


@pytest.mark.parametrize(
    "venv_configured, tool_name, domain",
    [
        pytest.param(True, "extract_structure_from_image", "vision", id="configured_domain_stays_listed"),
        # Core tools have no specialized domain and must pass through whatever the gate says.
        pytest.param(False, "get_document_content", None, id="tools_without_domain_never_dropped"),
    ],
)
def test_a_configured_domain_stays_listed(monkeypatch, venv_configured, tool_name, domain):
    from plugin.mcp.mcp_protocol import drop_unavailable_domains

    monkeypatch.setattr("plugin.vision.vision_availability.vision_venv_configured",
                        lambda _ctx: venv_configured)
    registry = _FakeRegistry({tool_name: domain})
    schemas = [{"name": tool_name}]

    assert drop_unavailable_domains(schemas, registry, object()) == schemas

def test_no_context_means_no_filtering():
    """tools/list before a document resolves has no ctx to ask; advertising is the safe default."""
    from plugin.mcp.mcp_protocol import drop_unavailable_domains

    registry = _FakeRegistry({"extract_structure_from_image": "vision"})
    schemas = [{"name": "extract_structure_from_image"}]

    assert drop_unavailable_domains(schemas, registry, None) == schemas


def test_calc_base_does_not_import_writer_or_draw():
    import subprocess
    import sys

    cmd = [
        sys.executable,
        "-c",
        "import sys, plugin.calc.base; assert 'plugin.writer' not in sys.modules; assert 'plugin.draw' not in sys.modules",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


_MARKER_ALLOWLIST = {
    "Writer": frozenset({"document_research", "python", "vision", "web_research"}),
    "Calc": frozenset({"document_research", "web_research", "vision", "images"}),
    "Draw": frozenset({"document_research", "python", "vision", "web_research"}),
}


@pytest.fixture(scope="module")
def _initialized_app_modules():
    from unittest.mock import patch
    from plugin.calc import CalcModule
    from plugin.draw import DrawModule
    from plugin.framework.service import ServiceRegistry
    from plugin.framework.tool import ToolRegistry
    from plugin.writer import WriterModule

    services = ServiceRegistry()
    services.register("tools", ToolRegistry(services))
    with patch("plugin.framework.config._resolve_config_path_from_ctx", return_value="/tmp/writeragent.json"):
        WriterModule().initialize(services)
        CalcModule().initialize(services)
        DrawModule().initialize(services)


@pytest.mark.parametrize("label", ["Writer", "Calc", "Draw"])
def test_catalog_domains_have_tools_or_marker_allowlist(label, _initialized_app_modules):
    from plugin.framework.tool import ToolBaseDummy

    bases = _app_bases()
    all_tools = _all_tool_classes()
    cat_domains = {e["domain"] for e in get_specialized_domain_catalog(agent_label=label, ctx=None, for_discovery=True)}

    app_tools = [t for t in all_tools if issubclass(t, bases[label]) and not issubclass(t, ToolBaseDummy)]
    tool_domains = {getattr(t, "specialized_domain", None) for t in app_tools}

    # Test-module tool classes (e.g. tests/framework/test_tool.py's styles_peer_test) stay in
    # __subclasses__() until garbage collected, so the catalog can still list their domains.
    fixture_domains = {getattr(t, "specialized_domain", None) for t in _subclasses_in_tests(bases[label])}
    allowlist = _MARKER_ALLOWLIST[label]
    for domain in cat_domains - fixture_domains:
        assert domain in allowlist or domain in tool_domains, (
            f"{label}: Catalog domain {domain!r} has no concrete tools and is not in marker allowlist"
        )

