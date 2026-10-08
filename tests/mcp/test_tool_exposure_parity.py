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

from plugin.framework.prompts import get_specialized_domain_catalog
from plugin.framework.tool import ToolBase
from plugin.mcp.mcp_protocol import MCP_DIRECT_FLAT_EXCLUDE_TIERS


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


def _app_bases():
    import plugin.calc  # noqa: F401
    import plugin.draw  # noqa: F401
    import plugin.writer  # noqa: F401
    from plugin.calc.base import ToolCalcSpecialBase
    from plugin.draw.base import ToolDrawSpecialBase
    from plugin.writer.specialized_base import ToolWriterSpecialBase

    return {"Writer": ToolWriterSpecialBase, "Calc": ToolCalcSpecialBase, "Draw": ToolDrawSpecialBase}


def _direct_flat_tools(label):
    """``{name: specialized_domain}`` for one app's specialized tools that direct_flat lists.

    Read off the classes rather than a live registry: the registry is populated by module
    discovery at start-up, and this invariant is about what the code declares, not about what one
    session happened to load.
    """
    from plugin.framework.prompts import (
        IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS,
        WRITER_SIDEBAR_ONLY_DOMAINS,
    )

    bases = _app_bases()
    wanted = tuple(bases.values()) if label is None else (bases[label],)
    # direct_flat drops the sidebar-only flows itself (sidebar_only_tool_names): they need session
    # orchestration the direct modes do not provide, so they are absent from BOTH modes by design
    # and are not a parity gap.
    sidebar_only = WRITER_SIDEBAR_ONLY_DOMAINS | IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS
    return {cls.name: getattr(cls, "specialized_domain", None)
            for cls in _all_tool_classes()
            if issubclass(cls, wanted)
            and getattr(cls, "tier", None) not in MCP_DIRECT_FLAT_EXCLUDE_TIERS
            and getattr(cls, "specialized_domain", None) not in sidebar_only}


@pytest.mark.parametrize("label", ["Writer", "Calc", "Draw", None])
def test_every_listed_specialized_tool_has_a_catalogued_domain(label):
    listed = _direct_flat_tools(label)
    offered = {e["domain"] for e in get_specialized_domain_catalog(agent_label=label, ctx=None, for_discovery=True)}

    # Guard against the check passing because it found nothing to check.
    assert listed, "no specialized tools were found for %s" % label
    assert any(domain for domain in listed.values()), (
        "no listed tool reported a specialized_domain for %s — the domain lookup is broken, not "
        "the catalog" % label)

    stranded = sorted(name for name, domain in listed.items() if domain and domain not in offered)

    assert not stranded, (
        "%s: direct_flat lists these tools but no discovery domain mentions them, so a "
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
    "value, value_2, value_3, value_4",
    [
        pytest.param(True, "extract_structure_from_image", "vision", "extract_structure_from_image", id="test_a_configured_domain_stays_listed"),
        # Core tools have no specialized domain and must pass through whatever the gate says.
        pytest.param(False, "get_document_content", None, "get_document_content", id="test_tools_without_a_domain_are_never_dropped"),
    ],
)
def test_a_configured_domain_stays_listed(monkeypatch, value, value_2, value_3, value_4):
    from plugin.mcp.mcp_protocol import drop_unavailable_domains

    monkeypatch.setattr("plugin.vision.vision_availability.vision_venv_configured",
                        lambda _ctx: value)
    registry = _FakeRegistry({value_2: value_3})
    schemas = [{"name": value_4}]

    assert drop_unavailable_domains(schemas, registry, object()) == schemas

def test_no_context_means_no_filtering():
    """tools/list before a document resolves has no ctx to ask; advertising is the safe default."""
    from plugin.mcp.mcp_protocol import drop_unavailable_domains

    registry = _FakeRegistry({"extract_structure_from_image": "vision"})
    schemas = [{"name": "extract_structure_from_image"}]

    assert drop_unavailable_domains(schemas, registry, None) == schemas
