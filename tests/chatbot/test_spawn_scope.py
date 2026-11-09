from types import SimpleNamespace

from plugin.framework.send_cancellation import SendCancellation
from plugin.chatbot.send_state import spawn_scope


def test_spawn_scope_reads_host_field():
    scope = SendCancellation()
    host = SimpleNamespace(_send_cancellation=scope)
    assert spawn_scope(host) is scope
    assert spawn_scope(SimpleNamespace(_send_cancellation=None)) is None
