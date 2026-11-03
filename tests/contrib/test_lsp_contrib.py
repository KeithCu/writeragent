# WriterAgent - tests for vendored LSP helpers
from __future__ import annotations

from io import BytesIO

from plugin.contrib.lsp import json_rpc_framing
from plugin.contrib.lsp.position_codec import ClientPosition, PositionCodec


def test_position_codec_utf16_emoji() -> None:
    codec = PositionCodec("utf-16")
    lines = ["a👋b"]
    pos = codec.position_from_client_units(lines, ClientPosition(line=0, character=3))
    assert pos.character == 2


def test_json_rpc_framing_roundtrip() -> None:
    payload = {"jsonrpc": "2.0", "id": 1, "result": {}}
    encoded = json_rpc_framing.encode_frame(payload)
    stream = BytesIO(encoded)
    assert json_rpc_framing.read_frame(stream) == payload


def test_json_rpc_framing_rejects_oversized_frame() -> None:
    body = b"x" * (json_rpc_framing._LSP_MAX_FRAME_BYTES + 1)
    stream = BytesIO(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    try:
        json_rpc_framing.read_frame(stream)
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "Content-Length" in str(exc)
    assert raised
