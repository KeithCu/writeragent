# WriterAgent - Python Compute Service Cython Startup Test
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from compute_service.config import ComputeSettings
from compute_service.server import run_server
from plugin.scripting.payload_codec import get_cython_status_info

_REPO = Path(__file__).resolve().parents[2]


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO)
    return env


def test_compute_service_cython_status_logged(capsys) -> None:
    """Verify that compute service logs Cython status on startup."""
    from plugin.scripting.payload_codec import load_cython_accelerator

    load_cython_accelerator()
    is_active, source_loc, expected_line = get_cython_status_info()

    settings = ComputeSettings(
        host="127.0.0.1",
        port=8000,
        threads=1,
        workers=1,
        ocr_workers=0,
        log_level="INFO",
    )

    with (
        patch("compute_service.server.WSGIDualStackServer") as mock_server_cls,
        patch("compute_service.formula_pool.get_formula_pool"),
        patch("compute_service.server.check_dependencies"),
    ):
        mock_server = mock_server_cls.return_value
        mock_server.serve_forever.side_effect = KeyboardInterrupt

        try:
            run_server(settings)
        except KeyboardInterrupt:
            pass

        captured = capsys.readouterr()
        assert expected_line in captured.err
        if is_active:
            assert "Active" in captured.err
            assert source_loc is not None
        else:
            assert "Inactive" in captured.err


def test_formula_worker_source_does_not_load_cython() -> None:
    """Formula workers unpack / pack ndarrays; they must not call load."""
    text = (_REPO / "compute_service" / "formula_worker.py").read_text(encoding="utf-8")
    assert "from plugin.scripting.payload_codec import load_cython_accelerator" not in text
    vision = (_REPO / "compute_service" / "vision_worker.py").read_text(encoding="utf-8")
    assert "from plugin.scripting.payload_codec import load_cython_accelerator" not in vision


def test_dockerfile_copies_contrib_vec_pack() -> None:
    text = (_REPO / "compute_service" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY contrib/vec_pack" in text
    assert "COPY plugin/framework/deal_shim.py" in text
    assert (_REPO / "contrib" / "vec_pack" / "__init__.py").is_file()


def test_payload_codec_import_does_not_load_cython() -> None:
    """Workers import unpack helpers; import must not bind or claim Cython."""
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
    """Desktop / HTTP host pack is the intentional load site (not module import)."""
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


def test_formula_worker_import_does_not_load_cython() -> None:
    """Importing the worker module (and thus unpack helpers) must stay Inactive."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import compute_service.formula_worker; "
            "from plugin.scripting.payload_codec import fast_flatten_grid_2d, get_cython_status_info; "
            "active, _loc, line = get_cython_status_info(); "
            "assert fast_flatten_grid_2d is None, line; "
            "assert active is False, line",
        ],
        cwd=_REPO,
        env=_child_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
