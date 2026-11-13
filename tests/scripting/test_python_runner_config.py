from unittest.mock import MagicMock, patch
import sys


def test_run_python_script_receives_sandbox_policy():
    from plugin.scripting.python_runner import PreparedScript, _run_prepared_rps

    prepared = PreparedScript(
        ctx=MagicMock(),
        doc=None,
        code="result = 42",
        t0=0.0,
        bindings=None,
        py_data=None,
        session_id="rps:sid",
        script_session_id="doc:pin",
    )
    with (
        patch("plugin.scripting.venv_worker.python_exec_timeout", return_value=10),
        patch("plugin.scripting.venv_worker.resolve_libreoffice_python", return_value=sys.executable),
        patch("plugin.scripting.venv_worker.PythonWorkerManager.execute") as mock_execute,
        patch(
            "plugin.scripting.venv_worker.get_config",
            side_effect=lambda k: True
            if k == "scripting.python_allow_network"
            else ("extra_pkg" if k == "scripting.python_extra_modules" else False if k in ("scripting.python_allow_files", "scripting.python_allow_os") else ""),
        ),
    ):
        mock_execute.return_value = {"status": "ok", "result": 42}
        res = _run_prepared_rps(prepared)
        assert res == {"status": "ok", "result": 42}
        mock_execute.assert_called_once()
        assert mock_execute.call_args.args[0].sandbox_policy == {
            "network": True,
            "files": False,
            "os": False,
            "extra_modules": "extra_pkg",
        }
