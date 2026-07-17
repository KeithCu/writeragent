"""Settings → Python Test shows a modal dialog with incremental probe output."""

from unittest.mock import MagicMock, patch

from plugin.chatbot.dialog_views import _dialog_parent_for_child
from plugin.scripting.venv_probe_ui import ScriptingVenvTestListener


def test_python_test_uses_modal_incremental_probe() -> None:
    order: list[str] = []
    fake_ctx = MagicMock()
    fake_dlg = MagicMock()
    probe_displays: list[str] = []
    probe_statuses: list[str] = []
    captured_extra: list[tuple[str, ...]] = []

    class _FakeProgress:
        def __init__(self, ctx, parent_dlg=None):
            order.append("progress_ctor")
            assert parent_dlg is fake_dlg

        def run_modal_probe(self, probe_fn, *, title=None):
            order.append("progress_run_modal")
            probe_fn(probe_displays.append, probe_statuses.append)
            return True

    def fake_probe(_raw, on_display, on_status=None, extra_lines_after_header=None):
        captured_extra.append(tuple(extra_lines_after_header or ()))
        on_display(
            "Python 3.12 responds OK. Cython Accelerator: Inactive (Pure Python)\n\n"
            "Scientific Libraries: numpy\nMissing: pandas"
        )
        if on_status:
            on_status("Scientific Libraries: numpy")
        return True, "Python 3.12 responds OK."

    def fake_status(*, reload=False):
        order.append(f"cython_status_reload={reload}")
        return "Cython Accelerator: Inactive (Pure Python)"

    listener = ScriptingVenvTestListener(fake_ctx, fake_dlg)
    with (
        patch("plugin.scripting.venv_probe_ui.get_optional", return_value=None),
        patch("plugin.scripting.venv_probe_ui.VenvProbeProgressDialog", _FakeProgress),
        patch("plugin.scripting.venv_diagnostics.probe_venv_path_with_progress", side_effect=fake_probe),
        patch("plugin.scripting.payload_codec.host_cython_status_line", side_effect=fake_status),
        patch("plugin.scripting.audio_recorder_service.ensure_downloaded_audio_on_path"),
    ):
        listener.on_action_performed(MagicMock())

    assert "progress_run_modal" in order
    # Status reload happens on the main thread before the probe worker runs.
    assert order.index("cython_status_reload=True") < order.index("progress_run_modal")
    assert captured_extra == [("Cython Accelerator: Inactive (Pure Python)",)]
    assert any("Scientific Libraries: numpy" in text for text in probe_displays)
    assert any("Cython Accelerator" in text for text in probe_displays)
    assert any("numpy" in status for status in probe_statuses)


def test_dialog_parent_for_child_prefers_settings_peer() -> None:
    parent = MagicMock()
    parent.getPeer.return_value = "settings-peer"
    assert _dialog_parent_for_child(MagicMock(), parent) == "settings-peer"
    parent.getPeer.assert_called_once()
