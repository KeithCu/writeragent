# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep Linguistic grammar registry XML aligned with ``GRAMMAR_REGISTRY_LOCALE_TAGS``."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET

from plugin.framework.grammar_rule_ids import GRAMMAR_REGISTRY_LOCALE_TAGS

_OOR = "http://openoffice.org/2001/registry"
_IMPLEMENTATION = "org.extension.writeragent.comp.pyuno.AiGrammarProofreader"


def _n(local: str) -> str:
    return f"{{{_OOR}}}{local}"


def _local_tag(elem: ET.Element) -> str:
    t = elem.tag
    if t.startswith("{"):
        return t.rsplit("}", 1)[-1]
    return t


def _oor_name(elem: ET.Element) -> str | None:
    return elem.get(_n("name"))


def _repo_root() -> str:
    _resolved = os.path.abspath(os.path.dirname(__file__))
    if "plugin" in _resolved.split(os.sep):
        return os.path.abspath(os.path.join(_resolved, "..", "..", "..", ".."))
    return os.path.abspath(os.path.join(_resolved, "..", "..", ".."))


def _child_node(parent: ET.Element, name: str) -> ET.Element:
    for c in parent:
        if _local_tag(c) == "node" and _oor_name(c) == name:
            return c
    raise AssertionError(f"missing <node oor:name={name!r}>")


def test_linguistic_writer_agent_grammar_xcu_locales_match_registry() -> None:
    path = os.path.join(
        _repo_root(),
        "extension",
        "registry",
        "org",
        "openoffice",
        "Office",
        "LinguisticWriterAgentGrammar.xcu",
    )
    root = ET.parse(path).getroot()
    assert root.tag == _n("component-data")
    assert _oor_name(root) == "Linguistic"
    sm = _child_node(root, "ServiceManager")
    gc = _child_node(sm, "GrammarCheckers")
    impl = _child_node(gc, _IMPLEMENTATION)
    locales_prop = None
    for c in impl:
        if _local_tag(c) == "prop" and _oor_name(c) == "Locales":
            locales_prop = c
            break
    assert locales_prop is not None
    val_el = None
    for c in locales_prop:
        if _local_tag(c) == "value":
            val_el = c
            break
    assert val_el is not None and val_el.text is not None
    tags = tuple(val_el.text.split())
    assert tags == GRAMMAR_REGISTRY_LOCALE_TAGS
