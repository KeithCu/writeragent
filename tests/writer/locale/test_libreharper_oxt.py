# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for LibreHarper OXT packaging and Linguistic XCU."""

from __future__ import annotations

import ast
import os
import xml.etree.ElementTree as ET
from types import SimpleNamespace

from plugin.writer.locale.harper_proofreader import HARPER_LOCALE_TAGS, IMPLEMENTATION_NAME, normalize_harper_locale_to_bcp47

_OOR = "http://openoffice.org/2001/registry"


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _n(local: str) -> str:
    return f"{{{_OOR}}}{local}"


def _local_tag(elem: ET.Element) -> str:
    t = elem.tag
    if t.startswith("{"):
        return t.rsplit("}", 1)[-1]
    return t


def _oor_name(elem: ET.Element) -> str | None:
    return elem.get(_n("name"))


def _child_node(parent: ET.Element, name: str) -> ET.Element:
    for c in parent:
        if _local_tag(c) == "node" and _oor_name(c) == name:
            return c
    raise AssertionError(f"missing <node oor:name={name!r}>")


def test_linguistic_libreharper_grammar_xcu_locales_match_tags() -> None:
    path = os.path.join(
        _repo_root(),
        "extension-harper",
        "registry",
        "org",
        "openoffice",
        "Office",
        "LinguisticLibreHarperGrammar.xcu",
    )
    root = ET.parse(path).getroot()
    sm = _child_node(root, "ServiceManager")
    gc = _child_node(sm, "GrammarCheckers")
    impl = _child_node(gc, IMPLEMENTATION_NAME)
    locales_prop = None
    for c in impl:
        if _local_tag(c) == "prop" and _oor_name(c) == "Locales":
            locales_prop = c
            break
    assert locales_prop is not None
    val_el = next(c for c in locales_prop if _local_tag(c) == "value")
    assert val_el.text is not None
    assert tuple(val_el.text.split()) == HARPER_LOCALE_TAGS


def test_libreharper_preserves_supported_english_dialects() -> None:
    for country, expected in (("US", "en-US"), ("GB", "en-GB"), ("AU", "en-AU"), ("CA", "en-CA"), ("IN", "en-IN")):
        locale = SimpleNamespace(Language="en", Country=country, Variant="")
        assert normalize_harper_locale_to_bcp47(locale) == expected

    assert normalize_harper_locale_to_bcp47(SimpleNamespace(Language="en-AU", Country="", Variant="")) == "en-AU"
    assert normalize_harper_locale_to_bcp47(SimpleNamespace(Language="de", Country="DE", Variant="")) is None


def test_libreharper_manifest_registers_harper_proofreader_only() -> None:
    path = os.path.join(_repo_root(), "extension-harper", "META-INF", "manifest.xml")
    body = open(path, encoding="utf-8").read()
    assert "plugin/writer/locale/harper_proofreader.py" in body
    assert "LinguisticLibreHarperGrammar.xcu" in body
    assert "ai_grammar_proofreader.py" not in body
    assert "CalcAddIns" not in body
    assert "Jobs.xcu" not in body


def test_grammar_work_queue_has_no_top_level_framework_client_package_import() -> None:
    """Harper OXT must not pull LLM/embeddings via ``from plugin.framework.client import …``."""
    path = os.path.join(_repo_root(), "plugin", "writer", "locale", "grammar_work_queue.py")
    with open(path, encoding="utf-8") as f:
        module = ast.parse(f.read(), filename=path)
    top_level = [
        node.module
        for node in module.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    ]
    assert "plugin.framework.client" not in top_level
    assert "plugin.framework.client.request_controls" not in top_level
    assert "plugin.framework.client.llm_client" not in top_level
    assert "plugin.framework.client.model_fetcher" not in top_level
    assert "plugin.writer.locale.grammar_worker_llm" not in top_level


def test_collect_libreharper_plugin_paths() -> None:
    from scripts.libreharper_bundle_paths import collect_libreharper_plugin_paths

    paths = collect_libreharper_plugin_paths(_repo_root())
    assert "plugin/writer/locale/harper_host.py" in paths
    assert "plugin/writer/locale/harper.py" in paths
    assert "plugin/doc/udprops.py" in paths
    assert not any(p.endswith("grammar_worker_llm.py") for p in paths)
    assert not any("llm_client" in p for p in paths)
