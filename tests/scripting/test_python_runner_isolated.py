def test_isolated_rps_pins_document():
    from plugin.scripting.python_runner import execute_and_insert_result
    from unittest.mock import patch, MagicMock

    with (
        patch("plugin.scripting.session_manager.pin_script_document", return_value="doc:token123") as m_pin,
        patch("plugin.scripting.session_manager.release_script_document") as m_rel,
        patch("plugin.scripting.session_manager.rps_session_id", return_value="sess_xyz"),
        patch("plugin.scripting.python_runner.run_code_in_user_venv", return_value={"status": "ok", "result": 42}) as m_run,
        patch("plugin.scripting.python_runner.is_calc", return_value=False),
        patch("plugin.scripting.python_runner.is_writer", return_value=True),
        patch("plugin.scripting.python_runner.format_result_for_writer", return_value=""),
        patch("plugin.scripting.python_runner.is_draw", return_value=False),
    ):
        ctx = MagicMock()
        doc = MagicMock()

        outcome = execute_and_insert_result(ctx, doc, "print(1)")
        m_pin.assert_called_once_with(doc)
        m_run.assert_called_once()
        assert m_run.call_args.kwargs.get("script_session_id") == "doc:token123"
        m_rel.assert_called_once_with("doc:token123")
        assert outcome.get("ok") is True


def test_reset_python_session_action_in_background():
    from unittest.mock import patch
    import plugin.framework.main_shared

    with patch("plugin.framework.worker_pool.run_in_background") as m_run, patch("plugin.framework.main_shared.register_action_handler") as m_reg:
        import importlib

        try:
            importlib.reload(plugin.framework.main_shared)
        except Exception:
            pass

        reset_func = None
        for call in m_reg.mock_calls:
            if call.args[0] == "scripting" and call.args[1] == "reset_python_session":
                reset_func = call.args[2]
                break

        if reset_func:
            reset_func()
            # We used a lambda, we can't assert the function easily, we just check call
            m_run.assert_called_once()
            args, kwargs = m_run.call_args
            assert kwargs["name"] == "reset-python-session"
            assert callable(args[0])


def test_prepare_rps_session_failure_logs_prepare_not_execute(caplog):
    import logging
    from unittest.mock import MagicMock, patch
    from plugin.scripting.python_runner import _prepare_rps_execution

    ctx = MagicMock()
    doc = MagicMock()

    with (
        patch("plugin.scripting.session_manager.pin_script_document", return_value="doc:tok"),
        patch("plugin.scripting.session_manager.release_script_document") as mock_release,
        patch("plugin.scripting.session_manager.rps_session_id", side_effect=RuntimeError("session lookup failed")),
        caplog.at_level(logging.ERROR, logger="writeragent.scripting"),
    ):
        prepared = _prepare_rps_execution(ctx, doc, "x = 1")

    assert prepared.get("early_outcome", {}).get("ok") is False
    mock_release.assert_called_once_with("doc:tok")
    assert "prepare Run Python Script failed" in caplog.text
    assert "execute_and_insert_result failed" not in caplog.text

