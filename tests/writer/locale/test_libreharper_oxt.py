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


def test_libreharper_bundle_covers_top_level_plugin_imports() -> None:
    """Every ``plugin.*`` a shipped module imports at load time must also ship.

    ``unopkg add`` executes ``harper_proofreader.py``, so a module missing from
    the allowlist fails installation rather than degrading at runtime. Two were
    missing this way: ``config.py`` reaches ``config_schema`` via
    ``from plugin.framework import config_schema`` and ``client/requests.py``
    reaches ``client/errors.py`` via ``from .errors import ...`` — neither is a
    plain ``import plugin.x.y``, so both need submodule-aware resolution.
    """
    from pathlib import Path

    from scripts.libreharper_bundle_paths import collect_libreharper_plugin_paths
    from tests.scripts.test_librepy_import_graph import (
        _is_shipped_plugin_module,
        _module_level_import_nodes,
        _plugin_mod_to_candidates,
        _resolved_import_from_module,
    )

    root = Path(_repo_root())
    shipped = set(collect_libreharper_plugin_paths(str(root)))
    missing: list[str] = []

    for rel in sorted(shipped):
        if not rel.endswith(".py"):
            continue
        path = root / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in _module_level_import_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("plugin.") and not _is_shipped_plugin_module(alias.name, shipped):
                        missing.append(f"{rel} -> import {alias.name}")
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            mod = _resolved_import_from_module(path, node)
            if not mod or not (mod == "plugin" or mod.startswith("plugin.")):
                continue
            if not _is_shipped_plugin_module(mod, shipped):
                missing.append(f"{rel} -> from {mod}")
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                sub = f"{mod}.{alias.name}"
                if not any((root / cand).is_file() for cand in _plugin_mod_to_candidates(sub)):
                    continue  # an attribute, not a submodule
                if not _is_shipped_plugin_module(sub, shipped):
                    missing.append(f"{rel} -> from {mod} import {alias.name}")

    assert missing == []


def test_collect_libreharper_plugin_paths() -> None:
    from scripts.libreharper_bundle_paths import collect_libreharper_plugin_paths

    paths = collect_libreharper_plugin_paths(_repo_root())
    assert "plugin/framework/config_schema.py" in paths
    assert "plugin/framework/client/errors.py" in paths
    assert "plugin/writer/locale/harper.py" in paths
    assert not any(p.endswith("harper_host.py") for p in paths)
    assert "plugin/doc/udprops.py" in paths
    assert "plugin/writer/locale/grammar_worker.py" in paths
    assert not any("llm_client" in p for p in paths)
    # constants and other framework modules import deal via deal_shim
    assert "plugin/framework/deal_shim.py" in paths
    # Weekly update check (msgbox + HTTP); lazy-imported off the grammar hot path.
    assert "plugin/chatbot/extension_update_check.py" in paths
    assert "plugin/chatbot/dialogs.py" in paths
    assert "plugin/framework/client/requests.py" in paths

