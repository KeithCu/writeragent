# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep Linguistic grammar registry XML aligned with ``GRAMMAR_REGISTRY_LOCALE_TAGS``."""

from __future__ import annotations

import os
import ast
import xml.etree.ElementTree as ET

from plugin.modules.writer import grammar_proofread_engine as eng

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
    # plugin/tests/<this_file> -> writeragent repo root
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _child_node(parent: ET.Element, name: str) -> ET.Element:
    for c in parent:
        if _local_tag(c) == "node" and _oor_name(c) == name:
            return c
    raise AssertionError(f"missing <node oor:name={name!r}>")


def test_linguistic_writer_agent_grammar_xcu_locales_match_engine() -> None:
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
    assert tags == eng.GRAMMAR_REGISTRY_LOCALE_TAGS


def test_linguistic_writer_agent_grammar_xcu_is_minimal() -> None:
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
    sm = _child_node(root, "ServiceManager")
    assert [_oor_name(c) for c in sm if _local_tag(c) == "node"] == ["GrammarCheckers"]


def test_generated_manifest_omits_linguistic_grammar_xcu_by_default() -> None:
    """Default builds keep the native Linguistic XCU out to avoid LO startup/sidebar crashes."""
    mf = os.path.join(_repo_root(), "extension", "META-INF", "manifest.xml")
    with open(mf, encoding="utf-8") as f:
        body = f.read()
    assert "registry/org/openoffice/Office/LinguisticWriterAgentGrammar.xcu" not in body


def test_ai_grammar_uno_component_has_lightweight_top_level_imports() -> None:
    """Linguistic enumeration imports this component before real proofreading starts."""
    path = os.path.join(
        _repo_root(),
        "plugin",
        "modules",
        "writer",
        "ai_grammar_proofreader.py",
    )
    with open(path, encoding="utf-8") as f:
        module = ast.parse(f.read(), filename=path)
    top_level_from_imports = [
        node.module
        for node in module.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    ]
    assert "plugin.framework.config" not in top_level_from_imports
    assert "plugin.framework.logging" not in top_level_from_imports
    assert "plugin.framework.worker_pool" not in top_level_from_imports


def test_ai_grammar_stub_uses_same_implementation_id() -> None:
    real_path = os.path.join(
        _repo_root(),
        "plugin",
        "modules",
        "writer",
        "ai_grammar_proofreader.py",
    )
    stub_path = os.path.join(
        _repo_root(),
        "plugin",
        "modules",
        "writer",
        "ai_grammar_proofreader_stub.py",
    )
    with open(real_path, encoding="utf-8") as f:
        real = ast.parse(f.read(), filename=real_path)
    with open(stub_path, encoding="utf-8") as f:
        stub = ast.parse(f.read(), filename=stub_path)

    def constant_value(module: ast.Module, name: str) -> str:
        for node in module.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        assert isinstance(node.value, ast.Constant)
                        assert isinstance(node.value.value, str)
                        return node.value.value
        raise AssertionError(f"missing constant {name}")

    assert constant_value(stub, "IMPLEMENTATION_NAME") == constant_value(real, "IMPLEMENTATION_NAME")
    assert constant_value(stub, "SERVICE_NAME") == constant_value(real, "SERVICE_NAME")
