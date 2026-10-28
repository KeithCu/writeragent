# WriterAgent - Standalone Compute Service Bundle Tests
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the isolated compute_service bundle."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.build_compute_bundle import (  # noqa: E402
    COMPUTE_PLUGIN_EXTRAS,
    assemble_compute_bundle,
    collect_compute_plugin_paths,
)
from scripts.librepy_bundle_paths import (  # noqa: E402
    LIBREPY_SMOLAGENTS_INIT,
    collect_librepy_plugin_paths,
)


def test_compute_plugin_paths_subset_of_librepy():
    compute_paths = set(collect_compute_plugin_paths(str(_REPO_ROOT)))
    librepy_paths = set(collect_librepy_plugin_paths(str(_REPO_ROOT)))
    extras = compute_paths - librepy_paths
    assert extras <= set(COMPUTE_PLUGIN_EXTRAS), f"Unexpected extras: {extras - set(COMPUTE_PLUGIN_EXTRAS)}"


def test_compute_bundle_assembly_and_execution(tmp_path: Path):
    dest = tmp_path / "bundle-compute"
    assemble_compute_bundle(str(_REPO_ROOT), str(dest), strip=True)

    # Verify smolagents init is slimmed
    smol_init = dest / "plugin" / "contrib" / "smolagents" / "__init__.py"
    assert smol_init.read_text(encoding="utf-8") == LIBREPY_SMOLAGENTS_INIT

    # Verify forbidden/desktop modules are absent
    forbidden = [
        "plugin/main.py",
        "plugin/main_core.py",
        "plugin/framework/client/llm_client.py",
        "plugin/embeddings",
        "plugin/chatbot",
        "plugin/writer",
        "plugin/calc",
        "plugin/doc/common_module.py",
        "plugin/scripting/calc_functions.py",
        "plugin/scripting/venv/duckdb_sql.py",
        "plugin/scripting/writeragent_api.py",
        "plugin/scripting/calc_functions_common.py",
        "plugin/scripting/sheet_layout.py",
        "plugin/scripting/venv/trusted_dispatch.py",
        "plugin/vision/vision_tools.py",
        "plugin/vision/vision_docling.py",
        "plugin/vision/vision_paddle.py",
        "plugin/contrib/vec_pack",
        "plugin/contrib/smolagents/agents.py",
    ]
    for rel in forbidden:
        p = dest / rel
        assert not p.exists(), f"Forbidden file or directory present in compute bundle: {rel}"

    # First subprocess: verify clean hermetic imports and worker cell executions
    worker_test_code = """
import sys, os

staged = sys.argv[1]
repo_real = os.path.realpath(sys.argv[2])

# Clear editable finder hooks and path hooks so nothing loads from git worktree
sys.meta_path = [
    finder for finder in sys.meta_path
    if "editable" not in finder.__class__.__name__.lower()
    and "editable" not in getattr(finder, "__module__", "").lower()
]
sys.path_hooks = [
    h for h in sys.path_hooks
    if "editable" not in h.__class__.__name__.lower()
    and "editable" not in getattr(h, "__module__", "").lower()
]

# Prepend staged bundle; filter out repo root and empty cwd while keeping site-packages
filtered_path = []
for p in sys.path:
    if not p:
        continue
    p_real = os.path.realpath(p)
    if p_real == repo_real or "editable" in p.lower():
        continue
    filtered_path.append(p)
sys.path = [staged] + filtered_path

import compute_service.server
import compute_service.formula_pool
import compute_service.vision
from compute_service.json_forward import parse_execute_request

# Verify peel path works and loads json_peel
parts = parse_execute_request(b'{"code":"result = 1"}', "application/json")
assert "compute_service.json_peel" in sys.modules

from compute_service.formula_worker import execute_code

# Cell 1: result = 1
r1 = execute_code("result = 1")
assert r1.get("status") == "ok", r1
assert r1.get("result") == 1, r1
assert not r1.get("images")

# Cell 2: numpy / pandas
r2 = execute_code(
    "import numpy as np, pandas as pd\\nresult = float(np.array(data).sum()) + len(pd.DataFrame(data))",
    data=[[1, 2], [3, 4]],
)
assert r2.get("status") == "ok", r2
assert r2.get("result") == 12.0, r2

# Cell 3: matplotlib SVG plot
r3 = execute_code('import matplotlib\\nmatplotlib.use("Agg")\\nimport matplotlib.pyplot as plt\\nplt.plot([1, 2], [3, 4])')
assert r3.get("status") == "ok", r3
assert r3.get("result") is None, r3
imgs = r3.get("images") or []
assert len(imgs) == 1, imgs
assert imgs[0].get("format") == "svg"
assert len(imgs[0].get("data_b64", "")) > 100

# Cell 4: init_script factor = 41
r4 = execute_code("result = factor + 1", init_script="factor = 41")
assert r4.get("status") == "ok", r4
assert r4.get("result") == 42, r4

# Cell 5: shared session calc:wb
r5_1 = execute_code("result = 10", session_id="calc:wb", mode="shared")
assert r5_1.get("status") == "ok", r5_1
assert r5_1.get("result") == 10, r5_1
r5_2 = execute_code("result = result + 35", session_id="calc:wb", mode="shared")
assert r5_2.get("status") == "ok", r5_2
assert r5_2.get("result") == 45, r5_2

# Cell 6: data.to_pandas() exercises coerce.py lock
r6 = execute_code("result = data.to_pandas().shape[0]", data=[[1, 2], [3, 4]])
assert r6.get("status") == "ok", r6
assert r6.get("result") == 1, r6

# Check that all loaded plugin and compute_service modules are from staged bundle
staged_real = os.path.realpath(staged)
for mod_name, mod in list(sys.modules.items()):
    if mod_name.startswith("plugin") or mod_name.startswith("compute_service"):
        f = getattr(mod, "__file__", None)
        if f:
            real_f = os.path.realpath(f)
            assert real_f.startswith(staged_real), f"Leaked module {mod_name} from {f}"

# Ensure UNO is never imported
assert "uno" not in sys.modules
"""

    res = subprocess.run(
        [sys.executable, "-c", worker_test_code, str(dest), str(_REPO_ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"Worker test failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"

    # Second subprocess: verify execution works even if deal is completely absent/blocked
    deal_block_code = """
import sys, os

staged = sys.argv[1]
repo_real = os.path.realpath(sys.argv[2])

sys.meta_path = [
    finder for finder in sys.meta_path
    if "editable" not in finder.__class__.__name__.lower()
    and "editable" not in getattr(finder, "__module__", "").lower()
]
sys.path_hooks = [
    h for h in sys.path_hooks
    if "editable" not in h.__class__.__name__.lower()
    and "editable" not in getattr(h, "__module__", "").lower()
]

filtered_path = []
for p in sys.path:
    if not p:
        continue
    p_real = os.path.realpath(p)
    if p_real == repo_real or "editable" in p.lower():
        continue
    filtered_path.append(p)
sys.path = [staged] + filtered_path

# Block 'deal' import
class DealBlocker:
    def find_spec(self, fullname, path, target=None):
        if fullname == "deal" or fullname.startswith("deal."):
            raise ImportError(f"No module named '{fullname}' (blocked by test)")
        return None

sys.meta_path.insert(0, DealBlocker())

import compute_service.server
from compute_service.formula_worker import execute_code
res = execute_code("result = 1")
assert res.get("status") == "ok", res
assert res.get("result") == 1, res
"""

    res2 = subprocess.run(
        [sys.executable, "-c", deal_block_code, str(dest), str(_REPO_ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res2.returncode == 0, f"Deal blocker test failed:\nSTDOUT:\n{res2.stdout}\nSTDERR:\n{res2.stderr}"
