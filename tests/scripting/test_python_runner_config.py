from unittest.mock import MagicMock, patch
import sys

import pytest


@pytest.mark.parametrize(
    ("allow_files", "allow_os", "expected_files"),
    [
        (False, False, False),
        (True, False, True),
        # os access implies file access (venv_worker builds files = allow_files or allow_os)
        (False, True, True),
        (True, True, True),
    ],
)
def test_run_python_script_receives_sandbox_policy(
    allow_files: bool, allow_os: bool, expected_files: bool
) -> None:
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
    config = {
        "scripting.python_allow_network": True,
        "scripting.python_extra_modules": "extra_pkg",
        "scripting.python_allow_files": allow_files,
        "scripting.python_allow_os": allow_os,
    }
    with (
        patch("plugin.scripting.venv_worker.python_exec_timeout", return_value=10),
        patch("plugin.scripting.venv_worker.resolve_libreoffice_python", return_value=sys.executable),
        patch("plugin.scripting.venv_worker.PythonWorkerManager.execute") as mock_execute,
        patch("plugin.scripting.venv_worker.get_config", side_effect=lambda k: config.get(k, "")),
    ):
        mock_execute.return_value = {"status": "ok", "result": 42}
        res = _run_prepared_rps(prepared)
        assert res == {"status": "ok", "result": 42}
        mock_execute.assert_called_once()
        assert mock_execute.call_args.args[0].sandbox_policy == {
            "network": True,
            "files": expected_files,
            "os": allow_os,
            "extra_modules": "extra_pkg",
        }
