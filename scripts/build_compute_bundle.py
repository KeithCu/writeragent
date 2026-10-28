#!/usr/bin/env python3
# WriterAgent - Standalone Compute Service Bundle Builder
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build target for compute_service bundle.

Copies only the runtime closure required for compute_service from LibrePy,
slims smolagents init, strips debug/logging calls, and validates bytecode compilation.
"""

from __future__ import annotations

import argparse
import compileall
import os
import shutil
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.librepy_bundle_paths import (  # noqa: E402
    collect_librepy_plugin_paths,
    slim_librepy_smolagents_init,
)
from scripts.strip_code import strip_production_code  # noqa: E402

COMPUTE_BUNDLE_DIR = "build/bundle-compute"
COMPUTE_PLUGIN_EXTRAS = (
    "plugin/framework/http_server.py",
    "plugin/framework/process_worker.py",
)

# Traced closure of import server+formula_pool+vision and execute_code
# (result=1, numpy/pandas, matplotlib svg, init_script, shared session),
# plus venv/coerce.py for CalcRange.to_pandas. Checked against
# collect_librepy_plugin_paths. The two extras are the only names LibrePy
# does not already ship.
COMPUTE_PLUGIN_FILES = (
    "plugin/__init__.py",
    "plugin/version.py",
    "plugin/contrib/__init__.py",
    "plugin/contrib/smolagents/__init__.py",
    "plugin/contrib/smolagents/local_python_executor.py",
    "plugin/contrib/smolagents/utils.py",
    "plugin/doc/__init__.py",
    "plugin/doc/doc_type.py",
    "plugin/framework/__init__.py",
    "plugin/framework/constants.py",
    "plugin/framework/deal_shim.py",
    "plugin/framework/errors.py",
    "plugin/framework/i18n.py",
    "plugin/framework/json_utils.py",
    "plugin/framework/managed_subprocess.py",
    "plugin/framework/module_base.py",
    "plugin/framework/module_graph.py",
    "plugin/framework/thread_guard.py",
    "plugin/framework/worker_pool.py",
    "plugin/scripting/__init__.py",
    "plugin/scripting/calc_range.py",
    "plugin/scripting/config_limits.py",
    "plugin/scripting/domain_registry.py",
    "plugin/scripting/excel_xl.py",
    "plugin/scripting/helper_domain.py",
    "plugin/scripting/ipc.py",
    "plugin/scripting/named_scripts.py",
    "plugin/scripting/payload_codec.py",
    "plugin/scripting/sandbox.py",
    "plugin/scripting/sandbox_cache.py",
    "plugin/scripting/venv/__init__.py",
    "plugin/scripting/venv/coerce.py",
    "plugin/scripting/venv/venv_sandbox.py",
    "plugin/scripting/writeragent_namespace.py",
    "plugin/vision/__init__.py",
    "plugin/vision/venv/__init__.py",
    "plugin/vision/venv/vision.py",
    "plugin/vision/vision_common.py",
    *COMPUTE_PLUGIN_EXTRAS,
)


class BuildError(Exception):
    """Raised when compute bundle assembly fails."""


def collect_compute_plugin_paths(base_dir: str = PROJECT_ROOT) -> list[str]:
    """Return sorted plugin relative paths required by compute_service.

    Raises BuildError if any path is missing on disk or if any non-extra path
    is absent from collect_librepy_plugin_paths.
    """
    librepy_paths = set(collect_librepy_plugin_paths(base_dir))
    extras_set = set(COMPUTE_PLUGIN_EXTRAS)

    for rel in COMPUTE_PLUGIN_FILES:
        full_path = os.path.join(base_dir, rel)
        if not os.path.isfile(full_path):
            raise BuildError(f"Missing file on disk: {rel}")
        if rel not in extras_set and rel not in librepy_paths:
            raise BuildError(f"Path {rel} is not in LibrePy plugin paths and not in extras")

    return sorted(COMPUTE_PLUGIN_FILES)


def assemble_compute_bundle(
    base_dir: str,
    dest: str,
    *,
    strip: bool = True,
) -> int:
    """Assemble the isolated compute_service bundle into dest."""
    dest_path = os.path.join(base_dir, dest) if not os.path.isabs(dest) else dest
    if os.path.exists(dest_path):
        shutil.rmtree(dest_path)
    os.makedirs(dest_path, exist_ok=True)

    count = 0
    # Copy plugin closure
    plugin_paths = collect_compute_plugin_paths(base_dir)
    for rel in plugin_paths:
        src = os.path.join(base_dir, rel)
        dst = os.path.join(dest_path, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        count += 1

    # Copy compute_service/*.py and docker-entrypoint.sh only
    cs_dir = os.path.join(base_dir, "compute_service")
    cs_dst = os.path.join(dest_path, "compute_service")
    os.makedirs(cs_dst, exist_ok=True)
    for fname in sorted(os.listdir(cs_dir)):
        if fname.endswith(".py"):
            shutil.copy2(os.path.join(cs_dir, fname), os.path.join(cs_dst, fname))
            count += 1
    shutil.copy2(
        os.path.join(cs_dir, "docker-entrypoint.sh"),
        os.path.join(cs_dst, "docker-entrypoint.sh"),
    )
    count += 1

    # Slim smolagents __init__.py for local_python_executor
    slim_librepy_smolagents_init(os.path.join(dest_path, "plugin"))

    # Production code strip (asserts, deal decorators, thread_guard stub, etc.)
    if strip:
        strip_production_code(dest_path, dry_run=False)

    # Verify syntax & clean __pycache__
    if not compileall.compile_dir(dest_path, quiet=1):
        raise BuildError(f"compileall failed: invalid Python syntax in {dest_path}")
    for root, dirnames, _ in os.walk(dest_path):
        if "__pycache__" in dirnames:
            shutil.rmtree(os.path.join(root, "__pycache__"), ignore_errors=True)

    print(f"Assembled {count} files in {dest}")
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Build compute_service bundle")
    parser.add_argument(
        "--output",
        default=COMPUTE_BUNDLE_DIR,
        help="Destination directory (default: build/bundle-compute)",
    )
    parser.add_argument(
        "--no-strip",
        action="store_true",
        help="Do not strip assertions and debug logs",
    )
    args = parser.parse_args()

    assemble_compute_bundle(PROJECT_ROOT, args.output, strip=not args.no_strip)
    return 0


if __name__ == "__main__":
    sys.exit(main())
