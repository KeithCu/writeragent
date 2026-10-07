with open('tests/chatbot/test_panel.py', 'r') as f:
    content = f.read()

import re

# Correct mock patch string
test_code = """
def test_ask_text_restored_on_post_clear_error() -> None:
    from plugin.chatbot.panel import SendButtonListener
    from unittest.mock import MagicMock, patch
    import pytest

    with patch("plugin.scripting.audio_recorder_service.is_audio_recording_supported", return_value=False), patch("plugin.scripting.audio_recorder_service.is_audio_recording_configured", return_value=False), patch("plugin.chatbot.panel.ChatSession"):
        listener = SendButtonListener(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), "test")

    listener.query_control = MagicMock()
    listener.query_control.getModel.return_value = MagicMock()

    with patch("plugin.chatbot.panel.get_control_text", return_value="some text"), \
         patch("plugin.chatbot.panel.set_control_text"), \
         patch("plugin.chatbot.panel.sync_sidebar_text_model", side_effect=RuntimeError("boom")), \
         patch.object(listener, "_restore_query_text") as mock_restore:

        listener._get_document_model = MagicMock(return_value=MagicMock())
        listener.cached_doc_type = "writer"
        listener.audio_wav_path = None
        listener._stt_inflight = False
        listener.sidebar_state = MagicMock()
        listener.sidebar_state.send.has_audio = False
        listener.ctx = MagicMock()
        listener.model_selector = MagicMock()
        listener.ensure_path_fn = None

        with pytest.raises(RuntimeError, match="boom"):
            listener._do_send()

        mock_restore.assert_called_once_with("some text")
"""
content = re.sub(r'def test_ask_text_restored_on_post_clear_error\(\).*?(?=\n\n|\Z)', test_code.strip(), content, flags=re.DOTALL)

with open('tests/chatbot/test_panel.py', 'w') as f:
    f.write(content)
