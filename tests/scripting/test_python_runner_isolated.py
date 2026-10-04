def test_isolated_rps_pins_document():
    from plugin.scripting.python_runner import execute_and_insert_result
    from unittest.mock import patch, MagicMock

    with patch("plugin.scripting.session_manager.pin_script_document") as m_pin, patch("plugin.scripting.session_manager.rps_session_id", return_value="sess_xyz"):
        ctx = MagicMock()
        doc = MagicMock()

        try:
            execute_and_insert_result(ctx, doc, "print(1)")
        except Exception:
            pass
        m_pin.assert_called_once_with(doc)


def test_reset_python_session_action_in_background():
    from unittest.mock import patch
    import plugin.framework.main_shared

    with patch("plugin.framework.worker_pool.run_in_background") as m_run, patch("plugin.framework.uno_context.get_ctx") as m_ctx, patch("plugin.framework.main_shared.register_action_handler") as m_reg:
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
            from plugin.scripting.session_manager import reset_workbook_python_session

            m_run.assert_called_once_with(reset_workbook_python_session, m_ctx.return_value, name="reset-python-session")
