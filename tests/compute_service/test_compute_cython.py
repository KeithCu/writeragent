# WriterAgent - Python Compute Service Cython Startup Test
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from compute_service.config import ComputeSettings
from compute_service.formula_pool import FormulaProcessPool
from compute_service.server import run_server
from plugin.scripting.payload_codec import get_cython_status_info

_REPO = Path(__file__).resolve().parents[2]


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO)
    return env


def test_compute_service_logs_host_and_one_worker_cython_status(capsys) -> None:
    """Host startup line stays; one worker probe is logged, not as a substitute."""
    from plugin.scripting.payload_codec import load_cython_accelerator

    load_cython_accelerator()
    is_active, source_loc, expected_host = get_cython_status_info()
    # Distinct from the host line so we can prove both sides appear.
    worker_line = "Cython Accelerator: Inactive (Pure Python; not found)"

    settings = ComputeSettings(
        host="127.0.0.1",
        port=8000,
        threads=1,
        workers=1,
        ocr_workers=0,
        log_level="INFO",
    )

    mock_pool = MagicMock()
    mock_pool.probe_cython_status.return_value = worker_line

    with (
        patch("compute_service.server.WSGIDualStackServer") as mock_server_cls,
        patch("compute_service.formula_pool.get_formula_pool", return_value=mock_pool),
        patch("compute_service.server.check_dependencies"),
    ):
        mock_server = mock_server_cls.return_value
        mock_server.serve_forever.side_effect = KeyboardInterrupt

        try:
            run_server(settings)
        except KeyboardInterrupt:
            pass

        captured = capsys.readouterr()
        assert expected_host in captured.err
        assert f"Formula worker {worker_line}" in captured.err
        mock_pool.probe_cython_status.assert_called_once()
        if is_active:
            assert "Active" in captured.err
            assert source_loc is not None
        else:
            assert "Inactive" in captured.err


def test_vision_worker_source_does_not_load_cython() -> None:
    """Vision workers do not pack list grids; they must not load."""
    vision = (_REPO / "compute_service" / "vision_worker.py").read_text(encoding="utf-8")
    assert "from plugin.scripting.payload_codec import load_cython_accelerator" not in vision
    assert "load_cython_accelerator" not in vision


def test_formula_worker_source_loads_cython() -> None:
    """Formula workers load for list-grid egress; they are not the sole status reporter."""
    text = (_REPO / "compute_service" / "formula_worker.py").read_text(encoding="utf-8")
    assert "from plugin.scripting.payload_codec import get_cython_status_info, load_cython_accelerator" in text
    assert "load_cython_accelerator()" in text
    server = (_REPO / "compute_service" / "server.py").read_text(encoding="utf-8")
    assert "log.info(\"%s\", cy_status)" in server
    assert "Formula worker %s" in server
    assert "probe_cython_status" in server


def test_dockerfile_copies_contrib_vec_pack() -> None:
    text = (_REPO / "compute_service" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY contrib/vec_pack" in text
    assert "COPY plugin/framework/deal_shim.py" in text
    assert (_REPO / "contrib" / "vec_pack" / "__init__.py").is_file()


def test_payload_codec_import_does_not_load_cython() -> None:
    """Importing unpack helpers must not bind or claim Cython."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from plugin.scripting.payload_codec import fast_flatten_grid_2d, get_cython_status_info; "
            "active, _loc, line = get_cython_status_info(); "
            "assert fast_flatten_grid_2d is None, line; "
            "assert active is False, line; "
            "assert 'Inactive' in line, line",
        ],
        cwd=_REPO,
        env=_child_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_host_pack_data_attempts_cython_load() -> None:
    """Desktop / HTTP host pack is an intentional load site (not module import)."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from plugin.scripting import payload_codec as pc; "
            "assert pc.fast_flatten_grid_2d is None; "
            "pc.host_pack_data([[1.0, 2.0], [3.0, 4.0]], force='always'); "
            "assert pc.fast_flatten_grid_2d is not None or pc._CYTHON_ACCELERATOR_DISABLED",
        ],
        cwd=_REPO,
        env=_child_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_formula_worker_import_loads_cython_and_list_grid_can_use_it() -> None:
    """Importing the worker loads Cython; list-grid pack uses flatten when present."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import compute_service.formula_worker; "
            "from plugin.scripting import payload_codec as pc; "
            "from plugin.scripting.payload_codec import child_pack_result, get_cython_status_info, is_split_grid; "
            "active, _loc, line = get_cython_status_info(); "
            "assert 'Cython Accelerator:' in line, line; "
            "assert active is (pc.fast_flatten_grid_2d is not None), line; "
            "assert active or pc._CYTHON_ACCELERATOR_DISABLED, line; "
            "grid = [[float(i), float(i + 1)] for i in range(80)]; "
            "orig = pc.fast_flatten_grid_2d; "
            "calls = []; "
            "pc.fast_flatten_grid_2d = (lambda g, ncols: calls.append(ncols) or orig(g, ncols)) if orig is not None else None; "
            "wire = child_pack_result(grid, force='always'); "
            "assert is_split_grid(wire), wire; "
            "assert orig is None or calls, 'list-grid pack must use Cython flatten when loaded'",
        ],
        cwd=_REPO,
        env=_child_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_formula_pool_probes_one_worker_cython_status() -> None:
    """Live child returns Active/Inactive (+ source/reason); host is still the reporter."""
    pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15, idle_worker_ttl_sec=None)
    try:
        line = pool.probe_cython_status()
        assert line is not None
        assert line.startswith("Cython Accelerator:")
        assert "Active" in line or "Inactive" in line
        again = pool.probe_cython_status()
        assert again == line
    finally:
        pool.shutdown()
