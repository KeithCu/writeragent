"""Incremental venv self-check progress reporting."""

from unittest.mock import MagicMock, patch

from plugin.scripting.venv_worker import run_venv_self_check_with_progress


def test_run_venv_self_check_with_progress_emits_grouped_present_missing() -> None:
    displays: list[str] = []
    statuses: list[str] = []

    def fake_execute(_self, script, timeout_sec=10):
        if "platform" in script:
            return {"status": "ok", "result": {"v": "3.12.0", "arch": "x86_64"}}
        if "numpy" in script:
            return {"status": "ok", "result": "present"}
        return {"status": "ok", "result": None}

    mock_mgr = MagicMock()
    mock_mgr.execute.side_effect = lambda script, timeout_sec=10: fake_execute(None, script, timeout_sec)

    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._probe_vision_packages", return_value=({"docling": "present"}, None)),
        patch(
            "plugin.scripting.venv_diagnostics._probe_embeddings_packages",
            return_value=({"envwrap": "present", "sqlite_vec": "present"}, None),
        ),
    ):
        ok, msg = run_venv_self_check_with_progress(
            "/fake/python",
            displays.append,
            timeout=30.0,
            on_status=statuses.append,
            extra_lines_after_header=("Cython Accelerator: Active (Optimized)",),
        )

    assert ok is True
    assert "responds OK" in msg
    assert "Scientific Libraries: numpy" in msg
    assert "Missing:" in msg
    assert "Cython Accelerator: Active (Optimized)" in msg
    assert any("Scientific Libraries: numpy" in text for text in displays)
    assert any("numpy" in text for text in displays)
    assert any("Vision" in text for text in displays)
    assert any("Embeddings" in text for text in displays)
    assert any("docling" in text for text in displays)
    assert any("envwrap" in text for text in displays)
    assert not any("... OK" in text for text in displays)
    assert any("numpy" in status for status in statuses)
