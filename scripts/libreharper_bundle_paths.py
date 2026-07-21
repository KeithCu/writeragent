"""File selection for LibreHarper OXT (Harper grammar only; no chat/Calc/venv worker)."""

from __future__ import annotations

import os

# vendor/ package dirs copied into plugin/lib/ (config JSON repair).
LIBREHARPER_VENDOR_PACKAGES: frozenset[str] = frozenset({"json_repair"})

LIBREHARPER_WRITER_INIT = '''\
"""Slim writer package init for LibreHarper (no Writer tool registration)."""
'''

LIBREHARPER_DOC_INIT = '''\
"""Slim doc package init for LibreHarper (udprops only; no CommonModule tools)."""
'''

LIBREHARPER_CLIENT_INIT = '''\
"""Empty client package for LibreHarper (no LLM / embeddings package imports)."""
'''

# Explicit files for the Harper-only Linguistic2 proofreader OXT.
LIBREHARPER_PLUGIN_FILES: tuple[str, ...] = (
    "plugin/__init__.py",
    "plugin/version.py",
    "plugin/framework/__init__.py",
    "plugin/framework/config.py",
    "plugin/framework/constants.py",
    "plugin/framework/errors.py",
    "plugin/framework/json_utils.py",
    "plugin/framework/i18n.py",
    "plugin/framework/event_bus.py",
    "plugin/framework/service.py",
    "plugin/framework/url_utils.py",
    "plugin/framework/thread_guard.py",
    "plugin/framework/uno_bootstrap.py",
    "plugin/framework/logging.py",
    "plugin/framework/uno_context.py",
    "plugin/framework/worker_pool.py",
    "plugin/framework/queue_executor.py",
    "plugin/framework/uno_listeners.py",
    "plugin/doc/udprops.py",
    "plugin/scripting/__init__.py",
    "plugin/scripting/sandbox.py",
    "plugin/scripting/venv/__init__.py",
    "plugin/writer/locale/__init__.py",
    "plugin/writer/locale/ai_grammar_proofreader.py",
    "plugin/writer/locale/harper_host.py",
    "plugin/writer/locale/harper.py",
    "plugin/writer/locale/harper_binary.py",
    "plugin/writer/locale/harper_proofreader.py",
    "plugin/writer/locale/locale_abbrev.py",
    "plugin/writer/locale/grammar_work_queue.py",
    "plugin/writer/locale/grammar_proofread_cache.py",
    "plugin/writer/locale/grammar_proofread_locale.py",
    "plugin/writer/locale/grammar_proofread_text.py",
    "plugin/writer/locale/grammar_proofread_json.py",
    "plugin/writer/locale/grammar_persistence.py",
    "plugin/writer/locale/grammar_ignore_rules.py",
    "plugin/writer/locale/grammar_obs.py",
    "plugin/writer/locale/grammar_worker_phases.py",
    "plugin/contrib/__init__.py",
    "plugin/contrib/lsp/__init__.py",
    "plugin/contrib/lsp/json_rpc_framing.py",
    "plugin/contrib/lsp/position_codec.py",
    "plugin/contrib/lsp/README.md",
    "plugin/contrib/pooch/__init__.py",
    "plugin/contrib/pooch/core.py",
    "plugin/contrib/pooch/downloaders.py",
    "plugin/contrib/pooch/hashes.py",
    "plugin/contrib/pooch/processors.py",
    "plugin/contrib/pooch/utils.py",
    "plugin/contrib/pooch/README.md",
)


def _norm(path: str) -> str:
    return path.replace(os.sep, "/")


def collect_libreharper_plugin_paths(base_dir: str) -> list[str]:
    """Return project-relative plugin paths to copy into the LibreHarper bundle."""
    found: set[str] = set()
    for rel in LIBREHARPER_PLUGIN_FILES:
        full = os.path.join(base_dir, rel)
        if not os.path.isfile(full):
            raise FileNotFoundError("LibreHarper bundle missing required plugin file: %s" % rel)
        found.add(_norm(rel))
    return sorted(found)


def iter_libreharper_vendor_packages(vendor_dir: str) -> list[str]:
    """Return vendor/ top-level package directory names to copy into LibreHarper plugin/lib/."""
    if not os.path.isdir(vendor_dir):
        return []
    found: list[str] = []
    for entry in sorted(os.listdir(vendor_dir)):
        if entry.endswith(".dist-info") or entry.startswith(("_", ".")):
            continue
        src_path = os.path.join(vendor_dir, entry)
        if not os.path.isdir(src_path):
            continue
        if entry in LIBREHARPER_VENDOR_PACKAGES:
            found.append(entry)
    missing = sorted(LIBREHARPER_VENDOR_PACKAGES - set(found))
    if missing:
        raise FileNotFoundError(
            "LibreHarper vendor missing required packages under %s: %s (run: make vendor)"
            % (vendor_dir, ", ".join(missing))
        )
    return found


def slim_libreharper_package_inits(bundle_plugin_dir: str) -> None:
    """Replace heavy package __init__ files with stubs for the Harper-only OXT."""
    writer_init = os.path.join(bundle_plugin_dir, "writer", "__init__.py")
    os.makedirs(os.path.dirname(writer_init), exist_ok=True)
    with open(writer_init, "w", encoding="utf-8") as fh:
        fh.write(LIBREHARPER_WRITER_INIT)

    doc_init = os.path.join(bundle_plugin_dir, "doc", "__init__.py")
    os.makedirs(os.path.dirname(doc_init), exist_ok=True)
    with open(doc_init, "w", encoding="utf-8") as fh:
        fh.write(LIBREHARPER_DOC_INIT)

    client_dir = os.path.join(bundle_plugin_dir, "framework", "client")
    os.makedirs(client_dir, exist_ok=True)
    with open(os.path.join(client_dir, "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write(LIBREHARPER_CLIENT_INIT)

    # scripting/venv needs to be a package for harper imports.
    venv_init = os.path.join(bundle_plugin_dir, "scripting", "venv", "__init__.py")
    if not os.path.isfile(venv_init):
        with open(venv_init, "w", encoding="utf-8") as fh:
            fh.write('"""Harper binary + LSP helpers (LibreHarper)."""\n')
