"""File selection for LibrePy OXT (Layers 0–6 only; no chat/MCP/embeddings trees)."""

from __future__ import annotations

import os

# Spreadsheet-import xl parity helpers — WriterAgent only for now; see
# docs/libreoffice-core-python-extension-split.md (Explicit exclusions).
LIBREPY_CALC_FUNCTIONS_EXCLUDES: tuple[str, ...] = (
    "calc_functions.py",
    "venv/calc_functions.py",
    "venv/calc_functions_a_c.py",
    "venv/calc_functions_d_h.py",
    "venv/calc_functions_i_m.py",
    "venv/calc_functions_n_s.py",
    "venv/calc_functions_t_z.py",
)

# vendor/ package dirs copied into plugin/lib/ (WriterAgent ships full requirements-vendor.txt).
# Excluded from LibrePy: snowballstemmer (grammar/embeddings), websockets (CDP), defusedxml (embeddings).
LIBREPY_VENDOR_PACKAGES: frozenset[str] = frozenset({"json_repair", "latex2mathml"})

# Whole subtrees under plugin/, minus relative paths in the exclude set.
LIBREPY_PLUGIN_DIRS: dict[str, tuple[str, ...]] = {
    "plugin/scripting/": (
        *LIBREPY_CALC_FUNCTIONS_EXCLUDES,
        "duckdb_sql.py",
        "venv/duckdb_sql.py",
        "venv/languagetool.py",
        "venv/vale.py",
        "venv/harper.py",
        "venv/harper_binary.py",
        "venv/audio_recorder.py",
        "venv/audio_record_main.py",
        "audio_silence_detector.py",
        "writeragent_api.py",
    ),
    "plugin/vision/": ("vision_tools.py",),
    "plugin/contrib/scripting/assets/editor/": (),
    # Excel Python-in-Excel → DAG =PY auto-convert on open (stdlib OOXML + optional openpyxl write).
    "plugin/calc/excel_py_convert/": (),
}

# Explicit files outside the directory rules above.
LIBREPY_PLUGIN_FILES: tuple[str, ...] = (
    "plugin/__init__.py",
    "plugin/main_core.py",
    "plugin/version.py",
    "plugin/librepy/__init__.py",
    "plugin/librepy/settings.py",
    "plugin/librepy/panel_factory.py",
    "plugin/librepy/python_sidebar.py",
    "plugin/calc/__init__.py",
    "plugin/calc/addin_common.py",
    "plugin/calc/calc_addin_data.py",
    "plugin/calc/bridge.py",
    "plugin/calc/address_utils.py",
    "plugin/calc/calc_utils.py",
    "plugin/calc/manipulator.py",
    "plugin/calc/inspector.py",
    "plugin/calc/analyzer.py",
    "plugin/calc/navigation.py",
    "plugin/calc/tabular_egress.py",
    "plugin/calc/rich_html.py",
    "plugin/calc/analysis_runner.py",
    "plugin/calc/analysis_egress.py",
    "plugin/calc/viz_auto_plot.py",
    "plugin/calc/forecast_auto_plot.py",
    "plugin/calc/quant_egress.py",
    "plugin/calc/vision_egress.py",
    "plugin/calc/python/addin_librepy.py",
    "plugin/calc/python/function.py",
    "plugin/calc/python/editor.py",
    "plugin/calc/python/formula_edit.py",
    "plugin/calc/python/editor_context_menu.py",
    "plugin/calc/python/workbook_lifecycle.py",
    "plugin/calc/python/image_egress.py",
    "plugin/calc/python/cell_discovery.py",
    "plugin/calc/python/diagnostics.py",
    "plugin/calc/python/init_script_editor.py",
    "plugin/doc/__init__.py",
    "plugin/doc/document_helpers.py",
    "plugin/doc/udprops.py",
    "plugin/doc/visual_helpers.py",
    "plugin/draw/bridge.py",
    "plugin/writer/format.py",
    "plugin/writer/ops.py",
    "plugin/writer/review_authors.py",
    "plugin/writer/xhtml_style_postprocess.py",
    "plugin/writer/math/__init__.py",
    "plugin/writer/math/latex_dialog.py",
    "plugin/writer/math/math_mml_convert.py",
    "plugin/writer/math/html_math_segment.py",
    "plugin/writer/images/__init__.py",
    "plugin/writer/images/image_tools.py",
    "plugin/chatbot/dialogs.py",
    "plugin/chatbot/listeners.py",
    "plugin/chatbot/module_config_dialog.py",
    "plugin/chatbot/settings_fields.py",
    "plugin/chatbot/settings_tab_order.py",
    "plugin/framework/__init__.py",
    "plugin/framework/config.py",
    "plugin/framework/constants.py",
    "plugin/framework/errors.py",
    "plugin/framework/bug_report.py",
    "plugin/framework/json_utils.py",
    "plugin/framework/i18n.py",
    "plugin/framework/event_bus.py",
    "plugin/framework/service.py",
    "plugin/framework/url_utils.py",
    "plugin/framework/thread_guard.py",
    "plugin/framework/uno_bootstrap.py",
    "plugin/framework/main_shared.py",
    "plugin/framework/logging.py",
    "plugin/framework/uno_context.py",
    "plugin/framework/worker_pool.py",
    "plugin/framework/appearance.py",
    "plugin/framework/queue_executor.py",
    "plugin/framework/uno_listeners.py",
    "plugin/framework/tool.py",
    "plugin/framework/module_base.py",
    "plugin/framework/client/__init__.py",
    "plugin/framework/client/errors.py",
    "plugin/scripting/sandbox_cache.py",
    "plugin/scripting/audio_recorder_service.py",
    "plugin/contrib/__init__.py",
    "plugin/contrib/smolagents/__init__.py",
    "plugin/contrib/smolagents/local_python_executor.py",
    "plugin/contrib/smolagents/tools.py",
    "plugin/contrib/smolagents/utils.py",
    "plugin/contrib/smolagents/agent_types.py",
    "plugin/contrib/smolagents/tool_validation.py",
    "plugin/contrib/smolagents/_function_type_hints_utils.py",
    "plugin/contrib/vec_pack/__init__.py",
)

LIBREPY_SMOLAGENTS_EXCLUDE = frozenset(
    {
        "agents.py",
        "models.py",
        "memory.py",
        "default_tools.py",
        "remote_executors.py",
        "cli.py",
        "monitoring.py",
        "gradio_ui.py",
        "prompts",
        "vision",
    }
)

# Venv worker imports local_python_executor only; full __init__.py star-imports excluded modules.
LIBREPY_SMOLAGENTS_INIT = '''\
"""Slim smolagents package init for LibrePy (venv worker needs local_python_executor only)."""

__version__ = "1.25.0.dev0"
'''


def slim_librepy_smolagents_init(bundle_plugin_dir: str) -> None:
    """Replace vendored smolagents __init__.py so excluded modules are not imported."""
    init_path = os.path.join(bundle_plugin_dir, "contrib", "smolagents", "__init__.py")
    if not os.path.isfile(init_path):
        raise FileNotFoundError("LibrePy bundle missing smolagents __init__: %s" % init_path)
    with open(init_path, "w", encoding="utf-8") as fh:
        fh.write(LIBREPY_SMOLAGENTS_INIT)


def iter_librepy_vendor_packages(vendor_dir: str) -> list[str]:
    """Return vendor/ top-level package directory names to copy into LibrePy plugin/lib/."""
    if not os.path.isdir(vendor_dir):
        return []
    found: list[str] = []
    for entry in sorted(os.listdir(vendor_dir)):
        if entry.endswith(".dist-info") or entry.startswith(("_", ".")):
            continue
        src_path = os.path.join(vendor_dir, entry)
        if not os.path.isdir(src_path):
            continue
        if entry in LIBREPY_VENDOR_PACKAGES:
            found.append(entry)
    missing = sorted(LIBREPY_VENDOR_PACKAGES - set(found))
    if missing:
        raise FileNotFoundError(
            "LibrePy vendor missing required packages under %s: %s (run: make vendor)"
            % (vendor_dir, ", ".join(missing))
        )
    return found


def _norm(path: str) -> str:
    return path.replace(os.sep, "/")


def _is_excluded(rel: str, excludes: tuple[str, ...]) -> bool:
    rel = _norm(rel)
    for pat in excludes:
        pat = _norm(pat)
        if rel == pat or rel.endswith("/" + pat):
            return True
    return False


def collect_librepy_plugin_paths(base_dir: str) -> list[str]:
    """Return project-relative plugin paths to copy into the LibrePy bundle."""
    found: set[str] = set()

    for rel in LIBREPY_PLUGIN_FILES:
        full = os.path.join(base_dir, rel)
        if os.path.isfile(full):
            found.add(_norm(rel))

    for dir_rel, excludes in LIBREPY_PLUGIN_DIRS.items():
        full_dir = os.path.join(base_dir, dir_rel)
        if not os.path.isdir(full_dir):
            continue
        for root, dirnames, filenames in os.walk(full_dir):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                if fn.endswith((".pyc", ".pyo")):
                    continue
                filepath = os.path.join(root, fn)
                rel = _norm(os.path.relpath(filepath, base_dir))
                if _is_excluded(os.path.relpath(filepath, full_dir), excludes):
                    continue
                if rel.startswith("plugin/contrib/smolagents/"):
                    name = os.path.basename(rel)
                    if name in LIBREPY_SMOLAGENTS_EXCLUDE:
                        continue
                found.add(rel)

    missing = [p for p in LIBREPY_PLUGIN_FILES if p not in found]
    if missing:
        raise FileNotFoundError("LibrePy bundle missing required plugin files: %s" % ", ".join(missing))

    return sorted(found)
