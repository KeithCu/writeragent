import pytest
from unittest.mock import MagicMock
from plugin.mcp.server import HttpServer

def test_tls_listen_socket_leaks():
    registry = MagicMock()
    # pass invalid certs to cause an exception
    server = HttpServer(registry, port=0, use_ssl=True, ssl_cert="does-not-exist.pem", ssl_key="does-not-exist.pem")

    with pytest.raises(Exception):
        server.start()

    assert not server.is_running()

    # At this point, if it leaked, server._server.socket is still open
    if server._server:
        sock = server._server.socket
        assert sock.fileno() == -1, "Socket leaked!"

if __name__ == "__main__":
    pytest.main(["-v", "test_tls_leak.py"])
