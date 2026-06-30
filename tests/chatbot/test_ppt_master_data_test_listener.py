"""Settings → Python PPT-Master Test uses the modal probe dialog."""

from unittest.mock import MagicMock, patch

from plugin.chatbot.dialog_views import PptMasterDataTestListener


def test_ppt_master_data_test_uses_modal_probe() -> None:
    order: list[str] = []
    fake_ctx = MagicMock()
    fake_dlg = MagicMock()
    probe_displays: list[str] = []

    class _FakeProgress:
        def __init__(self, ctx, parent_dlg=None):
            order.append("progress_ctor")
            assert parent_dlg is fake_dlg

        def run_modal_probe(self, probe_fn):
            order.append("progress_run_modal")
            probe_fn(probe_displays.append, None)
            return True

    def fake_probe(raw, on_display, on_status=None):
        on_display("Data root: /tmp/skills/ppt-master\nSKILL.md: yes")
        if on_status:
            on_status("PPT-Master data root OK")
        return True, "ok"

    listener = PptMasterDataTestListener(fake_ctx, fake_dlg)
    with (
        patch("plugin.chatbot.dialog_views.get_optional", return_value=None),
        patch("plugin.chatbot.dialog_views._VenvProbeProgressDialog", _FakeProgress),
        patch("plugin.ppt_master.paths.probe_data_path_with_progress", side_effect=fake_probe),
    ):
        listener.on_action_performed(MagicMock())

    assert "progress_run_modal" in order
    assert any("SKILL.md" in text for text in probe_displays)
